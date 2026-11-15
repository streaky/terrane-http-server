//! Owned conversions and native HTTP body pin/poll/drop mechanics.
//! Routing, protocol formatting, authorization and redaction remain in Terrane.
use axum::body::{Body, Bytes};
use http_body::Frame;
use std::{future::Future, pin::Pin, task::{Context, Poll}, time::Duration};

pub fn body_from_text(text: String) -> Body { Body::from(text) }
pub fn buffer_into_text(buffer: Bytes) -> Option<String> { String::from_utf8(buffer.into()).ok() }
pub fn decode_path_component(value: String) -> Option<String> {
    match urlencoding::decode(&value) {
        Ok(std::borrow::Cow::Borrowed(_)) => Some(value),
        Ok(std::borrow::Cow::Owned(decoded)) => Some(decoded),
        Err(_) => None,
    }
}

/// Distinct outcomes let Source retain timeout versus size/error diagnostics.
/// Axum's collector bounds incremental consumption without quadratic concatenation.
pub async fn bounded_body_to_bytes(body: Body, maximum: usize, timeout_ms: u64) -> Result<Bytes, u16> {
    let read = axum::body::to_bytes(body, maximum);
    if timeout_ms == 0 {
        read.await.map_err(|_| 413)
    } else {
        tokio::time::timeout(Duration::from_millis(timeout_ms), read)
            .await.map_err(|_| 408u16)?.map_err(|_| 413)
    }
}

/// An unknown size hint avoids inventing Content-Length: 0 for metadata-only HEAD.
pub fn metadata_only_body() -> Body {
    struct MetadataOnly;
    impl http_body::Body for MetadataOnly {
        type Data = Bytes;
        type Error = std::convert::Infallible;
        fn poll_frame(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> { Poll::Ready(None) }
    }
    Body::new(MetadataOnly)
}

/// Only one next-chunk future is retained; native demand controls production.
/// Cancellation drops that future before calling the finalizer exactly once.
pub fn streaming_body<F, Fut, C, E>(next: F, cancel: C) -> Body
where
    F: FnMut() -> Fut + Send + 'static,
    Fut: Future<Output = Result<Option<String>, E>> + Unpin + Send + 'static,
    C: FnMut() + Send + 'static,
    E: Send + 'static,
{
    struct StreamBody<F, Fut, C: FnMut()> {
        next: F,
        cancel: Option<C>,
        pending: Option<Fut>,
        done: bool,
    }
    // Source's callable ABI already pins/boxes its future; do not box it again.
    impl<F, Fut, C: FnMut()> Unpin for StreamBody<F, Fut, C> {}
    impl<F, Fut, C: FnMut()> StreamBody<F, Fut, C> {
        fn finish(&mut self) {
            self.done = true;
            self.pending = None;
            if let Some(mut cancel) = self.cancel.take() { cancel(); }
        }
    }
    impl<F, Fut, C: FnMut()> Drop for StreamBody<F, Fut, C> {
        fn drop(&mut self) { self.finish(); }
    }
    impl<F, Fut, C, E> http_body::Body for StreamBody<F, Fut, C>
    where
        F: FnMut() -> Fut + Send + 'static,
        Fut: Future<Output = Result<Option<String>, E>> + Unpin + Send + 'static,
        C: FnMut() + Send + 'static,
        E: Send + 'static,
    {
        type Data = Bytes;
        type Error = std::io::Error;
        fn poll_frame(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            let this = self.get_mut();
            if this.done { return Poll::Ready(None); }
            if this.pending.is_none() { this.pending = Some((this.next)()); }
            match Pin::new(this.pending.as_mut().expect("installed next-chunk future")).poll(cx) {
                Poll::Pending => Poll::Pending,
                Poll::Ready(Ok(Some(chunk))) => {
                    this.pending = None;
                    if chunk.is_empty() { cx.waker().wake_by_ref(); return Poll::Pending; }
                    Poll::Ready(Some(Ok(Frame::data(Bytes::from(chunk)))))
                }
                Poll::Ready(Ok(None)) => { this.finish(); Poll::Ready(None) }
                Poll::Ready(Err(_)) => {
                    this.finish();
                    // An unsuccessful producer must abort HTTP framing, not look like EOF.
                    Poll::Ready(Some(Err(std::io::Error::other("response producer failed"))))
                }
            }
        }
        fn is_end_stream(&self) -> bool { self.done }
    }
    Body::new(StreamBody { next, cancel: Some(cancel), pending: None, done: false })
}
