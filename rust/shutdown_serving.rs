//! Pinned native connection futures and shutdown coordination for `serve-*-until`.
//!
//! Route composition, access policy and WebSocket handlers stay in Terrane. Rust
//! owns the select/pin graph here: it must stop accepting without cancelling live
//! Hyper response futures, request graceful shutdown on each connection, and retain
//! connection slots through upgraded sessions. The projected APIs do not expose a
//! source-storable pinned connection or an addressable graceful-shutdown operation
//! on its live future. Remove this module when that complete graph is expressible
//! in source, including the bounded accept path and upgraded-session lease drain.

use axum::{Router, extract::ConnectInfo};
use hyper_util::{
    rt::{TokioExecutor, TokioIo},
    server::conn::auto::Builder,
    service::TowerToHyperService,
};
use std::{
    future::{Future, poll_fn},
    net::SocketAddr,
    pin::Pin,
    task::Poll,
    time::Duration,
};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::{broadcast, watch},
    task::JoinSet,
};
use tower_http::add_extension::AddExtension;

struct Listener {
    socket: TcpListener,
    retry: Option<Pin<Box<tokio::time::Sleep>>>,
}

pub async fn serve_listeners_until<F, E>(
    listeners: Vec<TcpListener>,
    router: Router,
    maximum: u32,
    shutdown: F,
) -> Result<(), E>
where
    F: Future<Output = Result<(), E>> + Send + 'static,
    E: Send + 'static,
{
    let (stop, stopping) = watch::channel(false);
    let mut listeners: Vec<_> = listeners
        .into_iter()
        .map(|socket| Listener {
            socket,
            retry: None,
        })
        .collect();
    let mut next_listener = 0;
    let mut workers = JoinSet::new();
    tokio::pin!(shutdown);
    let outcome = loop {
        if maximum > 0 && workers.len() >= maximum as usize {
            tokio::select! {
                biased;
                result = &mut shutdown => break result,
                _ = workers.join_next() => {}
            }
            continue;
        }
        tokio::select! {
            biased;
            result = &mut shutdown => break result,
            _ = workers.join_next(), if !workers.is_empty() => {},
            (stream, peer) = poll_fn(|cx| {
                let count = listeners.len();
                for _ in 0..count {
                    let listener = &mut listeners[next_listener];
                    next_listener = (next_listener + 1) % count;
                    if let Some(retry) = &mut listener.retry {
                        if retry.as_mut().poll(cx).is_pending() {
                            continue;
                        }
                        listener.retry = None;
                    }
                    match listener.socket.poll_accept(cx) {
                        Poll::Pending => {}
                        Poll::Ready(Ok(accepted)) => return Poll::Ready(accepted),
                        Poll::Ready(Err(error)) => {
                            eprintln!("http-server.accept: {error}");
                            let mut retry = Box::pin(tokio::time::sleep(Duration::from_secs(1)));
                            let _ = retry.as_mut().poll(cx);
                            listener.retry = Some(retry);
                        }
                    }
                }
                Poll::Pending
            }) => {
                workers.spawn(connection(stream, peer, router.clone(), stopping.clone()));
            }
        }
    };
    drop(listeners);
    let _ = stop.send(true);
    while workers.join_next().await.is_some() {}
    outcome
}

async fn connection(
    stream: TcpStream,
    peer: SocketAddr,
    router: Router,
    mut stopping: watch::Receiver<bool>,
) {
    let (lease, mut released) = broadcast::channel::<u8>(1);
    {
        let service = AddExtension::new(AddExtension::new(router, ConnectInfo(peer)), lease);
        let builder = Builder::new(TokioExecutor::new());
        let connection = builder.serve_connection_with_upgrades(
            TokioIo::new(stream),
            TowerToHyperService::new(service),
        );
        tokio::pin!(connection);
        if *stopping.borrow() {
            connection.as_mut().graceful_shutdown();
            let _ = connection.await;
        } else {
            tokio::select! {
                biased;
                _ = stopping.changed() => {
                    connection.as_mut().graceful_shutdown();
                    let _ = connection.await;
                }
                _ = &mut connection => {}
            }
        }
    }
    // HTTP completion alone does not end an upgraded WebSocket session's slot.
    let _ = released.recv().await;
}
