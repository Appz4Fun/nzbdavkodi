use anyhow::Result;
use rusqlite::{params, Transaction};
use crate::api::types_movie::ImageRef;

/// art(aspect_ratio INTEGER, quality INTEGER, iso_language TEXT, iso_country TEXT,
///     icon TEXT, type TEXT, extension TEXT, rating INTEGER, votes INTEGER, parent_id TEXT,
///     UNIQUE (icon, type, parent_id))
///
/// `art_type` is one of: posters, backdrops, logos, profiles, stills.
/// Note: `aspect_ratio` and `quality` are stored as REAL even though declared INTEGER (TMDBHelper quirk).
pub fn write_image(tx: &Transaction, parent_id: &str, art_type: &str, img: &ImageRef) -> Result<()> {
    let extension = img.file_path.rsplit('.').next().unwrap_or("jpg");
    tx.execute(
        "INSERT OR IGNORE INTO art (aspect_ratio, quality, iso_language, iso_country, icon, type, extension, rating, votes, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        params![
            img.aspect_ratio,
            img.width,
            img.iso_639_1.as_deref(),
            None::<&str>,
            &img.file_path,
            art_type,
            extension,
            img.vote_average,
            img.vote_count,
            parent_id,
        ],
    )?;
    Ok(())
}
