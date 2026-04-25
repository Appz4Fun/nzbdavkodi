pub mod types_movie;
pub mod types_tv;
pub mod types_person;
pub mod types_collection;

use anyhow::{anyhow, Context, Result};
use reqwest::{Client, StatusCode};
use std::time::Duration;
use tracing::warn;

const TMDB_BASE: &str = "https://api.themoviedb.org/3";
const APPEND_MOVIE: &str = "images,videos,credits,external_ids,release_dates,translations,keywords,similar,recommendations,watch/providers";
const APPEND_TV: &str = "images,videos,credits,external_ids,content_ratings,translations,keywords,similar,recommendations,watch/providers,episode_groups";
const APPEND_PERSON: &str = "images,combined_credits,external_ids,translations";
const APPEND_COLLECTION: &str = "images,translations";

#[derive(Clone)]
pub struct TmdbClient {
    client: Client,
    api_key: String,
}

impl TmdbClient {
    pub fn new(api_key: String) -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(50)
            .build()
            .context("build reqwest client")?;
        Ok(Self { client, api_key })
    }

    /// GET one movie with all sub-resources packed via append_to_response.
    pub async fn get_movie(&self, id: i64) -> Result<types_movie::MovieResponse> {
        self.get_json(&format!("{}/movie/{}", TMDB_BASE, id), APPEND_MOVIE).await
    }

    pub async fn get_tv(&self, id: i64) -> Result<types_tv::TvResponse> {
        self.get_json(&format!("{}/tv/{}", TMDB_BASE, id), APPEND_TV).await
    }

    pub async fn get_tv_season(&self, tv_id: i64, season: i64) -> Result<types_tv::SeasonResponse> {
        let url = format!("{}/tv/{}/season/{}", TMDB_BASE, tv_id, season);
        self.get_json(&url, "images,videos,credits,external_ids,translations").await
    }

    pub async fn get_person(&self, id: i64) -> Result<types_person::PersonResponse> {
        self.get_json(&format!("{}/person/{}", TMDB_BASE, id), APPEND_PERSON).await
    }

    pub async fn get_collection(&self, id: i64) -> Result<types_collection::CollectionResponse> {
        self.get_json(&format!("{}/collection/{}", TMDB_BASE, id), APPEND_COLLECTION).await
    }

    async fn get_json<T: serde::de::DeserializeOwned>(&self, base_url: &str, append: &str) -> Result<T> {
        // include_image_language=en,null matches TMDBHelper default (English + language-neutral images).
        // include_video_language=en,null likewise fetches English + languageless trailers.
        let url = format!(
            "{}?api_key={}&append_to_response={}&include_image_language=en,null&include_video_language=en,null",
            base_url, self.api_key, append
        );
        for attempt in 0..3 {
            let resp = match self.client.get(&url).send().await {
                Ok(r) => r,
                Err(e) if e.is_timeout() || e.is_connect() || e.is_request() => {
                    let backoff = Duration::from_millis(500 * (1 << attempt));
                    warn!("tmdb transport error for {}, retry {} in {:?}: {}", base_url, attempt + 1, backoff, e);
                    tokio::time::sleep(backoff).await;
                    continue;
                }
                Err(e) => return Err(e).context("send request"),
            };
            let status = resp.status();
            if status.is_success() {
                return resp.json::<T>().await.with_context(|| format!("decode JSON from {}", base_url));
            }
            if status == StatusCode::NOT_FOUND {
                return Err(anyhow!("tmdb 404 for {}", base_url));
            }
            if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
                let base_backoff = Duration::from_millis(500 * (1 << attempt));
                let retry_after = resp.headers().get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| s.parse::<u64>().ok())
                    .map(Duration::from_secs);
                let backoff = retry_after.map_or(base_backoff, |ra| ra.max(base_backoff));
                warn!("tmdb {} for {}, retry {} in {:?}", status, base_url, attempt + 1, backoff);
                tokio::time::sleep(backoff).await;
                continue;
            }
            return Err(anyhow!("tmdb {} for {}", status, base_url));
        }
        Err(anyhow!("tmdb retries exhausted for {}", base_url))
    }
}
