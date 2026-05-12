//! In-process harness — spins up the orchestrator on an OS-assigned
//! port so tests can drive `/v1/*` directly without docker-compose.
//!
//! Phase 0 only boots the orchestrator. Phase 5+ scenarios that
//! exercise faults call `Harness::with_fault_proxy()` to add an
//! upstream fault-proxy on its own port pair.

use std::net::SocketAddr;
use std::time::Duration;

use anyhow::Context;
use orchestrator_server::router;
use reqwest::Client;
use tokio::task::JoinHandle;

/// One running orchestrator instance, bound to a loopback port the
/// harness owns. Drop semantics aren't relied on — call `shutdown()`
/// explicitly so the scenario can fail loudly if the server panicked
/// mid-flight.
pub struct Harness {
    pub orch_addr: SocketAddr,
    pub orch_url: String,
    pub client: Client,
    server_task: Option<JoinHandle<()>>,
    shutdown_tx: Option<tokio::sync::oneshot::Sender<()>>,
}

impl Harness {
    /// Bind the orchestrator on `127.0.0.1:0` and return once
    /// `/v1/health` answers `200 OK`. The graceful-shutdown channel
    /// is plumbed so the scenario can stop the server without
    /// leaking a tokio task between runs.
    pub async fn start() -> anyhow::Result<Self> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .context("binding orchestrator listener")?;
        let orch_addr = listener.local_addr()?;
        let orch_url = format!("http://{orch_addr}");

        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let app = router();
        let server_task = tokio::spawn(async move {
            // Graceful shutdown completes when `rx` fires or when
            // both senders are dropped. The harness owns the
            // sender; if it's dropped without firing, the server
            // also exits cleanly.
            let _ = axum::serve(listener, app)
                .with_graceful_shutdown(async move {
                    let _ = rx.await;
                })
                .await;
        });

        let client = Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .context("building reqwest client")?;

        let harness = Self {
            orch_addr,
            orch_url,
            client,
            server_task: Some(server_task),
            shutdown_tx: Some(tx),
        };
        harness.wait_ready().await?;
        Ok(harness)
    }

    /// Poll `/v1/health` until it answers `200 OK` or we hit ~2s of
    /// retries — bounded so a misconfigured server can't hang the
    /// scenario indefinitely.
    pub async fn wait_ready(&self) -> anyhow::Result<()> {
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            let res = self
                .client
                .get(format!("{}/v1/health", self.orch_url))
                .send()
                .await;
            if let Ok(r) = res {
                if r.status().is_success() {
                    return Ok(());
                }
            }
            if std::time::Instant::now() >= deadline {
                anyhow::bail!("orchestrator did not become ready within 2s");
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    pub async fn shutdown(mut self) -> anyhow::Result<()> {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        if let Some(task) = self.server_task.take() {
            // Bounded join — a stuck server should fail the
            // scenario loudly, not silently leak the task.
            let join = tokio::time::timeout(Duration::from_secs(3), task).await;
            join.context("server task did not exit within 3s")??;
        }
        Ok(())
    }
}
