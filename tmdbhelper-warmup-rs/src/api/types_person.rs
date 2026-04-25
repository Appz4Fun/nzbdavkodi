use serde::Deserialize;
use crate::api::types_movie::{Images, ExternalIds, TranslationList};

#[derive(Debug, Deserialize)]
pub struct PersonResponse {
    pub id: i64,
    pub name: Option<String>,
    pub also_known_as: Option<Vec<String>>,
    pub biography: Option<String>,
    pub birthday: Option<String>,
    pub deathday: Option<String>,
    pub gender: Option<i64>,
    pub homepage: Option<String>,
    pub imdb_id: Option<String>,
    pub known_for_department: Option<String>,
    pub place_of_birth: Option<String>,
    pub popularity: Option<f64>,
    pub profile_path: Option<String>,
    pub images: Option<Images>,
    pub external_ids: Option<ExternalIds>,
    pub translations: Option<TranslationList>,
    pub combined_credits: Option<CombinedCredits>,
}

#[derive(Debug, Deserialize, Default)]
pub struct CombinedCredits {
    #[serde(default)]
    pub cast: Vec<CombinedCreditEntry>,
    #[serde(default)]
    pub crew: Vec<CombinedCreditEntry>,
}

#[derive(Debug, Deserialize)]
pub struct CombinedCreditEntry {
    pub id: i64,
    pub media_type: Option<String>,
}
