# PR #4475 stand — single-machine reproducer

One-command reproducer for the measurement behind upstream crowdsec PR [`#4475`](https://github.com/crowdsecurity/crowdsec/pull/4475) (`disable_allowlist_ingestion` feature flag).

The script brings up an isolated Docker stack (MySQL + crowdsec built from upstream v1.7.8 plus the feature-flag patch + open-loop loadgen), runs a paired measurement (variant A vs variant B back-to-back in randomized order) and writes an HTML report you can open in a browser. Prometheus and Grafana are available as an opt-in observability profile for live dashboards (see `OBSERVABILITY=1` below).

## What you'll see

After a successful run:

- An HTML report at `runs/<grid-id>/report.html` with headline boxes for p50/p95/p99 latency difference between variant A (allowlist active) and variant B (allowlist skipped), 95% bootstrap confidence intervals, the MySQL `Com_select` counter delta, latency distribution and per-pair stability plots, and (when `CAPTURE_PPROF=1`) a profile data section with diff bar charts and a CPU call graph SVG.
- Raw per-attempt latency JSONL, MySQL `SHOW GLOBAL STATUS` snapshots, loadgen aggregate counters under `runs/<grid-id>/pair-NN/arm-{A,B}/`.
- With `OBSERVABILITY=1`: a live Grafana dashboard at <http://127.0.0.1:3000> ("PR4475" folder, panel "allowlist call effect on LAPI") showing POST `/v1/alerts` p50/p95, MySQL `Com_select` rate, crowdsec CPU. Variant boundaries are pushed as Grafana annotations either way (skipped silently when observability is off).

## Hardware / OS

**Minimum (Linux native Docker):** 4 vCPU, 4 GB RAM, 10 GB free disk (Go module cache, Docker image layers, MySQL data volume).

**Recommended (especially for macOS / Docker Desktop):** 6-8 vCPU, 8 GB RAM, 15 GB free disk. On macOS, raise Docker Desktop's resource limits in Settings -> Resources to at least 6 CPU and 8 GB before running; defaults (2 CPU / 4 GB) are too tight and the initial Go build will be slow.

**Wall-clock:**

- First run: ~35 minutes for the default 3 pairs (~25 min of measurement + ~10 min one-time crowdsec build from upstream v1.7.8 + patch).
- Subsequent runs reuse the built image and take ~25 minutes for the default 3 pairs.

Tested on Linux x86_64 (Ubuntu 24.04, Docker 26+) and macOS arm64 (Docker Desktop 4.x).

## Run (one command)

```bash
git clone <this repo>
cd crowdsec-issue-4115/stand
./scripts/run.sh
```

That's it. Default config:

- 3 paired iterations (variant A then variant B back-to-back)
- 30 s warm-up + 180 s steady + 30 s drain per variant
- 10 requests/s x 10 alerts/batch = 100 alerts/s
- `max_open_conns = 250` on the crowdsec->MySQL pool

When done, open the printed `file://...report.html` URL.

## Knobs

```bash
PAIRS=20 ./scripts/run.sh           # tight 95% CIs, ~3-4 h wall-clock
CAPTURE_PPROF=1 ./scripts/run.sh    # also scrape cpu/block/mutex/goroutine
OBSERVABILITY=1 ./scripts/run.sh    # also bring up prometheus + grafana
POOL_SIZE=10 PAIRS=5 ./scripts/run.sh   # exploratory pool-pressure sweep
```

Knobs combine. For a publication-grade run with profiles and live dashboards: `PAIRS=20 CAPTURE_PPROF=1 OBSERVABILITY=1 ./scripts/run.sh`.

## Why these defaults?

Each parameter has a reason; nothing is tuned to inflate the result.

- **`RATE = 10`, `BATCH = 10`** = 100 alerts/s. Higher rates (`RATE=60 BATCH=100`) saturate to the 5 s deadline and make latency uninformative; this is the smoke-tested non-saturating point.
- **`POOL_SIZE = 250`** (crowdsec->MySQL). Pool is not saturated at the headline rate. The `POOL_SIZE` env var lets you set a non-default value for exploratory measurements.
- **`WARMUP=30 / STEADY=180 / DRAIN=30 s`** per variant. Only the steady window is analyzed.
- **`PAIRS=3`** enough to see the median effect; raise to 20 for tight confidence intervals on tail percentiles.

## Reading the HTML report

The report opens with an Operational point table (single-line summary of variants, sample size, build, total duration) and a short "How to read this report" section explaining the paired design and bootstrap CIs.

The main sections, in order:

- **Headline result** — three coloured boxes for p50, p95, p99 latency delta (A minus B) with 95% bootstrap CIs. Green = positive delta with CI excluding zero (the expected sign).
- **Counter delta (MySQL `SHOW GLOBAL STATUS`)** — table of per-variant counter deltas. The `Com_select` row should show **A minus B** is exactly `+1.0 per alert` — allowlist adds exactly one SELECT per ingested alert, measured deterministically at the database.
- **Latency distribution** — aggregate CDF (log-x) for variant A vs variant B across all paired steady windows.
- **Per-pair stability** — p50 and p95 traces showing variant A consistently above variant B in each pair.
- **Per-pair detail** — table of per-pair latency percentiles and counter deltas.
- **Profile data** (only when `CAPTURE_PPROF=1`) — diff bar charts for CPU/block/mutex profiles, a CPU call graph SVG (diff inline plus per-variant in details), and links to the raw .pprof files for offline analysis with `go tool pprof`.
- **Notes** — methodology footnotes (steady-window analysis, p99 noise at small N, hardware/transport dependence).

## What the measurement is and isn't

**Establishes:**

- The allowlist lookup adds a measurable per-call median latency in the milliseconds range (not microseconds). The exact number depends on hardware, MySQL latency and pool pressure — your numbers will differ from ours, but the sign and order of magnitude should match.
- The allowlist lookup adds exactly +1 SELECT per ingested alert at the counter level, deterministically.

**Does not establish:**

- A specific incident in any production deployment. The stand is synthetic, not a recording of any operator's prod traffic.
- That the allowlist call is the dominant bottleneck under all conditions. The stand measures per-call overhead at a non-saturating rate; behaviour at saturation (small pools, high concurrency) is not characterised here.
- Universal numbers. Pool size, hardware, MySQL transport and load shape all affect the magnitude. The PR #4475 feature flag is an **opt-out**, meant for operators who don't use allowlist and don't want to pay the per-alert SELECT cost.

## Files

```
stand/
├── README.md                          # this file
├── docker-compose.yml                 # mysql + crowdsec (default); loadgen (control profile); prometheus/grafana/exporter (observability profile)
├── Dockerfile.crowdsec                # multi-stage build: v1.7.8 tag + patch
├── Dockerfile.loadgen                 # builds stand/loadgen
├── loadgen/                           # Go source for the open-loop alert generator
├── prometheus.yml
├── exporter.my.cnf
├── crowdsec/
│   ├── config.yaml                    # base config (with pool size)
│   ├── feature.yaml                   # active feature flags (swapped each variant)
│   ├── feature-variant-A.yaml         # vanilla
│   ├── feature-variant-B.yaml         # with disable_allowlist_ingestion
│   ├── acquis.yaml                    # LAPI-only mode (empty acquisition)
│   └── profiles.yaml                  # remediation profile
├── mysql/
│   └── init-exporter.sql              # mysqld_exporter user (observability)
├── grafana/
│   ├── provisioning/datasources/
│   ├── provisioning/dashboards/
│   └── dashboards/pr4475-effect.json  # live dashboard
├── scripts/
│   └── run.sh                         # the entry point
├── analysis/
│   └── report.py                      # HTML report generator
└── runs/                              # output (created on first run)
    └── <grid-id>/
        ├── pair-NN/arm-{A,B}/
        │   ├── meta.json
        │   ├── latency.jsonl          # per-attempt timings
        │   ├── metrics.jsonl          # 1s aggregate counters
        │   ├── status-before.txt
        │   ├── status-after.txt
        │   ├── loadgen.log
        │   └── *.pprof                # if CAPTURE_PPROF=1
        └── report.html
```

## Teardown

```bash
docker compose down -v
```

Wipes containers, volumes and the crowdsec image. Add `--profile observability --profile control` if you also brought up those profiles and want to clean them in one call.

## Troubleshooting

- **"crowdsec did not come up healthy"** — the first crowdsec image build is slow (~10 min). If it's stuck longer, run `docker compose logs crowdsec` to see what's wrong.
- **HTML report missing plots** — `run.sh` automatically creates a venv under `stand/.venv` and installs matplotlib + numpy on first run. If that fails (no python3 on PATH, no `venv` module), install Python 3.9+ and rerun.

## Provenance

- Built from the official upstream tag `v1.7.8` with one patch applied, `patches/disable-allowlist-ingestion.patch` (the feature-flag change only). A reviewer can read the patch in full instead of trusting a fork.
- Loadgen source code is in `./loadgen/` (open-loop: fixed-rate arrivals, no self-throttling when the server slows).
- Analysis methodology lives in this directory: `scripts/run.sh` for orchestration, `analysis/report.py` for the renderer.
