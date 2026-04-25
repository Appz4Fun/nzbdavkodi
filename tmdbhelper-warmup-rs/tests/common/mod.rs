use rusqlite::Connection;
use std::path::PathBuf;

/// Path to a fresh scratch ItemDetails.db, schema-cloned from a known-good snapshot.
/// The snapshot is checked in at tests/fixtures/ItemDetails-empty.db.
/// To regenerate: scp the live ItemDetails.db, run `sqlite3 ItemDetails.db ".schema" | sqlite3 ItemDetails-empty.db`.
pub fn scratch_db() -> (Connection, PathBuf) {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("scratch.db");
    let fixture = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/ItemDetails-empty.db");
    std::fs::copy(&fixture, &path).expect("copy fixture");
    let conn = Connection::open(&path).expect("open scratch");
    // Leak the dir so tests can inspect; tempdir auto-cleans on drop.
    std::mem::forget(dir);
    (conn, path)
}
