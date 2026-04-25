use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct MovieResponse {
    pub id: i64,
    pub title: Option<String>,
    pub original_title: Option<String>,
    pub overview: Option<String>,
}
