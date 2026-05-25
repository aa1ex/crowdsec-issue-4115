# Measurement results — PR #4475 (`disable_allowlist_ingestion`)

## What this document measures

The PR #4475 review noted that a function-level measurement of the allowlist check shows microseconds of work, suggesting the call is negligible. This document measures the same call end-to-end in an ingestion stand, where the cost is dominated by a database round-trip rather than CPU time.

The allowlist check is not CPU-bound work: the call path `isAllowListed -> IsAllowlistedBy -> ent query -> SELECT` issues one SQL query per alert. A function-level or warm-SQLite measurement sees only the CPU portion and misses the round-trip; the stand here runs the call in its actual context (LAPI under load, real MySQL ingestion path).

The methodology is a paired A/B design: both variants ingest the same alert stream and write the alerts; the only difference is whether the allowlist lookup runs. The reported delta is therefore exactly the cost of the lookup, on top of an otherwise identical write path. Pair-level bootstrap gives 95% confidence intervals; the MySQL `Com_select` counter provides an independent deterministic check. PR #4475 adds an opt-out for operators who do not use the allowlist and do not want to pay a per-alert SELECT they get nothing from.

## Scope

This is an overhead measurement: how much does the allowlist lookup add to a single alert ingestion, in database work and in latency. It does not claim to reproduce the unresponsive-LAPI behavior in issue #4115; whether the measured per-call cost is enough to tip a given production workload over is out of scope. See "What this proves and what it does not" for the explicit boundary.

## Headline

At the MySQL level the allowlist check adds **exactly +1 SELECT per alert** (deterministic counter from `SHOW GLOBAL STATUS`, zero variance across pairs). This is a property of the call path itself, not of any particular run.

End-to-end on the stand, that round-trip costs **~234 μs per alert on an Apple M3 Pro** and **~1.15 ms per alert on an older Intel i7-8700** at the median (N=20 pairs per host, 95% bootstrap CIs on every measured percentile exclude zero). Per ingestion request (batch of 10 alerts) that is **+2.3 ms** and **+11.5 ms** at the median respectively — equal to **~18% and ~27% of the variant-A median per-request latency**. The per-call cost is not microseconds in the colloquial sense (a microbenchmark of the function alone is), and it is not negligible relative to the rest of the per-request work. See the Main measurement section for the full table.

## Stand

Two variants, one binary. The only difference between variant A and variant B is one line in `feature.yaml`:

- variant A: vanilla behavior (allowlist call runs).
- variant B: feature flag `disable_allowlist_ingestion: true` (call is skipped at the top of `isAllowListed`).

A run is a pair: variant A then variant B back-to-back, with the first variant chosen by a deterministic random seed. Between variants the crowdsec container is restarted and the alerts/decisions tables are cleared so each variant starts from the same clean state.

Load: 100 alerts/s offered (10 req/s x 10 alerts/req) from `stand/loadgen` (open-loop: fixed-rate arrivals, the generator does not throttle when the server slows). crowdsec uses `max_open_conns: 250` (pool not saturated at this rate). 30s warmup + 180s steady + 30s drain per variant; only the steady window is analyzed. Both variants run with `pprof_block_profile` enabled symmetrically, so any profiler overhead cancels in the paired difference.

The crowdsec binary is built from the official upstream tag `v1.7.8` with one small patch applied on top: `patches/disable-allowlist-ingestion.patch`. The patch touches three files (the feature-flag registration, the four-line short-circuit in `isAllowListed`, and a test) and nothing else, so a reviewer can read it in full instead of trusting a fork. It applies cleanly on the pristine tag.

## Protocol

For each variant in each pair we collect per-attempt latency (one JSONL line per HTTP attempt from the loadgen), MySQL `SHOW GLOBAL STATUS` snapshots before and after, 1-second aggregate loadgen counters, and — when `CAPTURE_PPROF=1` — CPU / block / mutex / goroutine profiles. CPU is centered in the steady window; block and mutex span it.

Stats: paired hierarchical bootstrap (resample the 20 pairs, then resample latency points within each variant), default 5,000 resamples; 95% CIs on delta-p50/p95/p99. Decision rule: CI excludes 0 -> "real effect"; CI crosses 0 -> "not resolved at this N". No outlier dropping. Raw artifacts are not overwritten.

## Main measurement (N=20 pairs)

The same binary and stand were run on two hosts with very different hardware to expose the dependence of the absolute magnitude on per-thread performance and the MySQL I/O path. Latency rows are per ingestion request (batch of 10 alerts); divide by 10 for a per-alert figure.

**Host A — Apple M3 Pro / macOS 15.6.1 / Docker Desktop 28.3.3 / 12 cores / 36 GB** — full report: [`stand/runs/run-20260524T204950Z/report.html`](stand/runs/run-20260524T204950Z/report.html)

| metric (variant A minus variant B) | mean   | 95% CI           | excludes 0? |
|------------------------------------|--------|------------------|-------------|
| SELECT per alert (MySQL counter)   | +1.000 | [+1.000, +1.000] | yes (exact) |
| p50 latency, ms (per request)      | +2.34  | [+2.24, +2.45]   | yes         |
| p95 latency, ms (per request)      | +2.20  | [+1.97, +2.46]   | yes         |
| p99 latency, ms (per request)      | +2.46  | [+1.78, +3.06]   | yes         |

**Host B — Intel i7-8700 / Ubuntu 24.04 / Docker 29.1.3 / 12 cores / 62 GB** — full report: [`stand/runs/run-20260524T204613Z/report.html`](stand/runs/run-20260524T204613Z/report.html)

| metric (variant A minus variant B) | mean   | 95% CI           | excludes 0? |
|------------------------------------|--------|------------------|-------------|
| SELECT per alert (MySQL counter)   | +1.000 | [+1.000, +1.000] | yes (exact) |
| p50 latency, ms (per request)      | +11.53 | [+11.43, +11.63] | yes         |
| p95 latency, ms (per request)      | +11.37 | [+10.47, +11.92] | yes         |
| p99 latency, ms (per request)      | +11.30 | [+9.60, +12.53]  | yes         |

Counter is exactly +1 SELECT per alert on both hosts, with zero variance across pairs. Latency CIs exclude zero on every measured percentile on both hosts, including p99.

**Per-call and share-of-latency:** the per-request delta divides by 10 (batch) for a per-alert figure. The variant-A median per-request latency is **12.7 ms on Mac (B baseline 10.4 ms + 2.3 ms delta)** and **42.7 ms on Linux (B baseline 31.2 ms + 11.5 ms delta)** — the allowlist round-trip is **~18% (Mac) and ~27% (Linux) of the total per-request work** under non-saturating load.

| Host | per-alert delta (p50) | per-request delta (p50) | variant-A median | share of variant-A median |
|---|---|---|---|---|
| Apple M3 Pro | **+234 μs** | +2.34 ms | 12.70 ms | **18.4%** |
| Intel i7-8700 | **+1.15 ms** | +11.53 ms | 42.72 ms | **27.0%** |

Each per-host report linked above contains the Operational point, per-pair plots, per-pair detail table, and a Profile data section with CPU / block / mutex diff bar charts and an interactive CPU call graph diff (variant A minus variant B).

## What this proves and what it does not

Proven on this stand at N=20:

- The allowlist lookup adds exactly one SELECT per alert at the database level. This is a property of the call path; the deterministic MySQL counter confirms it with zero variance.
- The lookup is a database round-trip, not microseconds of CPU. A function-level CPU measurement does not capture its cost.
- The per-call wall-clock cost is hundreds of microseconds to over a millisecond on commodity hardware (~234 μs on Apple M3 Pro, ~1.15 ms on Intel i7-8700 at p50), with both 95% CIs excluding zero on every measured percentile (p50, p95, p99). Per ingestion request (batch of 10), this is 18-27% of the total per-request median latency.

Not proven on this stand:

- The allowlist call is not claimed to be the sole cause of any specific production outage; the stand reproduces per-call cost, not a cascading unresponsive state.
- Pool-wait contention is not claimed as the growth mechanism: at tested pool sizes the pool is either not saturated or the system diverges into timeouts before pool-wait dominates the block profile.
- The absolute milliseconds do not generalize across hardware, MySQL transport (TCP vs unix socket) or network proximity. The sign and the +1 SELECT per alert are invariant; the millisecond magnitude is not.

## Reproduce it

```
git clone https://github.com/aa1ex/crowdsec-issue-4115
cd crowdsec-issue-4115/stand
./scripts/run.sh
```

About 30 minutes on a Mac with Docker Desktop set to 6 CPU and 8 GB RAM. At the default smaller sample size, the +1 SELECT per alert and the sign of the latency delta reproduce; the exact milliseconds depend on hardware. Add `CAPTURE_PPROF=1` to also capture CPU / block / mutex / goroutine profiles per variant.

## Repo layout

- `stand/` — single-machine reproducer, README inside.
- `patches/disable-allowlist-ingestion.patch` — the single feature-flag patch applied on top of upstream v1.7.8.
- `stand/runs/run-20260524T204950Z/`, `stand/runs/run-20260524T204613Z/` — full N=20 measurement output for each host: rendered `report.html` (linked from Main measurement above) plus raw per-pair data (`latency.jsonl`, `metrics.jsonl`, `meta.json`, `status-{before,after}.txt`, `*.pprof`) for independent verification.
