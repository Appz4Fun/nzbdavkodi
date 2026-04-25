use crate::api::TmdbClient;
use crate::id::TmdbType;
use anyhow::Result;
use std::collections::HashSet;
use tracing::{info, warn};

pub struct SeedItem {
    pub tmdb_id: i64,
    pub tmdb_type: TmdbType,
    pub popularity: f64,
}

struct Endpoint {
    path: &'static str,
    pages: u32,
    default_type: Option<TmdbType>,
}

const ENDPOINTS: &[Endpoint] = &[
    Endpoint { path: "trending/all/week", pages: 5, default_type: None },
    Endpoint { path: "movie/popular", pages: 5, default_type: Some(TmdbType::Movie) },
    Endpoint { path: "movie/now_playing", pages: 3, default_type: Some(TmdbType::Movie) },
    Endpoint { path: "movie/upcoming", pages: 3, default_type: Some(TmdbType::Movie) },
    Endpoint { path: "tv/popular", pages: 5, default_type: Some(TmdbType::Tv) },
    Endpoint { path: "person/popular", pages: 3, default_type: Some(TmdbType::Person) },
];

fn parse_media_type(s: &str) -> Option<TmdbType> {
    match s {
        "movie" => Some(TmdbType::Movie),
        "tv" => Some(TmdbType::Tv),
        "person" => Some(TmdbType::Person),
        _ => None,
    }
}

pub async fn fetch_seeds(client: &TmdbClient) -> Result<Vec<SeedItem>> {
    let mut seen = HashSet::new();
    let mut items = Vec::new();

    for ep in ENDPOINTS {
        for page in 1..=ep.pages {
            let resp = match client.get_list(ep.path, page).await {
                Ok(r) => r,
                Err(e) => {
                    warn!("seed endpoint {} page {} failed: {:#}", ep.path, page, e);
                    break;
                }
            };
            for li in &resp.results {
                let tmdb_type = ep.default_type.or_else(|| {
                    li.media_type.as_deref().and_then(parse_media_type)
                });
                let Some(tt) = tmdb_type else { continue };
                if seen.insert((li.id, tt)) {
                    items.push(SeedItem {
                        tmdb_id: li.id,
                        tmdb_type: tt,
                        popularity: li.popularity.unwrap_or(0.0),
                    });
                }
            }
            if resp.total_pages.is_some_and(|tp| page >= tp) {
                break;
            }
        }
    }

    info!("seeder fetched {} unique items from TMDB lists", items.len());
    Ok(items)
}
