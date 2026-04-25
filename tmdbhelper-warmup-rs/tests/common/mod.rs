use rusqlite::Connection;
use std::path::PathBuf;
use warmup_rs::cache::open_writer;

/// Path to a fresh scratch ItemDetails.db, schema-cloned from a known-good snapshot.
/// The snapshot is checked in at tests/fixtures/ItemDetails-empty.db.
/// To regenerate: scp the live ItemDetails.db, run `sqlite3 ItemDetails.db ".schema" | sqlite3 ItemDetails-empty.db`.
///
/// Connection is opened via `cache::open_writer()` so tests run with the same
/// PRAGMAs as production — including `foreign_keys=ON`. Tests that fail FK
/// constraints here will also fail in production; tests that pass here will
/// not be ambushed by unexpected FK errors against the live Kodi DB.
pub fn scratch_db() -> (Connection, PathBuf) {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("scratch.db");
    let fixture = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/ItemDetails-empty.db");
    std::fs::copy(&fixture, &path).expect("copy fixture");
    let conn = open_writer(&path).expect("open scratch via cache::open_writer");
    // Leak the dir so tests can inspect; tempdir auto-cleans on drop.
    std::mem::forget(dir);
    (conn, path)
}
