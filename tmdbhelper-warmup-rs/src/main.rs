mod api;
mod cache;
mod id;
mod state;
mod worker;

use clap::Parser;
use tracing::info;

#[derive(Parser, Debug)]
#[command(name = "warmup-rs")]
struct Args {
    #[arg(long)]
    smoke: bool,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .with_target(false)
        .init();

    let args = Args::parse();
    info!("warmup-rs smoke build, sqlite={}, smoke_flag={}", rusqlite::version(), args.smoke);

    let _ = rusqlite::Connection::open_in_memory()?;
    let _ = reqwest::Client::builder().build()?;
    info!("rusqlite + reqwest linked OK");
    Ok(())
}
