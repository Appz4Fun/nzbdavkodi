mod common;

use warmup_rs::api::TmdbClient;
use warmup_rs::cache::{tv, open_writer};

#[tokio::test]
async fn warm_breaking_bad_writes_expected_rows() {
    let client = TmdbClient::new("a07324c669cac4d96789197134ce272b".into()).unwrap();
    let t = client.get_tv(1396).await.expect("breaking bad fetch");

    let (_h, path) = common::scratch_db();
    let mut writer = open_writer(&path).unwrap();
    tv::write_tv(&mut writer, &t).expect("write");

    let title: String = writer.query_row("SELECT title FROM tvshow WHERE tmdb_id=1396", [], |r| r.get(0)).unwrap();
    assert_eq!(title, "Breaking Bad");

    let seasons: i64 = writer.query_row("SELECT totalseasons FROM tvshow WHERE tmdb_id=1396", [], |r| r.get(0)).unwrap();
    assert_eq!(seasons, 5);

    let season_count: i64 = writer.query_row("SELECT COUNT(*) FROM season WHERE tvshow_id='tv.1396'", [], |r| r.get(0)).unwrap();
    assert!(season_count >= 5, "expected ≥5 season stubs, got {}", season_count);

    let datalevel: i64 = writer.query_row("SELECT datalevel FROM baseitem WHERE id='tv.1396'", [], |r| r.get(0)).unwrap();
    assert_eq!(datalevel, 5);
}
