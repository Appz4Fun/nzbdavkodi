//! `harness-runner` binary — `cargo run --bin harness-runner -- <scenario>`.

use clap::Parser;
use harness_runner::Scenario;
use tracing_subscriber::EnvFilter;

#[derive(Parser, Debug)]
#[command(
    name = "harness-runner",
    about = "Drive orchestrator scenarios with no Kodi."
)]
struct Args {
    /// Scenario name (e.g. `golden_path`). With no scenario arg the
    /// runner enumerates every scenario known to the registry.
    scenario: Option<String>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_env("HARNESS_RUNNER_LOG")
                .unwrap_or_else(|_| EnvFilter::new("harness_runner=info,warn")),
        )
        .with_target(false)
        .init();

    let args = Args::parse();
    let scenarios: Vec<Scenario> = match args.scenario {
        Some(name) => vec![Scenario::parse(&name)?],
        None => Scenario::all().to_vec(),
    };

    let mut failures = Vec::new();
    for scenario in scenarios {
        tracing::info!(scenario = %scenario, "running scenario");
        match scenario.run().await {
            Ok(()) => tracing::info!(scenario = %scenario, "scenario passed"),
            Err(e) => {
                tracing::error!(scenario = %scenario, error = %e, "scenario failed");
                failures.push((scenario, e));
            }
        }
    }

    if !failures.is_empty() {
        anyhow::bail!("{} scenario(s) failed", failures.len());
    }
    Ok(())
}
