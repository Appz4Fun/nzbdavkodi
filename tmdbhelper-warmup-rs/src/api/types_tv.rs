use serde::Deserialize;
use crate::api::types_movie::{Images, VideoList, Credits, ExternalIds, TranslationList, KeywordList, RelatedList, WatchProviderRoot, Genre, Company, CountryRef, SpokenLanguage};

#[derive(Debug, Deserialize)]
pub struct TvResponse {
    pub id: i64,
    pub name: Option<String>,
    pub original_name: Option<String>,
    pub original_language: Option<String>,
    pub overview: Option<String>,
    pub tagline: Option<String>,
    pub first_air_date: Option<String>,
    pub last_air_date: Option<String>,
    pub status: Option<String>,
    pub vote_average: Option<f64>,
    pub vote_count: Option<i64>,
    pub popularity: Option<f64>,
    pub number_of_seasons: Option<i64>,
    pub number_of_episodes: Option<i64>,
    pub homepage: Option<String>,
    pub episode_run_time: Option<Vec<i64>>,
    pub last_episode_to_air: Option<EpisodeStub>,
    pub next_episode_to_air: Option<EpisodeStub>,
    #[serde(default)] pub seasons: Vec<SeasonStub>,
    #[serde(default)] pub genres: Vec<Genre>,
    #[serde(default)] pub production_companies: Vec<Company>,
    #[serde(default)] pub production_countries: Vec<CountryRef>,
    #[serde(default)] pub spoken_languages: Vec<SpokenLanguage>,
    #[serde(default)] pub networks: Vec<Network>,
    pub images: Option<Images>,
    pub videos: Option<VideoList>,
    pub credits: Option<Credits>,
    pub external_ids: Option<ExternalIds>,
    pub translations: Option<TranslationList>,
    pub keywords: Option<KeywordList>,
    pub similar: Option<RelatedList>,
    pub recommendations: Option<RelatedList>,
    #[serde(rename = "watch/providers")]
    pub watch_providers: Option<WatchProviderRoot>,
}

#[derive(Debug, Deserialize)]
pub struct Network {
    pub id: i64,
    pub name: String,
    pub logo_path: Option<String>,
    pub origin_country: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct SeasonStub {
    pub id: i64,
    pub season_number: i64,
    pub name: Option<String>,
    pub overview: Option<String>,
    pub air_date: Option<String>,
    pub episode_count: Option<i64>,
    pub poster_path: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct EpisodeStub {
    pub id: i64,
    pub episode_number: i64,
    pub season_number: i64,
    pub name: Option<String>,
    pub overview: Option<String>,
    pub air_date: Option<String>,
    pub runtime: Option<i64>,
    pub still_path: Option<String>,
    pub vote_average: Option<f64>,
    pub vote_count: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct SeasonResponse {
    pub id: i64,
    pub season_number: i64,
    pub name: Option<String>,
    pub overview: Option<String>,
    pub air_date: Option<String>,
    pub poster_path: Option<String>,
    #[serde(default)] pub episodes: Vec<EpisodeStub>,
    pub images: Option<Images>,
    pub credits: Option<Credits>,
}

pub type EpisodeResponse = EpisodeStub;
