use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TmdbType {
    Movie,
    Tv,
    Person,
    Collection,
}

impl TmdbType {
    /// The string used in `baseitem.mediatype`. Note: collection→"set", tv→"tvshow".
    pub fn mediatype(&self) -> &'static str {
        match self {
            TmdbType::Movie => "movie",
            TmdbType::Tv => "tvshow",
            TmdbType::Person => "person",
            TmdbType::Collection => "set",
        }
    }

    /// The prefix used in `baseitem.id` strings. tv→"tv", collection→"set".
    pub fn id_prefix(&self) -> &'static str {
        match self {
            TmdbType::Movie => "movie",
            TmdbType::Tv => "tv",
            TmdbType::Person => "person",
            TmdbType::Collection => "set",
        }
    }
}

impl fmt::Display for TmdbType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            TmdbType::Movie => "movie",
            TmdbType::Tv => "tv",
            TmdbType::Person => "person",
            TmdbType::Collection => "collection",
        })
    }
}

/// Build the baseitem.id string. Examples:
/// build_item_id(Movie, 550) → "movie.550"
/// build_item_id(Tv, 76479)  → "tv.76479"
/// build_item_id(Collection, 10) → "set.10"
pub fn build_item_id(t: TmdbType, tmdb_id: i64) -> String {
    format!("{}.{}", t.id_prefix(), tmdb_id)
}

pub fn build_season_id(tv_tmdb_id: i64, season: i64) -> String {
    format!("tv.{}.{}", tv_tmdb_id, season)
}

pub fn build_episode_id(tv_tmdb_id: i64, season: i64, episode: i64) -> String {
    format!("tv.{}.{}.{}", tv_tmdb_id, season, episode)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_format_matches_observed_tmdbhelper_strings() {
        assert_eq!(build_item_id(TmdbType::Movie, 1226863), "movie.1226863");
        assert_eq!(build_item_id(TmdbType::Tv, 76479), "tv.76479");
        assert_eq!(build_item_id(TmdbType::Person, 58321), "person.58321");
        assert_eq!(build_item_id(TmdbType::Collection, 10), "set.10");
        assert_eq!(build_season_id(76479, 5), "tv.76479.5");
        assert_eq!(build_episode_id(76479, 5, 3), "tv.76479.5.3");
    }

    #[test]
    fn mediatype_collection_is_set() {
        assert_eq!(TmdbType::Collection.mediatype(), "set");
    }
}
