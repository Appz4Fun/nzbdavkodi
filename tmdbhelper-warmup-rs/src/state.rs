use anyhow::{Context, Result};
use rusqlite::{params, Connection, OpenFlags};
use std::path::Path;
use crate::id::TmdbType;

pub type ChildBatch<'a> = (i64, TmdbType, &'a [(i64, TmdbType, f64)], i64);

pub struct StateDb {
    conn: Connection,
}

#[derive(Debug, Clone)]
pub struct QueueItem {
    pub tmdb_id: i64,
    pub tmdb_type: TmdbType,
    pub depth: i64,
    pub popularity: f64,
}

fn type_to_str(t: TmdbType) -> &'static str {
    match t {
        TmdbType::Movie => "movie",
        TmdbType::Tv => "tv",
        TmdbType::Person => "person",
        TmdbType::Collection => "collection",
    }
}

fn type_from_str(s: &str) -> Option<TmdbType> {
    Some(match s {
        "movie" => TmdbType::Movie,
        "tv" => TmdbType::Tv,
        "person" => TmdbType::Person,
        "collection" | "set" => TmdbType::Collection,
        _ => return None,
    })
}

impl StateDb {
    pub fn open(path: &Path) -> Result<Self> {
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE)
            .with_context(|| format!("open state {}", path.display()))?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=OFF;
             PRAGMA busy_timeout=120000;
             PRAGMA cache_size=-65536;",
        )?;
        Ok(Self { conn })
    }

    /// Pop a batch of pending items, ordered by (depth ASC, popularity DESC).
    /// Removed from queue atomically. Caller must mark visited or re-enqueue on failure.
    pub fn pop_batch(&mut self, limit: usize) -> Result<Vec<QueueItem>> {
        let tx = self.conn.transaction()?;
        let mut items = Vec::new();
        {
            let mut stmt = tx.prepare(
                "SELECT tmdb_id, tmdb_type, depth, popularity FROM queue
                 ORDER BY depth ASC, popularity DESC, enqueued_at ASC LIMIT ?1",
            )?;
            let rows = stmt.query_map(params![limit as i64], |r| {
                let type_s: String = r.get(1)?;
                Ok((r.get::<_, i64>(0)?, type_s, r.get::<_, i64>(2)?, r.get::<_, f64>(3).unwrap_or(0.0)))
            })?;
            for row in rows {
                let (id, type_s, depth, pop) = row?;
                if let Some(t) = type_from_str(&type_s) {
                    items.push(QueueItem { tmdb_id: id, tmdb_type: t, depth, popularity: pop });
                }
            }
        }
        for item in &items {
            tx.execute(
                "DELETE FROM queue WHERE tmdb_id=?1 AND tmdb_type=?2",
                params![item.tmdb_id, type_to_str(item.tmdb_type)],
            )?;
        }
        tx.commit()?;
        Ok(items)
    }

    pub fn mark_visited(&mut self, tmdb_id: i64, tmdb_type: TmdbType) -> Result<()> {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
        self.conn.execute(
            "INSERT OR REPLACE INTO visited (tmdb_id, tmdb_type, visited_at, result) VALUES (?1, ?2, ?3, 'ok')",
            params![tmdb_id, type_to_str(tmdb_type), now],
        )?;
        Ok(())
    }

    pub fn enqueue_child(&mut self, tmdb_id: i64, tmdb_type: TmdbType, depth: i64, popularity: f64) -> Result<()> {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
        let visited: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM visited WHERE tmdb_id=?1 AND tmdb_type=?2",
            params![tmdb_id, type_to_str(tmdb_type)], |r| r.get(0))?;
        if visited > 0 { return Ok(()); }
        self.conn.execute(
            "INSERT OR IGNORE INTO queue (tmdb_id, tmdb_type, depth, popularity, enqueued_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![tmdb_id, type_to_str(tmdb_type), depth, popularity, now],
        )?;
        Ok(())
    }

    /// Batch-enqueue children + mark one item visited in a SINGLE transaction.
    /// This is critical for throughput: un-batched auto-commits on a USB HDD
    /// cost ~10ms each, so 100 children × 2 ops = ~2 seconds. One transaction
    /// collapses that to ~50ms (one WAL commit).
    pub fn visit_and_enqueue_batch(
        &mut self,
        tmdb_id: i64,
        tmdb_type: TmdbType,
        children: &[(i64, TmdbType, f64)],
        child_depth: i64,
    ) -> Result<()> {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
        let tx = self.conn.transaction()?;
        tx.execute(
            "INSERT OR REPLACE INTO visited (tmdb_id, tmdb_type, visited_at, result) VALUES (?1, ?2, ?3, 'ok')",
            params![tmdb_id, type_to_str(tmdb_type), now],
        )?;
        for (cid, ctype, pop) in children {
            let already: i64 = tx.query_row(
                "SELECT COUNT(*) FROM visited WHERE tmdb_id=?1 AND tmdb_type=?2",
                params![*cid, type_to_str(*ctype)], |r| r.get(0))?;
            if already > 0 { continue; }
            tx.execute(
                "INSERT OR IGNORE INTO queue (tmdb_id, tmdb_type, depth, popularity, enqueued_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![*cid, type_to_str(*ctype), child_depth, *pop, now],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Mega-batch: visit + enqueue children for MULTIPLE items in ONE transaction.
    /// At 7/s with 20 items/batch, the old per-item tx pattern still costs 20 WAL
    /// commits per batch. This collapses them to 1.
    pub fn visit_and_enqueue_multi(
        &mut self,
        items: &[ChildBatch<'_>],
    ) -> Result<()> {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
        let tx = self.conn.transaction()?;
        for (tmdb_id, tmdb_type, children, child_depth) in items {
            tx.execute(
                "INSERT OR REPLACE INTO visited (tmdb_id, tmdb_type, visited_at, result) VALUES (?1, ?2, ?3, 'ok')",
                params![*tmdb_id, type_to_str(*tmdb_type), now],
            )?;
            for (cid, ctype, pop) in *children {
                let already: i64 = tx.query_row(
                    "SELECT COUNT(*) FROM visited WHERE tmdb_id=?1 AND tmdb_type=?2",
                    params![*cid, type_to_str(*ctype)], |r| r.get(0))?;
                if already > 0 { continue; }
                tx.execute(
                    "INSERT OR IGNORE INTO queue (tmdb_id, tmdb_type, depth, popularity, enqueued_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![*cid, type_to_str(*ctype), *child_depth, *pop, now],
                )?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn queue_size(&self) -> Result<i64> {
        Ok(self.conn.query_row("SELECT COUNT(*) FROM queue", [], |r| r.get(0))?)
    }

    pub fn visited_count(&self) -> Result<i64> {
        Ok(self.conn.query_row("SELECT COUNT(*) FROM visited", [], |r| r.get(0))?)
    }
}
