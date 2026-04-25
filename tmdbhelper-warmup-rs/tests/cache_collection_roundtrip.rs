mod common;

use warmup_rs::api::TmdbClient;
use warmup_rs::cache::{collection, open_writer};

#[tokio::test]
async fn warm_lord_of_the_rings_collection() {
    let key = match option_env!("TMDB_API_KEY") {
        Some(k) => k,
        None => { eprintln!("TMDB_API_KEY not set, skipping"); return; }
    };
    let client = TmdbClient::new(key.into()).unwrap();
    let c = client.get_collection(119).await.expect("LotR collection fetch");

    let (_h, path) = common::scratch_db();
    let mut writer = open_writer(&path).unwrap();
    let tx = writer.transaction().unwrap();
    collection::write_collection(&tx, &c).expect("write");
    tx.commit().unwrap();

    let title: String = writer.query_row("SELECT title FROM collection WHERE tmdb_id=119", [], |r| r.get(0)).unwrap();
    assert!(title.contains("Lord of the Rings"), "got: {}", title);

    let mediatype: String = writer.query_row("SELECT mediatype FROM baseitem WHERE id='set.119'", [], |r| r.get(0)).unwrap();
    assert_eq!(mediatype, "set");

    let parts: i64 = writer.query_row("SELECT COUNT(*) FROM belongs WHERE parent_id='set.119'", [], |r| r.get(0)).unwrap();
    assert!(parts >= 3, "expected ≥3 parts in trilogy, got {}", parts);

    let translation: i64 = writer.query_row("SELECT translation FROM baseitem WHERE id='set.119'", [], |r| r.get(0)).unwrap();
    assert_eq!(translation, 1, "expected translation=1 for collection");
}
