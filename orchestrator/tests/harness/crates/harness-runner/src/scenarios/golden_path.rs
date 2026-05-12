//! Phase 0 scenario: orchestrator boots, `/v1/health` answers, version
//! field is present. This is the no-Kodi equivalent of the existing
//! extreme suite's "addon comes up and connects to its services" smoke.

use anyhow::Context;

use crate::Harness;

pub async fn run(harness: &Harness) -> anyhow::Result<()> {
    let res = harness
        .client
        .get(format!("{}/v1/health", harness.orch_url))
        .send()
        .await
        .context("GET /v1/health")?;
    anyhow::ensure!(
        res.status().is_success(),
        "GET /v1/health returned {} (expected 2xx)",
        res.status()
    );
    let body: serde_json::Value = res.json().await.context("decoding /v1/health JSON")?;
    anyhow::ensure!(
        body["status"] == "ok",
        "expected status='ok', got {}",
        body["status"]
    );
    anyhow::ensure!(
        body["version"]
            .as_str()
            .map(|v| !v.is_empty())
            .unwrap_or(false),
        "version field must be a non-empty string"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn golden_path_passes_against_stub_orchestrator() {
        let harness = Harness::start().await.expect("harness boots");
        run(&harness).await.expect("golden_path passes");
        harness.shutdown().await.expect("harness shuts down");
    }
}
