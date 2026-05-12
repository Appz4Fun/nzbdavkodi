//! Orchestrator binary entry point.
//!
//! Phase 0: parse CLI / env config, install the JSON-envelope log
//! layer, bind the axum `/v1/health` route, sit on the listener until
//! Ctrl-C or SIGTERM. The graceful-shutdown path matters because
//! `service.py` will `terminate()` this process on Kodi shutdown and
//! we want the `service.stopped` event in Loki before exit.

use std::net::{IpAddr, SocketAddr};
use std::time::Instant;

use anyhow::Context;
use clap::Parser;
use orchestrator_server::{logging::LogEnvelopeLayer, logging::Outcome, router};
use tokio::signal;
use tracing::info;
use tracing_subscriber::prelude::*;
use tracing_subscriber::EnvFilter;
use ulid::Ulid;

#[derive(Parser, Debug)]
#[command(name = "orchestrator", about = "nzbdav-orchestrator HTTP server")]
struct Args {
    /// Bind address. Loopback by default — see plan §10 decision 1
    /// (sidecar per Kodi box, localhost proxy URL semantics preserved).
    #[arg(long, env = "ORCHESTRATOR_BIND", default_value = "127.0.0.1")]
    bind: IpAddr,

    /// Bind port. 0 = OS-assigned (used by harness tests).
    #[arg(long, env = "ORCHESTRATOR_PORT", default_value_t = 0)]
    port: u16,

    /// Optional file the server writes its bound `host:port` to once
    /// the listener is up. `service.py` reads it to learn where to
    /// route /v1/* calls when port=0 was used.
    #[arg(long, env = "ORCHESTRATOR_ADDR_FILE")]
    addr_file: Option<std::path::PathBuf>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    install_logging();

    let started = Instant::now();
    let args = Args::parse();
    let boot_id = Ulid::new().to_string();

    info!(
        event = "service.started",
        outcome = Outcome::Started.as_str(),
        request_id = %boot_id,
        bind = %args.bind,
        port = args.port as u64,
        "orchestrator starting"
    );

    let addr = SocketAddr::new(args.bind, args.port);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("failed to bind {addr}"))?;
    let local = listener.local_addr()?;

    if let Some(path) = args.addr_file.as_ref() {
        // Best-effort: addr file is purely an IPC convenience. The
        // server still starts even if the write fails — callers that
        // hard-required it will retry once the port is known another
        // way.
        let body = format!("{}:{}", local.ip(), local.port());
        if let Err(e) = tokio::fs::write(path, &body).await {
            tracing::warn!(
                event = "service.addr_file_write_failed",
                outcome = Outcome::Error.as_str(),
                reason = %e,
                "addr-file write failed; continuing"
            );
        }
    }

    info!(
        event = "service.listening",
        outcome = Outcome::Ok.as_str(),
        request_id = %boot_id,
        duration_ms = started.elapsed().as_millis() as u64,
        bind = %local.ip(),
        port = local.port() as u64,
        "orchestrator listening"
    );

    let app = router();
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("axum serve failed")?;

    info!(
        event = "service.stopped",
        outcome = Outcome::Ok.as_str(),
        request_id = %boot_id,
        "orchestrator stopped"
    );

    Ok(())
}

fn install_logging() {
    let filter = EnvFilter::try_from_env("ORCHESTRATOR_LOG")
        .unwrap_or_else(|_| EnvFilter::new("orchestrator=info,orchestrator_server=info,warn"));
    tracing_subscriber::registry()
        .with(filter)
        .with(LogEnvelopeLayer::stdout())
        .init();
}

async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c().await.expect("install ctrl_c handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}
