use warmup_rs::id::TmdbType;
use warmup_rs::state::StateDb;
use rusqlite::Connection;

fn make_state_schema(path: &std::path::Path) {
    let c = Connection::open(path).unwrap();
    c.execute_batch("
        CREATE TABLE queue (tmdb_id INTEGER, tmdb_type TEXT, depth INTEGER, popularity REAL DEFAULT 0.0,
                            enqueued_at REAL, PRIMARY KEY(tmdb_id, tmdb_type));
        CREATE INDEX idx_queue_priority ON queue(depth ASC, popularity DESC);
        CREATE TABLE visited (tmdb_id INTEGER, tmdb_type TEXT, visited_at REAL,
                              result TEXT NOT NULL DEFAULT 'ok',
                              PRIMARY KEY(tmdb_id, tmdb_type));
    ").unwrap();
}

#[test]
fn enqueue_pop_visited_roundtrip() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("state.db");
    make_state_schema(&p);

    let mut s = StateDb::open(&p).unwrap();
    s.enqueue_child(550, TmdbType::Movie, 0, 100.0).unwrap();
    s.enqueue_child(680, TmdbType::Movie, 0, 50.0).unwrap();
    s.enqueue_child(287, TmdbType::Person, 1, 80.0).unwrap();

    assert_eq!(s.queue_size().unwrap(), 3);
    let batch = s.pop_batch(10).unwrap();
    assert_eq!(batch.len(), 3);
    // Depth 0 items first, ordered by popularity DESC
    assert_eq!(batch[0].tmdb_id, 550);
    assert_eq!(batch[1].tmdb_id, 680);
    assert_eq!(batch[2].tmdb_id, 287);
    assert_eq!(s.queue_size().unwrap(), 0);

    s.mark_visited(550, TmdbType::Movie).unwrap();
    assert_eq!(s.visited_count().unwrap(), 1);

    // Re-enqueue should skip already-visited
    s.enqueue_child(550, TmdbType::Movie, 0, 100.0).unwrap();
    assert_eq!(s.queue_size().unwrap(), 0, "visited items should not re-enqueue");
}
