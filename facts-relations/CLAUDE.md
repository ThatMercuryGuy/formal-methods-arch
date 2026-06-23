# Facts & Relations: Lightweight Cache Microarchitecture Verification

A minimal framework for stating falsifiable claims about cache replacement and verifying them against gem5 simulation data.

## What This Is

A relation is a claim: "if cache A has property X and cache B has property Y, then the outcome satisfies Z (within tolerance ε)."

For example: "If cache A has size ≥ cache B's size, then A's hit rate ≥ B's hit rate (within epsilon_1)."

## Files

- **`relations.py`** — 15 relation definitions (facts F1–F2 and relations R1–R14). Each relation is a dict with:
  - `name`: identifier (F1, R1, etc.)
  - `kind`: how the data is sourced and paired — `single`, `interval`, `cross_config`, `opt`, or `unsupported` (see below)
  - `base_metrics`: metric names each entity needs (suffix-free); the evaluator resolves them per entity and appends the kind's suffix
  - `premise`: callable filtering which pairs the claim applies to (None for single-entity facts)
  - `claim`: callable that checks if the relation holds for given data
  - `epsilon_name`: name of slack variable (or None for exact relations)
  - `requires`: list of (suffixed) metric names the claim needs

- **`eval.py`** — Evaluator. Discovers the data layout, pairs datasets per relation `kind`, resolves metrics, checks premise + claim, and reports HOLDS / VIOLATED / VACUOUS / UNEVALUABLE per pair. Outputs to eval_results.txt.

- **`corpus.md`** — Human-readable LaTeX descriptions of each relation (unchanged).

- **`system-relations.md`** — Coupled epsilon systems and interactions (unchanged).

## Running

```bash
# Auto-discover the standard data layout and evaluate EVERY relation with the
# pairing its kind implies. This is the normal mode.
python3 eval.py

# Or point it at explicit stats files (single-entity + interval pairing only;
# cross_config / opt relations need the discovered layout and report UNEVALUABLE)
python3 eval.py /path/to/stats.txt [more/stats.txt ...]

# Results go to eval_results.txt
```

### Data layout (auto-discovery)

The evaluator expects two sibling directories of this repo:

- `../gem5-configs/output/<bench>/stats.txt` — the default (8 MiB LLC) run, with
  several ROI windows per file (delimited by `Begin/End Simulation Statistics`).
- `../gem5-configs/output/<bench>/llc_4MiB/<bench>/stats.txt` and `llc_16MiB/...`
  — LLC size variants, used for cross-config pairing (R1).
- `../output-opt/<bench>/trace_llc_results.txt` — per-ROI Belady-OPT hit rates,
  paired window-by-window against the policy run for R3.

Only benchmarks with comparison data (an LLC variant or an OPT trace) are
evaluated; smoke-test dirs like `ls_test` are skipped.

### How each `kind` is paired

| kind | source | keys | covers |
|------|--------|------|--------|
| `single` | one ROI window | bare (`hit_rate`) | F1, F2, R7, R11 |
| `interval` | ordered ROI-window pairs within one file | `_a` / `_b` | R5, R6, R14 |
| `cross_config` | ordered LLC config pairs with size_a ≥ size_b | `_a` / `_b` (+ `size_a`/`size_b`) | R1 |
| `opt` | OPT trace window vs policy window | `_opt` / `_policy` | R3 |
| `unsupported` | — | — | R2, R4, R9, R10, R12, R13 (data not collected) |

`unsupported` relations are reported as UNEVALUABLE **with the specific missing
metrics named**, so it's clear *why* (e.g. R12 needs a 4C miss decomposition gem5
doesn't emit; R2/R4 need associativity-variant runs; R9/R10 need prefetch on/off
runs; R13 needs multi-core coherence traffic).

> Note: this data has no `lsq0.criticalMissRate` or per-`ReadReq`/`WriteReq` LLC
> stats, so R6's `critical_hit_rate` and R7/R11's load/store hit rates fall back
> to the overall LLC hit rate. Collect those stats to make those relations sharp.

## Adding a New Relation

1. Add a dict to `relations.py` with `name`, `description`, `kind`, `base_metrics`, `premise`, `claim`, `epsilon_name`, `requires`.
2. Add it to `ALL_RELATIONS` list.
3. Update the assert at the bottom.

Example (an interval relation pairing ROI windows):

```python
R_my_claim = {
    'name': 'R15',
    'description': 'my claim about cache behavior',
    'kind': 'interval',                       # how eval.py pairs the data
    'base_metrics': ['outcome', 'driver'],    # suffix-free; resolver appends _a/_b
    'premises': ['driver_a >= driver_b'],     # human-readable
    'premise': lambda d: d.get('driver_a', 0) >= d.get('driver_b', 0),
    'claim': lambda d: d.get('outcome_a', 0) >= d.get('outcome_b', 0),
    'epsilon_name': 'eps_15',
    'requires': ['outcome_a', 'outcome_b', 'driver_a', 'driver_b'],
}

ALL_RELATIONS.append(R_my_claim)
assert len(ALL_RELATIONS) == 16  # update count
```

If the new claim needs data we don't yet collect, set `kind='unsupported'` and
list the needed `base_metrics` — it will report UNEVALUABLE naming them, rather
than silently failing.

## Metric Resolution

`resolve_metric(stats, name)` resolves a high-level metric against one gem5 stats
dict, prioritizing the LLC (L3) cache:
- `hit_rate` → `overallHits / overallAccesses` (falls back to `(accesses - misses)/accesses`)
- `miss_rate` → `overallMisses / overallAccesses`
- `load_hit_rate`, `store_hit_rate` → `ReadReq`/`WriteReq` hit rates (fall back to overall)
- `critical_hit_rate` → `1 - lsq0.criticalMissRate` (falls back to overall hit rate)
- `stalls` → `numCycles - simInsts`
- `evictions` (→ `replacements`), `writebacks`, `total_misses` → direct stat lookups

The cross-config and OPT drivers supply some keys directly (`size_a`/`size_b`
from the config dir; OPT hits/accesses from the trace), so those don't go through
`resolve_metric`. Returns `None` for anything it can't resolve — which is how
`unsupported` relations surface their missing metrics. Add more cases in
`resolve_metric()`. Stats are under `board.cache_hierarchy.llcache.*`.

## Design Notes

- Relations are just dicts + lambdas — no heavy AST machinery.
- Each claim is a pure function: `data dict -> bool`.
- Epsilon computation is intentionally simple for now (can be refined later).
- Single-entity relations (F1, F2) check properties of one dataset.
- Multi-entity relations compare two or more datasets (pass them as separate keys in data dict).
