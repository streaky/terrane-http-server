//! Host-controlled body and shutdown gates for real transport regression tests.
//! Files coordinate with the external client using host time, not virtual time.
use axum::{
    Router,
    body::{Body, Bytes},
    extract::ConnectInfo,
    response::Response,
    routing::get,
};
use http_body::{Body as HttpBody, Frame};
use std::{
    convert::Infallible,
    future::Future,
    pin::Pin,
    task::{Context, Poll},
};

async fn wait_for(name: &str) {
    while !std::path::Path::new(name).exists() {
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
}

pub async fn shutdown() -> bool {
    wait_for("shutdown").await;
    std::path::Path::new("fail-shutdown").exists()
}

struct ControlledBody {
    stage: u8,
    release: Pin<Box<dyn Future<Output = ()> + Send>>,
}

impl HttpBody for ControlledBody {
    type Data = Bytes;
    type Error = Infallible;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        match self.stage {
            0 => {
                self.stage = 1;
                Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"started\n")))))
            }
            1 => {
                if self.release.as_mut().poll(cx).is_pending() {
                    return Poll::Pending;
                }
                self.stage = 2;
                Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"finished\n")))))
            }
            _ => Poll::Ready(None),
        }
    }
}

pub fn router() -> Router {
    Router::new()
        .route(
            "/slow",
            get(|| async {
                Response::new(Body::new(ControlledBody {
                    stage: 0,
                    release: Box::pin(wait_for("release")),
                }))
            }),
        )
        .route("/probe", get(|| async { "probe" }))
        .route(
            "/peer",
            get(
                |ConnectInfo(peer): ConnectInfo<std::net::SocketAddr>| async move {
                    peer.ip().to_string()
                },
            ),
        )
}
