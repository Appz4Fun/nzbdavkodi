use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct MovieResponse {
    pub id: i64,
    pub title: Option<String>,
    pub original_title: Option<String>,
    pub original_language: Option<String>,
    pub overview: Option<String>,
    pub tagline: Option<String>,
    pub release_date: Option<String>,
    pub runtime: Option<i64>,
    pub status: Option<String>,
    pub vote_average: Option<f64>,
    pub vote_count: Option<i64>,
    pub popularity: Option<f64>,
    pub imdb_id: Option<String>,
    pub homepage: Option<String>,
    pub belongs_to_collection: Option<CollectionStub>,
    #[serde(default)]
    pub genres: Vec<Genre>,
    #[serde(default)]
    pub production_companies: Vec<Company>,
    #[serde(default)]
    pub production_countries: Vec<CountryRef>,
    #[serde(default)]
    pub spoken_languages: Vec<SpokenLanguage>,
    pub images: Option<Images>,
    pub videos: Option<VideoList>,
    pub credits: Option<Credits>,
    pub external_ids: Option<ExternalIds>,
    pub release_dates: Option<ReleaseDateList>,
    pub translations: Option<TranslationList>,
    pub keywords: Option<KeywordList>,
    pub similar: Option<RelatedList>,
    pub recommendations: Option<RelatedList>,
    // TMDB returns this sub-resource under the literal key "watch/providers".
    #[serde(rename = "watch/providers")]
    pub watch_providers: Option<WatchProviderRoot>,
}

#[derive(Debug, Deserialize)]
pub struct CollectionStub {
    pub id: i64,
    pub name: String,
    pub poster_path: Option<String>,
    pub backdrop_path: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct Genre {
    pub id: i64,
    pub name: String,
}

#[derive(Debug, Deserialize)]
pub struct Company {
    pub id: i64,
    pub name: String,
    pub logo_path: Option<String>,
    pub origin_country: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct CountryRef {
    pub iso_3166_1: String,
    pub name: String,
}

#[derive(Debug, Deserialize)]
pub struct SpokenLanguage {
    pub iso_639_1: String,
    pub name: String,
    pub english_name: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct Images {
    #[serde(default)]
    pub backdrops: Vec<ImageRef>,
    #[serde(default)]
    pub posters: Vec<ImageRef>,
    #[serde(default)]
    pub logos: Vec<ImageRef>,
    #[serde(default)]
    pub profiles: Vec<ImageRef>,
    #[serde(default)]
    pub stills: Vec<ImageRef>,
}

#[derive(Debug, Deserialize)]
pub struct ImageRef {
    pub file_path: String,
    pub aspect_ratio: Option<f64>,
    pub iso_639_1: Option<String>,
    /// TMDB returns iso_3166_1 on poster/logo images (e.g. "US"); null for backdrops.
    pub iso_3166_1: Option<String>,
    pub width: Option<i64>,
    pub height: Option<i64>,
    pub vote_average: Option<f64>,
    pub vote_count: Option<i64>,
}

#[derive(Debug, Deserialize, Default)]
pub struct VideoList {
    #[serde(default)]
    pub results: Vec<Video>,
}

#[derive(Debug, Deserialize)]
pub struct Video {
    pub id: String,
    pub key: String,
    pub name: String,
    pub site: String,
    #[serde(rename = "type")]
    pub type_: String,
    pub iso_639_1: Option<String>,
    pub iso_3166_1: Option<String>,
    pub published_at: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct Credits {
    #[serde(default)]
    pub cast: Vec<CastMember>,
    #[serde(default)]
    pub crew: Vec<CrewMember>,
}

#[derive(Debug, Deserialize)]
pub struct CastMember {
    pub id: i64,
    pub character: Option<String>,
    pub order: Option<i64>,
    pub name: Option<String>,
    pub profile_path: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct CrewMember {
    pub id: i64,
    pub job: Option<String>,
    pub department: Option<String>,
    pub name: Option<String>,
    pub profile_path: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct ExternalIds {
    pub imdb_id: Option<String>,
    pub tvdb_id: Option<i64>,
    pub freebase_mid: Option<String>,
    pub freebase_id: Option<String>,
    pub tvrage_id: Option<i64>,
    pub wikidata_id: Option<String>,
    pub facebook_id: Option<String>,
    pub instagram_id: Option<String>,
    pub twitter_id: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct ReleaseDateList {
    #[serde(default)]
    pub results: Vec<ReleaseDateCountry>,
}

#[derive(Debug, Deserialize)]
pub struct ReleaseDateCountry {
    pub iso_3166_1: String,
    #[serde(default)]
    pub release_dates: Vec<ReleaseDate>,
}

#[derive(Debug, Deserialize)]
pub struct ReleaseDate {
    pub certification: Option<String>,
    pub iso_639_1: Option<String>,
    pub release_date: Option<String>,
    #[serde(rename = "type")]
    pub type_: Option<i64>,
    pub note: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct TranslationList {
    #[serde(default)]
    pub translations: Vec<Translation>,
}

#[derive(Debug, Deserialize)]
pub struct Translation {
    pub iso_3166_1: Option<String>,
    pub iso_639_1: Option<String>,
    pub name: Option<String>,
    pub english_name: Option<String>,
    pub data: Option<TranslationData>,
}

#[derive(Debug, Deserialize)]
pub struct TranslationData {
    pub homepage: Option<String>,
    pub overview: Option<String>,
    pub runtime: Option<i64>,
    pub tagline: Option<String>,
    // `title` for movies, `name` for TV/person — TMDB never returns both.
    pub title: Option<String>,
    pub name: Option<String>,
    pub biography: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct KeywordList {
    #[serde(default)]
    pub keywords: Vec<Keyword>,
    #[serde(default)]
    pub results: Vec<Keyword>,
}

#[derive(Debug, Deserialize)]
pub struct Keyword {
    pub id: i64,
    pub name: String,
}

#[derive(Debug, Deserialize, Default)]
pub struct RelatedList {
    #[serde(default)]
    pub results: Vec<RelatedItem>,
}

#[derive(Debug, Deserialize)]
pub struct RelatedItem {
    pub id: i64,
    pub title: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
pub struct WatchProviderRoot {
    #[serde(default)]
    pub results: std::collections::HashMap<String, WatchProviderCountry>,
}

#[derive(Debug, Deserialize)]
pub struct WatchProviderCountry {
    pub link: Option<String>,
    #[serde(default)]
    pub flatrate: Vec<WatchProvider>,
    #[serde(default)]
    pub buy: Vec<WatchProvider>,
    #[serde(default)]
    pub rent: Vec<WatchProvider>,
    #[serde(default)]
    pub free: Vec<WatchProvider>,
    #[serde(default)]
    pub ads: Vec<WatchProvider>,
}

#[derive(Debug, Deserialize)]
pub struct WatchProvider {
    pub provider_id: i64,
    pub provider_name: String,
    pub logo_path: Option<String>,
    pub display_priority: Option<i64>,
}
