mod common;

use warmup_rs::api::TmdbClient;
use warmup_rs::cache::{collection, open_writer};

#[tokio::test]
async fn warm_lord_of_the_rings_collection() {
    let client = TmdbClient::new("a07324c669cac4d96789197134ce272b".into()).unwrap();
    let c = client.get_collection(119).await.expect("LotR collection fetch");

    let (_h, path) = common::scratch_db();
    let mut writer = open_writer(&path).unwrap();
    collection::write_collection(&mut writer, &c).expect("write");

    let title: String = writer.query_row("SELECT title FROM collection WHERE tmdb_id=119", [], |r| r.get(0)).unwrap();
    assert!(title.contains("Lord of the Rings"), "got: {}", title);

    let mediatype: String = writer.query_row("SELECT mediatype FROM baseitem WHERE id='set.119'", [], |r| r.get(0)).unwrap();
    assert_eq!(mediatype, "set");

    let parts: i64 = writer.query_row("SELECT COUNT(*) FROM belongs WHERE parent_id='set.119'", [], |r| r.get(0)).unwrap();
    assert!(parts >= 3, "expected ≥3 parts in trilogy, got {}", parts);
}
