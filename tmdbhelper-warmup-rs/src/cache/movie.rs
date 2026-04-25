use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_movie::MovieResponse;
use crate::cache::{art, credits, dimensions, default_expiry, DATALEVEL_FULL};
use crate::id;

pub fn write_movie(conn: &Connection, m: &MovieResponse) -> Result<()> {
    let item_id = id::build_item_id(id::TmdbType::Movie, m.id);
    let expiry = default_expiry();
    let year: Option<i64> = m.release_date.as_deref().and_then(|d| d.get(0..4).and_then(|y| y.parse().ok()));
    let duration_seconds: Option<i64> = m.runtime.map(|r| r * 60);

    // 1. baseitem (must come first — children FK to it)
    // translation=1: we fetch translations (step 12), matching TMDBHelper's flag.
    // fanart_tv=0: we do not fetch fanart.tv images; TMDBHelper only sets this when fanart.tv is enabled.
    // language: TMDBHelper stores the user's locale (e.g. "en-US"); we store the movie's original_language.
    // NOTE: TMDBHelper actually stores the user locale ("en-US") not the movie's original_language.
    // Using original_language here is a known acceptable divergence — the column is used for UI locale, not content.
    conn.execute(
        "INSERT OR REPLACE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
         VALUES (?1, 'movie', ?2, ?3, 0, 1, ?4)",
        params![&item_id, expiry, DATALEVEL_FULL, m.original_language.as_deref()],
    )?;

    // 2. movie row
    conn.execute(
        "INSERT OR REPLACE INTO movie
         (id, tmdb_id, year, plot, title, originaltitle, duration, tagline, premiered, status, rating, votes, popularity)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
        params![
            &item_id, m.id, year,
            m.overview.as_deref(), m.title.as_deref(), m.original_title.as_deref(),
            duration_seconds, m.tagline.as_deref(), m.release_date.as_deref(),
            m.status.as_deref(), m.vote_average, m.vote_count, m.popularity,
        ],
    )?;

    // 3. ratings
    let tmdb_rating_int = m.vote_average.map(|r| (r * 10.0).round() as i64);
    conn.execute(
        "INSERT OR REPLACE INTO ratings (id, tmdb_rating, tmdb_votes, expiry) VALUES (?1, ?2, ?3, ?4)",
        params![&item_id, tmdb_rating_int, m.vote_count, expiry],
    )?;

    // 4. genres
    for g in &m.genres {
        conn.execute(
            "INSERT OR IGNORE INTO genre (name, tmdb_id, parent_id) VALUES (?1, ?2, ?3)",
            params![&g.name, g.id, &item_id],
        )?;
    }

    // 5. spoken languages
    for lang in &m.spoken_languages {
        dimensions::upsert_language(conn, &lang.iso_639_1, &lang.name, lang.english_name.as_deref().unwrap_or(&lang.name))?;
        conn.execute(
            "INSERT OR IGNORE INTO language (iso_language, parent_id) VALUES (?1, ?2)",
            params![&lang.iso_639_1, &item_id],
        )?;
    }

    // 6. production countries
    for c in &m.production_countries {
        dimensions::upsert_country(conn, &c.iso_3166_1, &c.name)?;
        conn.execute(
            "INSERT OR IGNORE INTO country (iso_country, parent_id) VALUES (?1, ?2)",
            params![&c.iso_3166_1, &item_id],
        )?;
    }

    // 7. studios (production companies)
    for co in &m.production_companies {
        dimensions::upsert_company(conn, co.id, &co.name, co.logo_path.as_deref(), co.origin_country.as_deref())?;
        conn.execute(
            "INSERT OR IGNORE INTO studio (tmdb_id, parent_id) VALUES (?1, ?2)",
            params![co.id, &item_id],
        )?;
    }

    // 8. art (posters, backdrops, logos)
    if let Some(images) = &m.images {
        for img in &images.posters { art::write_image(conn, &item_id, "posters", img)?; }
        for img in &images.backdrops { art::write_image(conn, &item_id, "backdrops", img)?; }
        for img in &images.logos { art::write_image(conn, &item_id, "logos", img)?; }
    }

    // 9. credits
    if let Some(cr) = &m.credits {
        for c in &cr.cast { credits::write_castmember(conn, &item_id, c)?; }
        for c in &cr.crew { credits::write_crewmember(conn, &item_id, c)?; }
    }

    // 10. external_ids → unique_id
    if let Some(ext) = &m.external_ids {
        if let Some(imdb) = &ext.imdb_id {
            conn.execute("INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('imdb', ?1, ?2)", params![imdb, &item_id])?;
        }
        if let Some(wd) = &ext.wikidata_id {
            conn.execute("INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('wikidata', ?1, ?2)", params![wd, &item_id])?;
        }
    }
    conn.execute(
        "INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('tmdb', ?1, ?2)",
        params![m.id.to_string(), &item_id],
    )?;

    // 11. videos (trailers etc) — TMDBHelper filters to YouTube only (mappings.py:607)
    if let Some(vlist) = &m.videos {
        for v in &vlist.results {
            if v.site != "YouTube" { continue; }
            let path = format!("plugin://plugin.video.youtube/play/?video_id={}", v.key);
            conn.execute(
                "INSERT OR IGNORE INTO video (name, iso_country, iso_language, release_date, key, path, content, parent_id)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![&v.name, v.iso_3166_1.as_deref(), v.iso_639_1.as_deref(), v.published_at.as_deref(), &v.key, &path, &v.type_, &item_id],
            )?;
        }
    }

    // 12. translations
    if let Some(tl) = &m.translations {
        for t in &tl.translations {
            let plot = t.data.as_ref().and_then(|d| d.overview.as_deref());
            let title = t.data.as_ref().and_then(|d| d.title.as_deref().or(d.name.as_deref()));
            let tagline = t.data.as_ref().and_then(|d| d.tagline.as_deref());
            conn.execute(
                "INSERT OR IGNORE INTO translation (iso_country, iso_language, plot, title, tagline, parent_id)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![t.iso_3166_1.as_deref(), t.iso_639_1.as_deref(), plot, title, tagline, &item_id],
            )?;
        }
    }

    // 13. release certifications
    // TMDBHelper (mappings.py:155): converts release type integer to string enum,
    // and normalises empty strings to NULL via get_blanks_none().
    // release_type map: {1:'Premiere', 2:'Limited', 3:'Theatrical', 4:'Digital', 5:'Physical', 6:'TV'}
    const RELEASE_TYPE_NAMES: [Option<&str>; 7] = [
        None,                  // 0 — unused
        Some("Premiere"),      // 1
        Some("Limited"),       // 2
        Some("Theatrical"),    // 3
        Some("Digital"),       // 4
        Some("Physical"),      // 5
        Some("TV"),            // 6
    ];
    if let Some(rdl) = &m.release_dates {
        for country in &rdl.results {
            for rd in &country.release_dates {
                // get_blanks_none: empty string → NULL
                let cert_name = rd.certification.as_deref().filter(|s| !s.is_empty());
                let cert_lang = rd.iso_639_1.as_deref().filter(|s| !s.is_empty());
                let release_type_str = rd.type_
                    .and_then(|t| RELEASE_TYPE_NAMES.get(t as usize).copied().flatten());
                conn.execute(
                    "INSERT OR IGNORE INTO certification (name, iso_country, iso_language, release_date, release_type, parent_id)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        cert_name,
                        &country.iso_3166_1,
                        cert_lang,
                        rd.release_date.as_deref(),
                        release_type_str,
                        &item_id,
                    ],
                )?;
            }
        }
    }

    // 14. watch providers — availability column reflects the actual kind so TMDBHelper's
    // "Where to watch" UI distinguishes streaming (flatrate) from buy/rent/free/ads.
    if let Some(wpr) = &m.watch_providers {
        for (country, providers) in &wpr.results {
            for (kind_name, kind) in [
                ("flatrate", &providers.flatrate),
                ("buy", &providers.buy),
                ("rent", &providers.rent),
                ("free", &providers.free),
                ("ads", &providers.ads),
            ] {
                for p in kind.iter() {
                    dimensions::upsert_service(conn, p.provider_id, &p.provider_name, p.logo_path.as_deref(), p.display_priority)?;
                    conn.execute(
                        "INSERT OR IGNORE INTO provider (tmdb_id, availability, iso_country, parent_id) VALUES (?1, ?2, ?3, ?4)",
                        params![p.provider_id, kind_name, country, &item_id],
                    )?;
                }
            }
        }
    }

    // 15. belongs (collection membership)
    if let Some(coll) = &m.belongs_to_collection {
        let coll_id = id::build_item_id(id::TmdbType::Collection, coll.id);
        // Insert a stub baseitem for the collection if missing (will be fully warmed when we visit it).
        conn.execute(
            "INSERT OR IGNORE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
             VALUES (?1, 'set', ?2, 0, 0, 0, NULL)",
            params![&coll_id, expiry],
        )?;
        conn.execute(
            "INSERT OR IGNORE INTO collection (id, tmdb_id, plot, title) VALUES (?1, ?2, NULL, ?3)",
            params![&coll_id, coll.id, &coll.name],
        )?;
        conn.execute(
            "INSERT OR IGNORE INTO belongs (id, parent_id) VALUES (?1, ?2)",
            params![&item_id, &coll_id],
        )?;
    }

    Ok(())
}
