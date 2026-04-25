use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct TvResponse { pub id: i64, pub name: Option<String> }

#[derive(Debug, Deserialize)]
pub struct SeasonResponse { pub id: i64, pub season_number: i64 }

#[derive(Debug, Deserialize)]
pub struct EpisodeResponse { pub id: i64, pub episode_number: i64, pub season_number: i64 }
