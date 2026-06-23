"""
Lightweight evaluator: check relations against gem5 stats.

Two modes:

    # Auto-discover the standard data layout (gem5-configs/output + output-opt)
    # and evaluate every relation with the pairing its 'kind' implies.
    python3 eval.py

    # Or point it at explicit stats files (single-entity + interval pairing only)
    python3 eval.py /path/to/stats.txt [/path/to/another/stats.txt ...]

For each relation, per (benchmark, pairing):
  - resolve the base metrics for each entity,
  - check the premise (multi-entity) — pairs that fail are VACUOUS, not counted,
  - check the claim (consequent),
  - report HOLDS / VIOLATED / VACUOUS / UNEVALUABLE to stdout and eval_results.txt.

Pairing is driven by each relation's 'kind' (see relations.py):
    single        one window, bare metric keys
    interval      ordered ROI-window pairs within one file -> _a/_b keys
    cross_config  ordered LLC config pairs by size           -> _a/_b keys
    opt           OPT trace window vs policy window           -> _opt/_policy keys
    unsupported   the data needed isn't collected -> honestly UNEVALUABLE
"""

from pathlib import Path
from collections import defaultdict
import sys

from relations import ALL_RELATIONS


# Standard data layout (relative to this file's parent dir).
REPO_PARENT = Path(__file__).resolve().parent.parent
MULTI_ROI_DIR = REPO_PARENT / "gem5-configs" / "output"
OPT_DIR = REPO_PARENT / "output-opt"
LLC_PREFIX = "board.cache_hierarchy.llcache"

# LLC config dirs -> size in bytes (for cross-config pairing / R1).
LLC_CONFIG_SIZES = {
    "default": 8 * 1024 * 1024,
    "llc_4MiB": 4 * 1024 * 1024,
    "llc_16MiB": 16 * 1024 * 1024,
}


# =============================================================================
# STATS PARSING
# =============================================================================

def parse_stats_file(path) -> dict:
    """Parse gem5 stats.txt into {stat_name: float}."""
    stats: dict = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("---") or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            key = parts[0]
            val_str = parts[1]
            try:
                val = float(val_str)
            except ValueError:
                continue
            if val != val or val_str.lower() in ("inf", "-inf"):
                continue
            stats[key] = val
    return stats


def parse_stats_file_windows(path) -> list:
    """Parse gem5 stats.txt with multiple ROI dumps into per-window dicts."""
    BEGIN = "---------- Begin Simulation Statistics ----------"
    END = "---------- End Simulation Statistics   ----------"

    windows: list = []
    current: dict = {}
    inside = False

    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped == BEGIN:
                inside = True
                current = {}
                continue
            if stripped == END:
                if current:
                    windows.append(current)
                inside = False
                continue
            if inside:
                parts = stripped.split()
                if len(parts) >= 2:
                    key = parts[0]
                    val_str = parts[1]
                    try:
                        val = float(val_str)
                        if val == val and val_str.lower() not in ("inf", "-inf"):
                            current[key] = val
                    except ValueError:
                        continue

    if not windows:
        return [parse_stats_file(path)]
    return windows


def parse_opt_trace_results(path) -> list:
    """Parse a Belady-OPT trace_llc_results.txt into per-ROI hit-rate dicts.

    Table with header: roi  hits  misses  hit_rate  warmup  roi_accesses.
    The i-th row lines up with the i-th ROI window of the matching stats.txt.
    """
    rows: list = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                roi = int(parts[0])
                hits = float(parts[1])
                misses = float(parts[2])
                hit_rate = float(parts[3])
                accesses = float(parts[5])
            except ValueError:
                continue
            rows.append({
                "roi": roi, "hits": hits, "misses": misses,
                "hit_rate": hit_rate, "accesses": accesses,
            })
    return rows


# =============================================================================
# METRIC RESOLUTION (single dataset -> one metric value)
# =============================================================================

def resolve_metric(stats: dict, metric_name: str):
    """Resolve a high-level metric name to a value from one gem5 stats dict.

    Prioritizes the LLC (L3) cache. Returns None if it can't be resolved.
    """
    metric_name = metric_name.lower()

    if metric_name in stats:
        return stats[metric_name]

    def _div(num_key, den_key):
        num = stats.get(num_key)
        den = stats.get(den_key)
        if num is not None and den is not None and den > 0:
            return num / den
        return None

    overall = _div(f"{LLC_PREFIX}.overallHits::total",
                   f"{LLC_PREFIX}.overallAccesses::total")

    if metric_name in ("hit_rate", "hit_rate_llc", "hit_rate_l3"):
        if overall is not None:
            return overall
        # fallback: derive from misses
        accesses = stats.get(f"{LLC_PREFIX}.overallAccesses::total")
        misses = stats.get(f"{LLC_PREFIX}.overallMisses::total")
        if accesses is not None and misses is not None and accesses > 0:
            return (accesses - misses) / accesses
        return None

    if metric_name in ("miss_rate", "miss_rate_llc", "miss_rate_l3"):
        return _div(f"{LLC_PREFIX}.overallMisses::total",
                    f"{LLC_PREFIX}.overallAccesses::total")

    if metric_name == "load_hit_rate":
        r = _div(f"{LLC_PREFIX}.ReadReq_hits::total",
                 f"{LLC_PREFIX}.ReadReq_accesses::total")
        return r if r is not None else overall  # fallback to overall

    if metric_name == "store_hit_rate":
        r = _div(f"{LLC_PREFIX}.WriteReq_hits::total",
                 f"{LLC_PREFIX}.WriteReq_accesses::total")
        return r if r is not None else overall  # fallback to overall

    if metric_name == "critical_hit_rate":
        cmr = stats.get("board.processor.switch.core.lsq0.criticalMissRate")
        if cmr is not None:
            return 1.0 - cmr
        return overall  # fallback: overall hit rate when criticalMissRate absent

    if metric_name == "stalls":
        # Backend-stall *fraction*: cycles dispatch (entry to the OOO backend)
        # was blocked on ROB/IQ/LSQ-full, divided by total cycles. A rate, not a
        # raw count, so it's comparable across ROI windows of differing length
        # (R5/R6 pair unequal-length windows of one run — a raw cycle count is
        # biased by window size, not behavior). dispatchStatus::blocked is
        # tighter than rename.status::Blocked: it's the actual backend gate, not
        # the upstream rename symptom (which also overlaps decode.status::Blocked
        # on the same cycles, and ignores bad-speculation squash cycles).
        cycles = (stats.get("board.processor.switch.core.numCycles")
                  or stats.get("board.processor.cores.core.numCycles"))
        blocked = stats.get("board.processor.switch.core.iew.dispatchStatus::blocked")
        if blocked is not None and cycles and cycles > 0:
            return blocked / cycles
        # fallback: rename-blocked fraction if the iew dispatch stat is absent
        rename_blocked = stats.get("board.processor.switch.core.rename.status::Blocked")
        if rename_blocked is not None and cycles and cycles > 0:
            return rename_blocked / cycles
        return None

    if metric_name == "total_misses":
        return stats.get(f"{LLC_PREFIX}.overallMisses::total")

    if metric_name == "evictions":
        return stats.get(f"{LLC_PREFIX}.replacements")

    if metric_name == "writebacks":
        return stats.get(f"{LLC_PREFIX}.writebacks::total")

    # No resolution path (compulsory/capacity/conflict/coherence misses,
    # prefetch_coverage, prefetch_accuracy, invalidations, demand_hit_rate, ...)
    return None


def resolve_entity(stats: dict, base_metrics: list, suffix: str):
    """Resolve every base metric for one entity into suffixed keys.

    Returns (data, missing): data maps '<metric><suffix>' -> value for the
    metrics that resolved; missing lists the base metrics that didn't.
    """
    data, missing = {}, []
    for m in base_metrics:
        val = resolve_metric(stats, m)
        if val is None:
            missing.append(m)
        else:
            data[f"{m}{suffix}"] = val
    return data, missing


# =============================================================================
# PAIR EVALUATION
# =============================================================================

def _verdict(relation: dict, data: dict, missing: list) -> dict:
    """Run premise + claim over an assembled data dict -> a result dict."""
    name = relation["name"]
    if missing:
        return {"name": name, "status": "UNEVALUABLE", "epsilon": None,
                "missing": sorted(set(missing)), "detail": None}

    premise = relation.get("premise")
    try:
        if premise is not None and not premise(data):
            return {"name": name, "status": "VACUOUS", "epsilon": None,
                    "missing": [], "detail": None}
        holds = relation["claim"](data)
    except Exception as e:  # defensive: a bad lambda shouldn't abort the run
        return {"name": name, "status": "UNEVALUABLE", "epsilon": None,
                "missing": [f"error: {e}"], "detail": None}

    epsilon = None
    if relation.get("epsilon_name"):
        slack = relation.get("slack")
        if slack is not None:
            try:
                # tolerance this pair needs: 0 if it already holds, else the
                # magnitude by which the strict claim is violated.
                epsilon = max(0.0, float(slack(data)))
            except Exception:
                epsilon = 0.0 if holds else None
        else:
            epsilon = 0.0 if holds else None  # minimal: 0 if it holds as-is
    return {"name": name, "status": "HOLDS" if holds else "VIOLATED",
            "epsilon": epsilon, "missing": [], "detail": None}


def eval_single(relation: dict, windows: list) -> list:
    """Single-entity relation: evaluate once per window with bare metric keys."""
    results = []
    for w_idx, stats in enumerate(windows):
        data, missing = resolve_entity(stats, relation["base_metrics"], "")
        r = _verdict(relation, data, missing)
        r["pair"] = f"w{w_idx}"
        results.append(r)
    return results


def eval_interval(relation: dict, windows: list) -> list:
    """Interval relation: all ordered ROI-window pairs (i != j) -> _a/_b keys."""
    results = []
    for i in range(len(windows)):
        for j in range(len(windows)):
            if i == j:
                continue
            data_a, miss_a = resolve_entity(windows[i], relation["base_metrics"], "_a")
            data_b, miss_b = resolve_entity(windows[j], relation["base_metrics"], "_b")
            data = {**data_a, **data_b}
            r = _verdict(relation, data, miss_a + miss_b)
            r["pair"] = f"(w{i},w{j})"
            results.append(r)
    return results


def eval_cross_config(relation: dict, config_windows: dict, config_sizes: dict) -> list:
    """Cross-config relation: ordered config pairs with size_a >= size_b, per window."""
    results = []
    names = sorted(config_windows, key=lambda c: config_sizes[c])
    num_windows = min(len(ws) for ws in config_windows.values())
    for a in names:
        for b in names:
            if a == b or config_sizes[a] < config_sizes[b]:
                continue
            for w in range(num_windows):
                data_a, miss_a = resolve_entity(config_windows[a][w], relation["base_metrics"], "_a")
                data_b, miss_b = resolve_entity(config_windows[b][w], relation["base_metrics"], "_b")
                data = {**data_a, **data_b,
                        "size_a": config_sizes[a], "size_b": config_sizes[b]}
                r = _verdict(relation, data, miss_a + miss_b)
                r["pair"] = f"({a},{b})@w{w}"
                results.append(r)
    return results


def eval_opt(relation: dict, opt_rows: list, policy_windows: list) -> list:
    """OPT relation: OPT trace hit rate vs policy hit rate, paired per window."""
    results = []
    n = min(len(opt_rows), len(policy_windows))
    for w in range(n):
        opt_stats = {
            f"{LLC_PREFIX}.overallHits::total": opt_rows[w]["hits"],
            f"{LLC_PREFIX}.overallMisses::total": opt_rows[w]["misses"],
            f"{LLC_PREFIX}.overallAccesses::total": opt_rows[w]["accesses"],
        }
        data_opt, miss_opt = resolve_entity(opt_stats, relation["base_metrics"], "_opt")
        data_pol, miss_pol = resolve_entity(policy_windows[w], relation["base_metrics"], "_policy")
        data = {**data_opt, **data_pol}
        r = _verdict(relation, data, miss_opt + miss_pol)
        r["pair"] = f"w{w}"
        results.append(r)
    return results


# =============================================================================
# AGGREGATION + FORMATTING
# =============================================================================

def max_epsilon(results: list):
    """Largest slack any applicable pair needed, or None if no eps was computed.

    The max over a relation's pairs is the tolerance that would make the
    relaxed claim hold for *every* pair — i.e. the worst-case slack.
    """
    eps = [r["epsilon"] for r in results
           if r["status"] in ("HOLDS", "VIOLATED") and r.get("epsilon") is not None]
    return max(eps) if eps else None


def _fmt_eps(eps) -> str:
    """Compact format for an epsilon: fraction-scale gets decimals, big counts
    (e.g. stall cycles, writebacks) get a grouped integer."""
    if eps is None:
        return "-"
    if eps == 0:
        return "0"
    if eps < 1000:
        return f"{eps:.4g}"
    return f"{eps:,.0f}"


def summarize(results: list) -> str:
    """One-line tally of a list of pair results for one relation/benchmark."""
    holds = sum(1 for r in results if r["status"] == "HOLDS")
    violated = sum(1 for r in results if r["status"] == "VIOLATED")
    uneval = sum(1 for r in results if r["status"] == "UNEVALUABLE")
    applicable = holds + violated
    parts = [f"HOLDS {holds}/{applicable}" if applicable else "HOLDS 0/0",
             f"VIOLATED {violated}"]
    if uneval:
        parts.append(f"uneval {uneval}")
    eps = max_epsilon(results)
    if eps is not None:
        parts.append(f"max_eps {_fmt_eps(eps)}")
    return ", ".join(parts)


def evaluate_relation_over_benchmark(relation: dict, bench: str, bench_dir: Path) -> list:
    """Dispatch one relation to its pairing driver for one benchmark.

    Returns a list of per-pair result dicts (possibly a single UNEVALUABLE entry
    when the relation's kind isn't supported by the available data).
    """
    kind = relation.get("kind", "unsupported")

    if kind == "unsupported":
        # Resolve once to report exactly which metrics are missing, so the
        # output explains *why* rather than silently dropping the relation.
        windows = parse_stats_file_windows(bench_dir / "stats.txt")
        suffix = "_a" if relation.get("premise") else ""
        _, missing = resolve_entity(windows[0], relation["base_metrics"], suffix)
        return [{"name": relation["name"], "status": "UNEVALUABLE", "epsilon": None,
                 "missing": missing or list(relation["base_metrics"]),
                 "pair": "-", "detail": "kind=unsupported (data not collected)"}]

    if kind == "single":
        return eval_single(relation, parse_stats_file_windows(bench_dir / "stats.txt"))

    if kind == "interval":
        return eval_interval(relation, parse_stats_file_windows(bench_dir / "stats.txt"))

    if kind == "cross_config":
        config_windows, config_sizes = {}, {}
        default_stats = bench_dir / "stats.txt"
        if default_stats.exists():
            config_windows["default"] = parse_stats_file_windows(default_stats)
            config_sizes["default"] = LLC_CONFIG_SIZES["default"]
        for variant in sorted(bench_dir.iterdir()):
            if variant.is_dir() and variant.name.startswith("llc_") \
                    and variant.name in LLC_CONFIG_SIZES:
                vstats = variant / bench / "stats.txt"
                if vstats.exists():
                    config_windows[variant.name] = parse_stats_file_windows(vstats)
                    config_sizes[variant.name] = LLC_CONFIG_SIZES[variant.name]
        if len(config_windows) < 2:
            return [{"name": relation["name"], "status": "UNEVALUABLE", "epsilon": None,
                     "missing": ["<2 LLC configs found"], "pair": "-", "detail": None}]
        return eval_cross_config(relation, config_windows, config_sizes)

    if kind == "opt":
        opt_path = OPT_DIR / bench / "trace_llc_results.txt"
        policy_stats = OPT_DIR / bench / "stats.txt"
        if not opt_path.exists() or not policy_stats.exists():
            return [{"name": relation["name"], "status": "UNEVALUABLE", "epsilon": None,
                     "missing": [f"no OPT trace at {opt_path}"], "pair": "-", "detail": None}]
        return eval_opt(relation, parse_opt_trace_results(opt_path),
                        parse_stats_file_windows(policy_stats))

    return [{"name": relation["name"], "status": "UNEVALUABLE", "epsilon": None,
             "missing": [f"unknown kind={kind}"], "pair": "-", "detail": None}]


def write_report(by_bench: dict, out_path: Path):
    """Write the full report (per benchmark, per relation, with pair detail)."""
    lines = ["RELATION EVALUATION SUMMARY", "=" * 70, ""]
    for bench in sorted(by_bench):
        lines.append(f"Benchmark: {bench}")
        lines.append("-" * 70)
        for relation in ALL_RELATIONS:
            results = by_bench[bench].get(relation["name"], [])
            if not results:
                continue
            lines.append(f"  [{relation['name']}] {summarize(results)}  "
                         f"(kind={relation.get('kind', '?')})")
            # show up to 6 applicable (HOLDS/VIOLATED) pairs for detail
            shown = [r for r in results if r["status"] in ("HOLDS", "VIOLATED")][:6]
            for r in shown:
                eps = f" eps={r['epsilon']}" if r.get("epsilon") is not None else ""
                lines.append(f"        {r['pair']}: {r['status']}{eps}")
            uneval = next((r for r in results if r["status"] == "UNEVALUABLE"), None)
            if uneval and not shown:
                miss = ", ".join(uneval["missing"][:6])
                extra = f" — {uneval['detail']}" if uneval.get("detail") else ""
                lines.append(f"        UNEVALUABLE — missing: {miss}{extra}")
        lines.append("")

    lines.extend(relation_table(by_bench))
    lines.extend(epsilon_table(by_bench))
    out_path.write_text("\n".join(lines) + "\n")


def _cell(results: list, pct: bool = False) -> str:
    """One table cell: 'holds/applicable', '-' if unevaluable/empty.

    A cell where holds < applicable is where failures live. With pct=True the
    holds-rate is appended as a percentage (used for the pooled ALL total).
    """
    if not results:
        return "-"
    holds = sum(1 for r in results if r["status"] == "HOLDS")
    violated = sum(1 for r in results if r["status"] == "VIOLATED")
    applicable = holds + violated
    if applicable == 0:
        return "-"  # only unevaluable/vacuous pairs
    cell = f"{holds}/{applicable}"
    if pct:
        cell += f" ({100.0 * holds / applicable:.0f}%)"
    return cell


def relation_table(by_bench: dict) -> list:
    """Per-relation × per-benchmark table; cells show holds/applicable so it's
    clear which benchmarks have failures. A trailing column pools all benches."""
    benches = sorted(by_bench)
    # Keep only relations that produced at least one result somewhere.
    rels = [r for r in ALL_RELATIONS
            if any(by_bench[b].get(r["name"]) for b in benches)]

    header = ["relation"] + benches + ["ALL"]
    rows = [header]
    for relation in rels:
        name = relation["name"]
        pooled = [r for b in benches for r in by_bench[b].get(name, [])]
        row = [name] + [_cell(by_bench[b].get(name, [])) for b in benches]
        row.append(_cell(pooled, pct=True))
        rows.append(row)

    widths = [max(len(r[c]) for r in rows) for c in range(len(header))]

    def fmt(row):
        cells = [row[0].ljust(widths[0])]
        cells += [row[c].rjust(widths[c]) for c in range(1, len(row))]
        return "  ".join(cells)

    out = ["PER-RELATION SUMMARY (cells: holds/applicable, '-' = n/a)",
           "=" * 70, fmt(rows[0]), "-" * len(fmt(rows[0]))]
    out += [fmt(r) for r in rows[1:]]
    out.append("")
    return out


def epsilon_table(by_bench: dict) -> list:
    """Per-relation × per-benchmark max epsilon; the trailing ALL column pools
    all benches to give the global max epsilon per relation."""
    benches = sorted(by_bench)
    # Keep only relations that produced a computable epsilon somewhere.
    rels = [r for r in ALL_RELATIONS
            if any(max_epsilon(by_bench[b].get(r["name"], [])) is not None
                   for b in benches)]

    def cell(results):
        return _fmt_eps(max_epsilon(results)) if results else "-"

    header = ["relation"] + benches + ["ALL", "unit"]
    rows = [header]
    for relation in rels:
        name = relation["name"]
        pooled = [r for b in benches for r in by_bench[b].get(name, [])]
        row = [name] + [cell(by_bench[b].get(name, [])) for b in benches]
        row.append(cell(pooled))
        row.append(relation.get("epsilon_unit", "-"))
        rows.append(row)

    widths = [max(len(r[c]) for r in rows) for c in range(len(header))]

    def fmt(row):
        cells = [row[0].ljust(widths[0])]
        cells += [row[c].rjust(widths[c]) for c in range(1, len(row))]
        return "  ".join(cells)

    out = ["MAX EPSILON PER RELATION (worst-case slack; ALL = global max, '-' = n/a)",
           "=" * 70, fmt(rows[0]), "-" * len(fmt(rows[0]))]
    out += [fmt(r) for r in rows[1:]]
    out.append("")
    return out


# =============================================================================
# MAIN
# =============================================================================

def run_auto() -> dict:
    """Evaluate all relations over the standard discovered data layout."""
    if not MULTI_ROI_DIR.exists():
        print(f"Data dir not found: {MULTI_ROI_DIR}")
        sys.exit(1)
    def has_comparison_data(d: Path) -> bool:
        """Real benchmark: has LLC size variants or an OPT trace to compare against."""
        has_variants = any(c.is_dir() and c.name.startswith("llc_") for c in d.iterdir())
        has_opt = (OPT_DIR / d.name / "trace_llc_results.txt").exists()
        return has_variants or has_opt

    bench_dirs = sorted(d for d in MULTI_ROI_DIR.iterdir()
                        if d.is_dir() and (d / "stats.txt").exists()
                        and has_comparison_data(d))
    by_bench: dict = {}
    for bench_dir in bench_dirs:
        bench = bench_dir.name
        print(f"\n{'=' * 70}\nBenchmark: {bench}\n{'=' * 70}")
        by_bench[bench] = {}
        for relation in ALL_RELATIONS:
            results = evaluate_relation_over_benchmark(relation, bench, bench_dir)
            by_bench[bench][relation["name"]] = results
            print(f"  [{relation['name']}] {summarize(results)}  "
                  f"(kind={relation.get('kind', '?')})")
    return by_bench


def run_explicit(paths: list) -> dict:
    """Evaluate single + interval relations against explicitly listed stats files.

    Cross-config and OPT relations need the discovered layout, so they report
    UNEVALUABLE here.
    """
    by_bench: dict = {}
    for p in paths:
        bench = str(p)
        print(f"\n{'=' * 70}\nFile: {bench}\n{'=' * 70}")
        windows = parse_stats_file_windows(p)
        by_bench[bench] = {}
        for relation in ALL_RELATIONS:
            kind = relation.get("kind", "unsupported")
            if kind == "single":
                results = eval_single(relation, windows)
            elif kind == "interval":
                results = eval_interval(relation, windows)
            else:
                results = [{"name": relation["name"], "status": "UNEVALUABLE",
                            "epsilon": None,
                            "missing": [f"kind={kind} needs discovered layout; "
                                        f"run `python3 eval.py` with no args"],
                            "pair": "-", "detail": None}]
            by_bench[bench][relation["name"]] = results
            print(f"  [{relation['name']}] {summarize(results)}  (kind={kind})")
    return by_bench


def main():
    if len(sys.argv) > 1:
        by_bench = run_explicit(sys.argv[1:])
    else:
        by_bench = run_auto()

    out_path = Path(__file__).resolve().parent / "eval_results.txt"
    write_report(by_bench, out_path)
    print(f"\n\nResults written to {out_path}")


if __name__ == "__main__":
    main()
