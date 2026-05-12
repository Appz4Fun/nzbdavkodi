//! fault-proxy binary — matches the env-var contract of
//! `tests/extreme/fault_proxy.py` so docker-compose can swap them.

use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::Arc;

use clap::Parser;
use fault_proxy::{
    control::ControlState, events::EventSink, proxy::run, proxy::ProxyConfig, proxy::ProxyHandler,
    state::ProxyState,
};
use tracing_subscriber::EnvFilter;

#[derive(Parser, Debug)]
#[command(
    name = "fault-proxy",
    about = "Rust port of tests/extreme/fault_proxy.py"
)]
struct Args {
    #[arg(
        long,
        env = "FAULT_PROXY_UPSTREAM",
        default_value = "http://nzbdav-rs:8080"
    )]
    upstream: String,

    #[arg(long, env = "FAULT_PROXY_LISTEN", default_value = "0.0.0.0")]
    listen: IpAddr,

    #[arg(long, env = "FAULT_PROXY_PORT", default_value_t = 19080)]
    port: u16,

    #[arg(long, env = "FAULT_PROXY_CONTROL_PORT", default_value_t = 19081)]
    control_port: u16,

    #[arg(long, env = "FAULT_PROXY_FAIL_BYTES", default_value_t = 4 * 1024 * 1024)]
    fail_bytes: usize,

    #[arg(long, env = "FAULT_PROXY_SLOW_BPS", default_value_t = 50 * 1024)]
    slow_bps: usize,

    #[arg(long, env = "FAULT_PROXY_SLOW_DURATION", default_value_t = 30.0)]
    slow_duration: f64,

    #[arg(long, env = "FAULT_PROXY_MIN_FAIL_START", default_value_t = 1024 * 1024)]
    min_fail_start: u64,

    #[arg(long, env = "FAULT_PROXY_MAX_FAIL_START", default_value_t = i64::MAX as u64)]
    max_fail_start: u64,

    #[arg(long, env = "FAULT_PROXY_EVENTS")]
    events_path: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_env("FAULT_PROXY_LOG")
                .unwrap_or_else(|_| EnvFilter::new("fault_proxy=info,warn")),
        )
        .with_target(false)
        .init();

    let args = Args::parse();

    let state = Arc::new(ProxyState::new());
    let sink = Arc::new(EventSink::new(args.events_path.clone()));

    let config = ProxyConfig {
        upstream: args.upstream.clone(),
        fail_bytes: args.fail_bytes,
        slow_bps: args.slow_bps,
        slow_duration_secs: args.slow_duration,
        min_fail_start: args.min_fail_start,
        max_fail_start: args.max_fail_start,
    };

    let handler = ProxyHandler::new(state.clone(), sink, config);
    let control_state = ControlState {
        state: state.clone(),
    };

    let proxy_addr = SocketAddr::new(args.listen, args.port);
    let control_addr = SocketAddr::new(args.listen, args.control_port);

    run(proxy_addr, control_addr, handler, control_state).await
}
