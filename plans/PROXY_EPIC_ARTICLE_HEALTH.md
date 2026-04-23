# Proxy Epic — Article-Health Pre-Submit Filter

**Status:** gated epic. Do not start until entry criteria are satisfied.

**Goal:** reduce mid-stream recoveries and zero-fill by filtering unhealthy releases before submit.

---

## Entry criteria

- ≥1 week of post-merge observability data showing zero-fill incidents keyed to specific NZBs.
- nzbdav SABnzbd-compat endpoint (or NZBHydra detail endpoint) that exposes per-NZB article-completeness metrics, verified in a local nzbdav-rs build.
- Owner assigned.
- Scope doc drafted and reviewed.

---

## High-level design

Query the health endpoint during `_handle_play` / `_handle_search`, then down-rank or hard-filter results below a threshold in `resources/lib/filter.py`.

---

## Risks

- False negatives on actually-playable releases.
- Cross-subsystem dependency on nzbdav API surface.
- Threshold calibration remains empirical.

---

## Out of scope

- Automatic re-search on filter rejection.
- Health-aware caching.
