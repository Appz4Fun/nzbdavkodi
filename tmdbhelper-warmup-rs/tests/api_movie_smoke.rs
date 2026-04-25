use warmup_rs::api::TmdbClient;

#[tokio::test]
async fn fight_club_fetch_full() {
    let client = TmdbClient::new("a07324c669cac4d96789197134ce272b".into()).unwrap();
    let m = client.get_movie(550).await.expect("fight club fetch");
    assert_eq!(m.id, 550);
    assert_eq!(m.title.as_deref(), Some("Fight Club"));
    assert!(!m.genres.is_empty(), "expected genres");
    let credits = m.credits.expect("credits present");
    assert!(credits.cast.len() >= 5, "expected at least 5 cast");
    assert!(credits.crew.iter().any(|c| c.job.as_deref() == Some("Director")), "expected Director in crew");
    let images = m.images.expect("images present");
    assert!(!images.backdrops.is_empty(), "expected backdrops");
    assert_eq!(m.imdb_id.as_deref(), Some("tt0137523"), "imdb_id field deserialized");
    let ext = m.external_ids.expect("external_ids present");
    assert_eq!(ext.imdb_id.as_deref(), Some("tt0137523"), "external_ids.imdb_id deserialized");
    assert!(m.belongs_to_collection.is_none(), "Fight Club is not in a collection");
}
