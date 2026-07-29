# Victim-cache vs. NINE — formal comparison via Z3

## What this project is

We are using Z3 (Python bindings, `from z3 import *`) to determine whether a
memory hierarchy whose L3-position structure is a **victim cache** can ever incur
*more* total DRAM lookups than a hierarchy with an independent **NINE**
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
victim-vs-NINE structural difference. Likewise `w2` is shared. Both designs
count DRAM lookups identically. Every gap must be attributable to *policy
structure* alone.

### Cache representation

A cache is an ordered list of line-labels (Z3 **bit-vectors** of width
`ceil(log2(K + max_ways))`): **position 0 = MRU, last position = LRU**. A real
line is a label in `[0, K)`; empty slot i holds the sentinel `K + i` (a value no
real access can match; all comparisons unsigned — `ULT`/`ULE`). Bit-vectors keep
the whole model in QF_BV, which bit-blasts to SAT instead of invoking the
integer-arithmetic engine — this is the main state-space-explosion mitigation.

The trajectories are **If-term DAGs, not fresh variables**: each `step_*` returns
next-state terms over the current state, so the N access symbols are the
formula's only free variables (canonical bounded-model-checking form). Cost sums
stay in Int arithmetic; only cache contents and accesses are bit-vectors.

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
    = #(NINE-L3 hits among L2-miss steps) - #(victim-cache hits among L2-miss steps)
```

Report this derived integer hit-count difference alongside the raw DRAM-lookup
totals — it is the interpretable, hand-checkable quantity.

## Cost model

The cost of a design is simply its **count of DRAM lookups** — L2 and L3
latencies are treated as negligible and no latency constant is modeled. Each
access that misses both L2 and the mid level costs 1; every hit costs 0.

```
L2 hit or mid-level hit     : 0
L2 miss and mid-level miss  : 1     (one DRAM lookup)
```

This isolates the gap to the *frequency* of mid-level hits conditioned on L2
miss — the structural difference between NINE L3 (independent) and victim cache
(exclusive). Because both designs count DRAM lookups identically, any gap is
attributable to *policy*, not asymmetric hardware costs. `gap = C_victim -
C_NINE` is therefore already the hit-count difference, with no scale factor.

## The query

```
Hypothesis H:  C_victim <= C_NINE   for every trace of length N over alphabet K
Search:        assert the negation (C_victim > C_NINE); on SAT, maximize the gap
               by binary search over "gap >= g" on one incremental Solver
               (push/pop). NOT z3.Optimize — MaxSMT disables preprocessing and
               is far slower than plain SAT probes on this encoding.
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
- **Search**: feasibility check on `gap >= 1` first; on SAT, binary-search the
  maximal gap with incremental `push`/`pop` (see "The query"). `gap <= N` bounds
  the search.
- **QF_BV encoding + term-DAG trajectories** (see "Cache representation"):
  bit-vectors, not Ints; no per-timestep state variables. Do not reintroduce
  either — the Int/fresh-variable encoding took ~30 min where this takes ~2.5.

## Coding principles (STRICT — the user cares about these)

- **`from z3 import *`** at the top; use `Or`, `And`, `If`, `Int`, etc. directly
  (no `z3.` prefix).
- **Self-documenting code.** `model.py` is *the model* — keep it lean. No module
  docstrings, no verbose per-function prose. At most a **precise** one-to-few
  line comment above a function explaining behavior. Extended prose belongs in
  separate docs, not in `model.py`.
- **Comments minimal — no huge comment blocks.** One or two lines maximum per
  comment; never multi-paragraph. Do not explain rationale, encoding choices, or
  why an approach is correct in code comments — that goes in CLAUDE.md or
  `analysis.txt`. If a comment needs a third line, it belongs in a doc.
- **Precise, non-casual comment language.** The user rejected phrasing like
  "pluck out" / "falls off". Say "remove line_to_find if present", "the LRU entry
  is evicted", etc. Comments must read precisely months later.
- **Blank lines between logical groups** within a dataclass/function (e.g. group
  the way-counts and the bounds in `Params`).
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

- `cache_state` — ordered list of line-label bit-vector terms for one structure
  at one timestep (position 0 = MRU).
- `line_label` — one line identifier (Z3 bit-vector).
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

- **State scaffolding**: `Params` dataclass (`w2, w3, N, K`; `w3` is the single
  shared L3 size); `init_empty(num_ways, K, width)` → concrete sentinel
  constants `K, K+1, ...`; `constrain_trace(access_sequence, K)` → each access
  in `[0, K)` plus the restricted-growth canonical labeling (exact S_K symmetry
  reduction).
- **Primitives**: `is_present`, `lru_line`, `updated_cache` — the single
  strict-LRU update (remove `line_to_find` if present, place `line_to_insert` at
  MRU, else evict LRU), reused for every structure.
- **Transitions**: `step_l2(l2_now, access)`, `step_nine(l3_now, access,
  l2_hit)`, `step_victim(l2_now, victim_now, access, l2_hit)`. Each returns
  `(next_state_terms, hit_flag)` — terms, not constraints.
- **Assembly**: `build_model(params)` unrolls both designs over one shared
  symbolic trace, returning a `Bundle` dataclass (trace constraints,
  access_sequence, all trajectories, both DRAM-lookup sums, per-step hit-flag
  vectors). Cost is inlined at the accumulation site: `+= If(Not(Or(l2_hit,
  mid_hit)), 1, 0)`.
- **Search + report**: `solve_for_counterexample(bundle, params)` (feasibility
  check, then binary-search maximization; returns `(model_or_None, result)`) and
  `report_result(model, result, bundle, params)` (full witness on SAT with the
  hand-checkable hit-count difference; bounded-result note on UNSAT).

**Verification already run and passing** (concrete-value sanity checks):
- `updated_cache` hit / miss / swap all correct (move-to-front, insert-evict,
  one-out-one-in victim swap).
- `step_nine` / `step_victim` correct across all hit/miss combinations.
- **Shared-L2 spine PROVEN**: `Or(l2_next_nine[i] != l2_next_victim[i])` over a
  symbolic L2 + access is `unsat` — the designs can never disagree on L2.
- `access_cost` counts DRAM lookups only (0 on any hit, 1 on both-miss).
- `build_model` smoke test: `sat`, trace `[0,0,0,0]`, both costs 0, gap 0.

## Where the research stands (see `analysis.txt` for full detail)

- **Bounded result solid**: H (C_victim <= C_NINE) is UNSAT at every swept
  (N, K, w2, w3). Mechanism: exclusivity gives the victim design the larger
  distinct non-L2 footprint (NINE's L3 wastes slots on L2 duplicates).
- **Set invariant `(L3 \ L2) subset victim`**: TRUE on all reachable states in
  the bound, but NOT inductive (the inductive step is SAT from unreachable
  pre-states). Cause: NINE orders recency by access, victim by eviction.
- **Do NOT propose differential L3 sizing** (`w3_nine != w3`): the same-size
  comparison is definitional (see "Same-size L3 is definitional"). `model.py`
  models `w3` only; do not reintroduce a per-design sizing knob.
- **Do NOT propose "pure exclusive L3 vs. victim L3, same size"**: at whole-L3
  scale these are the identical state machine (`step_victim` IS the exclusive
  policy); gap == 0 by construction. Exclusivity forbids demand-path insertion
  definitionally, so it cannot be isolated as an independent knob.

## Next steps (ranked — pursue in this order)

1. **Ghost-cache decomposition (first)**: add a reference LRU of size
   `w2 + w3` driven by the same trace, then prove two separate
   invariants: (a) `L2 ∪ victim == ghost contents` (classical exclusive-
   hierarchy stack property, likely inductive as-is) and (b)
   `NINE-L3 subset ghost`. Together they imply stepwise dominance; each half
   relates one design to a canonical object instead of coupling the two
   recency orders directly.
2. **Automated invariant synthesis**: encode the transition system as CHCs for
   Z3's Spacer (IC3-style), or try cheap k-induction.
3. **Small-model lemma for K**: show any counterexample relabels into
   `w2 + 2*w3 + 1` distinct lines, making each bounded UNSAT hold for all K.
4. **Off-ramp if induction stalls (timeboxed)**: quantify the victim's win
   (maximize `C_NINE - C_victim`), vary the victim insertion position, extend
   to non-LRU policies. Publishable characterization even without the theorem.
