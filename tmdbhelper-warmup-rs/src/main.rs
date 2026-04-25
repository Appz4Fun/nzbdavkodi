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

    #[arg(long, env = "WARMUP_TMDB_API_KEY")]
    tmdb_api_key: String,

    #[arg(long, env = "WARMUP_CONCURRENCY", default_value_t = 40, value_parser = clap::value_parser!(u64).range(1..))]
    concurrency: u64,

    #[arg(long, env = "WARMUP_BATCH_SIZE", default_value_t = 200, value_parser = clap::value_parser!(u64).range(1..))]
    batch_size: u64,

    #[arg(long, env = "WARMUP_MODE", default_value = "metadata")]
    mode: String,

    #[arg(long, env = "WARMUP_TEXTURES_DB", default_value = "/var/media/CACHE_DRIVE/tmdb/Textures13.db")]
    textures_db: PathBuf,

    #[arg(long, env = "WARMUP_THUMBNAILS_DIR", default_value = "/var/media/CACHE_DRIVE/tmdb/Thumbnails")]
    thumbnails_dir: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "warmup_rs=info,warn".into()))
        .with_target(false)
        .init();

    let args = Args::parse();
    let concurrency = args.concurrency as usize;
    let batch_size = args.batch_size as usize;
    info!("warmup-rs starting mode={} concurrency={} batch={}",
          args.mode, concurrency, batch_size);

    match args.mode.as_str() {
        "metadata" => {
            info!("metadata mode: state={} target={}", args.state_db.display(), args.item_details_db.display());
            warmup_rs::worker::run(args.state_db, args.item_details_db, args.tmdb_api_key, concurrency, batch_size).await?;
        }
        "images" => {
            info!("images mode: textures={} thumbnails={}", args.textures_db.display(), args.thumbnails_dir.display());
            warmup_rs::images::run(args.item_details_db, args.textures_db, args.thumbnails_dir, concurrency).await?;
        }
        "smoke" => {
            let _ = rusqlite::Connection::open_in_memory()?;
            let _ = reqwest::Client::builder().build()?;
            info!("smoke ok: rusqlite={}", rusqlite::version());
        }
        other => {
            anyhow::bail!("unknown mode '{}', expected: metadata, images, smoke", other);
        }
    }
    Ok(())
}
