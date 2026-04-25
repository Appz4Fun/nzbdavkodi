use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_tv::{TvResponse, SeasonStub};
use crate::cache::{art, credits, dimensions, default_expiry};
use crate::id;

pub fn write_tv(conn: &Connection, t: &TvResponse) -> Result<()> {
    let item_id = id::build_item_id(id::TmdbType::Tv, t.id);
    let expiry = default_expiry();
    let year: Option<i64> = t.first_air_date.as_deref().and_then(|d| d.get(0..4).and_then(|y| y.parse().ok()));
    let avg_runtime = t.episode_run_time.as_ref().and_then(|v| v.first().copied()).map(|m| m * 60);
    let next_id = t.next_episode_to_air.as_ref().map(|e| id::build_episode_id(t.id, e.season_number, e.episode_number));
    let last_id = t.last_episode_to_air.as_ref().map(|e| id::build_episode_id(t.id, e.season_number, e.episode_number));

    // datalevel=3 (partial): TV writer doesn't yet cache content_ratings, episode_groups,
    // providers, or translations, so we mark partial so TMDBHelper re-fetches the rest.
    const TV_DATALEVEL: i64 = 3;
    conn.execute(
        "INSERT OR REPLACE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
         VALUES (?1, 'tvshow', ?2, ?3, 0, 1, ?4)",
        params![&item_id, expiry, TV_DATALEVEL, t.original_language.as_deref()],
    )?;

    conn.execute(
        "INSERT OR REPLACE INTO tvshow
         (id, tmdb_id, year, plot, title, originaltitle, duration, tagline, premiered, status,
          rating, votes, popularity, next_episode_to_air_id, last_episode_to_air_id, totalseasons, totalepisodes)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17)",
        params![
            &item_id, t.id, year, t.overview.as_deref(), t.name.as_deref(), t.original_name.as_deref(),
            avg_runtime, t.tagline.as_deref(), t.first_air_date.as_deref(), t.status.as_deref(),
            t.vote_average, t.vote_count, t.popularity,
            next_id.as_deref(), last_id.as_deref(),
            t.number_of_seasons, t.number_of_episodes,
        ],
    )?;

    let tmdb_rating_int = t.vote_average.map(|r| (r * 10.0).round() as i64);
    conn.execute(
        "INSERT INTO ratings (id, tmdb_rating, tmdb_votes, expiry) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(id) DO UPDATE SET tmdb_rating=excluded.tmdb_rating, tmdb_votes=excluded.tmdb_votes, expiry=excluded.expiry",
        params![&item_id, tmdb_rating_int, t.vote_count, expiry],
    )?;

    for g in &t.genres {
        conn.execute("INSERT OR IGNORE INTO genre (name, tmdb_id, parent_id) VALUES (?1, ?2, ?3)", params![&g.name, g.id, &item_id])?;
    }
    for lang in &t.spoken_languages {
        dimensions::upsert_language(conn, &lang.iso_639_1, &lang.name, lang.english_name.as_deref().unwrap_or(&lang.name))?;
        conn.execute("INSERT OR IGNORE INTO language (iso_language, parent_id) VALUES (?1, ?2)", params![&lang.iso_639_1, &item_id])?;
    }
    for c in &t.production_countries {
        dimensions::upsert_country(conn, &c.iso_3166_1, &c.name)?;
        conn.execute("INSERT OR IGNORE INTO country (iso_country, parent_id) VALUES (?1, ?2)", params![&c.iso_3166_1, &item_id])?;
    }
    for co in &t.production_companies {
        dimensions::upsert_company(conn, co.id, &co.name, co.logo_path.as_deref(), co.origin_country.as_deref())?;
        conn.execute("INSERT OR IGNORE INTO studio (tmdb_id, parent_id) VALUES (?1, ?2)", params![co.id, &item_id])?;
    }
    for n in &t.networks {
        dimensions::upsert_broadcaster(conn, n.id, &n.name, n.logo_path.as_deref(), n.origin_country.as_deref())?;
        conn.execute("INSERT OR IGNORE INTO network (tmdb_id, parent_id) VALUES (?1, ?2)", params![n.id, &item_id])?;
    }

    if let Some(images) = &t.images {
        for img in &images.posters { art::write_image(conn, &item_id, "posters", img)?; }
        for img in &images.backdrops { art::write_image(conn, &item_id, "backdrops", img)?; }
        for img in &images.logos { art::write_image(conn, &item_id, "logos", img)?; }
    }

    if let Some(cr) = &t.credits {
        for c in &cr.cast { credits::write_castmember(conn, &item_id, c)?; }
        for c in &cr.crew { credits::write_crewmember(conn, &item_id, c)?; }
    }

    if let Some(ext) = &t.external_ids {
        if let Some(imdb) = &ext.imdb_id {
            conn.execute("INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('imdb', ?1, ?2)", params![imdb, &item_id])?;
        }
        if let Some(tvdb) = ext.tvdb_id {
            conn.execute("INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('tvdb', ?1, ?2)", params![tvdb.to_string(), &item_id])?;
        }
    }
    conn.execute("INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('tmdb', ?1, ?2)", params![t.id.to_string(), &item_id])?;

    // season stubs (full season details fetched separately by worker)
    for s in &t.seasons {
        write_season_stub(conn, t.id, s, expiry)?;
    }

    Ok(())
}

fn write_season_stub(conn: &Connection, tv_id: i64, s: &SeasonStub, expiry: i64) -> Result<()> {
    let season_id = id::build_season_id(tv_id, s.season_number);
    let tv_item_id = id::build_item_id(id::TmdbType::Tv, tv_id);
    let year: Option<i64> = s.air_date.as_deref().and_then(|d| d.get(0..4).and_then(|y| y.parse().ok()));

    conn.execute(
        "INSERT OR IGNORE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
         VALUES (?1, 'season', ?2, 1, 0, 0, NULL)",
        params![&season_id, expiry],
    )?;
    conn.execute(
        "INSERT OR IGNORE INTO season (id, season, year, plot, title, premiered, tvshow_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![&season_id, s.season_number, year, s.overview.as_deref(), s.name.as_deref(), s.air_date.as_deref(), &tv_item_id],
    )?;
    if let Some(poster) = &s.poster_path {
        let img = crate::api::types_movie::ImageRef {
            file_path: poster.clone(),
            aspect_ratio: None,
            iso_639_1: None,
            iso_3166_1: None,
            width: None,
            height: None,
            vote_average: None,
            vote_count: None,
        };
        art::write_image(conn, &season_id, "posters", &img)?;
    }
    Ok(())
}
