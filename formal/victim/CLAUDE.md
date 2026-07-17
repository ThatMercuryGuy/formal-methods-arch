# Victim-cache vs. NINE — formal comparison via Z3

## What this project is

We are using Z3 (Python bindings, `from z3 import *`) to determine whether a
memory hierarchy whose L3-position structure is a **victim cache** can ever cost
*more* total lookup cycles than a hierarchy with an independent **NINE**
(non-inclusive, non-exclusive) L3, under some load trace.

We follow the **CCAC methodology** (Arun et al., "Toward Formally Verifying
Congestion Control Behavior", SIGCOMM 2021): model each design as a
deterministic state machine, leave the input (here: the access trace) as a
**free symbolic variable** constrained only by what is definitionally true,
negate the performance hypothesis, and ask the solver to construct a
counterexample trace. We do **not** hand-pick traces or hypothesize what the bad
case looks like — Z3 does the talking.

Loads only: every line is clean, eviction is a silent discard, fill is a pure
fetch. No stores, dirty state, writebacks, or coherence.

## The two systems being compared

Both are whole hierarchies driven by **one shared symbolic trace**, and both
share the **exact same L2** (same ways `w2`, same strict-LRU policy).

- **NINE** `{ L2, L3 }` — L2 and L3 are independent LRU caches. On any L2 miss,
  L3 is probed and updated (promote on L3 hit, DRAM-fill on L3 miss); on an L2
  hit L3 is untouched. Redundant: a line can live in both L2 and L3.

- **Victim** `{ L2, victim-L3 }` — the **entire L3** is managed as a victim
  cache (as in industry, not a small separate structure beside L2). A line
  enters it **iff** it was just evicted from L2 (never a direct DRAM fill). On an
  L2 miss that hits the victim cache, the accessed line is **swapped out** of the
  victim cache into L2 while L2's evicted line takes its place — exclusive: no
  line is ever in both L2 and the victim cache at once.

**Same-size L3 is definitional, not a fairness knob.** Both designs repurpose the
*same* physical L3 storage, so `v == w3` **always**. Comparing different L3 sizes
(`w3 != v`) is meaningless — any gap would be a capacity artifact, not the
victim-vs-NINE structural difference. Likewise `w2` is shared. All latencies are
equal across designs. Every gap must be attributable to *policy structure*
alone.

### Cache representation

A cache is an ordered list of line-labels (Z3 Ints): **position 0 = MRU, last
position = LRU**. A real line is a label in `[0, K)`; an empty slot is a distinct
**negative** sentinel (so it can never match a real access).

## Key structural fact (exploited, not asserted)

L2's next state depends **only** on L2's current state and the access — it never
reads the lower level. On an L2 miss the accessed line installs into L2
regardless of whether L3, the victim cache, or DRAM served it. Therefore L2's
trajectory and its per-step hit flag `l2_hit` are **identical** across both
designs. We build **one shared L2** and feed both designs from it. All cost
divergence is isolated to the lower-level probe on L2-miss steps, and the gap
collapses algebraically to:

```
gap = C_victim - C_NINE
    = ld * [ #(NINE-L3 hits among L2-miss steps) - #(victim-cache hits among L2-miss steps) ]
```

Report this derived integer hit-count difference alongside raw cycle totals — it
is the interpretable, hand-checkable quantity.

## Cost model

Cumulative — each access pays for every tier probed until it resolves:

```
L2 hit                    : l2
L2 miss, mid-level hit     : l2 + l3          (mid = L3 for NINE, victim cache for Victim)
L2 miss, mid-level miss    : l2 + l3 + ld     (DRAM always resolves)
```

Latencies are deliberately **equal across designs** (`l2` shared; `l3` charged
for both L3 and victim-cache lookups) so any gap is attributable to *structure*,
not to a latency asymmetry.

## The query

```
Hypothesis H:  C_victim <= C_NINE   for every trace of length N over alphabet K
Search:        z3.Optimize, assert the negation (C_victim > C_NINE), maximize(gap)
```

- **UNSAT** → H holds up to this bounded `(N, K)` — a bounded result, NOT a
  general proof. Report the bound explicitly.
- **SAT** → report the full trace, both full state trajectories, `C_NINE`,
  `C_victim`, `gap`, and the derived integer hit-count difference so the result
  is hand-checkable.

## Design decisions already settled (do not relitigate)

- **Shared L2**: one L2 trajectory feeds both designs (identity true by
  construction). `step_victim` takes `l2_now` / `evicted_from_l2` as given inputs.
- **Z3-only**: NO plain-Python reference simulator. Hand-checkability comes from
  printing the trajectory and the derived hit-count difference.
- **Search**: `z3.Optimize` with `maximize(gap)` subject to `gap > 0`.

## Coding principles (STRICT — the user cares about these)

- **`from z3 import *`** at the top; use `Or`, `And`, `If`, `Int`, etc. directly
  (no `z3.` prefix).
- **Self-documenting code.** `model.py` is *the model* — keep it lean. No module
  docstrings, no verbose per-function prose. At most a **precise** one-to-few
  line comment above a function explaining behavior. Extended prose belongs in
  separate docs, not in `model.py`.
- **Precise, non-casual comment language.** The user rejected phrasing like
  "pluck out" / "falls off". Say "remove line_to_find if present", "the LRU entry
  is evicted", etc. Comments must read precisely months later.
- **Blank lines between logical groups** within a dataclass/function (e.g. group
  the way-counts, the bounds, the latencies in `Params`).
- **Small functions, one at a time.** Build incrementally in dependency order and
  verify each layer before moving on. Do NOT dump large chunks of code. The user
  wants to work through functions individually and understand each.
- **No assumptions about the adversarial workload — explicit or implicit.** The
  trace is constrained ONLY by `access in [0, K)`. Never bias, seed, shape,
  order, or hint the trace; never special-case a value; never add a constraint
  that narrows the search on a hunch. Only the cold-start initial state and the
  LRU transition rules are encoded. If a constraint is not a
  physical/definitional truth about the hardware, it does not go in.

## Vocabulary (use these exact names)

- `cache_state` — ordered list of line-label Z3 Ints for one structure at one
  timestep (position 0 = MRU).
- `line_label` — one line identifier (Z3 Int).
- `access` — the line requested this timestep (`a_t`); Z3's only free variable.
- `evicted_from_l2` — the line L2 pushes out to admit a new line (`e_t`);
  computed as `lru_line(l2_now)`. Derived, never freely chosen.
- `line_to_find` / `line_to_insert` — the two args of `updated_cache`: the line
  to remove (search for) and the line to place at MRU. Equal for an ordinary LRU
  access; they differ only in the victim swap.

## Environment

- Working dir: `/home/rao/research/project-code/formal/victim/`
- Single file: `model.py` (started fresh; ignore conventions in `../mlp-code`).
- z3-solver **4.17.0** installed and confirmed working (`python3 -c "import z3"`).
- Run: `python3 model.py`.

## Current state of `model.py` (DONE and verified)

The whole model is implemented and unit-verified. Only the end-to-end run +
hand-check of the witness remains. Layers, in dependency order:

- **State scaffolding**: `Params` dataclass (`w2, w3, v, N, K, l2, l3, ld`);
  `fresh_cache(name, num_ways, timestep)` → named Z3 Ints (`L2_t3_slot0`, ...);
  `init_empty(cache_state)` → distinct negative sentinels `-1, -2, ...`;
  `constrain_trace(access_sequence, K)` → each access in `[0, K)`.
- **Primitives**: `is_present`, `lru_line`, `updated_cache` — the single
  strict-LRU update (remove `line_to_find` if present, place `line_to_insert` at
  MRU, else evict LRU), reused for every structure.
- **Transitions**: `step_nine(l2_now, l3_now, access, l2_next, l3_next)` and
  `step_victim(l2_now, victim_now, access, l2_next, victim_next)`. Each returns
  `(constraints, l2_hit, mid_hit)`.
- **Cost**: `access_cost(l2_hit, mid_hit, params)` — one function for both
  designs.
- **Assembly**: `build_model(params)` unrolls both designs over one shared
  symbolic trace, returning a `Bundle` dataclass (constraints, access_sequence,
  all trajectories, both cost sums, per-step hit-flag vectors). The gap
  `cost_victim - cost_nine` is used inline at the `maximize` site (trivial
  one-liners are inlined, not wrapped).
- **Search + report**: `solve_for_counterexample(bundle, params)` (Optimize,
  negated hypothesis, maximize gap) and `report_result(opt, result, bundle,
  params)` (full witness on SAT with the hand-checkable hit-count difference;
  bounded-result note on UNSAT).

**Verification already run and passing** (concrete-value sanity checks):
- `updated_cache` hit / miss / swap all correct (move-to-front, insert-evict,
  one-out-one-in victim swap).
- `step_nine` / `step_victim` correct across all hit/miss combinations.
- **Shared-L2 spine PROVEN**: `Or(l2_next_nine[i] != l2_next_victim[i])` over a
  symbolic L2 + access is `unsat` — the designs can never disagree on L2.
- `access_cost` gives 1 / 11 / 111 for the three tiers.
- `build_model` smoke test: `sat`, trace `[0,0,0,0]`, both costs 114, gap 0.

See `TODO.md` for the one remaining item (first real run + hand-check).
