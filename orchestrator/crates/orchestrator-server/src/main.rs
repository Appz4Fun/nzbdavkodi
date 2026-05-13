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
use orchestrator_server::{
    admin::{AdminState, IndexerStore},
    logging::LogEnvelopeLayer,
    logging::Outcome,
    peer_pool::{PeerPoolCachePolicy, PeerPoolStore},
    router_with_admin_peer_pool_and_policy,
};
use tokio::signal;
use tracing::{info, warn};
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

    /// JSON-on-disk indexer store. Same shape as the Python
    /// `indexer_store.py` writer so the two readers/writers can
    /// round-trip during the migration. Defaults to
    /// `./indexers.json` next to the binary which is fine for
    /// development; the addon spawn always sets
    /// ORCHESTRATOR_INDEXER_STORE_PATH explicitly.
    #[arg(
        long,
        env = "ORCHESTRATOR_INDEXER_STORE_PATH",
        default_value = "indexers.json"
    )]
    indexer_store_path: std::path::PathBuf,

    /// SQLite peer-pool cache. Phase 3 persists validated resolve
    /// responses here so `/v1/peers/<resolve_id>` and `/v1/health`
    /// survive process restarts.
    #[arg(
        long,
        env = "ORCHESTRATOR_PEER_POOL_DB_PATH",
        default_value = "peer_pool.sqlite3"
    )]
    peer_pool_db_path: std::path::PathBuf,

    /// Maximum age for peer-pool cache hits. A value of 0 disables
    /// cache hits; stale entries fall through to live resolve and are
    /// overwritten when validation succeeds.
    #[arg(
        long,
        env = "ORCHESTRATOR_PEER_POOL_CACHE_MAX_AGE_SECS",
        default_value_t = PeerPoolCachePolicy::DEFAULT_MAX_AGE_SECS
    )]
    peer_pool_cache_max_age_secs: u64,
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

    let store = IndexerStore::new(args.indexer_store_path.clone()).with_context(|| {
        format!(
            "loading indexer store from {}",
            args.indexer_store_path.display()
        )
    })?;
    let peer_pool = PeerPoolStore::open(args.peer_pool_db_path.clone()).with_context(|| {
        format!(
            "opening peer-pool database at {}",
            args.peer_pool_db_path.display()
        )
    })?;
    let cache_policy = PeerPoolCachePolicy::from_max_age_secs(args.peer_pool_cache_max_age_secs);
    match peer_pool.prune_stale(cache_policy) {
        Ok(stats) => {
            info!(
                event = "peer_pool.pruned",
                outcome = Outcome::Ok.as_str(),
                peer_pools_deleted = stats.peer_pools_deleted,
                resolve_events_deleted = stats.resolve_events_deleted,
                "peer-pool stale cache cleanup completed"
            );
        }
        Err(error) => {
            warn!(
                event = "peer_pool.prune_failed",
                outcome = Outcome::Error.as_str(),
                reason = %error,
                "peer-pool stale cache cleanup failed; continuing"
            );
        }
    }
    let app = router_with_admin_peer_pool_and_policy(AdminState { store }, peer_pool, cache_policy);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_accepts_peer_pool_db_path() {
        let args = Args::try_parse_from([
            "orchestrator",
            "--peer-pool-db-path",
            "/tmp/nzbdav-peer-pool.sqlite3",
        ])
        .unwrap();

        assert_eq!(
            args.peer_pool_db_path,
            std::path::PathBuf::from("/tmp/nzbdav-peer-pool.sqlite3")
        );
    }

    #[test]
    fn cli_accepts_peer_pool_cache_max_age() {
        let args = Args::try_parse_from(["orchestrator", "--peer-pool-cache-max-age-secs", "120"])
            .unwrap();

        assert_eq!(args.peer_pool_cache_max_age_secs, 120);
    }
}
