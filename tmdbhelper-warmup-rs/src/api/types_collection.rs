use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct CollectionResponse { pub id: i64, pub name: Option<String> }
