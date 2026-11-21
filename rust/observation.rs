//! Opt-in HTTP transport observation.
//!
//! Terrane cannot currently author a Tower `Layer` or an `http_body::Body`
//! adapter with pin-projected async polling, so this boundary wraps Axum's
//! generic router/body and emits bounded diagnostic events without buffering
//! either stream. Remove it when source code can implement and poll those
//! protocols directly while preserving demand-driven bodies and drop events.
use axum::{body::{Body, Bytes}, extract::Request, Router};
use http_body::{Body as _, Frame};
use serde_json::{json, Value};
use std::{future::Future, pin::Pin, sync::{Arc, atomic::{AtomicU64, Ordering}}, task::{Context, Poll}, time::Instant};
use tower::{Layer, Service};

static NEXT_REQUEST_ID: AtomicU64 = AtomicU64::new(1);

/// Install a transport observer without reading or buffering request bodies.
pub fn observe<F>(router: Router, callback: F, capture_limit: u32, capture_all: bool) -> Router
where F: Fn(String) + Send + Sync + 'static {
    let callback = Arc::new(callback);
    router.layer(ObservationLayer { callback, capture_limit: capture_limit as usize, capture_all })
}

struct ObservationLayer<F> { callback: Arc<F>, capture_limit: usize, capture_all: bool }
impl<F> Clone for ObservationLayer<F> {
    fn clone(&self) -> Self { Self { callback: self.callback.clone(), capture_limit: self.capture_limit, capture_all: self.capture_all } }
}

impl<S, F> Layer<S> for ObservationLayer<F> {
    type Service = ObservationService<S, F>;
    fn layer(&self, inner: S) -> Self::Service {
        ObservationService { inner, callback: self.callback.clone(), capture_limit: self.capture_limit, capture_all: self.capture_all }
    }
}

struct ObservationService<S, F> { inner: S, callback: Arc<F>, capture_limit: usize, capture_all: bool }
impl<S: Clone, F> Clone for ObservationService<S, F> {
    fn clone(&self) -> Self { Self { inner: self.inner.clone(), callback: self.callback.clone(), capture_limit: self.capture_limit, capture_all: self.capture_all } }
}

impl<S, F> Service<Request<Body>> for ObservationService<S, F>
where S: Service<Request<Body>, Response = axum::response::Response> + Clone + Send + 'static,
      S::Future: Send + 'static, S::Error: Send + 'static, F: Fn(String) + Send + Sync + 'static {
    type Response = axum::response::Response;
    type Error = S::Error;
    type Future = ObservationFuture<S::Future, F>;
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> { self.inner.poll_ready(cx) }
    fn call(&mut self, request: Request<Body>) -> Self::Future {
        let id = NEXT_REQUEST_ID.fetch_add(1, Ordering::Relaxed);
        let started = Instant::now();
        let (parts, body) = request.into_parts();
        emit(self.callback.as_ref(), json!({"event":"request_head","request_id":id,"method":parts.method.as_str(),"target":parts.uri.to_string(),"path":parts.uri.path(),"headers":headers_json(&parts.headers)}));
        let tracked = Body::new(ObservedBody::new(body, self.callback.clone(), id, "request", self.capture_limit, self.capture_all));
        let inner = self.inner.call(Request::from_parts(parts, tracked));
        ObservationFuture { inner, callback: self.callback.clone(), id, started, limit: self.capture_limit, capture_all: self.capture_all }
    }
}

pin_project_lite::pin_project! {
    struct ObservationFuture<Fut, F> {
        #[pin]
        inner: Fut,
        callback: Arc<F>,
        id: u64,
        started: Instant,
        limit: usize,
        capture_all: bool,
    }
}

impl<Fut, F, E> Future for ObservationFuture<Fut, F>
where Fut: Future<Output = Result<axum::response::Response, E>>,
      F: Fn(String) + Send + Sync + 'static {
    type Output = Result<axum::response::Response, E>;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.project();
        let response = std::task::ready!(this.inner.poll(cx))?;
        emit(this.callback.as_ref(), json!({"event":"response_head","request_id":*this.id,"status":response.status().as_u16(),"elapsed_ns":this.started.elapsed().as_nanos(),"headers":headers_json(response.headers())}));
        let (parts, body) = response.into_parts();
        let tracked = ObservedBody::new(body, this.callback.clone(), *this.id, "response", *this.limit, *this.capture_all);
        Poll::Ready(Ok(axum::response::Response::from_parts(parts, Body::new(tracked))))
    }
}

fn headers_json(headers: &http::HeaderMap) -> Value {
    let mut entries = Vec::new();
    for name in headers.keys() {
        for value in headers.get_all(name).iter() {
            entries.push(json!({"name":name.as_str(),"value":byte_value(value.as_bytes())}));
        }
    }
    Value::Array(entries)
}
fn byte_value(bytes: &[u8]) -> Value {
    match std::str::from_utf8(bytes) { Ok(s) => json!({"encoding":"utf8","data":s}), Err(_) => json!({"encoding":"hex","data":hex(bytes)}) }
}
fn hex(bytes: &[u8]) -> String { const HEX: &[u8;16] = b"0123456789abcdef"; let mut out=String::with_capacity(bytes.len()*2); for &b in bytes { out.push(HEX[(b>>4) as usize] as char); out.push(HEX[(b&15) as usize] as char); } out }
fn emit(callback: &dyn Fn(String), value: Value) { callback(value.to_string()); }

struct ObservedBody {
    inner: Body, callback: Arc<dyn Fn(String) + Send + Sync>, id: u64, direction: &'static str,
    limit: usize, capture_all: bool, captured: Vec<u8>, total: u64, finished: bool, index: u64,
}
impl ObservedBody {
    fn new<F: Fn(String) + Send + Sync + 'static>(inner: Body, callback: Arc<F>, id: u64, direction: &'static str, limit: usize, capture_all: bool) -> Self {
        Self { inner, callback, id, direction, limit, capture_all, captured: Vec::new(), total: 0, finished: false, index: 0 }
    }
    fn finish(&mut self, event: &'static str) {
        if self.finished { return; }
        self.finished = true;
        if self.direction == "request" {
            let body_event = if event == "transport_complete" { "request_body_complete" } else { "request_body_dropped" };
            emit(self.callback.as_ref(), json!({"event":body_event,"request_id":self.id,"total_bytes":self.total,"captured":byte_value(&self.captured),"truncated":!self.capture_all && self.total > self.captured.len() as u64}));
        }
        if event != "request_body_dropped" {
            emit(self.callback.as_ref(), json!({"event":event,"request_id":self.id,"direction":self.direction,"total_bytes":self.total}));
        }
    }
}
impl http_body::Body for ObservedBody {
    type Data = Bytes;
    type Error = <Body as http_body::Body>::Error;
    fn poll_frame(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_frame(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(None) => { this.finish("transport_complete"); Poll::Ready(None) }
            Poll::Ready(Some(Err(error))) => { this.finish("transport_error"); Poll::Ready(Some(Err(error))) }
            Poll::Ready(Some(Ok(frame))) => {
                if let Some(data) = frame.data_ref() {
                    if !data.is_empty() {
                        this.total = this.total.saturating_add(data.len() as u64);
                        if this.direction == "request" {
                            let room = if this.capture_all { data.len() } else { this.limit.saturating_sub(this.captured.len()) };
                            this.captured.extend_from_slice(&data[..room.min(data.len())]);
                        } else {
                            let take = if this.capture_all { data.len() } else { this.limit.min(data.len()) };
                            emit(this.callback.as_ref(), json!({"event":"response_data","request_id":this.id,"chunk_index":this.index,"bytes":data.len(),"data":byte_value(&data[..take]),"truncated":take < data.len()}));
                            this.index += 1;
                        }
                    }
                }
                if this.inner.is_end_stream() {
                    this.finish("transport_complete");
                }
                Poll::Ready(Some(Ok(frame)))
            }
        }
    }
    fn is_end_stream(&self) -> bool { self.finished || self.inner.is_end_stream() }
    fn size_hint(&self) -> http_body::SizeHint { self.inner.size_hint() }
}
impl Drop for ObservedBody {
    fn drop(&mut self) {
        let event = if self.inner.is_end_stream() {
            "transport_complete"
        } else if self.direction == "response" {
            "response_dropped"
        } else {
            "request_body_dropped"
        };
        self.finish(event);
    }
}
