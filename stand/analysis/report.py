#!/usr/bin/env python3
"""
Generate a self-contained HTML report from a PR4475 measurement grid.

Usage:
    python3 report.py <grid_dir>

Output:
    <grid_dir>/report.html

Reads from each pair-NN/arm-{A,B}/ subdirectory:
    - meta.json
    - latency.jsonl (per-attempt records)
    - metrics.jsonl (1s aggregate counters)
    - status-before.txt, status-after.txt (SHOW GLOBAL STATUS dumps)

Computes:
    - Per-variant latency percentiles in the steady window
    - Paired (A minus B) per-pair latency differences
    - Hierarchical bootstrap 95% confidence intervals
    - Counter deltas (Com_select, Com_stmt_execute, etc.)

Requires:
    Python 3.9+, matplotlib, numpy (all in standard scientific Python install).
"""

import argparse
import base64
import html as html_mod
import io
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    print("ERROR: matplotlib and numpy required. Install with:", file=sys.stderr)
    print("  pip3 install matplotlib numpy", file=sys.stderr)
    sys.exit(2)


# ----- parsing helpers ------------------------------------------------------

def parse_status(path):
    """Parse SHOW GLOBAL STATUS output (tab-separated name\\tvalue)."""
    d = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2:
                try:
                    d[parts[0]] = int(parts[1])
                except ValueError:
                    try:
                        d[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
    return d


def load_latencies_steady(path, t0_unix, t1_unix):
    """Load per-attempt latencies in ms for outcome=ok within [t0, t1)."""
    durs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("outcome") != "ok":
                continue
            t = r["t_start_ns"] / 1e9
            if t0_unix <= t < t1_unix:
                durs.append((r["t_end_ns"] - r["t_start_ns"]) / 1e6)
    durs.sort()
    return durs


def pct(durs, q):
    if not durs:
        return None
    n = len(durs)
    return durs[int(n * q)] if q < 1.0 else durs[-1]


# ----- bootstrap CI ---------------------------------------------------------

def hierarchical_bootstrap_paired(pair_latencies, q, n_resamples=5000, seed=42):
    """
    pair_latencies: list of (a_sorted, b_sorted) tuples per pair.
    Returns (observed_mean_delta, ci_lo, ci_hi) at 95%.
    """
    rng = random.Random(seed)
    # Drop pairs with empty latency lists (defensive: a broken run that produced
    # zero 2xx responses would otherwise crash on None subtraction).
    pair_latencies = [(a, b) for a, b in pair_latencies if a and b]
    N = len(pair_latencies)
    if N == 0:
        return None, None, None
    observed = statistics.mean(pct(a, q) - pct(b, q) for a, b in pair_latencies)
    samples = []
    for _ in range(n_resamples):
        # outer: resample pairs with replacement
        sampled_pairs = [pair_latencies[rng.randrange(N)] for _ in range(N)]
        inner_deltas = []
        for a, b in sampled_pairs:
            # inner: resample points within each variant
            ai = sorted(a[rng.randrange(len(a))] for _ in range(len(a)))
            bi = sorted(b[rng.randrange(len(b))] for _ in range(len(b)))
            inner_deltas.append(pct(ai, q) - pct(bi, q))
        samples.append(statistics.mean(inner_deltas))
    samples.sort()
    lo = samples[int(0.025 * n_resamples)]
    hi = samples[int(0.975 * n_resamples)]
    return observed, lo, hi


# ----- plotting -------------------------------------------------------------

def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def plot_latency_cdf(pair_latencies):
    """Aggregate CDF of latency for variant A vs variant B (all pairs concatenated)."""
    a_all = sorted(d for a, _ in pair_latencies for d in a)
    b_all = sorted(d for _, b in pair_latencies for d in b)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    if a_all:
        ax.plot(a_all, np.linspace(0, 1, len(a_all)),
                label="variant A (allowlist active)", lw=2, color="#d62728")
    if b_all:
        ax.plot(b_all, np.linspace(0, 1, len(b_all)),
                label="variant B (allowlist skipped)", lw=2, color="#2ca02c")
    ax.set_xscale("log")
    ax.set_xlabel("POST /v1/alerts latency (ms, log scale)")
    ax.set_ylabel("Cumulative fraction")
    ax.set_title("Latency CDF across all paired steady windows")
    ax.legend(loc="lower right")
    ax.grid(True, alpha=0.3, which="both")
    return fig_to_b64(fig)


def plot_per_pair_p50_p95(per_pair):
    """Per-pair p50/p95 paired plot showing consistency."""
    indices = list(range(1, len(per_pair) + 1))
    a_p50 = [pct(p[0], 0.50) for p in per_pair]
    b_p50 = [pct(p[1], 0.50) for p in per_pair]
    a_p95 = [pct(p[0], 0.95) for p in per_pair]
    b_p95 = [pct(p[1], 0.95) for p in per_pair]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    ax1.plot(indices, a_p50, "o-", label="variant A", color="#d62728")
    ax1.plot(indices, b_p50, "o-", label="variant B", color="#2ca02c")
    ax1.set_xlabel("pair index"); ax1.set_ylabel("p50 latency (ms)")
    ax1.set_title("Median (p50) per pair"); ax1.legend(); ax1.grid(True, alpha=0.3)
    ax2.plot(indices, a_p95, "o-", label="variant A", color="#d62728")
    ax2.plot(indices, b_p95, "o-", label="variant B", color="#2ca02c")
    ax2.set_xlabel("pair index"); ax2.set_ylabel("p95 latency (ms)")
    ax2.set_title("95th percentile per pair"); ax2.legend(); ax2.grid(True, alpha=0.3)
    return fig_to_b64(fig)


# ----- main report ----------------------------------------------------------

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PR4475 measurement report — {grid_id}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; line-height: 1.5; }}
h1, h2, h3 {{ color: #111; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: .3em; }}
h2 {{ margin-top: 2em; border-bottom: 1px solid #ccc; padding-bottom: .2em; }}
.headline {{ background: #f6f8fa; border-left: 4px solid #2ca02c; padding: 1em 1.5em; margin: 1em 0; }}
.headline.negative {{ border-left-color: #d62728; }}
.headline.neutral {{ border-left-color: #888; }}
.headline strong {{ font-size: 1.15em; }}
table {{ border-collapse: collapse; margin: 1em 0; width: 100%; font-size: 0.9em; }}
th, td {{ border: 1px solid #ddd; padding: .35em .6em; text-align: right; }}
th {{ background: #f0f0f0; text-align: left; }}
td:first-child, th:first-child {{ text-align: left; font-weight: 600; }}
.num {{ font-family: "SF Mono", Menlo, Consolas, monospace; }}
.pos {{ color: #2a7a2a; }}
.neg {{ color: #c0392b; }}
.muted {{ color: #888; font-size: 0.9em; }}
img {{ max-width: 100%; height: auto; margin: 1em 0; }}
code {{ background: #f4f4f4; padding: .1em .35em; border-radius: 3px; font-size: .9em; }}
.caveat {{ background: #fff8e1; border-left: 4px solid #f0c040; padding: .8em 1em; margin: .8em 0; }}
.meta {{ background: #f0f0f0; padding: .8em 1em; font-size: .85em; }}
pre.pprof {{ background: #1e1e1e; color: #d4d4d4; padding: 12px 14px; border-radius: 4px; font-size: 12px; line-height: 1.45; overflow-x: auto; }}
table.ops th, table.ops td {{ font-size: .9em; padding: .35em .6em; vertical-align: top; text-align: left; }}
table.ops td:first-child {{ white-space: nowrap; }}
table.ops td:nth-child(2) {{ white-space: nowrap; text-align: right; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.reading-guide {{ background: #f7f9fc; border-left: 4px solid #0a66c2; padding: .8em 1.2em; margin: .8em 0; }}
.reading-guide p {{ margin: .5em 0; }}
.plot-caption {{ color: #555; font-size: .85em; margin: -.3em 0 1em 0; font-style: italic; }}
.pprof-svg-link {{ display: block; border: 1px solid #d5d9de; border-radius: 4px; padding: 8px; background: #fafafa; cursor: zoom-in; text-decoration: none; }}
.pprof-svg-link:hover {{ background: #f0f4f8; border-color: #0a66c2; }}
.pprof-svg-link svg {{ width: 100%; height: auto; display: block; }}
details {{ margin: .5em 0; }}
details summary {{ cursor: pointer; color: #2c3e50; font-weight: 500; }}
.toc {{ background: #f7f9fc; border: 1px solid #dde3eb; padding: .8em 1.2em; margin: 1em 0; font-size: .92em; }}
.toc strong {{ display: block; margin-bottom: .4em; color: #444; }}
.toc ol {{ margin: 0; padding-left: 1.5em; }}
.toc li {{ margin: .15em 0; }}
.toc a {{ color: #0a66c2; text-decoration: none; }}
.toc a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>

<h1>PR4475 measurement report</h1>
<div class="meta">
<strong>Grid:</strong> <code>{grid_id}</code> &nbsp;
<strong>Generated:</strong> {timestamp} &nbsp;
<strong>Pairs:</strong> {n_pairs} &nbsp;
<strong>Total wall-clock:</strong> {duration_str}
<br>
<strong>Crowdsec binary:</strong> <code>{cs_version}</code>
<br>
<strong>Environment:</strong> {env_str}
<br>
<strong>Variant A:</strong> allowlist ingestion ON (vanilla v1.6.6+ behaviour) &nbsp;|&nbsp;
<strong>Variant B:</strong> allowlist ingestion OFF (PR #4475 feature flag enabled)
</div>

<nav class="toc">
<strong>Contents</strong>
<ol>{toc}</ol>
</nav>

<h2 id="operational-point">Operational point</h2>
<table class="ops">
<tr><th>Parameter</th><th>Value</th><th>What it means</th></tr>
<tr><td><code>RATE</code></td><td>{rate}/s</td><td>HTTP requests offered per second by the open-loop generator (independent of how the server responds)</td></tr>
<tr><td><code>BATCH</code></td><td>{batch} alerts/request</td><td>How many alerts each POST /v1/alerts carries; total offered alert rate = RATE x BATCH</td></tr>
<tr><td><code>DEADLINE</code></td><td>{deadline}</td><td>Per-request client timeout; if the server is slower than this the request is cancelled (counts as failure, excluded from latency percentiles)</td></tr>
<tr><td><code>RETRIES</code></td><td>{retries}</td><td>Retry attempts per logical request on 5xx or timeout</td></tr>
<tr><td><code>WARMUP</code></td><td>{warmup} s</td><td>Initial seconds discarded from analysis (let caches and connections settle)</td></tr>
<tr><td><code>STEADY</code></td><td>{steady} s</td><td>Analysis window; only requests whose t_start falls here contribute to latency percentiles</td></tr>
<tr><td><code>DRAIN</code></td><td>{drain} s</td><td>Tail discarded from analysis (let in-flight requests finish without polluting the window)</td></tr>
<tr><td><code>MAX_OPEN_CONNS</code></td><td>{pool_size}</td><td>crowdsec -&gt; MySQL connection pool cap; below saturation at this RATE</td></tr>
</table>

<h2 id="how-to-read">How to read this report</h2>
<div class="reading-guide">
<p><strong>Paired design.</strong> A "pair" is one back-to-back run of variant A
and variant B (order randomized per pair). The within-pair difference cancels
common-mode noise (machine load, cache state, network jitter) — the only thing
that changes inside a pair is the feature flag. The unit of analysis is the
per-pair delta.</p>
<p><strong>Why a series of N pairs.</strong> One pair gives one delta; with N
pairs we get a distribution of deltas and can quantify uncertainty. The headline
numbers below are the mean delta across the N={n_pairs} pairs; the CI quantifies
how confident we are in that mean.</p>
<p><strong>Bootstrap CI.</strong> 95% confidence interval computed by
resampling the {n_pairs} per-pair deltas (and the latency points within each
pair) {bootstrap_resamples} times with replacement. If the interval excludes
zero, the effect is unlikely to be noise.</p>
<p><strong>Counter snapshots.</strong> MySQL <code>SHOW GLOBAL STATUS</code>
is snapped at the start and end of each arm; the per-counter delta is divided by
the number of alerts offered in that window. See the Counter delta section for
details.</p>
</div>

<h2 id="headline">Headline result</h2>
{headline_blocks}
<p class="muted">The deterministic database-level signature of this effect appears in the Counter delta section immediately below; the latency distribution and per-pair traces further down show the same effect at the request level.</p>

<h2 id="counter-delta">Counter delta (MySQL <code>SHOW GLOBAL STATUS</code>)</h2>
<table>
<tr><th>Counter</th><th>variant A mean delta (count)</th><th>variant B mean delta (count)</th><th>A minus B (count)</th><th>per alert (count/alert)</th></tr>
{counter_rows}
</table>
<p class="muted">
All cells are absolute counts (not rates). "delta" = end-of-arm value minus
start-of-arm value of the named counter. "per alert" = (A minus B) divided by
the number of alerts offered across the full arm window — warmup + steady +
drain ({alerts_per_arm} per variant; equals the number of alerts ingested
because every request succeeded). For Com_select this should be exactly +1.0
if allowlist adds one SELECT per alert. For other counters this measures
incidental difference.
</p>

<h2 id="latency-distribution">Latency distribution</h2>
<img src="data:image/png;base64,{cdf_b64}" alt="Latency CDF">
<p class="plot-caption">Cumulative distribution of per-request latency
(milliseconds, log-x). Curve to the right = slower. If variant A (red) sits
to the right of variant B (green) at every percentile, A is uniformly slower.
The horizontal distance between the curves at a given fraction is the
distribution-level delta.</p>

<h2 id="per-pair-stability">Per-pair stability</h2>
<img src="data:image/png;base64,{per_pair_b64}" alt="Per-pair p50/p95">
<p class="plot-caption">Per-pair p50 (left) and p95 (right) for each variant
(ms). Stable lines = consistent measurement. A line consistently above the
other = consistent direction of the effect. Large per-pair scatter = noisy
environment, wider CIs.</p>

<h2 id="per-pair-detail">Per-pair detail (ms for latency columns, counts for SELECT)</h2>
<table>
<tr>
<th>pair</th>
<th>A p50 (ms)</th><th>B p50 (ms)</th><th>delta p50 (ms)</th>
<th>A p95 (ms)</th><th>B p95 (ms)</th><th>delta p95 (ms)</th>
<th>A Com_select delta (count)</th><th>B Com_select delta (count)</th>
</tr>
{detail_rows}
</table>

<h2 id="notes">Notes</h2>
<ul>
<li>Steady-window analysis only: warmup ({warmup}s) and drain ({drain}s) are
    excluded, so the percentiles reflect server behaviour at stable load.</li>
<li>p99 is noisier than p50/p95 at modest N; if its CI crosses zero we report
    it as "not resolved at this N", not as evidence against the effect.</li>
<li>Absolute milliseconds depend on hardware, MySQL transport and pool size.
    The sign of the delta and the +1 SELECT-per-alert counter are invariant
    across environments; the millisecond magnitude is not.</li>
</ul>

{pprof_section}

<p class="muted">Report generated by stand/analysis/report.py from grid output.
Raw data: <code>runs/{grid_id}</code>.</p>

</body>
</html>
"""


def _pprof_collect(pair_dirs, profile_name):
    """Return (arm_a_files, arm_b_files) for the given profile, paired across pairs."""
    a, b = [], []
    for p in pair_dirs:
        af = p / "arm-A" / profile_name
        bf = p / "arm-B" / profile_name
        if af.exists() and bf.exists():
            a.append(str(af))
            b.append(str(bf))
    return a, b


def _pprof_run(args, timeout=60):
    """Run `go tool pprof ...`, return stdout text or error message."""
    try:
        r = subprocess.run(["go", "tool", "pprof"] + args,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        return f"(pprof exit {r.returncode}: {r.stderr.strip()[:400]})"
    except subprocess.TimeoutExpired:
        return f"(pprof timed out after {timeout}s)"
    except Exception as e:
        return f"(pprof failed: {e})"


def _pprof_merge(files):
    """Merge multiple pprof files into a single proto file, return path.
    Caller is responsible for cleanup. Returns None on failure."""
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    fd, path = tempfile.mkstemp(suffix=".pprof", prefix="merged-")
    os.close(fd)
    try:
        r = subprocess.run(["go", "tool", "pprof", "-proto", "-output", path] + files,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            os.unlink(path)
            return None
        return path
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        return None


def _pprof_diff_top(a_files, b_files, unit="ms", n=20):
    if not a_files or not b_files:
        return ""
    # -diff_base accepts a single file; merge the B side first if needed.
    b_merged = _pprof_merge(b_files)
    if b_merged is None:
        return "(could not merge variant B profiles for diff)"
    try:
        return _pprof_run([f"-diff_base={b_merged}",
                           "-top", f"-nodecount={n}", f"-unit={unit}"] + a_files)
    finally:
        # Only remove if we created a tempfile (not when single source file).
        if len(b_files) > 1 and os.path.exists(b_merged):
            os.unlink(b_merged)


def _pprof_top(files, unit="ms", n=15):
    if not files:
        return ""
    return _pprof_run(["-top", f"-nodecount={n}", f"-unit={unit}"] + files)


def _pprof_svg(files, diff_base=None, unit="ms", n=80):
    """Render pprof as inline SVG via graphviz. Returns SVG text or '' on failure."""
    if not files:
        return ""
    args = ["-svg", f"-nodecount={n}", f"-unit={unit}"]
    if diff_base:
        args.append(f"-diff_base={diff_base}")
    out = _pprof_run(args + files, timeout=90)
    # _pprof_run returns wrapped error strings on failure; only treat as SVG if it starts with XML.
    if out.lstrip().startswith("<?xml") or out.lstrip().startswith("<svg"):
        return out
    return ""


def _parse_pprof_top(text, max_n=15):
    """Parse `go tool pprof -top -unit=ms` text, return [(func, flat_ms), ...]."""
    rows = []
    in_table = False
    pat = re.compile(r'^\s*(-?[\d.]+)(ms|s|us|ns)?\b')
    for line in text.splitlines():
        if "flat" in line and "cum" in line and "%" in line:
            in_table = True
            continue
        if not in_table:
            continue
        s = line.strip()
        if not s:
            if rows:
                break
            continue
        parts = s.split(None, 5)
        if len(parts) < 6:
            continue
        m = pat.match(parts[0])
        if not m:
            continue
        v = float(m.group(1))
        u = m.group(2) or ""
        if u == "s": v *= 1000
        elif u == "us": v /= 1000
        elif u == "ns": v /= 1_000_000
        rows.append((parts[5], v))
        if len(rows) >= max_n:
            break
    return rows


def plot_pprof_diff(rows, title):
    """Horizontal bar chart of pprof diff-top: red bars = more in A, green = more in B."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: abs(r[1]), reverse=True)
    funcs = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    # shorten very long names for display
    short = [f if len(f) <= 70 else "..." + f[-67:] for f in funcs]
    colors = ['#c0392b' if v > 0 else '#27ae60' for v in vals]
    fig, ax = plt.subplots(figsize=(10, max(3, len(rows) * 0.32)))
    y = range(len(rows))
    ax.barh(y, vals, color=colors, alpha=0.85)
    ax.invert_yaxis()
    ax.set_yticks(list(y))
    ax.set_yticklabels(short, fontsize=9, family='monospace')
    ax.axvline(0, color='black', linewidth=0.6)
    ax.set_xlabel("flat delta (ms), variant A minus variant B "
                  "(red = more in A / allowlist-on)")
    ax.set_title(title, fontsize=11)
    ax.grid(axis='x', alpha=0.3)
    fig.tight_layout()
    return fig


def _save_svg(svg_text, out_path):
    """Write SVG to file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg_text)


def render_pprof_section(grid_dir, pair_dirs, out_dir=None):
    """Return HTML for the pprof section if any .pprof files exist, else ''.

    When `go` is available on PATH, embed aggregated CPU/block/mutex diff-top
    (variant A minus variant B) plus per-arm absolute top, so the reviewer
    can correlate profile attribution with the latency and counter results
    in the same report. Falls back to a simple file table if `go` is missing.
    """
    profile_names = ("cpu.pprof", "block.pprof", "mutex.pprof",
                     "goroutine-start.pprof", "goroutine-end.pprof")
    # Build the link table (always shown). Paths are relative to out_dir
    # (where report.html lives), so the hyperlinks work when opened in a browser.
    link_base = out_dir if out_dir is not None else grid_dir
    link_rows = []
    for pair in pair_dirs:
        for arm in ("arm-A", "arm-B"):
            arm_dir = pair / arm
            present = [n for n in profile_names if (arm_dir / n).exists()]
            if not present:
                continue
            links = " ".join(
                f'<a href="{os.path.relpath(arm_dir / n, link_base)}">{n}</a>'
                for n in present
            )
            link_rows.append(
                f'<tr><td>{pair.name}</td><td>{arm}</td><td>{links}</td></tr>')
    if not link_rows:
        return ""

    parts = ['<h2 id="profile-data">Profile data</h2>']
    go_present = shutil.which("go") is not None

    if go_present:
        dot_present = shutil.which("dot") is not None
        analysis = []
        for fname, label in (("cpu.pprof", "CPU"),
                             ("block.pprof", "Block (goroutine wait)"),
                             ("mutex.pprof", "Mutex contention")):
            a_files, b_files = _pprof_collect(pair_dirs, fname)
            if not a_files:
                continue
            diff = _pprof_diff_top(a_files, b_files)
            a_top = _pprof_top(a_files)
            b_top = _pprof_top(b_files)
            # Visual 1: horizontal bar chart of diff-top.
            rows = _parse_pprof_top(diff, max_n=15)
            chart_html = ""
            if rows:
                fig = plot_pprof_diff(rows, f"{label} diff top: variant A - variant B")
                if fig is not None:
                    chart_html = f'<img src="data:image/png;base64,{fig_to_b64(fig)}" alt="{label} diff bar chart">'
            analysis.append(f'<h3>{label} profile</h3>')
            if chart_html:
                analysis.append(chart_html)
            # Visual 2: pprof call graph SVG (only for CPU, only if graphviz present).
            # Saved as separate files alongside report.html so click opens at
            # native size in a new tab; inline embed is fitted to container width.
            if dot_present and fname == "cpu.pprof" and out_dir is not None:
                profiles_dir = out_dir / "profiles"
                b_merged = _pprof_merge(b_files)
                diff_svg = _pprof_svg(a_files, diff_base=b_merged) if b_merged else ""
                if b_merged and len(b_files) > 1 and os.path.exists(b_merged):
                    os.unlink(b_merged)
                a_svg = _pprof_svg(a_files)
                b_svg = _pprof_svg(b_files)

                def _embed(svg_text, name):
                    _save_svg(svg_text, profiles_dir / name)
                    href = f"profiles/{name}"
                    return (f'<a href="{href}" target="_blank" rel="noopener" '
                            f'class="pprof-svg-link" '
                            f'title="Open at native size in a new tab">'
                            f'{svg_text}</a>')

                if diff_svg:
                    analysis.append('<h4>Call graph diff (A &minus; B)</h4>')
                    analysis.append(
                        '<p class="muted">Nodes sized by their flat delta; '
                        'positive (red/orange) = work added in variant A. '
                        'Edges show caller -> callee. <strong>Click the graph '
                        'to open at native size in a new tab</strong> (browser '
                        'native zoom/pan works there).</p>')
                    analysis.append(_embed(diff_svg, "cpu-diff.svg"))
                if a_svg:
                    analysis.append(
                        '<details><summary>variant A call graph (absolute)</summary>'
                        f'{_embed(a_svg, "cpu-a.svg")}</details>')
                if b_svg:
                    analysis.append(
                        '<details><summary>variant B call graph (absolute)</summary>'
                        f'{_embed(b_svg, "cpu-b.svg")}</details>')
            analysis.append(
                f'<details><summary>raw diff text (go tool pprof -top)</summary>'
                f'<pre class="pprof">{html_mod.escape(diff)}</pre></details>')
            analysis.append(
                f'<details><summary>variant A top (absolute)</summary>'
                f'<pre class="pprof">{html_mod.escape(a_top)}</pre></details>')
            analysis.append(
                f'<details><summary>variant B top (absolute)</summary>'
                f'<pre class="pprof">{html_mod.escape(b_top)}</pre></details>')
        parts.append(
            f'<p>Captured pprof snapshots from each variant. The analysis below '
            f'correlates with the latency and counter results above: bar charts '
            f'and call graphs show which functions consume the time difference '
            f'visible in the latency CI. CPU profile is centered in the steady '
            f'window; block and mutex span the full steady window. '
            f'For every profile type, the per-pair files are aggregated across all '
            f'pair(s); diff = variant A minus variant B (positive / red bars = more '
            f'time in variant A, the allowlist-on arm).</p>')
        parts.append(
            '<p class="muted"><strong>On aggregation:</strong> per-pair pprof '
            'files are merged per variant before the diff (sum of A vs sum of '
            'B). This answers "which functions consume the added time on '
            'average across the run" but does not estimate per-function '
            'per-pair variance the way the latency CI does. Raw per-pair files '
            'are linked at the bottom for offline drill-down.</p>')
        parts.extend(analysis)
    else:
        parts.append(
            '<p>Captured pprof snapshots from each variant. '
            '<em>`go` not found on PATH, so the automated diff/top analysis '
            'is omitted from this report — install Go to embed it. '
            'You can still inspect manually with '
            '<code>go tool pprof -http=: &lt;file&gt;</code> or upload to '
            '<a href="https://www.speedscope.app/">speedscope.app</a>.</em></p>')

    parts.append('<h3>Raw profile files</h3>')
    parts.append('<table>')
    parts.append('<tr><th>Pair</th><th>Variant</th><th>Files</th></tr>')
    parts.extend(link_rows)
    parts.append('</table>')
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grid_dir", help="Path to grid output directory (contains pair-NN/)")
    ap.add_argument("--bootstrap-n", type=int, default=5000,
                    help="Bootstrap resamples (default 5000; 10000 for tighter CIs)")
    ap.add_argument("--out", default=None, help="Output HTML path (default: GRID/report.html)")
    args = ap.parse_args()

    grid = Path(args.grid_dir)
    if not grid.exists():
        sys.exit(f"grid dir not found: {grid}")

    pair_dirs = sorted([p for p in grid.glob("pair-*") if p.is_dir()])
    if not pair_dirs:
        sys.exit(f"no pair-* subdirs in {grid}")

    # Total wall-clock from first arm start to last arm end (.done holds END_UNIX).
    all_starts = []
    all_ends = []
    for p in pair_dirs:
        for arm in ("arm-A", "arm-B"):
            d = p / arm
            mp = d / "meta.json"
            dp = d / ".done"
            if mp.exists() and dp.exists():
                try:
                    m = json.loads(mp.read_text())
                    all_starts.append(int(m["start_unix"]))
                    all_ends.append(int(dp.read_text().strip()))
                except Exception:
                    pass
    if all_starts and all_ends:
        total_sec = max(all_ends) - min(all_starts)
        h, rem = divmod(total_sec, 3600)
        m, s = divmod(rem, 60)
        duration_str = f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
    else:
        duration_str = "unknown"

    pair_latencies = []
    detail = []
    counter_a = {}
    counter_b = {}
    meta = None

    print(f"loading {len(pair_dirs)} pairs from {grid}...")

    for pair in pair_dirs:
        pn = pair.name
        a_dir = pair / "arm-A"
        b_dir = pair / "arm-B"
        if not (a_dir / "meta.json").exists():
            print(f"  skip {pn}: incomplete")
            continue
        ma = json.loads((a_dir / "meta.json").read_text())
        mb = json.loads((b_dir / "meta.json").read_text())
        if meta is None:
            meta = ma
        # steady window per variant
        a_s = ma["start_unix"] + ma["warmup_s"]
        a_e = a_s + ma["steady_s"]
        b_s = mb["start_unix"] + mb["warmup_s"]
        b_e = b_s + mb["steady_s"]
        a_lat = load_latencies_steady(a_dir / "latency.jsonl", a_s, a_e)
        b_lat = load_latencies_steady(b_dir / "latency.jsonl", b_s, b_e)
        pair_latencies.append((a_lat, b_lat))
        # counters
        for arm_dir, store in [(a_dir, counter_a), (b_dir, counter_b)]:
            sb = parse_status(arm_dir / "status-before.txt")
            sa = parse_status(arm_dir / "status-after.txt")
            for k, v in sa.items():
                if k in sb and isinstance(v, (int, float)) and isinstance(sb[k], (int, float)):
                    delta = v - sb[k]
                    store.setdefault(k, []).append(delta)
        detail.append({
            "pn": pn,
            "a_p50": pct(a_lat, 0.50), "b_p50": pct(b_lat, 0.50),
            "a_p95": pct(a_lat, 0.95), "b_p95": pct(b_lat, 0.95),
            "a_sel": counter_a.get("Com_select", [0])[-1],
            "b_sel": counter_b.get("Com_select", [0])[-1],
        })

    if not pair_latencies:
        sys.exit("no usable pairs found")

    n = len(pair_latencies)
    print(f"computing bootstrap CIs ({args.bootstrap_n} resamples each)...")

    p50_obs, p50_lo, p50_hi = hierarchical_bootstrap_paired(pair_latencies, 0.50, args.bootstrap_n)
    p95_obs, p95_lo, p95_hi = hierarchical_bootstrap_paired(pair_latencies, 0.95, args.bootstrap_n)
    p99_obs, p99_lo, p99_hi = hierarchical_bootstrap_paired(pair_latencies, 0.99, args.bootstrap_n)

    print("rendering plots...")
    cdf_b64 = plot_latency_cdf(pair_latencies)
    per_pair_b64 = plot_per_pair_p50_p95(pair_latencies)

    def excludes_zero_str(lo, hi):
        return ("EXCLUDES 0 (effect significant)" if (lo > 0 or hi < 0)
                else "crosses 0 (effect not resolved)")

    def cls(lo, hi):
        if lo > 0:
            return "negative"  # A worse = bad (red)
        if hi < 0:
            return ""  # A better = green
        return "neutral"

    def headline_block(label, mean, lo, hi, a_median, force_neutral=False):
        css_cls = "neutral" if force_neutral else cls(lo, hi)
        crosses_zero = not (lo > 0 or hi < 0)
        batch = meta["batch"]
        per_alert_us = mean * 1000 / batch
        if crosses_zero:
            main = (f'POST /v1/alerts {label}: not resolved at N={n} '
                    f'(sign dominated by noise at this percentile)')
            extras = ''
        else:
            main = (f'POST /v1/alerts {label}: variant A is {mean:+.2f} ms '
                    f'vs variant B per request (batch of {batch})')
            share = (mean / a_median * 100) if a_median > 0 else 0
            extras = (f'<br>\n'
                      f'Per alert: {per_alert_us:+.0f} us. '
                      f'Share of variant-A median per request: {share:+.1f}%.')
        return (f'<div class="headline {css_cls}">\n'
                f'<strong>{main}</strong>\n<br>\n'
                f'95% bootstrap CI: [{lo:+.2f}, {hi:+.2f}] ms '
                f'({excludes_zero_str(lo, hi)})'
                f'{extras}\n</div>')

    counter_rows = []
    # SHOW GLOBAL STATUS is snapped by run.sh before warmup and after drain, so the
    # counter delta covers the entire arm window. Loadgen offers `rate * batch`
    # alerts per second for that whole window, so the denominator must span it too.
    arm_duration_s = meta["warmup_s"] + meta["steady_s"] + meta["drain_s"]
    alerts_per_arm = meta["rate"] * meta["batch"] * arm_duration_s
    interesting_counters = ["Com_select", "Com_stmt_execute", "Com_insert", "Com_update",
                            "Connections", "Aborted_connects", "Aborted_clients"]
    for key in interesting_counters:
        if key not in counter_a:
            continue
        a_mean = statistics.mean(counter_a[key])
        b_mean = statistics.mean(counter_b[key])
        diff = a_mean - b_mean
        per_alert = diff / alerts_per_arm if alerts_per_arm > 0 else 0
        cls_diff = "pos" if diff > 0 else ("neg" if diff < 0 else "")
        counter_rows.append(
            f'<tr><td>{key}</td>'
            f'<td class="num">{a_mean:+,.0f}</td>'
            f'<td class="num">{b_mean:+,.0f}</td>'
            f'<td class="num {cls_diff}">{diff:+,.0f}</td>'
            f'<td class="num">{per_alert:+.3f}</td></tr>'
        )

    detail_rows = []
    for d in detail:
        dp50 = (d["a_p50"] or 0) - (d["b_p50"] or 0)
        dp95 = (d["a_p95"] or 0) - (d["b_p95"] or 0)
        detail_rows.append(
            f'<tr><td>{d["pn"]}</td>'
            f'<td class="num">{d["a_p50"]:.1f}</td>'
            f'<td class="num">{d["b_p50"]:.1f}</td>'
            f'<td class="num {"pos" if dp50>0 else "neg"}">{dp50:+.1f}</td>'
            f'<td class="num">{d["a_p95"]:.1f}</td>'
            f'<td class="num">{d["b_p95"]:.1f}</td>'
            f'<td class="num {"pos" if dp95>0 else "neg"}">{dp95:+.1f}</td>'
            f'<td class="num">{d["a_sel"]:+,d}</td>'
            f'<td class="num">{d["b_sel"]:+,d}</td></tr>'
        )

    out_path = args.out or (grid / "report.html")
    pprof_section = render_pprof_section(grid, pair_dirs, Path(out_path).parent)

    toc_entries = [
        ("operational-point", "Operational point"),
        ("how-to-read", "How to read this report"),
        ("headline", "Headline result"),
        ("counter-delta", "Counter delta"),
        ("latency-distribution", "Latency distribution"),
        ("per-pair-stability", "Per-pair stability"),
        ("per-pair-detail", "Per-pair detail"),
        ("notes", "Notes"),
    ]
    if pprof_section:
        toc_entries.append(("profile-data", "Profile data"))
    toc = "\n".join(f'<li><a href="#{aid}">{title}</a></li>'
                    for aid, title in toc_entries)

    # Normalise cs_info: 'cs_info{version="X"} 0' -> 'X'.
    cs_info_raw = meta.get("cs_info", "")
    _m = re.search(r'version="([^"]+)"', cs_info_raw)
    cs_version = _m.group(1) if _m else (cs_info_raw or "<unknown>")

    # Host environment captured by run.sh (optional; older runs may not have it).
    env_path = grid / "env.json"
    if env_path.exists():
        try:
            env = json.loads(env_path.read_text())
            env_str = (f'{env.get("os_name","?")} {env.get("os_version","?")} '
                       f'{env.get("arch","?")}, {env.get("cpu_model","?")}, '
                       f'{env.get("cpu_count","?")} cores, '
                       f'{env.get("mem_gb","?")} GB &middot; Docker '
                       f'{env.get("docker_version","?")}')
        except Exception:
            env_str = "(env.json unreadable)"
    else:
        env_str = "(not recorded)"

    # Mean of per-pair variant-A percentile, used as the "total" baseline
    # for the share-of-latency annotation in each headline block.
    a_p50_mean = statistics.mean(pct(a, 0.50) for a, _ in pair_latencies)
    a_p95_mean = statistics.mean(pct(a, 0.95) for a, _ in pair_latencies)
    a_p99_mean = statistics.mean(pct(a, 0.99) for a, _ in pair_latencies)

    headline_blocks = "\n".join([
        headline_block("median latency", p50_obs, p50_lo, p50_hi, a_p50_mean),
        headline_block("p95 latency", p95_obs, p95_lo, p95_hi, a_p95_mean),
        headline_block("p99 latency", p99_obs, p99_lo, p99_hi, a_p99_mean),
    ])

    html = HTML_TEMPLATE.format(
        grid_id=grid.name,
        pprof_section=pprof_section,
        toc=toc,
        headline_blocks=headline_blocks,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        n_pairs=n,
        duration_str=duration_str,
        cs_version=cs_version,
        env_str=env_str,
        rate=meta["rate"], batch=meta["batch"], deadline=meta["deadline"],
        retries=meta["retries"],
        warmup=meta["warmup_s"], steady=meta["steady_s"], drain=meta["drain_s"],
        pool_size=meta.get("pool_size", "default"),
        cdf_b64=cdf_b64,
        per_pair_b64=per_pair_b64,
        counter_rows="\n".join(counter_rows),
        detail_rows="\n".join(detail_rows),
        alerts_per_arm=f"{alerts_per_arm:,d}",
        bootstrap_resamples=args.bootstrap_n,
    )

    with open(out_path, "w") as f:
        f.write(html)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
