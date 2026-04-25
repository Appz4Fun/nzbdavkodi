use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_collection::CollectionResponse;
use crate::cache::{art, default_expiry, DATALEVEL_FULL};
use crate::id;

pub fn write_collection(conn: &Connection, c: &CollectionResponse) -> Result<()> {
    let item_id = id::build_item_id(id::TmdbType::Collection, c.id);
    let expiry = default_expiry();
    let has_translations = c.translations.as_ref().is_some_and(|t| !t.translations.is_empty());

    conn.execute(
        "INSERT OR REPLACE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
         VALUES (?1, 'set', ?2, ?3, 0, ?4, NULL)",
        params![&item_id, expiry, DATALEVEL_FULL, has_translations as i64],
    )?;
    conn.execute(
        "INSERT OR REPLACE INTO collection (id, tmdb_id, plot, title) VALUES (?1, ?2, ?3, ?4)",
        params![&item_id, c.id, c.overview.as_deref(), c.name.as_deref()],
    )?;

    if let Some(p) = &c.poster_path {
        let img = crate::api::types_movie::ImageRef { file_path: p.clone(), aspect_ratio: None, iso_639_1: None, iso_3166_1: None, width: None, height: None, vote_average: None, vote_count: None };
        art::write_image(conn, &item_id, "posters", &img)?;
    }
    if let Some(b) = &c.backdrop_path {
        let img = crate::api::types_movie::ImageRef { file_path: b.clone(), aspect_ratio: None, iso_639_1: None, iso_3166_1: None, width: None, height: None, vote_average: None, vote_count: None };
        art::write_image(conn, &item_id, "backdrops", &img)?;
    }
    if let Some(images) = &c.images {
        for img in &images.posters { art::write_image(conn, &item_id, "posters", img)?; }
        for img in &images.backdrops { art::write_image(conn, &item_id, "backdrops", img)?; }
    }

    if let Some(tl) = &c.translations {
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

    for part in &c.parts {
        let part_item = id::build_item_id(id::TmdbType::Movie, part.id);
        conn.execute(
            "INSERT OR IGNORE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
             VALUES (?1, 'movie', ?2, 0, 0, 0, NULL)",
            params![&part_item, expiry],
        )?;
        conn.execute("INSERT OR IGNORE INTO belongs (id, parent_id) VALUES (?1, ?2)", params![&part_item, &item_id])?;
    }

    Ok(())
}
