mod common;

use warmup_rs::api::TmdbClient;
use warmup_rs::cache::{person, open_writer};

#[tokio::test]
async fn warm_brad_pitt() {
    let client = TmdbClient::new("a07324c669cac4d96789197134ce272b".into()).unwrap();
    let p = client.get_person(287).await.expect("brad pitt fetch");

    let (_h, path) = common::scratch_db();
    let mut writer = open_writer(&path).unwrap();
    person::write_person(&mut writer, &p).expect("write");

    let name: String = writer.query_row("SELECT name FROM person WHERE tmdb_id=287", [], |r| r.get(0)).unwrap();
    assert_eq!(name, "Brad Pitt");

    let dl: i64 = writer.query_row("SELECT datalevel FROM baseitem WHERE id='person.287'", [], |r| r.get(0)).unwrap();
    assert_eq!(dl, 5);

    let bio: String = writer.query_row("SELECT biography FROM person WHERE tmdb_id=287", [], |r| r.get(0)).unwrap();
    assert!(bio.len() > 50, "expected non-trivial biography, got {} chars", bio.len());
}
