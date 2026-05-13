//! SQLite-backed peer-pool persistence for Phase 3.

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use tokio::sync::broadcast;

#[derive(Clone)]
pub struct PeerPoolStore {
    conn: Arc<Mutex<Connection>>,
    resolve_events: broadcast::Sender<ResolveEvent>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct PeerPoolStats {
    pub peer_cache_size: u64,
    pub peer_ready_count: u64,
    pub peer_validated_count: u64,
    pub peer_rejected_count: u64,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct PeerPoolPruneStats {
    pub peer_pools_deleted: u64,
    pub resolve_events_deleted: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerPoolCachePolicy {
    max_age: Option<Duration>,
}

impl PeerPoolCachePolicy {
    pub const DEFAULT_MAX_AGE_SECS: u64 = 6 * 60 * 60;

    pub fn from_max_age_secs(max_age_secs: u64) -> Self {
        Self {
            max_age: Some(Duration::from_secs(max_age_secs)),
        }
    }

    pub fn disabled() -> Self {
        Self::from_max_age_secs(0)
    }

    fn is_fresh(self, updated_at_unix_ms: u64, now_unix_ms: u64) -> bool {
        match self.max_age {
            None => true,
            Some(max_age) if max_age.is_zero() => false,
            Some(max_age) => {
                let max_age_ms = max_age.as_millis().min(u128::from(u64::MAX)) as u64;
                now_unix_ms.saturating_sub(updated_at_unix_ms) <= max_age_ms
            }
        }
    }

    fn prune_cutoff_unix_ms(self, now_unix_ms: u64) -> Option<i64> {
        match self.max_age {
            None => None,
            Some(max_age) if max_age.is_zero() => Some(i64::MAX),
            Some(max_age) => {
                let max_age_ms = max_age.as_millis().min(u128::from(u64::MAX)) as u64;
                Some(now_unix_ms.saturating_sub(max_age_ms).min(i64::MAX as u64) as i64)
            }
        }
    }
}

impl Default for PeerPoolCachePolicy {
    fn default() -> Self {
        Self::from_max_age_secs(Self::DEFAULT_MAX_AGE_SECS)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResolveEvent {
    pub sequence: u64,
    pub resolve_id: String,
    pub event: String,
    pub peer_id: Option<String>,
    pub state: String,
    pub reason: Option<String>,
    pub payload: serde_json::Value,
    pub created_at_unix_ms: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum PeerPoolError {
    #[error("SQLite peer-pool error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("invalid peer-pool JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid peer-pool response: {0}")]
    InvalidResponse(String),
    #[error("io error opening peer-pool store: {0}")]
    Io(#[from] std::io::Error),
}

impl PeerPoolStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, PeerPoolError> {
        if let Some(parent) = path.as_ref().parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let (resolve_events, _) = broadcast::channel(1024);
        let store = Self {
            conn: Arc::new(Mutex::new(Connection::open(path)?)),
            resolve_events,
        };
        store.init()?;
        Ok(store)
    }

    pub fn subscribe_resolve_events(&self) -> broadcast::Receiver<ResolveEvent> {
        self.resolve_events.subscribe()
    }

    pub fn save_response<T: Serialize>(&self, response: &T) -> Result<(), PeerPoolError> {
        self.save_response_with_cache_key(response, None)
    }

    pub fn save_response_with_cache_key<T: Serialize>(
        &self,
        response: &T,
        cache_key: Option<&str>,
    ) -> Result<(), PeerPoolError> {
        let value = serde_json::to_value(response)?;
        let resolve_id = value
            .get("resolve_id")
            .and_then(|v| v.as_str())
            .filter(|v| !v.trim().is_empty())
            .ok_or_else(|| PeerPoolError::InvalidResponse("resolve_id is required".to_string()))?;
        let cache_key = cache_key.and_then(normalize_cache_key);
        let counts = peer_counts(&value);
        let body = serde_json::to_string(&value)?;
        let updated_at_unix_ms = unix_ms();
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        conn.execute(
            r#"
            INSERT INTO peer_pools (
                resolve_id,
                cache_key,
                response_json,
                peer_count,
                ready_peer_count,
                validated_peer_count,
                rejected_peer_count,
                updated_at_unix_ms
            )
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
            ON CONFLICT(resolve_id) DO UPDATE SET
                cache_key = excluded.cache_key,
                response_json = excluded.response_json,
                peer_count = excluded.peer_count,
                ready_peer_count = excluded.ready_peer_count,
                validated_peer_count = excluded.validated_peer_count,
                rejected_peer_count = excluded.rejected_peer_count,
                updated_at_unix_ms = excluded.updated_at_unix_ms
            "#,
            params![
                resolve_id,
                cache_key.as_deref(),
                body,
                counts.peer_count as i64,
                counts.ready_peer_count as i64,
                counts.validated_peer_count as i64,
                counts.rejected_peer_count as i64,
                updated_at_unix_ms as i64,
            ],
        )?;
        Ok(())
    }

    pub fn get_latest_response_by_cache_key(
        &self,
        cache_key: &str,
    ) -> Result<Option<serde_json::Value>, PeerPoolError> {
        self.get_latest_response_by_cache_key_with_policy(cache_key, PeerPoolCachePolicy::default())
    }

    pub fn get_latest_response_by_cache_key_with_policy(
        &self,
        cache_key: &str,
        policy: PeerPoolCachePolicy,
    ) -> Result<Option<serde_json::Value>, PeerPoolError> {
        let Some(cache_key) = normalize_cache_key(cache_key) else {
            return Ok(None);
        };
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        let row = conn
            .query_row(
                r#"
                SELECT response_json, updated_at_unix_ms
                FROM peer_pools
                WHERE cache_key = ?1
                ORDER BY updated_at_unix_ms DESC, rowid DESC
                LIMIT 1
                "#,
                [cache_key],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1).unwrap_or_default() as u64,
                    ))
                },
            )
            .optional()?;
        let Some((body, updated_at_unix_ms)) = row else {
            return Ok(None);
        };
        if !policy.is_fresh(updated_at_unix_ms, unix_ms()) {
            return Ok(None);
        }
        serde_json::from_str(&body)
            .map(Some)
            .map_err(PeerPoolError::from)
    }

    pub fn get_response(
        &self,
        resolve_id: &str,
    ) -> Result<Option<serde_json::Value>, PeerPoolError> {
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        let body = conn
            .query_row(
                "SELECT response_json FROM peer_pools WHERE resolve_id = ?1",
                [resolve_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        body.map(|text| serde_json::from_str(&text))
            .transpose()
            .map_err(PeerPoolError::from)
    }

    pub fn append_resolve_event(
        &self,
        resolve_id: &str,
        event: &str,
        peer_id: Option<&str>,
        state: &str,
        reason: Option<&str>,
        payload: serde_json::Value,
    ) -> Result<ResolveEvent, PeerPoolError> {
        let resolve_id = normalize_required(resolve_id, "resolve_id")?;
        let event = normalize_required(event, "event")?;
        let state = normalize_required(state, "state")?;
        let peer_id = peer_id.and_then(normalize_optional);
        let reason = reason.and_then(normalize_optional);
        let payload_json = serde_json::to_string(&payload)?;
        let created_at_unix_ms = unix_ms();
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        conn.execute(
            r#"
            INSERT INTO resolve_events (
                resolve_id,
                event_type,
                peer_id,
                state,
                reason,
                payload_json,
                created_at_unix_ms
            )
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
            "#,
            params![
                resolve_id,
                event,
                peer_id.as_deref(),
                state,
                reason.as_deref(),
                payload_json,
                created_at_unix_ms as i64,
            ],
        )?;
        let resolve_event = ResolveEvent {
            sequence: conn.last_insert_rowid() as u64,
            resolve_id,
            event,
            peer_id,
            state,
            reason,
            payload,
            created_at_unix_ms,
        };
        let _ = self.resolve_events.send(resolve_event.clone());
        Ok(resolve_event)
    }

    pub fn list_resolve_events(
        &self,
        resolve_id: &str,
    ) -> Result<Vec<ResolveEvent>, PeerPoolError> {
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        let mut stmt = conn.prepare(
            r#"
            SELECT
                sequence,
                resolve_id,
                event_type,
                peer_id,
                state,
                reason,
                payload_json,
                created_at_unix_ms
            FROM resolve_events
            WHERE resolve_id = ?1
            ORDER BY sequence ASC
            "#,
        )?;
        let rows = stmt
            .query_map([resolve_id], |row| {
                Ok(ResolveEventRow {
                    sequence: row.get::<_, i64>(0)? as u64,
                    resolve_id: row.get(1)?,
                    event: row.get(2)?,
                    peer_id: row.get(3)?,
                    state: row.get(4)?,
                    reason: row.get(5)?,
                    payload_json: row.get(6)?,
                    created_at_unix_ms: row.get::<_, i64>(7)? as u64,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|row| {
                Ok(ResolveEvent {
                    sequence: row.sequence,
                    resolve_id: row.resolve_id,
                    event: row.event,
                    peer_id: row.peer_id,
                    state: row.state,
                    reason: row.reason,
                    payload: serde_json::from_str(&row.payload_json)?,
                    created_at_unix_ms: row.created_at_unix_ms,
                })
            })
            .collect()
    }

    pub fn prune_stale(
        &self,
        policy: PeerPoolCachePolicy,
    ) -> Result<PeerPoolPruneStats, PeerPoolError> {
        self.prune_stale_at(policy, unix_ms())
    }

    fn prune_stale_at(
        &self,
        policy: PeerPoolCachePolicy,
        now_unix_ms: u64,
    ) -> Result<PeerPoolPruneStats, PeerPoolError> {
        let Some(cutoff_unix_ms) = policy.prune_cutoff_unix_ms(now_unix_ms) else {
            return Ok(PeerPoolPruneStats::default());
        };
        let mut conn = self.conn.lock().expect("peer-pool store poisoned");
        let tx = conn.transaction()?;
        let resolve_events_for_pools = tx.execute(
            r#"
            DELETE FROM resolve_events
            WHERE resolve_id IN (
                SELECT resolve_id
                FROM peer_pools
                WHERE updated_at_unix_ms < ?1
            )
            "#,
            [cutoff_unix_ms],
        )?;
        let peer_pools_deleted = tx.execute(
            "DELETE FROM peer_pools WHERE updated_at_unix_ms < ?1",
            [cutoff_unix_ms],
        )?;
        let orphan_events_deleted = tx.execute(
            r#"
            DELETE FROM resolve_events
            WHERE created_at_unix_ms < ?1
              AND NOT EXISTS (
                SELECT 1
                FROM peer_pools
                WHERE peer_pools.resolve_id = resolve_events.resolve_id
              )
            "#,
            [cutoff_unix_ms],
        )?;
        tx.commit()?;
        conn.execute_batch("PRAGMA optimize")?;
        Ok(PeerPoolPruneStats {
            peer_pools_deleted: peer_pools_deleted as u64,
            resolve_events_deleted: (resolve_events_for_pools + orphan_events_deleted) as u64,
        })
    }

    pub fn stats(&self) -> Result<PeerPoolStats, PeerPoolError> {
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        conn.query_row(
            r#"
            SELECT
                COUNT(*),
                COALESCE(SUM(ready_peer_count), 0),
                COALESCE(SUM(validated_peer_count), 0),
                COALESCE(SUM(rejected_peer_count), 0)
            FROM peer_pools
            "#,
            [],
            |row| {
                Ok(PeerPoolStats {
                    peer_cache_size: row.get::<_, i64>(0)? as u64,
                    peer_ready_count: row.get::<_, i64>(1)? as u64,
                    peer_validated_count: row.get::<_, i64>(2)? as u64,
                    peer_rejected_count: row.get::<_, i64>(3)? as u64,
                })
            },
        )
        .map_err(PeerPoolError::from)
    }

    fn init(&self) -> Result<(), PeerPoolError> {
        let conn = self.conn.lock().expect("peer-pool store poisoned");
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS peer_pools (
                resolve_id TEXT PRIMARY KEY,
                cache_key TEXT,
                response_json TEXT NOT NULL,
                peer_count INTEGER NOT NULL DEFAULT 0,
                ready_peer_count INTEGER NOT NULL DEFAULT 0,
                validated_peer_count INTEGER NOT NULL DEFAULT 0,
                rejected_peer_count INTEGER NOT NULL DEFAULT 0,
                updated_at_unix_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_peer_pools_cache_key_updated
                ON peer_pools(cache_key, updated_at_unix_ms DESC)
                WHERE cache_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS resolve_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                resolve_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                peer_id TEXT,
                state TEXT NOT NULL,
                reason TEXT,
                payload_json TEXT NOT NULL,
                created_at_unix_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_resolve_events_resolve_sequence
                ON resolve_events(resolve_id, sequence);
            "#,
        )?;
        ensure_cache_key_column(&conn)?;
        Ok(())
    }
}

struct ResolveEventRow {
    sequence: u64,
    resolve_id: String,
    event: String,
    peer_id: Option<String>,
    state: String,
    reason: Option<String>,
    payload_json: String,
    created_at_unix_ms: u64,
}

fn ensure_cache_key_column(conn: &Connection) -> Result<(), rusqlite::Error> {
    let mut stmt = conn.prepare("PRAGMA table_info(peer_pools)")?;
    let columns = stmt
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    if !columns.iter().any(|column| column == "cache_key") {
        conn.execute("ALTER TABLE peer_pools ADD COLUMN cache_key TEXT", [])?;
    }
    conn.execute(
        r#"
        CREATE INDEX IF NOT EXISTS idx_peer_pools_cache_key_updated
            ON peer_pools(cache_key, updated_at_unix_ms DESC)
            WHERE cache_key IS NOT NULL
        "#,
        [],
    )?;
    Ok(())
}

fn normalize_required(input: &str, field: &str) -> Result<String, PeerPoolError> {
    normalize_optional(input)
        .ok_or_else(|| PeerPoolError::InvalidResponse(format!("{field} is required")))
}

fn normalize_optional(input: &str) -> Option<String> {
    let normalized = input.trim();
    if normalized.is_empty() {
        None
    } else {
        Some(normalized.to_string())
    }
}

fn normalize_cache_key(input: &str) -> Option<String> {
    let normalized = input.trim().to_ascii_lowercase();
    if normalized.is_empty() {
        None
    } else {
        Some(normalized)
    }
}

#[derive(Default)]
struct PeerCounts {
    peer_count: u64,
    ready_peer_count: u64,
    validated_peer_count: u64,
    rejected_peer_count: u64,
}

fn peer_counts(value: &serde_json::Value) -> PeerCounts {
    let mut counts = PeerCounts::default();
    let Some(peers) = value.get("peers").and_then(|v| v.as_array()) else {
        return counts;
    };
    counts.peer_count = peers.len() as u64;
    for peer in peers {
        let state = peer.get("state").and_then(|v| v.as_str()).unwrap_or("");
        let validation_state = peer
            .get("validation_state")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if state == "ready" {
            counts.ready_peer_count += 1;
        }
        if state == "ready" && validation_state == "byte_sample_validated_phase_3" {
            counts.validated_peer_count += 1;
        }
        if state == "rejected" {
            counts.rejected_peer_count += 1;
        }
    }
    counts
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peer_pool_store_round_trips_resolve_response_and_stats() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path.clone()).unwrap();
        store
            .save_response(&serde_json::json!({
                "resolve_id": "01RESOLVE",
                "primary_peer_id": "01PRIMARY",
                "nzo_id": "nzo-1",
                "stream_url": "http://webdav/content/Movie.mkv",
                "stream_headers": {},
                "peer_cohort": [],
                "peers": [
                    {
                        "peer_id": "01PRIMARY",
                        "state": "ready",
                        "validation_state": "single_peer_phase_2",
                        "nzo_id": "nzo-1",
                        "stream_url": "http://webdav/content/Movie.mkv",
                        "stream_headers": {},
                        "content_length": 1234
                    },
                    {
                        "peer_id": "01VALID",
                        "state": "ready",
                        "validation_state": "byte_sample_validated_phase_3",
                        "nzo_id": "nzo-2",
                        "stream_url": "http://webdav/content/Movie-copy.mkv",
                        "stream_headers": {},
                        "content_length": 1234
                    },
                    {
                        "peer_id": "01REJECT",
                        "state": "rejected",
                        "validation_state": "byte_sample_mismatch_phase_3",
                        "nzo_id": "nzo-3",
                        "content_length": 1234
                    }
                ]
            }))
            .unwrap();

        let reopened = PeerPoolStore::open(path).unwrap();
        let stored = reopened.get_response("01RESOLVE").unwrap().unwrap();
        assert_eq!(stored["resolve_id"], "01RESOLVE");
        assert_eq!(
            stored["peers"][1]["validation_state"],
            "byte_sample_validated_phase_3"
        );

        let stats = reopened.stats().unwrap();
        assert_eq!(stats.peer_cache_size, 1);
        assert_eq!(stats.peer_ready_count, 2);
        assert_eq!(stats.peer_validated_count, 1);
        assert_eq!(stats.peer_rejected_count, 1);
    }

    #[test]
    fn peer_pool_store_finds_latest_response_by_cache_key() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path.clone()).unwrap();

        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01OLDER",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-old",
                    "stream_url": "http://webdav/content/old.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-old",
                            "stream_url": "http://webdav/content/old.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|fgt"),
            )
            .unwrap();
        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01NEWER",
                    "primary_peer_id": "01PRIMARY2",
                    "nzo_id": "nzo-new",
                    "stream_url": "http://webdav/content/new.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY2",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-new",
                            "stream_url": "http://webdav/content/new.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|fgt"),
            )
            .unwrap();

        let cached = store
            .get_latest_response_by_cache_key("tt1375666|1080p|fgt")
            .unwrap()
            .unwrap();
        assert_eq!(cached["resolve_id"], "01NEWER");
        assert_eq!(cached["stream_url"], "http://webdav/content/new.mkv");
        assert!(store
            .get_latest_response_by_cache_key("tt1375666|2160p|fgt")
            .unwrap()
            .is_none());
    }

    #[test]
    fn peer_pool_cache_policy_can_treat_entries_as_stale() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path).unwrap();

        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01CACHED",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/cached.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/cached.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|fgt"),
            )
            .unwrap();

        assert!(store
            .get_latest_response_by_cache_key_with_policy(
                "tt1375666|1080p|fgt",
                PeerPoolCachePolicy::disabled(),
            )
            .unwrap()
            .is_none());
        assert_eq!(
            store
                .get_latest_response_by_cache_key_with_policy(
                    "tt1375666|1080p|fgt",
                    PeerPoolCachePolicy::default(),
                )
                .unwrap()
                .unwrap()["resolve_id"],
            "01CACHED"
        );
    }

    #[test]
    fn peer_pool_store_prunes_stale_entries_and_events() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path).unwrap();

        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01OLD",
                    "primary_peer_id": "01OLDPRIMARY",
                    "nzo_id": "nzo-old",
                    "stream_url": "http://webdav/content/old.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01OLDPRIMARY",
                            "state": "ready",
                            "validation_state": "byte_sample_validated_phase_3",
                            "nzo_id": "nzo-old",
                            "stream_url": "http://webdav/content/old.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|old"),
            )
            .unwrap();
        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01FRESH",
                    "primary_peer_id": "01FRESHPRIMARY",
                    "nzo_id": "nzo-fresh",
                    "stream_url": "http://webdav/content/fresh.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01FRESHPRIMARY",
                            "state": "ready",
                            "validation_state": "byte_sample_validated_phase_3",
                            "nzo_id": "nzo-fresh",
                            "stream_url": "http://webdav/content/fresh.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|fresh"),
            )
            .unwrap();
        store
            .append_resolve_event(
                "01OLD",
                "resolve.cache_hit",
                Some("01OLDPRIMARY"),
                "ready",
                None,
                serde_json::json!({"peer_count": 1}),
            )
            .unwrap();
        store
            .append_resolve_event(
                "01FRESH",
                "resolve.cache_hit",
                Some("01FRESHPRIMARY"),
                "ready",
                None,
                serde_json::json!({"peer_count": 1}),
            )
            .unwrap();

        let old_ms = unix_ms().saturating_sub(120_000);
        {
            let conn = store.conn.lock().expect("peer-pool store poisoned");
            conn.execute(
                "UPDATE peer_pools SET updated_at_unix_ms = ?1 WHERE resolve_id = ?2",
                params![old_ms as i64, "01OLD"],
            )
            .unwrap();
            conn.execute(
                "UPDATE resolve_events SET created_at_unix_ms = ?1 WHERE resolve_id = ?2",
                params![old_ms as i64, "01OLD"],
            )
            .unwrap();
        }

        let pruned = store
            .prune_stale(PeerPoolCachePolicy::from_max_age_secs(60))
            .unwrap();

        assert_eq!(pruned.peer_pools_deleted, 1);
        assert_eq!(pruned.resolve_events_deleted, 1);
        assert!(store.get_response("01OLD").unwrap().is_none());
        assert!(store.list_resolve_events("01OLD").unwrap().is_empty());
        assert_eq!(
            store.get_response("01FRESH").unwrap().unwrap()["resolve_id"],
            "01FRESH"
        );
        assert_eq!(store.list_resolve_events("01FRESH").unwrap().len(), 1);
        assert_eq!(store.stats().unwrap().peer_cache_size, 1);
    }

    #[test]
    fn peer_pool_store_disabled_policy_prunes_all_cache_entries() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path).unwrap();

        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01CACHED",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/cached.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "byte_sample_validated_phase_3",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/cached.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|cached"),
            )
            .unwrap();
        store
            .append_resolve_event(
                "01CACHED",
                "resolve.cache_hit",
                Some("01PRIMARY"),
                "ready",
                None,
                serde_json::json!({"peer_count": 1}),
            )
            .unwrap();

        let pruned = store.prune_stale(PeerPoolCachePolicy::disabled()).unwrap();

        assert_eq!(pruned.peer_pools_deleted, 1);
        assert_eq!(pruned.resolve_events_deleted, 1);
        assert_eq!(store.stats().unwrap().peer_cache_size, 0);
        assert!(store.list_resolve_events("01CACHED").unwrap().is_empty());
    }

    #[test]
    fn peer_pool_store_keeps_entries_at_freshness_boundary() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path).unwrap();

        store
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01BOUNDARY",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/boundary.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "byte_sample_validated_phase_3",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/boundary.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666|1080p|boundary"),
            )
            .unwrap();
        store
            .append_resolve_event(
                "01BOUNDARY",
                "resolve.cache_hit",
                Some("01PRIMARY"),
                "ready",
                None,
                serde_json::json!({"peer_count": 1}),
            )
            .unwrap();

        {
            let conn = store.conn.lock().expect("peer-pool store poisoned");
            conn.execute(
                "UPDATE peer_pools SET updated_at_unix_ms = ?1 WHERE resolve_id = ?2",
                params![1_000_i64, "01BOUNDARY"],
            )
            .unwrap();
            conn.execute(
                "UPDATE resolve_events SET created_at_unix_ms = ?1 WHERE resolve_id = ?2",
                params![1_000_i64, "01BOUNDARY"],
            )
            .unwrap();
        }

        let pruned = store
            .prune_stale_at(PeerPoolCachePolicy::from_max_age_secs(1), 2_000)
            .unwrap();

        assert_eq!(pruned.peer_pools_deleted, 0);
        assert_eq!(pruned.resolve_events_deleted, 0);
        assert_eq!(
            store.get_response("01BOUNDARY").unwrap().unwrap()["resolve_id"],
            "01BOUNDARY"
        );
        assert_eq!(store.list_resolve_events("01BOUNDARY").unwrap().len(), 1);
    }

    #[test]
    fn peer_pool_store_round_trips_ordered_resolve_events() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("peer_pool.sqlite3");
        let store = PeerPoolStore::open(path.clone()).unwrap();

        store
            .append_resolve_event(
                "01RESOLVE",
                "submit.accepted",
                Some("01PRIMARY"),
                "submitted",
                None,
                serde_json::json!({"nzbdav_job_id": "nzo-1"}),
            )
            .unwrap();
        store
            .append_resolve_event(
                "01RESOLVE",
                "webdav.probe",
                Some("01PRIMARY"),
                "ready",
                Some("content_length_validated"),
                serde_json::json!({"content_length": 1234}),
            )
            .unwrap();

        let reopened = PeerPoolStore::open(path).unwrap();
        let events = reopened.list_resolve_events("01RESOLVE").unwrap();
        assert_eq!(events.len(), 2);
        assert!(events[0].sequence < events[1].sequence);
        assert_eq!(events[0].resolve_id, "01RESOLVE");
        assert_eq!(events[0].event, "submit.accepted");
        assert_eq!(events[0].peer_id.as_deref(), Some("01PRIMARY"));
        assert_eq!(events[0].state, "submitted");
        assert!(events[0].reason.is_none());
        assert_eq!(events[0].payload["nzbdav_job_id"], "nzo-1");
        assert_eq!(events[1].event, "webdav.probe");
        assert_eq!(events[1].state, "ready");
        assert_eq!(
            events[1].reason.as_deref(),
            Some("content_length_validated")
        );
        assert_eq!(events[1].payload["content_length"], 1234);
        assert!(reopened
            .list_resolve_events("01MISSING")
            .unwrap()
            .is_empty());
    }
}
