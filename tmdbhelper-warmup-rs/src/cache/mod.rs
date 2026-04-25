pub mod art;
pub mod collection;
pub mod credits;
pub mod dimensions;
pub mod movie;
pub mod person;
pub mod tv;

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
         PRAGMA synchronous=OFF;
         PRAGMA locking_mode=EXCLUSIVE;
         PRAGMA busy_timeout=120000;
         PRAGMA foreign_keys=ON;
         PRAGMA cache_size=-262144;
         PRAGMA mmap_size=268435456;
         PRAGMA wal_autocheckpoint=1000;",
    )?;
    Ok(conn)
}

pub fn checkpoint_passive(conn: &Connection) {
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
}

pub fn now_unix() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64
}

pub fn default_expiry() -> i64 {
    now_unix() + DEFAULT_EXPIRY_DAYS * 86400
}
