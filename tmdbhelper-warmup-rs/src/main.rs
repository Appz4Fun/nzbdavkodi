use clap::Parser;
use std::path::PathBuf;
use tracing::info;

#[derive(Parser, Debug)]
#[command(name = "warmup-rs", about = "Direct-write TMDBHelper cache warmer")]
struct Args {
    #[arg(long, env = "WARMUP_STATE_DB", default_value = "/var/media/CACHE_DRIVE/tmdb/scriptcache/state.db")]
    state_db: PathBuf,

    #[arg(long, env = "WARMUP_ITEM_DETAILS_DB",
          default_value = "/storage/.kodi/userdata/addon_data/plugin.video.themoviedb.helper/database_07/ItemDetails.db")]
    item_details_db: PathBuf,

    #[arg(long, env = "WARMUP_TMDB_API_KEY", default_value = "a07324c669cac4d96789197134ce272b")]
    tmdb_api_key: String,

    #[arg(long, env = "WARMUP_CONCURRENCY", default_value_t = 20)]
    concurrency: usize,

    #[arg(long, env = "WARMUP_BATCH_SIZE", default_value_t = 200)]
    batch_size: usize,

    #[arg(long)]
    smoke: bool,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "warmup_rs=info,warn".into()))
        .with_target(false)
        .init();

    let args = Args::parse();
    info!("warmup-rs starting concurrency={} batch={} state={} target={}",
          args.concurrency, args.batch_size, args.state_db.display(), args.item_details_db.display());

    if args.smoke {
        let _ = rusqlite::Connection::open_in_memory()?;
        let _ = reqwest::Client::builder().build()?;
        info!("smoke ok: rusqlite={}", rusqlite::version());
        return Ok(());
    }

    warmup_rs::worker::run(args.state_db, args.item_details_db, args.tmdb_api_key, args.concurrency, args.batch_size).await?;
    Ok(())
}
