use serde::Deserialize;
use crate::api::types_movie::Images;

#[derive(Debug, Deserialize)]
pub struct CollectionResponse {
    pub id: i64,
    pub name: Option<String>,
    pub overview: Option<String>,
    pub poster_path: Option<String>,
    pub backdrop_path: Option<String>,
    #[serde(default)] pub parts: Vec<CollectionPart>,
    pub images: Option<Images>,
}

#[derive(Debug, Deserialize)]
pub struct CollectionPart {
    pub id: i64,
    pub title: Option<String>,
    pub release_date: Option<String>,
}
