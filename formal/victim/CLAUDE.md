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

- **Victim** `{ L2, victim-L3 }` — the L3-position structure is a victim cache.
  A line enters it **iff** it was just evicted from L2 (never a direct DRAM
  fill). On an L2 miss that hits the victim cache, the accessed line is **swapped
  out** of the victim cache into L2 while L2's evicted line takes its place —
  exclusive: no line is ever in both L2 and the victim cache at once.

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

Implemented and tested on concrete values:

- `Params` dataclass (`w2, w3, v, N, K, l2, l3, ld`).
- `fresh_cache(name, num_ways, timestep)` → list of named Z3 Ints
  (`L2_t3_slot0`, ...).
- `init_empty(cache_state)` → constrains slots to distinct negative sentinels
  `-1, -2, ...`.
- `constrain_trace(access_sequence, K)` → each access in `[0, K)`.
- `is_present(cache_state, line_label)` → Z3 Bool (hit).
- `lru_line(cache_state)` → `cache_state[-1]`.
- `updated_cache(cache_state, line_to_find, line_to_insert)` → the single
  strict-LRU update, returned as a new list of Z3 expressions. This is the ONLY
  cache-update logic in the whole model — reused for every structure.

**Verification already run and passing** (concrete-value sanity check):
- `init_empty` → `[-1, -2, -3]`.
- `constrain_trace` rejects `access == K` (unsat).
- `updated_cache` hit `[20,10,30,40]` (moved to MRU, LRU 40 survives); miss
  `[99,10,20,30]` (LRU 40 evicted); swap find=20 insert=99 → `[99,10,30,40]`
  (20 removed, 99 at MRU, 40 survives — one-out-one-in, the victim swap).

See `TODO.md` for exactly what to build next and how.
