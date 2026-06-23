"""
Within-phase vs across-phase epsilon comparison for burst data.

The burst runs (gem5-configs/output/burst/<bench>/stats.txt) hold 200 stat dumps
each, structured as 4 bursts (macro phases) x 50 consecutive 1M-instruction ROI
intervals:

    phase 0 = windows   0..49
    phase 1 = windows  50..99
    phase 2 = windows 100..149
    phase 3 = windows 150..199

The interval relations (R5, R6, R14) pair ROI windows within one file. eval.py's
eval_interval() pairs EVERY ordered window pair (i,j) -- so on a burst file it
mixes intervals from behaviorally distinct phases.

Hypothesis: restricting comparisons to within the same phase (locally homogeneous
behavior) yields SMALLER epsilons than pairing across the whole 200-window run.

This script reuses eval.py's machinery verbatim: it just feeds eval_interval()
50-window slices (within-phase) and the full 200 windows (across-phase) and
compares max_epsilon. It does NOT modify eval.py.

    python3 burst_phase_eval.py
"""

from pathlib import Path

from relations import ALL_RELATIONS
from eval import parse_stats_file_windows, eval_interval, max_epsilon, _fmt_eps

BURST_DIR = Path(__file__).resolve().parent.parent / "gem5-configs" / "output" / "burst"
NUM_PHASES = 4

# Snapshot of the MAX EPSILON table in eval_results.txt at the time this script
# was written. These come from the NON-burst output/ layout (different runs,
# same benchmark names), shown only as a loose reference baseline. They are
# static report numbers, not live data -- re-snapshot if eval.py is re-run.
EVAL_RESULTS_SNAPSHOT = {
    "R5":  {"429.mcf": 0.1716,  "450.soplex": 0.002419, "471.omnetpp": 0.01477,
            "473.astar": 0.008308, "482.sphinx3": 0.003872, "483.xalancbmk": 0.004965},
    "R6":  {"429.mcf": 0.01644, "450.soplex": 0.04047,  "471.omnetpp": 0.1423,
            "473.astar": 0.00154,  "482.sphinx3": 0.02375,  "483.xalancbmk": 0.1407},
    "R14": {"429.mcf": 535810,  "450.soplex": 64119,    "471.omnetpp": 15992,
            "473.astar": None,     "482.sphinx3": 0,        "483.xalancbmk": None},
}


def discover_benchmarks():
    """Benchmark dirs under output/burst/ that have a stats.txt (skip plots/)."""
    return sorted(d for d in BURST_DIR.iterdir()
                  if d.is_dir() and (d / "stats.txt").exists())


def split_phases(windows):
    """Split ordered windows into NUM_PHASES contiguous, equal-size slices."""
    n = len(windows)
    if n != 200:
        print(f"  ! expected 200 windows, got {n}; "
              f"using phase size {n // NUM_PHASES}")
    size = n // NUM_PHASES
    return [windows[k * size:(k + 1) * size] for k in range(NUM_PHASES)]


def main():
    # Buffer every output line so we can both print it and write it to a file.
    out_lines = []
    def emit(line=""):
        print(line)
        out_lines.append(line)

    if not BURST_DIR.exists():
        print(f"Burst data dir not found: {BURST_DIR}")
        return

    interval_rels = [r for r in ALL_RELATIONS if r.get("kind") == "interval"]
    benches = discover_benchmarks()

    emit(f"Burst data: {BURST_DIR}")
    emit(f"Benchmarks: {', '.join(b.name for b in benches)}")
    emit(f"Interval relations: {', '.join(r['name'] for r in interval_rels)}")
    emit()

    # bench -> list of 200 windows (parsed once, reused across relations)
    windows_by_bench = {}
    for bd in benches:
        w = parse_stats_file_windows(bd / "stats.txt")
        windows_by_bench[bd.name] = w
        emit(f"  {bd.name}: parsed {len(w)} windows")
    emit()

    for rel in interval_rels:
        name = rel["name"]
        unit = rel.get("epsilon_unit", "-")
        emit("=" * 100)
        emit(f"[{name}] {rel['description']}")
        emit(f"        unit = {unit}")
        if name == "R6":
            emit("        NOTE: burst data has real lsq0.criticalMissRate, so R6's "
                 "premise is genuinely")
            emit("              distinct from R5 here (not the overall-hit-rate "
                 "fallback).")
        emit("-" * 100)
        header = (f"{'benchmark':<14} {'phase0':>10} {'phase1':>10} {'phase2':>10} "
                  f"{'phase3':>10} {'within-max':>12} {'across-200':>12} "
                  f"{'eval_res':>12}  verdict")
        emit(header)
        emit("-" * len(header))

        for bd in benches:
            b = bd.name
            windows = windows_by_bench[b]
            phases = split_phases(windows)

            # within-phase: eval_interval on each 50-window slice
            phase_eps = []
            for ph in phases:
                res = eval_interval(rel, ph)
                phase_eps.append(max_epsilon(res))

            # across-phase baseline on the SAME data (all 200 windows)
            across_eps = max_epsilon(eval_interval(rel, windows))

            within_vals = [e for e in phase_eps if e is not None]
            within_max = max(within_vals) if within_vals else None

            ref = EVAL_RESULTS_SNAPSHOT.get(name, {}).get(b)

            # verdict: did within-phase reduce vs the across-200 same-data baseline?
            if within_max is None or across_eps is None:
                verdict = "n/a"
            elif within_max < across_eps:
                verdict = f"REDUCED (-{_fmt_eps(across_eps - within_max)})"
            elif within_max == across_eps:
                verdict = "same"
            else:
                verdict = "INCREASED (?)"

            row = (f"{b:<14} "
                   f"{_fmt_eps(phase_eps[0]):>10} {_fmt_eps(phase_eps[1]):>10} "
                   f"{_fmt_eps(phase_eps[2]):>10} {_fmt_eps(phase_eps[3]):>10} "
                   f"{_fmt_eps(within_max):>12} {_fmt_eps(across_eps):>12} "
                   f"{_fmt_eps(ref):>12}  {verdict}")
            emit(row)
        emit()

    emit("=" * 100)
    emit("Reading the table:")
    emit("  phase0..3   = max epsilon (worst-case slack) needed WITHIN each 50-interval burst")
    emit("  within-max  = max over the 4 phases (slack to make the claim hold inside every burst)")
    emit("  across-200  = max epsilon when pairing ALL 200 intervals (current eval_interval behavior)")
    emit("  eval_res    = reference value from eval_results.txt (non-burst runs; loose comparison)")
    emit("  verdict     = within-max vs across-200 on the SAME burst data (the reduction being tested)")
    emit()
    emit("  within-max <= across-200 is mathematically guaranteed (within-phase pairs are a")
    emit("  subset of all-200 pairs); a strict reduction means cross-phase pairing was inflating eps.")

    out_path = Path(__file__).resolve().parent / "burst_phase_results.txt"
    out_path.write_text("\n".join(out_lines) + "\n")
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
