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
use std::{future::Future, net::SocketAddr, time::Duration};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::{broadcast, watch},
    task::JoinSet,
};
use tower_http::add_extension::AddExtension;

// Preserve the shutdown callback's source error identity after connection drain.
pub async fn serve_until<F, E>(
    listener: TcpListener,
    router: Router,
    maximum: u32,
    shutdown: F,
) -> Result<(), E>
where
    F: Future<Output = Result<(), E>> + Send + 'static,
    E: Send + 'static,
{
    let (stop, stopping) = watch::channel(false);
    let mut workers = JoinSet::new();
    tokio::pin!(shutdown);
    let outcome = loop {
        // A slot covers HTTP and any upgraded session, not merely the handshake.
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
            accepted = listener.accept() => match accepted {
                Ok((stream, peer)) => {
                    workers.spawn(connection(stream, peer, router.clone(), stopping.clone()));
                }
                Err(error) => {
                    eprintln!("http-server.accept: {error}");
                    tokio::select! {
                        biased;
                        result = &mut shutdown => break result,
                        _ = tokio::time::sleep(Duration::from_secs(1)) => {}
                    }
                }
            }
        }
    };
    // Release the port before waiting for responses/upgrades to finish.
    drop(listener);
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
