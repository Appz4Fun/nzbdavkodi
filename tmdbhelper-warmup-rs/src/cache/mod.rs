pub mod art;
pub mod credits;
pub mod dimensions;
pub mod movie;

use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub const DATALEVEL_FULL: i64 = 5;
pub const DEFAULT_EXPIRY_DAYS: i64 = 30;

pub fn open_writer(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_FULL_MUTEX,
    )
    .with_context(|| format!("open {}", path.display()))?;
    // busy_timeout=30s tolerates Kodi's periodic database vacuum cycles
    // (observed up to 60s on the live box).
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA busy_timeout=30000;
         PRAGMA foreign_keys=ON;",
    )?;
    Ok(conn)
}

pub fn now_unix() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64
}

pub fn default_expiry() -> i64 {
    now_unix() + DEFAULT_EXPIRY_DAYS * 86400
}
