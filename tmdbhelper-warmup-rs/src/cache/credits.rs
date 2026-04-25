use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_movie::{CastMember, CrewMember};

pub fn write_castmember(conn: &Connection, parent_id: &str, c: &CastMember) -> Result<()> {
    // appearances=NULL: TMDBHelper writes NULL here (it counts across episodes for TV;
    // for movies a hardcoded 1 would diverge from TMDBHelper's stored NULL).
    conn.execute(
        "INSERT OR IGNORE INTO castmember (tmdb_id, role, ordering, appearances, guest, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![c.id, c.character.as_deref(), c.order, None::<i64>, None::<i64>, parent_id],
    )?;
    Ok(())
}

pub fn write_crewmember(conn: &Connection, parent_id: &str, c: &CrewMember) -> Result<()> {
    // appearances=NULL: same as castmember — TMDBHelper writes NULL for movies.
    conn.execute(
        "INSERT OR IGNORE INTO crewmember (tmdb_id, role, department, appearances, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![c.id, c.job.as_deref(), c.department.as_deref(), None::<i64>, parent_id],
    )?;
    Ok(())
}
