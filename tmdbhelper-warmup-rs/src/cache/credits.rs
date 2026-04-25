use anyhow::Result;
use rusqlite::{params, Connection};
use crate::api::types_movie::{CastMember, CrewMember};

pub fn write_castmember(conn: &Connection, parent_id: &str, c: &CastMember) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO castmember (tmdb_id, role, ordering, appearances, guest, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
    )?.execute(params![c.id, c.character.as_deref(), c.order, None::<i64>, None::<i64>, parent_id])?;
    Ok(())
}

pub fn write_crewmember(conn: &Connection, parent_id: &str, c: &CrewMember) -> Result<()> {
    conn.prepare_cached(
        "INSERT OR IGNORE INTO crewmember (tmdb_id, role, department, appearances, parent_id)
         VALUES (?1, ?2, ?3, ?4, ?5)",
    )?.execute(params![c.id, c.job.as_deref(), c.department.as_deref(), None::<i64>, parent_id])?;
    Ok(())
}
