use anyhow::Result;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, Semaphore};
use tracing::{error, info, warn};
use crate::api::TmdbClient;
use crate::api::types_movie::MovieResponse;
use crate::api::types_tv::TvResponse;
use crate::api::types_person::PersonResponse;
use crate::api::types_collection::CollectionResponse;
use crate::cache::{movie, tv, person, collection, open_writer};
use crate::id::TmdbType;
use crate::state::{QueueItem, StateDb};

const MAX_DEPTH: i64 = 2;

pub struct WriteJob {
    pub item: QueueItem,
    pub payload: WritePayload,
    pub children: Vec<(i64, TmdbType, f64)>,
}

pub enum WritePayload {
    Movie(MovieResponse),
    Tv(TvResponse),
    Person(PersonResponse),
    Collection(CollectionResponse),
}

/// Extract child (tmdb_id, type, popularity) tuples from a fetched response.
/// ALL persons from credits are enqueued (user requirement: "every IMDb-grade page warm").
fn extract_children(payload: &WritePayload) -> Vec<(i64, TmdbType, f64)> {
    let mut children = Vec::new();
    match payload {
        WritePayload::Movie(m) => {
            if let Some(cr) = &m.credits {
                for c in &cr.cast { children.push((c.id, TmdbType::Person, 0.0)); }
                for c in &cr.crew { children.push((c.id, TmdbType::Person, 0.0)); }
            }
            if let Some(sim) = &m.similar {
                for r in &sim.results { children.push((r.id, TmdbType::Movie, 0.0)); }
            }
            if let Some(rec) = &m.recommendations {
                for r in &rec.results { children.push((r.id, TmdbType::Movie, 0.0)); }
            }
            if let Some(coll) = &m.belongs_to_collection {
                children.push((coll.id, TmdbType::Collection, 0.0));
            }
        }
        WritePayload::Tv(t) => {
            if let Some(cr) = &t.credits {
                for c in &cr.cast { children.push((c.id, TmdbType::Person, 0.0)); }
                for c in &cr.crew { children.push((c.id, TmdbType::Person, 0.0)); }
            }
            if let Some(sim) = &t.similar {
                for r in &sim.results { children.push((r.id, TmdbType::Tv, 0.0)); }
            }
            if let Some(rec) = &t.recommendations {
                for r in &rec.results { children.push((r.id, TmdbType::Tv, 0.0)); }
            }
        }
        WritePayload::Person(_p) => {
            // Person's combined_credits would enqueue movies/TV they appeared in.
            // We skip this for now — the queue already has those from seeds + movie/TV discovery.
            // Future: add combined_credits extraction if deeper crawl is needed.
        }
        WritePayload::Collection(c) => {
            for part in &c.parts {
                children.push((part.id, TmdbType::Movie, 0.0));
            }
        }
    }
    children
}

pub async fn run(
    state_path: PathBuf,
    item_details_path: PathBuf,
    api_key: String,
    concurrency: usize,
    batch_size: usize,
) -> Result<()> {
    let client = TmdbClient::new(api_key)?;
    let sem = Arc::new(Semaphore::new(concurrency));
    let (tx, mut rx) = mpsc::channel::<WriteJob>(concurrency * 2);

    // Spawn dedicated writer task (blocking — SQLite is synchronous)
    let writer_handle = {
        let item_details_path = item_details_path.clone();
        let state_path_for_writer = state_path.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut writer = open_writer(&item_details_path)?;
            let mut state = StateDb::open(&state_path_for_writer)?;
            let mut written = 0u64;
            let start = std::time::Instant::now();
            while let Some(job) = rx.blocking_recv() {
                let item = job.item;
                let res = match job.payload {
                    WritePayload::Movie(m) => movie::write_movie(&mut writer, &m),
                    WritePayload::Tv(t) => tv::write_tv(&mut writer, &t),
                    WritePayload::Person(p) => person::write_person(&mut writer, &p),
                    WritePayload::Collection(c) => collection::write_collection(&mut writer, &c),
                };
                match res {
                    Ok(_) => {
                        // Batch visit + child-enqueue in ONE transaction.
                        // Un-batched auto-commits on a USB HDD cost ~10ms each;
                        // 100 children = 1 second wasted. One tx = ~50ms.
                        let children = if item.depth + 1 <= MAX_DEPTH { &job.children[..] } else { &[] };
                        if let Err(e) = state.visit_and_enqueue_batch(item.tmdb_id, item.tmdb_type, children, item.depth + 1) {
                            error!("state batch failed for {:?} {}: {:?}", item.tmdb_type, item.tmdb_id, e);
                        }
                        written += 1;
                        if written % 100 == 0 {
                            let elapsed = start.elapsed().as_secs_f64().max(1.0);
                            let rate = written as f64 / elapsed;
                            let qsize = state.queue_size().unwrap_or(0);
                            let vcount = state.visited_count().unwrap_or(0);
                            info!("written={} visited={} queue={} rate={:.1}/s", written, vcount, qsize, rate);
                        }
                    }
                    Err(e) => error!("write failed for {:?} {}: {:?}", item.tmdb_type, item.tmdb_id, e),
                }
            }
            info!("writer task exiting, total written={}", written);
            Ok(())
        })
    };

    // Main loop: pop batches from state.db and dispatch async fetcher tasks
    let mut state = StateDb::open(&state_path)?;
    loop {
        let batch = state.pop_batch(batch_size)?;
        if batch.is_empty() {
            info!("queue empty, sleeping 60s before re-check");
            drop(state);
            tokio::time::sleep(Duration::from_secs(60)).await;
            state = StateDb::open(&state_path)?;
            let new_size = state.queue_size()?;
            if new_size == 0 {
                info!("queue still empty after 60s, shutting down");
                break;
            }
            continue;
        }
        for item in batch {
            let permit = sem.clone().acquire_owned().await?;
            let client = client.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                let _permit = permit;
                let payload = match item.tmdb_type {
                    TmdbType::Movie => client.get_movie(item.tmdb_id).await.map(WritePayload::Movie),
                    TmdbType::Tv => client.get_tv(item.tmdb_id).await.map(WritePayload::Tv),
                    TmdbType::Person => client.get_person(item.tmdb_id).await.map(WritePayload::Person),
                    TmdbType::Collection => client.get_collection(item.tmdb_id).await.map(WritePayload::Collection),
                };
                match payload {
                    Ok(p) => {
                        let children = extract_children(&p);
                        let _ = tx.send(WriteJob { item, payload: p, children }).await;
                    }
                    Err(e) => warn!("fetch failed for {:?} {}: {:?}", item.tmdb_type, item.tmdb_id, e),
                }
            });
        }
    }
    // Drop the sender so the writer task's recv loop exits
    drop(tx);
    writer_handle.await??;
    Ok(())
}
