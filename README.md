# crowdsec-issue-4115

Independent measurement of the per-alert cost of crowdsec's allowlist lookup, related to upstream PR [#4475](https://github.com/crowdsecurity/crowdsec/pull/4475) (`disable_allowlist_ingestion` feature flag).

## The short version

The PR #4475 review raised the question whether the per-call cost of the allowlist lookup is material at scale, since a microbenchmark of the function alone shows microseconds. This repo measures the cost end-to-end in a realistic ingestion stand.

On this stand the call adds exactly +1 SELECT per alert at the database level (MySQL counter, deterministic). End-to-end this is **~234 μs per alert on an Apple M3 Pro** and **~1.15 ms per alert on an older Intel desktop** at the median, accounting for **~18% (Mac) and ~27% (Linux) of the variant-A median per-request latency** under non-saturating load. N=20 paired runs per host, 95% bootstrap CIs exclude zero on every measured percentile. Full numbers in [`RESULTS.md`](RESULTS.md).

## Reproduce in ~30 minutes

```
git clone https://github.com/aa1ex/crowdsec-issue-4115
cd crowdsec-issue-4115/stand
./scripts/run.sh
```

Single-machine docker setup; produces an HTML report. At the default smaller sample size, the +1 SELECT per alert and the sign of the latency delta reproduce; the exact milliseconds depend on hardware. See [`stand/README.md`](stand/README.md) for hardware requirements and configuration knobs.

## Full results and methodology

See [`RESULTS.md`](RESULTS.md) — main measurement (N=20), protocol, and what the stand does and does not show.

## Repo layout

- [`RESULTS.md`](RESULTS.md) — results, methodology, caveats.
- [`patches/disable-allowlist-ingestion.patch`](patches/disable-allowlist-ingestion.patch) — the single feature-flag patch applied on top of upstream `v1.7.8`, read it in full to verify nothing else is going on.
- [`stand/README.md`](stand/README.md) — self-contained single-machine docker reproducer.
- [`stand/runs/run-20260524T204950Z/`](stand/runs/run-20260524T204950Z/), [`stand/runs/run-20260524T204613Z/`](stand/runs/run-20260524T204613Z/) — full N=20 measurement output for each host: `report.html` plus raw per-pair data (latency, metrics, MySQL status snapshots, pprof) for independent verification.

## Issue context

[crowdsecurity/crowdsec#4115](https://github.com/crowdsecurity/crowdsec/issues/4115) reports that the allowlist feature added in v1.6.6 makes the LAPI unresponsive under DDoS-scale alert load, with the per-alert call surfacing in crowdsec logs as `error while checking allowlist`. The reporter does not use the allowlist feature but still pays the SELECT on every alert, and downgrading to v1.6.5 (which lacks the call) makes the symptom go away. PR #4475 adds a feature flag to skip the call for operators in that situation.

This repo does not claim to have reproduced the specific production outage from the issue. It measures the per-call cost of the allowlist lookup and provides a self-contained stand for third parties to verify the measurement.
