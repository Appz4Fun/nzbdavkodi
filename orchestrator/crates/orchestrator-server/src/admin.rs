//! `/v1/admin/indexers` — CRUD over the dynamic-indexer config.
//!
//! Plan §5 specifies SQLite storage; Phase 1 keeps the JSON-on-disk
//! shape Python's `indexer_store.py` already writes so the Python
//! addon and the Rust orchestrator can read/write the same file
//! during the migration. Phase 3+ (peer-pool persistence) introduces
//! SQLite to this crate at a wider scope; the indexer store can move
//! there in a follow-up without touching the HTTP surface.
//!
//! Wire format (matches Python `indexer_store.load_indexers` /
//! `save_indexers`):
//!
//! ```json
//! {
//!   "version": 1,
//!   "indexers": [
//!     {
//!       "id": "nzbgeek",
//!       "preset_id": "nzbgeek",
//!       "name": "NZBGeek",
//!       "api_url": "https://api.nzbgeek.info/api",
//!       "api_key": "abcdef…",
//!       "enabled": true,
//!       "caps": { ... }
//!     }
//!   ]
//! }
//! ```

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, RwLock};

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};

const STORE_VERSION: u32 = 1;

/// One row in the indexer store. Field names match the Python
/// `normalize_indexer` shape so the two sides round-trip cleanly.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct IndexerEntry {
    pub id: String,
    #[serde(default)]
    pub preset_id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub api_url: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Free-form caps map. The Python normaliser stamps a shape on it
    /// (search_types / supported_params / categories); we keep it as
    /// raw JSON so the orchestrator can extend the caps schema
    /// without breaking the Python reader.
    #[serde(default)]
    pub caps: serde_json::Value,
    /// Tombstone flag — Python writes deleted entries with
    /// `enabled: false` + `deleted: true` so a future read can show
    /// "this was a managed indexer that's now off" history. We
    /// preserve the flag on round-trip but never produce it from the
    /// HTTP API.
    #[serde(default, skip_serializing_if = "is_false")]
    pub deleted: bool,
}

fn default_true() -> bool {
    true
}
fn is_false(v: &bool) -> bool {
    !*v
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct StorePayload {
    version: u32,
    #[serde(default)]
    indexers: Vec<IndexerEntry>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ListResponse {
    pub indexers: Vec<IndexerEntry>,
}

/// In-memory + on-disk store. Reads are cheap (Arc<RwLock>), writes
/// serialise + atomic-rename to keep the JSON consistent under
/// concurrent CRUD.
#[derive(Clone)]
pub struct IndexerStore {
    path: PathBuf,
    inner: Arc<RwLock<Vec<IndexerEntry>>>,
}

impl IndexerStore {
    pub fn new(path: PathBuf) -> std::io::Result<Self> {
        let initial = match std::fs::read_to_string(&path) {
            Ok(text) => {
                let payload: StorePayload = serde_json::from_str(&text).unwrap_or_default();
                payload.indexers
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(e) => return Err(e),
        };
        Ok(Self {
            path,
            inner: Arc::new(RwLock::new(initial)),
        })
    }

    pub fn list(&self) -> Vec<IndexerEntry> {
        self.inner.read().expect("indexer store poisoned").clone()
    }

    /// Insert a new entry; returns the inserted entry. Errors if an
    /// entry with the same `id` already exists.
    pub fn create(&self, entry: IndexerEntry) -> Result<IndexerEntry, StoreError> {
        let mut guard = self.inner.write().expect("indexer store poisoned");
        if entry.id.trim().is_empty() {
            return Err(StoreError::IdMissing);
        }
        if guard.iter().any(|e| e.id == entry.id && !e.deleted) {
            return Err(StoreError::AlreadyExists(entry.id));
        }
        guard.push(entry.clone());
        let snapshot = guard.clone();
        drop(guard);
        self.persist(&snapshot)?;
        Ok(entry)
    }

    /// Replace an entry by id. `id` in the body is ignored — the
    /// URL parameter wins.
    pub fn update(&self, id: &str, mut entry: IndexerEntry) -> Result<IndexerEntry, StoreError> {
        entry.id = id.to_string();
        let mut guard = self.inner.write().expect("indexer store poisoned");
        let Some(slot) = guard.iter_mut().find(|e| e.id == id) else {
            return Err(StoreError::NotFound(id.to_string()));
        };
        *slot = entry.clone();
        let snapshot = guard.clone();
        drop(guard);
        self.persist(&snapshot)?;
        Ok(entry)
    }

    /// Delete by id. Mirrors Python's tombstone behaviour — the row
    /// stays in the store with `deleted=true` + `enabled=false` so a
    /// future "show history" UI can present it.
    pub fn delete(&self, id: &str) -> Result<(), StoreError> {
        let mut guard = self.inner.write().expect("indexer store poisoned");
        let Some(slot) = guard.iter_mut().find(|e| e.id == id) else {
            return Err(StoreError::NotFound(id.to_string()));
        };
        slot.deleted = true;
        slot.enabled = false;
        let snapshot = guard.clone();
        drop(guard);
        self.persist(&snapshot)?;
        Ok(())
    }

    fn persist(&self, entries: &[IndexerEntry]) -> Result<(), StoreError> {
        let payload = StorePayload {
            version: STORE_VERSION,
            indexers: entries.to_vec(),
        };
        let body = serde_json::to_string_pretty(&payload)
            .map_err(|e| StoreError::Persist(e.to_string()))?;
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| StoreError::Persist(e.to_string()))?;
        }
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, body).map_err(|e| StoreError::Persist(e.to_string()))?;
        std::fs::rename(&tmp, &self.path).map_err(|e| StoreError::Persist(e.to_string()))?;
        Ok(())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("id is required")]
    IdMissing,
    #[error("indexer id already exists: {0}")]
    AlreadyExists(String),
    #[error("indexer not found: {0}")]
    NotFound(String),
    #[error("io error persisting store: {0}")]
    Persist(String),
}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Persist(e.to_string())
    }
}

impl IntoResponse for StoreError {
    fn into_response(self) -> axum::response::Response {
        let status = match &self {
            StoreError::IdMissing | StoreError::AlreadyExists(_) => StatusCode::BAD_REQUEST,
            StoreError::NotFound(_) => StatusCode::NOT_FOUND,
            StoreError::Persist(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };
        let body = BTreeMap::from([("error".to_string(), self.to_string())]);
        (status, Json(body)).into_response()
    }
}

#[derive(Clone)]
pub struct AdminState {
    pub store: IndexerStore,
}

pub fn admin_router(state: AdminState) -> Router {
    Router::new()
        .route("/v1/admin/indexers", get(list).post(create))
        .route(
            "/v1/admin/indexers/:id",
            put(update_by_id).delete(delete_by_id),
        )
        .with_state(state)
}

async fn list(State(state): State<AdminState>) -> Json<ListResponse> {
    Json(ListResponse {
        indexers: state.store.list(),
    })
}

async fn create(
    State(state): State<AdminState>,
    Json(entry): Json<IndexerEntry>,
) -> Result<(StatusCode, Json<IndexerEntry>), StoreError> {
    let created = state.store.create(entry)?;
    Ok((StatusCode::CREATED, Json(created)))
}

async fn update_by_id(
    State(state): State<AdminState>,
    Path(id): Path<String>,
    Json(entry): Json<IndexerEntry>,
) -> Result<Json<IndexerEntry>, StoreError> {
    Ok(Json(state.store.update(&id, entry)?))
}

async fn delete_by_id(
    State(state): State<AdminState>,
    Path(id): Path<String>,
) -> Result<StatusCode, StoreError> {
    state.store.delete(&id)?;
    Ok(StatusCode::NO_CONTENT)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::Request;
    use tower::ServiceExt;

    fn body_json(value: serde_json::Value) -> axum::body::Body {
        axum::body::Body::from(value.to_string())
    }

    fn empty_body() -> axum::body::Body {
        axum::body::Body::empty()
    }

    fn fresh_router(tmp: &tempfile::TempDir) -> Router {
        let path = tmp.path().join("indexers.json");
        let store = IndexerStore::new(path).unwrap();
        admin_router(AdminState { store })
    }

    #[tokio::test]
    async fn list_create_update_delete_full_cycle() {
        let tmp = tempfile::tempdir().unwrap();
        let app = fresh_router(&tmp);

        // List on an empty store -> empty array.
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/v1/admin/indexers")
                    .body(empty_body())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["indexers"], serde_json::json!([]));

        // Create one.
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/admin/indexers")
                    .header("content-type", "application/json")
                    .body(body_json(serde_json::json!({
                        "id": "nzbgeek",
                        "preset_id": "nzbgeek",
                        "name": "NZBGeek",
                        "api_url": "https://api.nzbgeek.info/api",
                        "api_key": "k",
                        "enabled": true
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::CREATED);

        // Duplicate create -> 400.
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/admin/indexers")
                    .header("content-type", "application/json")
                    .body(body_json(serde_json::json!({
                        "id": "nzbgeek",
                        "name": "NZBGeek duplicate",
                        "api_url": "https://api.nzbgeek.info/api",
                        "api_key": "k2",
                        "enabled": true
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);

        // Update.
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/v1/admin/indexers/nzbgeek")
                    .header("content-type", "application/json")
                    .body(body_json(serde_json::json!({
                        "id": "ignored-by-server",
                        "name": "NZBGeek (renamed)",
                        "api_url": "https://api.nzbgeek.info/api",
                        "api_key": "new-key",
                        "enabled": false
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["id"], "nzbgeek");
        assert_eq!(parsed["enabled"], false);

        // Update an unknown id -> 404.
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/v1/admin/indexers/missing")
                    .header("content-type", "application/json")
                    .body(body_json(serde_json::json!({
                        "id": "missing", "name": "x", "api_url": "x", "api_key": "x"
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NOT_FOUND);

        // Delete -> tombstone (still present, deleted=true).
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/v1/admin/indexers/nzbgeek")
                    .body(empty_body())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::NO_CONTENT);

        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/v1/admin/indexers")
                    .body(empty_body())
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["indexers"][0]["deleted"], true);
        assert_eq!(parsed["indexers"][0]["enabled"], false);
    }

    #[tokio::test]
    async fn store_round_trips_through_disk() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("indexers.json");
        let store = IndexerStore::new(path.clone()).unwrap();
        store
            .create(IndexerEntry {
                id: "x".into(),
                name: "x".into(),
                api_url: "http://localhost".into(),
                api_key: "k".into(),
                enabled: true,
                ..Default::default()
            })
            .unwrap();

        // A fresh store on the same path must see the written entry.
        let reopened = IndexerStore::new(path).unwrap();
        let entries = reopened.list();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].id, "x");
    }
}
