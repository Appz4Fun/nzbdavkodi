//! Scenario registry.
//!
//! Each variant is one bin-shaped flow the harness can run. Phase 0
//! lands `GoldenPath`. The other entries in plan §12.2 are placeholders
//! until their phase opens.

use std::fmt;

use crate::Harness;

pub mod golden_path;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scenario {
    GoldenPath,
}

impl Scenario {
    pub fn all() -> &'static [Scenario] {
        &[Scenario::GoldenPath]
    }

    pub fn name(self) -> &'static str {
        match self {
            Scenario::GoldenPath => "golden_path",
        }
    }

    pub fn parse(s: &str) -> anyhow::Result<Self> {
        match s {
            "golden_path" => Ok(Scenario::GoldenPath),
            other => anyhow::bail!("unknown scenario: {other}"),
        }
    }

    pub async fn run(self) -> anyhow::Result<()> {
        let harness = Harness::start().await?;
        let outcome = match self {
            Scenario::GoldenPath => golden_path::run(&harness).await,
        };
        // Always tear down the orchestrator regardless of outcome so
        // a failing assertion doesn't leak a listener across scenarios.
        let shutdown_outcome = harness.shutdown().await;
        outcome.and(shutdown_outcome)
    }
}

impl fmt::Display for Scenario {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}
