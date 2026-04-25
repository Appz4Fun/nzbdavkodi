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

pub struct RequeueItem {
    pub tmdb_id: i64,
    pub tmdb_type: TmdbType,
    pub depth: i64,
    pub popularity: f64,
}

const MAX_DEPTH: i64 = 5;

pub struct WriteJob {
    pub item: QueueItem,
    pub payload: WritePayload,
    pub children: Vec<(i64, TmdbType, f64)>,
}

pub enum WritePayload {
    Movie(Box<MovieResponse>),
    Tv(Box<TvResponse>),
    Person(Box<PersonResponse>),
    Collection(Box<CollectionResponse>),
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
        WritePayload::Person(p) => {
            if let Some(cc) = &p.combined_credits {
                for entry in cc.cast.iter().chain(cc.crew.iter()) {
                    let t = match entry.media_type.as_deref() {
                        Some("movie") => TmdbType::Movie,
                        Some("tv") => TmdbType::Tv,
                        _ => continue,
                    };
                    children.push((entry.id, t, 0.0));
                }
            }
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
    let (tx, mut rx) = mpsc::channel::<WriteJob>(200);
    let (requeue_tx, mut requeue_rx) = mpsc::channel::<RequeueItem>(200);

    const MEGA_TX_SIZE: usize = 200;

    let writer_handle = {
        let item_details_path = item_details_path.clone();
        let state_path_for_writer = state_path.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut writer = open_writer(&item_details_path)?;
            let mut state = StateDb::open(&state_path_for_writer)?;
            let mut batch: Vec<WriteJob> = Vec::with_capacity(MEGA_TX_SIZE);
            let mut written = 0u64;
            let start = std::time::Instant::now();
            let mut total_write_ms = 0u64;
            let mut total_state_ms = 0u64;

            #[allow(clippy::while_let_loop)]
            loop {
                // Drain requeue items between batches
                while let Ok(rq) = requeue_rx.try_recv() {
                    if let Err(e) = state.enqueue_child(rq.tmdb_id, rq.tmdb_type, rq.depth, rq.popularity) {
                        error!("requeue failed for {:?} {}: {:?}", rq.tmdb_type, rq.tmdb_id, e);
                    }
                }

                match rx.blocking_recv() {
                    Some(job) => batch.push(job),
                    None => break,
                }
                while batch.len() < MEGA_TX_SIZE {
                    match rx.try_recv() {
                        Ok(job) => batch.push(job),
                        Err(_) => break,
                    }
                }

                let t0 = std::time::Instant::now();
                let tx = writer.transaction()?;
                let mut type_ms: [u64; 4] = [0; 4];
                for job in &batch {
                    let ti = std::time::Instant::now();
                    let (idx, res) = match &job.payload {
                        WritePayload::Movie(m) => (0, movie::write_movie(&tx, m.as_ref())),
                        WritePayload::Tv(t) => (1, tv::write_tv(&tx, t.as_ref())),
                        WritePayload::Person(p) => (2, person::write_person(&tx, p.as_ref())),
                        WritePayload::Collection(c) => (3, collection::write_collection(&tx, c.as_ref())),
                    };
                    type_ms[idx] += ti.elapsed().as_millis() as u64;
                    if let Err(e) = res {
                        error!("write failed for {:?} {}: {:?}", job.item.tmdb_type, job.item.tmdb_id, e);
                    }
                }
                tx.commit()?;
                crate::cache::checkpoint_passive(&writer);
                let write_ms = t0.elapsed().as_millis() as u64;
                total_write_ms += write_ms;

                let t1 = std::time::Instant::now();
                let state_items: Vec<crate::state::ChildBatch<'_>> = batch.iter()
                    .map(|job| {
                        let children: &[(i64, TmdbType, f64)] = if job.item.depth < MAX_DEPTH { &job.children[..] } else { &[] };
                        (job.item.tmdb_id, job.item.tmdb_type, children, job.item.depth + 1)
                    })
                    .collect();
                if let Err(e) = state.visit_and_enqueue_multi(&state_items) {
                    error!("state mega-batch failed: {:?}", e);
                }
                let state_ms = t1.elapsed().as_millis() as u64;
                total_state_ms += state_ms;

                written += batch.len() as u64;
                if written % 100 < batch.len() as u64 || written == batch.len() as u64 {
                    let elapsed = start.elapsed().as_secs_f64().max(1.0);
                    let rate = written as f64 / elapsed;
                    let qsize = state.queue_size().unwrap_or(0);
                    let vcount = state.visited_count().unwrap_or(0);
                    let avg_write = if written > 0 { total_write_ms / written } else { 0 };
                    let avg_state = if written > 0 { total_state_ms / written } else { 0 };
                    info!("written={} visited={} queue={} rate={:.1}/s batch={} avg_write={}ms avg_state={}ms",
                          written, vcount, qsize, rate, batch.len(), avg_write, avg_state);
                }
                batch.clear();
            }

            // Final drain of requeue items
            while let Ok(rq) = requeue_rx.try_recv() {
                if let Err(e) = state.enqueue_child(rq.tmdb_id, rq.tmdb_type, rq.depth, rq.popularity) {
                    error!("requeue failed for {:?} {}: {:?}", rq.tmdb_type, rq.tmdb_id, e);
                }
            }

            info!("writer task exiting, total written={}", written);
            Ok(())
        })
    };

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
            let requeue_tx = requeue_tx.clone();
            tokio::spawn(async move {
                let _permit = permit;
                let payload = match item.tmdb_type {
                    TmdbType::Movie => client.get_movie(item.tmdb_id).await.map(|r| WritePayload::Movie(Box::new(r))),
                    TmdbType::Tv => client.get_tv(item.tmdb_id).await.map(|r| WritePayload::Tv(Box::new(r))),
                    TmdbType::Person => client.get_person(item.tmdb_id).await.map(|r| WritePayload::Person(Box::new(r))),
                    TmdbType::Collection => client.get_collection(item.tmdb_id).await.map(|r| WritePayload::Collection(Box::new(r))),
                };
                match payload {
                    Ok(p) => {
                        let children = extract_children(&p);
                        if tx.send(WriteJob { item: item.clone(), payload: p, children }).await.is_err() {
                            warn!("writer channel closed, requeuing {:?} {}", item.tmdb_type, item.tmdb_id);
                            let _ = requeue_tx.send(RequeueItem {
                                tmdb_id: item.tmdb_id, tmdb_type: item.tmdb_type,
                                depth: item.depth, popularity: item.popularity,
                            }).await;
                        }
                    }
                    Err(e) => {
                        warn!("fetch failed for {:?} {}: {:?}", item.tmdb_type, item.tmdb_id, e);
                        let _ = requeue_tx.send(RequeueItem {
                            tmdb_id: item.tmdb_id, tmdb_type: item.tmdb_type,
                            depth: item.depth, popularity: item.popularity,
                        }).await;
                    }
                }
            });
        }
    }
    drop(tx);
    drop(requeue_tx);
    writer_handle.await??;
    Ok(())
}
