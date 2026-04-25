/// Schema fidelity gate: compare Rust-warmed rows against a snapshot taken from the live
/// TMDBHelper-warmed ItemDetails.db on coreelec.local.
///
/// Target movie: **Project Hail Mary** (tmdb_id=687163)
///   - Popular well-known film, rich data (363 crew, 29 cast, 159 art, 90 video, 43 translations)
///   - No provider rows in the snapshot (no streaming for upcoming film yet) — clean comparison
///   - Snapshot taken 2026-04-24
///
/// Known acceptable divergences (documented per table):
///   - `baseitem.expiry`: timestamp-based, always differs — excluded from comparison
///   - `baseitem.language`: TMDBHelper stores user locale ("en-US"), we store movie's
///     original_language ("en") — acceptable, documented
///   - `unique_id` extra rows: TMDBHelper writes facebook/instagram/twitter unique_ids
///     from external_ids; we intentionally only write imdb/wikidata/tmdb — acceptable
///   - `ratings` table: TMDBHelper writes 30+ columns from MDB list/Trakt aggregation;
///     our writer only sets tmdb_rating/tmdb_votes/expiry — skip this table entirely
///   - TMDB data drift: a handful of new cast/crew rows may appear in Rust's version
///     (TMDB updates credits between snapshot and live fetch) — threshold: allow ≤10 extra
mod common;

use warmup_rs::api::TmdbClient;
use warmup_rs::cache::{movie, open_writer};
use rusqlite::Connection;
use std::collections::HashSet;

const TARGET_TMDB_ID: i64 = 687163;
const FIXTURE_PATH: &str = "tests/fixtures/tmdbhelper-warmed-movie-687163.sql";

/// Read all rows from a query as a set of formatted strings.
/// Each string is "col1=val1|col2=val2|..." for one row.
/// Uses HashSet so order within the result set doesn't matter.
fn row_set(conn: &Connection, sql: &str) -> HashSet<String> {
    let mut stmt = conn.prepare(sql).unwrap_or_else(|e| panic!("prepare '{}': {}", sql, e));
    let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    let rows: Vec<String> = stmt
        .query_map([], |row| {
            let mut parts = Vec::new();
            for (i, name) in names.iter().enumerate() {
                let v: rusqlite::types::Value = row.get(i)?;
                parts.push(format!("{}={:?}", name, v));
            }
            Ok(parts.join("|"))
        })
        .unwrap()
        .filter_map(|r| r.ok())
        .collect();
    rows.into_iter().collect()
}

#[tokio::test]
async fn rust_writes_match_tmdbhelper_for_movie() {
    // --- Step 1: Warm via Rust path into a scratch DB ---------------------------------
    let (_conn, scratch_path) = common::scratch_db();
    let mut writer = open_writer(&scratch_path).unwrap();
    let client = TmdbClient::new("a07324c669cac4d96789197134ce272b".into()).unwrap();
    let m = client.get_movie(TARGET_TMDB_ID).await.expect("TMDB fetch");
    movie::write_movie(&mut writer, &m).expect("write_movie");

    // --- Step 2: Load TMDBHelper snapshot into a separate scratch DB ------------------
    // The fixture SQL was dumped from the live ItemDetails.db on coreelec.local on 2026-04-24.
    // It contains only INSERTs for movie.687163 rows.
    // We apply it to a fresh schema-cloned DB so it can be queried identically.
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let fixture_path = format!("{}/{}", manifest_dir, FIXTURE_PATH);
    let snapshot_sql = std::fs::read_to_string(&fixture_path)
        .unwrap_or_else(|e| panic!("read fixture {}: {}", fixture_path, e));

    let (_conn2, snap_path) = common::scratch_db();
    let snap = Connection::open(&snap_path).unwrap();
    snap.execute_batch("PRAGMA foreign_keys=OFF;")
        .expect("disable fk for fixture load");
    snap.execute_batch(&snapshot_sql)
        .unwrap_or_else(|e| panic!("apply snapshot SQL: {}", e));
    snap.execute_batch("PRAGMA foreign_keys=ON;")
        .expect("re-enable fk");

    // --- Step 3: Compare table by table -----------------------------------------------
    let target_id = format!("movie.{}", TARGET_TMDB_ID);
    let mut diffs: Vec<String> = Vec::new();

    // --- movie table ------------------------------------------------------------------
    // Excluded columns:
    //   - rating, popularity: REAL stored as float; Python repr may differ from Rust's,
    //     and TMDB updates these values frequently (acceptable drift)
    //   - votes: TMDB vote_count changes continuously — always differs between snapshot and live
    {
        let sql = format!(
            "SELECT id, tmdb_id, year, title, originaltitle, duration, tagline, premiered, status \
             FROM movie WHERE tmdb_id={} ORDER BY id",
            TARGET_TMDB_ID
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("movie (excl. rating/popularity/votes)", &rust_rows, &tmdb_rows, 0, &mut diffs);
    }

    // --- baseitem table ---------------------------------------------------------------
    // Excluded columns:
    //   - expiry: timestamp-based, always differs
    //   - language: TMDBHelper stores user locale ("en-US"); we store movie original_language ("en")
    {
        let sql = format!(
            "SELECT id, mediatype, datalevel, fanart_tv, translation \
             FROM baseitem WHERE id='{}' ORDER BY id",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("baseitem (excl. expiry, language)", &rust_rows, &tmdb_rows, 0, &mut diffs);
    }

    // --- genre table ------------------------------------------------------------------
    {
        let sql = format!(
            "SELECT name, tmdb_id, parent_id FROM genre WHERE parent_id='{}' ORDER BY tmdb_id",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("genre", &rust_rows, &tmdb_rows, 0, &mut diffs);
    }

    // --- language table ---------------------------------------------------------------
    {
        let sql = format!(
            "SELECT iso_language, parent_id FROM language WHERE parent_id='{}' ORDER BY iso_language",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("language", &rust_rows, &tmdb_rows, 0, &mut diffs);
    }

    // --- country table ----------------------------------------------------------------
    {
        let sql = format!(
            "SELECT iso_country, parent_id FROM country WHERE parent_id='{}' ORDER BY iso_country",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("country", &rust_rows, &tmdb_rows, 0, &mut diffs);
    }

    // --- art table --------------------------------------------------------------------
    // After fixes C1 (aspect_ratio bucket), C2 (quality megapixel rank), C3 (rating*100 int),
    // C6 (iso_3166_1 from ImageRef), structural art rows should match well.
    // Excluded columns:
    //   - rating, votes: TMDB image vote counts change continuously — always differs between
    //     snapshot (e.g., rating=578) and live fetch (rating=579). Comparing structural
    //     identity (which images exist with what dimensions/language) is what matters for Kodi UI.
    // Allow a small tolerance for TMDB data drift (new images added since snapshot).
    {
        let sql = format!(
            "SELECT aspect_ratio, quality, iso_language, iso_country, icon, type, extension \
             FROM art WHERE parent_id='{}' ORDER BY icon",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        // Allow up to 5 extra rows in Rust (TMDB may have added images since snapshot);
        // allow up to 5 extra in TMDBHelper (images removed from TMDB since snapshot).
        check_diff("art (excl. rating/votes)", &rust_rows, &tmdb_rows, 5, &mut diffs);
    }

    // --- castmember table -------------------------------------------------------------
    // TMDB data drift: new cast members may be added or existing ones updated.
    // Allow up to 5 extra rows in Rust.
    {
        let sql = format!(
            "SELECT tmdb_id, role, ordering, appearances, guest \
             FROM castmember WHERE parent_id='{}' ORDER BY tmdb_id, role",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("castmember", &rust_rows, &tmdb_rows, 5, &mut diffs);
    }

    // --- crewmember table -------------------------------------------------------------
    // TMDB data drift: crew can change significantly (363 in snapshot).
    // Allow up to 10 extra rows in Rust.
    {
        let sql = format!(
            "SELECT tmdb_id, role, department, appearances \
             FROM crewmember WHERE parent_id='{}' ORDER BY tmdb_id, role, department",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("crewmember", &rust_rows, &tmdb_rows, 10, &mut diffs);
    }

    // --- unique_id table --------------------------------------------------------------
    // Known divergences:
    //   - Rust writes key="tmdb" with the string tmdb_id; TMDBHelper does NOT write this
    //     (TMDBHelper derives the tmdb_id from the item id "movie.687163" itself).
    //     Acceptable: our tmdb key is harmless metadata redundancy.
    //   - TMDBHelper writes facebook/instagram/twitter keys from external_ids.
    //     We intentionally skip these — they're not used by any TMDBHelper playback UI.
    // So: allow 1 extra in Rust (our "tmdb" key), allow ≤3 extra in TMDBHelper (social IDs).
    {
        let sql = format!(
            "SELECT key, value, parent_id FROM unique_id WHERE parent_id='{}' ORDER BY key",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        // Pin the known asymmetric extras so a future bug (e.g., wrong key written) can't
        // hide inside the threshold. The extras MUST be exactly: our 'tmdb' key, their socials.
        let only_in_rust: Vec<&String> = rust_rows.difference(&tmdb_rows).collect();
        let only_in_tmdb: Vec<&String> = tmdb_rows.difference(&rust_rows).collect();
        assert!(
            only_in_rust.iter().all(|r| r.contains("key=Text(\"tmdb\")")),
            "unique_id only_in_rust must be the 'tmdb' key only, got: {:?}",
            only_in_rust
        );
        assert!(
            only_in_tmdb.iter().all(|r| {
                r.contains("key=Text(\"facebook\")")
                    || r.contains("key=Text(\"instagram\")")
                    || r.contains("key=Text(\"twitter\")")
            }),
            "unique_id only_in_tmdb must be social keys only, got: {:?}",
            only_in_tmdb
        );
        check_diff_asymmetric("unique_id", &rust_rows, &tmdb_rows, 1, 3, &mut diffs);
    }

    // --- video table ------------------------------------------------------------------
    // Videos can change frequently (new trailers/featurettes added).
    // Allow up to 15 extra rows in Rust (new videos since snapshot).
    // Rows only in TMDBHelper (deleted videos) — allow up to 5.
    {
        let sql = format!(
            "SELECT name, iso_country, iso_language, release_date, key, path, content \
             FROM video WHERE parent_id='{}' ORDER BY key",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff_asymmetric("video", &rust_rows, &tmdb_rows, 15, 5, &mut diffs);
    }

    // --- certification table ----------------------------------------------------------
    // Known divergences:
    //   - TMDBHelper may write extra rows from a second request pass (with language=en-US
    //     region filter), producing both iso_language=Null and iso_language='en' variants
    //     for the same country+date+type. The UNIQUE constraint deduplicates within the
    //     same pass, but two passes can yield +1-3 extra rows.
    //   - New countries may be added to TMDB between snapshot and live fetch.
    // Allow up to 5 extra in either direction.
    {
        let sql = format!(
            "SELECT name, iso_country, iso_language, release_date, release_type \
             FROM certification WHERE parent_id='{}' ORDER BY iso_country, release_type, release_date",
            target_id
        );
        let rust_rows = row_set(&writer, &sql);
        let tmdb_rows = row_set(&snap, &sql);
        check_diff("certification", &rust_rows, &tmdb_rows, 5, &mut diffs);
    }

    // --- Final report -----------------------------------------------------------------
    if !diffs.is_empty() {
        panic!(
            "\n\n=== SCHEMA FIDELITY DIFFS (movie.{}) ===\n{}\n\n\
             Legend:\n  only_in_rust = rows we wrote that TMDBHelper didn't (new TMDB data or a bug)\n\
             only_in_tmdb = rows TMDBHelper wrote that we didn't (missing feature or data drift)\n",
            TARGET_TMDB_ID,
            diffs.join("\n\n")
        );
    }

    println!("PASS: all compared tables match within acceptable thresholds");
}

/// Compare two row sets. If the diff exceeds `max_extra_either_side` in either direction, record a diff.
fn check_diff(
    table: &str,
    rust: &HashSet<String>,
    tmdb: &HashSet<String>,
    max_extra_either_side: usize,
    diffs: &mut Vec<String>,
) {
    check_diff_asymmetric(table, rust, tmdb, max_extra_either_side, max_extra_either_side, diffs);
}

/// Compare two row sets with separate thresholds for each direction.
fn check_diff_asymmetric(
    table: &str,
    rust: &HashSet<String>,
    tmdb: &HashSet<String>,
    max_extra_in_rust: usize,
    max_extra_in_tmdb: usize,
    diffs: &mut Vec<String>,
) {
    let mut only_in_rust: Vec<&String> = rust.difference(tmdb).collect();
    let mut only_in_tmdb: Vec<&String> = tmdb.difference(rust).collect();
    only_in_rust.sort();
    only_in_tmdb.sort();

    let rust_exceeds = only_in_rust.len() > max_extra_in_rust;
    let tmdb_exceeds = only_in_tmdb.len() > max_extra_in_tmdb;

    if rust_exceeds || tmdb_exceeds {
        let show_rust: Vec<_> = only_in_rust.iter().take(20).collect();
        let show_tmdb: Vec<_> = only_in_tmdb.iter().take(20).collect();
        diffs.push(format!(
            "TABLE {} (rust={} rows, tmdb={} rows):\n\
             only_in_rust ({}, max allowed {}): {:#?}{}\n\
             only_in_tmdb ({}, max allowed {}): {:#?}{}",
            table,
            rust.len(),
            tmdb.len(),
            only_in_rust.len(),
            max_extra_in_rust,
            show_rust,
            if only_in_rust.len() > 20 { format!(" ... (+{})", only_in_rust.len() - 20) } else { String::new() },
            only_in_tmdb.len(),
            max_extra_in_tmdb,
            show_tmdb,
            if only_in_tmdb.len() > 20 { format!(" ... (+{})", only_in_tmdb.len() - 20) } else { String::new() },
        ));
    } else {
        println!(
            "  OK {}: rust={} tmdb={} | only_in_rust={} only_in_tmdb={}",
            table, rust.len(), tmdb.len(), only_in_rust.len(), only_in_tmdb.len()
        );
    }
}
