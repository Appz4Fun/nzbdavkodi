mod common;

use warmup_rs::api::TmdbClient;
use warmup_rs::cache::{movie, open_writer};

#[tokio::test]
async fn warm_fight_club_writes_expected_rows() {
    let key = match option_env!("TMDB_API_KEY") {
        Some(k) => k,
        None => { eprintln!("TMDB_API_KEY not set, skipping"); return; }
    };
    let client = TmdbClient::new(key.into()).unwrap();
    let m = client.get_movie(550).await.expect("fetch");

    let (_conn, path) = common::scratch_db();
    let mut writer = open_writer(&path).unwrap();
    let tx = writer.transaction().unwrap();
    movie::write_movie(&tx, &m).expect("write");
    tx.commit().unwrap();

    let title: String = writer.query_row("SELECT title FROM movie WHERE tmdb_id=550", [], |r| r.get(0)).unwrap();
    assert_eq!(title, "Fight Club");

    let datalevel: i64 = writer.query_row("SELECT datalevel FROM baseitem WHERE id='movie.550'", [], |r| r.get(0)).unwrap();
    assert_eq!(datalevel, 5);

    let mediatype: String = writer.query_row("SELECT mediatype FROM baseitem WHERE id='movie.550'", [], |r| r.get(0)).unwrap();
    assert_eq!(mediatype, "movie");

    let cast_count: i64 = writer.query_row("SELECT COUNT(*) FROM castmember WHERE parent_id='movie.550'", [], |r| r.get(0)).unwrap();
    assert!(cast_count >= 5, "expected ≥5 cast, got {}", cast_count);

    let crew_director: i64 = writer.query_row(
        "SELECT COUNT(*) FROM crewmember WHERE parent_id='movie.550' AND role='Director'",
        [], |r| r.get(0)).unwrap();
    assert!(crew_director >= 1, "expected at least one Director");

    let art_count: i64 = writer.query_row("SELECT COUNT(*) FROM art WHERE parent_id='movie.550'", [], |r| r.get(0)).unwrap();
    assert!(art_count >= 5, "expected ≥5 art rows, got {}", art_count);

    let genre_count: i64 = writer.query_row("SELECT COUNT(*) FROM genre WHERE parent_id='movie.550'", [], |r| r.get(0)).unwrap();
    assert!(genre_count >= 1, "expected ≥1 genre");

    let imdb: String = writer.query_row(
        "SELECT value FROM unique_id WHERE parent_id='movie.550' AND key='imdb'",
        [], |r| r.get(0)).unwrap();
    assert_eq!(imdb, "tt0137523");
}
