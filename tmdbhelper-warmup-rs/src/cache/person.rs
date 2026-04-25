use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_movie::ImageRef;
use crate::api::types_person::PersonResponse;
use crate::cache::{art, default_expiry, DATALEVEL_FULL};
use crate::id;

pub fn write_person(conn: &Connection, p: &PersonResponse) -> Result<()> {
    let item_id = id::build_item_id(id::TmdbType::Person, p.id);
    let expiry = default_expiry();
    let aka_joined = p.also_known_as.as_ref().map(|v| v.join("|"));

    // 1. baseitem (must come first — children FK to it)
    // translation=0: person translations are not written; TMDBHelper queries person directly by tmdb_id.
    // fanart_tv=0: persons have no fanart.tv entries.
    // language=NULL: persons have no primary language concept.
    conn.execute(
        "INSERT OR REPLACE INTO baseitem (id, mediatype, expiry, datalevel, fanart_tv, translation, language)
         VALUES (?1, 'person', ?2, ?3, 0, 0, NULL)",
        params![&item_id, expiry, DATALEVEL_FULL],
    )?;

    // 2. person row — 11 columns match schema exactly
    conn.execute(
        "INSERT OR REPLACE INTO person (id, tmdb_id, name, known_for_department, gender, biography, birthday, deathday, also_known_as, place_of_birth, popularity)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        params![
            &item_id, p.id, p.name.as_deref(), p.known_for_department.as_deref(),
            p.gender, p.biography.as_deref(), p.birthday.as_deref(), p.deathday.as_deref(),
            aka_joined.as_deref(), p.place_of_birth.as_deref(), p.popularity,
        ],
    )?;

    // 3. profile art — synthesised from top-level profile_path, then from images.profiles
    // Synthesised ImageRef must include iso_3166_1: None (C6 fix).
    if let Some(profile) = &p.profile_path {
        let img = ImageRef {
            file_path: profile.clone(),
            aspect_ratio: None,
            iso_639_1: None,
            iso_3166_1: None,
            width: None,
            height: None,
            vote_average: None,
            vote_count: None,
        };
        art::write_image(conn, &item_id, "profiles", &img)?;
    }
    if let Some(images) = &p.images {
        for img in &images.profiles {
            art::write_image(conn, &item_id, "profiles", img)?;
        }
    }

    // 4. external_ids → unique_id
    if let Some(ext) = &p.external_ids {
        if let Some(imdb) = &ext.imdb_id {
            conn.execute(
                "INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('imdb', ?1, ?2)",
                params![imdb, &item_id],
            )?;
        }
    }
    conn.execute(
        "INSERT OR IGNORE INTO unique_id (key, value, parent_id) VALUES ('tmdb', ?1, ?2)",
        params![p.id.to_string(), &item_id],
    )?;

    Ok(())
}
