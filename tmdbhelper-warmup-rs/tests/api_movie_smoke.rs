use warmup_rs::api::TmdbClient;

#[tokio::test]
async fn fight_club_fetch() {
    let client = TmdbClient::new("a07324c669cac4d96789197134ce272b".into()).unwrap();
    let m = client.get_movie(550).await.expect("fight club fetch");
    assert_eq!(m.id, 550);
    assert_eq!(m.title.as_deref(), Some("Fight Club"));
}
