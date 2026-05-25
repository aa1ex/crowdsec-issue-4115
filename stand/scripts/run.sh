#!/usr/bin/env bash
# PR #4475 measurement runner: one-command reproducer.
#
# Usage:
#   ./run.sh                  # 3 pairs, ~25 min (plus first-run build ~10 min)
#   PAIRS=20 ./run.sh         # tight CIs, ~3-4 h
#   CAPTURE_PPROF=1 ./run.sh  # also scrape cpu/block/mutex/goroutine
#   OBSERVABILITY=1 ./run.sh  # also bring up prometheus + grafana

set -euo pipefail

PAIRS="${PAIRS:-3}"
WARMUP="${WARMUP:-30}"
STEADY="${STEADY:-180}"
DRAIN="${DRAIN:-30}"
RATE="${RATE:-10}"
BATCH="${BATCH:-10}"
DEADLINE="${DEADLINE:-5s}"
RETRIES="${RETRIES:-1}"
POOL_SIZE="${POOL_SIZE:-250}"
CAPTURE_PPROF="${CAPTURE_PPROF:-0}"
CPU_SAMPLE="${CPU_SAMPLE:-60}"
OBSERVABILITY="${OBSERVABILITY:-0}"
GRID_ID="${GRID_ID:-run-$(date -u +%Y%m%dT%H%M%SZ)}"

HERE="$(cd "$(dirname "$0")/.." && pwd)"
RUNS="$HERE/runs/$GRID_ID"
mkdir -p "$RUNS"

log() { echo "[run $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Capture host environment (OS, CPU model, core count, RAM, docker version)
# into env.json once per grid. No hostname or username — those would leak.
capture_env() {
    local f="$1"
    local os_name os_version cpu_model cpu_count mem_gb docker_version arch
    arch=$(uname -m)
    docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "?")
    if [[ "$(uname)" == "Darwin" ]]; then
        os_name="macOS"
        os_version=$(sw_vers -productVersion 2>/dev/null || uname -r)
        cpu_model=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "?")
        cpu_count=$(sysctl -n hw.ncpu 2>/dev/null || echo 0)
        local mem_bytes
        mem_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        mem_gb=$(awk -v b="$mem_bytes" 'BEGIN { printf "%.1f", b/1073741824 }')
    elif [[ "$(uname)" == "Linux" ]]; then
        os_name=$(awk -F= '/^NAME=/ {gsub(/"/,"",$2); print $2}' /etc/os-release 2>/dev/null || echo "Linux")
        os_version=$(awk -F= '/^VERSION_ID=/ {gsub(/"/,"",$2); print $2}' /etc/os-release 2>/dev/null || uname -r)
        cpu_model=$(awk -F: '/^model name/ {gsub(/^ */,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null || echo "?")
        cpu_count=$(nproc 2>/dev/null || echo 0)
        mem_gb=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
    else
        os_name=$(uname)
        os_version=$(uname -r)
        cpu_model="?"; cpu_count=0; mem_gb=0
    fi
    cat > "$f" <<EOF
{
  "os_name": $(printf '%s' "$os_name" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),
  "os_version": $(printf '%s' "$os_version" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),
  "arch": "$arch",
  "cpu_model": $(printf '%s' "$cpu_model" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),
  "cpu_count": $cpu_count,
  "mem_gb": $mem_gb,
  "docker_version": $(printf '%s' "$docker_version" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))')
}
EOF
}

# Push a vertical-line annotation into Grafana marking variant start/end.
# Best-effort: skips silently if Grafana is not running.
push_arm_annotation() {
    local ARM="$1"
    local KIND="$2"
    local TS_MS=$(($(date +%s) * 1000))
    curl -s -m 2 -u admin:admin -H "Content-Type: application/json" \
        -X POST http://127.0.0.1:3000/api/annotations \
        -d "{\"time\": $TS_MS, \"text\": \"variant $ARM $KIND\", \"tags\": [\"variant-$ARM\", \"$KIND\"]}" \
        >/dev/null 2>&1 || true
}

cd "$HERE"

# Pre-flight: confirm the report renderer can be invoked at the end. Doing this
# up front prevents losing 3-4 hours of measurement to a missing python package.
preflight_python() {
    if ! command -v python3 >/dev/null; then
        echo "ERROR: python3 not found on PATH. Install Python 3.9+ before running." >&2
        return 1
    fi
    if python3 -c "import matplotlib, numpy" 2>/dev/null; then
        return 0
    fi
    if python3 -m ensurepip --version >/dev/null 2>&1; then
        return 0
    fi
    cat >&2 <<EOF
ERROR: python3 is present but cannot create a virtualenv (ensurepip missing),
       and matplotlib/numpy are not installed system-wide.

The report-generation step at the end of the run will fail. Install one of:

    Debian/Ubuntu:   sudo apt install python3-venv     # or python3.NN-venv
    Fedora/RHEL:     sudo dnf install python3-pip      # ensurepip usually bundled
    Alpine:          sudo apk add py3-pip py3-virtualenv
    macOS:           python3 from xcode-select / Homebrew works out of the box

Or pre-install the deps system-wide and re-run:
    pip3 install matplotlib numpy   # may need --break-system-packages on Ubuntu 23.04+
EOF
    return 1
}
preflight_python || exit 1

capture_env "$RUNS/env.json"

if [[ "$OBSERVABILITY" == "1" ]]; then
    export COMPOSE_PROFILES=observability
fi

log "starting docker-compose stack (observability=$OBSERVABILITY)"
# Build named services explicitly: loadgen sits behind profile "control" so
# `docker compose build` alone skips it, but run_arm invokes it via `docker run`.
docker compose build crowdsec loadgen
docker compose up -d
log "waiting for crowdsec /v1/watchers to respond"
for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/v1/watchers 2>/dev/null || true)
    code="${code:-000}"
    if [[ "$code" =~ ^[0-9]{3}$ && "$code" != "000" ]] && (( code < 500 )); then
        log "crowdsec up (HTTP $code after ${i}s)"
        break
    fi
    sleep 1
done

if [[ "$POOL_SIZE" != "250" ]]; then
    log "applying max_open_conns=$POOL_SIZE"
    sed -i.bak "s/^  max_open_conns: .*/  max_open_conns: $POOL_SIZE/" \
        "$HERE/crowdsec/config.yaml"
    rm -f "$HERE/crowdsec/config.yaml.bak"
    docker compose restart crowdsec
    sleep 5
fi

log "registering loadgen machine"
docker exec pr4475-stand-lapi cscli machines add loadgen --password loadgenpass --force -f - >/dev/null 2>&1 || true

run_arm() {
    local ARM="$1"
    local PAIR_DIR="$2"
    local OUT="$PAIR_DIR/arm-$ARM"
    mkdir -p "$OUT"

    log "variant $ARM: select feature flag"
    cp "$HERE/crowdsec/feature-variant-$ARM.yaml" "$HERE/crowdsec/feature.yaml"
    docker compose restart crowdsec >/dev/null
    sleep 6
    for i in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/v1/watchers 2>/dev/null || echo 000)
        if [[ "$code" != 000 && "$code" -lt 500 ]]; then break; fi
        sleep 1
    done

    # Clean slate per variant: drop alerts/decisions accumulated from prior variant.
    docker exec pr4475-stand-mysql mysql -u root -prootpass --silent crowdsec -e 'DELETE FROM alerts; DELETE FROM decisions;' >/dev/null 2>&1

    docker exec pr4475-stand-mysql mysql -u root -prootpass --silent crowdsec \
        -e 'SHOW GLOBAL STATUS;' > "$OUT/status-before.txt"

    START_UNIX=$(date +%s)
    CS_INFO=$(curl -s http://127.0.0.1:6060/metrics 2>/dev/null | grep -E '^cs_info' | head -1 || echo "")
    cat > "$OUT/meta.json" <<EOF
{
  "arm": "$ARM", "start_unix": $START_UNIX,
  "warmup_s": $WARMUP, "steady_s": $STEADY, "drain_s": $DRAIN, "cpu_sample_s": 60,
  "rate": $RATE, "deadline": "$DEADLINE", "batch": $BATCH, "retries": $RETRIES,
  "pool_size": $POOL_SIZE,
  "lapi_endpoint": "http://crowdsec:8080",
  "cs_info": $(printf '%s' "$CS_INFO" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))')
}
EOF

    TOTAL_DUR=$((WARMUP + STEADY + DRAIN))
    log "variant $ARM: start loadgen (${TOTAL_DUR}s @ ${RATE}rps)"
    push_arm_annotation "$ARM" "start"

    # pprof captures scheduled in background, scoped to the steady window.
    # /debug/pprof/* is live on 127.0.0.1:6060 (prometheus.enabled in config.yaml).
    PPROF_PIDS=()
    if [[ "$CAPTURE_PPROF" == "1" ]]; then
        log "variant $ARM: scheduling pprof captures (cpu ${CPU_SAMPLE}s, block/mutex ${STEADY}s)"
        # Each curl gets a max-time slightly above its expected duration so a stalled
        # crowdsec cannot wedge the parent's `wait` indefinitely.
        ( sleep "$WARMUP" && curl -s -m 30 -o "$OUT/goroutine-start.pprof" \
            http://127.0.0.1:6060/debug/pprof/goroutine ) & PPROF_PIDS+=($!)
        ( sleep "$WARMUP" && curl -s -m $((STEADY + 30)) -o "$OUT/block.pprof" \
            "http://127.0.0.1:6060/debug/pprof/block?seconds=$STEADY" ) & PPROF_PIDS+=($!)
        ( sleep "$WARMUP" && curl -s -m $((STEADY + 30)) -o "$OUT/mutex.pprof" \
            "http://127.0.0.1:6060/debug/pprof/mutex?seconds=$STEADY" ) & PPROF_PIDS+=($!)
        if (( STEADY > CPU_SAMPLE )); then
            CPU_DELAY=$(( WARMUP + (STEADY - CPU_SAMPLE) / 2 ))
        else
            CPU_DELAY=$WARMUP
            CPU_SAMPLE=$STEADY
        fi
        ( sleep "$CPU_DELAY" && curl -s -m $((CPU_SAMPLE + 30)) -o "$OUT/cpu.pprof" \
            "http://127.0.0.1:6060/debug/pprof/profile?seconds=$CPU_SAMPLE" ) & PPROF_PIDS+=($!)
        ( sleep $((WARMUP + STEADY)) && curl -s -m 30 -o "$OUT/goroutine-end.pprof" \
            http://127.0.0.1:6060/debug/pprof/goroutine ) & PPROF_PIDS+=($!)
    fi

    docker run --rm \
        --name pr4475-qs-loadgen-$$ \
        --network pr4475-stand_default \
        -v "$OUT:/out" \
        pr4475-stand-loadgen:latest \
        -target http://crowdsec:8080 \
        -rate "$RATE" \
        -duration "${TOTAL_DUR}s" \
        -deadline "$DEADLINE" \
        -batch "$BATCH" \
        -retries "$RETRIES" \
        -metrics /out/metrics.jsonl \
        -latency /out/latency.jsonl > "$OUT/loadgen.log" 2>&1

    # Wait for any in-flight pprof scrapes (block/mutex/cpu finish when their window ends).
    if (( ${#PPROF_PIDS[@]} > 0 )); then
        wait "${PPROF_PIDS[@]}" 2>/dev/null || true
    fi

    docker exec pr4475-stand-mysql mysql -u root -prootpass --silent crowdsec \
        -e 'SHOW GLOBAL STATUS;' > "$OUT/status-after.txt"

    END_UNIX=$(date +%s)
    echo "$END_UNIX" > "$OUT/.done"
    push_arm_annotation "$ARM" "end"
    log "variant $ARM: done in $((END_UNIX-START_UNIX))s"
}

log "running $PAIRS pairs (A/B back-to-back, randomized order) at pool_size=$POOL_SIZE"
for P in $(seq 1 "$PAIRS"); do
    PAIR_DIR="$RUNS/pair-$(printf '%02d' $P)"
    mkdir -p "$PAIR_DIR"

    FIRST=$(python3 -c "
import hashlib
h = hashlib.sha256(b'qs-$GRID_ID-$P').digest()
print('A' if h[0] % 2 == 0 else 'B')
")
    SECOND=$([[ "$FIRST" == "A" ]] && echo "B" || echo "A")

    log "pair $P: $FIRST then $SECOND"
    run_arm "$FIRST"  "$PAIR_DIR"
    run_arm "$SECOND" "$PAIR_DIR"
done

log "generating HTML report"
REPORT="$RUNS/report.html"
if command -v python3 >/dev/null && python3 -c "import matplotlib, numpy" 2>/dev/null; then
    python3 "$HERE/analysis/report.py" "$RUNS" --out "$REPORT"
else
    # PEP 668 ("externally managed") refuses plain pip3 install --user on modern
    # macOS/Linux Python. Fall back to a cached venv under $HERE/.venv.
    VENV="$HERE/.venv"
    if [[ ! -x "$VENV/bin/python" ]]; then
        log "creating analysis venv at $VENV"
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install --quiet matplotlib numpy
    fi
    "$VENV/bin/python" "$HERE/analysis/report.py" "$RUNS" --out "$REPORT"
fi

echo
echo "============================================================"
echo " PR #4475 run done."
echo "============================================================"
echo "  Pairs run:      $PAIRS"
echo "  Pool size:      $POOL_SIZE"
echo "  Output:         $RUNS"
echo "  HTML report:    $REPORT"
if [[ "$OBSERVABILITY" == "1" ]]; then
    echo "  Grafana:        http://127.0.0.1:3000 (admin/admin or anonymous)"
    echo "  Prometheus:     http://127.0.0.1:9090"
    echo "  Dashboard:      Folder 'PR4475' -> 'allowlist call effect on LAPI'"
fi
echo "============================================================"
echo
echo "Open the HTML report in your browser:"
echo "  file://$REPORT"
echo
echo "To tear down the stack:"
echo "  cd $HERE && docker compose down -v"
echo
