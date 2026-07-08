# CLAUDE.md

Guidance for working in this directory (`formal/`).

## What this is

A single-file Z3 (C++ API) Bounded Model Checking engine, `mlp.cpp`, that tests
the architectural dogma *"more Memory-Level Parallelism is always better."* It
unrolls `UNROLL_DEPTH` memory requests over two state machines (`System_HighMLP`,
`System_LowMLP`) that share one synthesized workload and differ only in their
MSHR / outstanding-miss window (`WINDOW_HIGH` vs `WINDOW_LOW`, the per-machine
`window`). The workload includes a per-request memory latency drawn by the solver
from `[LAT_MIN, LAT_MAX]` and shared by both machines, so completions reorder
(the memory controller returns loads out of issue order) and MSHR gating is
occupancy-based. A standard `z3::solver` (NOT `z3::optimize` —
the latter was measured 200×+ slower on this model; see "Why not `z3::optimize`?")
searches for a workload where `completion_HighMLP > completion_LowMLP`. Wrong-path speculation is
**always on** — it is the model's sole anti-MLP mechanism.

## Build & run

```
g++ -std=c++23 mlp.cpp -lz3 -o mlp -O3 -march=native
./mlp           # default 60s solver timeout
./mlp 120       # optional first arg = solver timeout in SECONDS (0 = unlimited)
```

Z3 4.x with `z3++.h` is installed system-wide (`/usr/include`, `libz3.so`).
g++ 15.2 (full C++23). There is no test harness or build script — compile and
run directly. The model is deterministic; the same config yields the same model.

The optional first CLI argument sets the solver timeout in seconds (default 60,
`0` = no timeout). It applies to both the discovery `check()` and every
maximization probe, so a larger value lets the worst-case search climb further.

The banner prints the full config identifiers (`UNROLL_DEPTH`, `LAT_MIN`/`LAT_MAX`,
`ADMISSION_GAP`, `WINDOW_HIGH`/`WINDOW_LOW`). The per-request tables keep compact
display labels (`request`, `arrival`, `branch-path`; `BR`/`wp` for the mispredicted
branch and its wrong-path shadow) so the columns stay readable.

**Solver tuning.** The code sets `sol.set("arith.solver", 2)`, which selects Z3's
Simplex-based arithmetic core instead of the default LRA core (`=6`). This is a
**search strategy only** (model-preserving), and is faster on this model because it
is dominated by difference-logic-style definitional equalities (`service_start`/`service_end`
chains) that Simplex bound propagation handles well.

**Do NOT set `arith.solver=1`.** Value `1` is Z3's Bellman-Ford *difference-logic-
only* engine. It is genuinely faster on the pure diff-logic subset, but it is
**incomplete for this model**, which also contains a genuine non-difference-logic
constraint: the `Σ ite(...)` counting sum for the `shadow_len` cap. Bellman-Ford
cannot represent it. At small `N` it merely warns (`smt.diff_logic: non-diff logic
expression ...`), but at the default `N=12` that sum grows and it **hard-aborts**
with `Overflow encountered when expanding vector`. Simplex (`=2`) is the complete,
non-crashing engine.

**Do NOT replace the `shadow_len` cap with `z3::atmost` (native cardinality).** It is
a tempting condense — the `Σ ite(...)` sum plus `<= MAX_SHADOW` is exactly an
at-most-k constraint, and Z3 has `z3::atmost` for it. It is a **measured regression**:
A/B at the default config (`N=12`, `MAX_SHADOW=8`, min-of-3) makes the discovery query
(`Delta>0`, which runs on *every* invocation) **~3× slower** (3.4 s → 10.9 s) and turns
two previously-solvable maximization probes (`Delta>=5`, `Delta>=10`) into 15 s
timeouts. The reason mirrors the `arith.solver` story: the whole model lives in the
Simplex arithmetic core, and the `Σ ite` sum composes inside that one theory, whereas
`z3::atmost` dispatches to a **separate pseudo-boolean engine** — adding a second
reasoning domain instead of staying in the tuned one. Keep the arithmetic sum.

## Where to change things

All knobs are in `namespace cfg` at the top of `mlp.cpp`:
- `UNROLL_DEPTH` — unroll depth (currently `12`).
- `LAT_MIN`, `LAT_MAX` — the per-request memory-latency range (currently `6`–`18`).
  Each request's latency is a solver-chosen integer in this range, **shared** by
  both machines. A short draw is a row-buffer hit, a long draw a row-miss
  (precharge+activate+CAS). Because latency varies, a later request can complete
  before an earlier one — this **is** the memory controller reordering completions.
  Set `LAT_MIN == LAT_MAX` for the constant-latency (in-order-completion) corner.
- `ADMISSION_GAP` — channel inter-admission gap (`1/bandwidth`, `< LAT_MIN`):
  the channel admits a new request every `ADMISSION_GAP` cycles, so requests
  overlap in flight (latency hiding).
- `WINDOW_HIGH`, `WINDOW_LOW` — the MSHR windows of the two machines (the only knob
  that differs between them). Currently `4` vs `2`.
- `MAX_SHADOW` — cap on the number of wrong-path (squashed) requests, for
  tractability. Default `8`. The binary prints the cap so it is never a silent
  truncation of the search space. Set it too low and it silently *ceilings* the
  wide machine's wrong-path depth, so the reported deviation becomes a cap artifact
  rather than a property of the model.
- `RESOLVE_DELAY` — branch resolves at `resolve = service_end[branch] + RESOLVE_DELAY`.
  Default `3`, modeling compare+redirect latency (`0` — resolve the instant the
  condition-load's data returns — is the unrealistic conservative corner: it
  understates the effect because the shorter wrong-path window lets the wide
  machine issue fewer speculative misses).

Changing the comparison (e.g. "4 vs 2 MSHRs") means editing `WINDOW_HIGH` /
`WINDOW_LOW` only — nothing else.

**Code layout.** `mlp.cpp` is organized top-to-bottom as: `namespace cfg` (knobs) →
small expression helpers (`zmax`, `zmin`, `bool_to_int`) → the `Timeline` data holder
→ `Namer` (definitional naming: every modeled quantity is a fresh const asserted equal
to its defining expression, which is what keeps the encoding Simplex-friendly) → the
per-request timeline **stages** → the two workload builders → the CLI/reporting block.

The per-request physics is split into one function per discrete stage, all invoked in
program order by the `build_machine` loop; **the order these emit their `sol.add`s is
the model**, so preserve it when editing:
`occupancy_gate` (MSHR-slot gating → `present`) → `admit` (pipelined channel →
`chan_free`, `service_start`) → `add_service` (`live`, `service_end`) →
`mshr_release` → `writeback_slot` (hand the entry back into the earliest-free slot).
After the loop, `pin_resolve` pins `resolve = service_end[branch] + RESOLVE_DELAY` and
`compute_completion` takes the max `service_end` over correct-path requests. The shared
workload is built by `synthesize_workload` (arrivals + latencies) and `add_speculation`
(branch, `squashed[]`, contiguity, the `shadow_len` cap). Everything from `parse_timeout`
down is CLI + witness printing (`print_*` helpers, `maximize_delta`) and touches no
model term. To add a per-request quantity, add a stage function and a `Timeline` vector;
to change the objective search, edit `maximize_delta` only.

## Critical modeling fact — do not regress this

A pipelined, work-conserving channel with variable memory latency is **monotone in
`window`** with the shadow empty: with a shared workload, a larger window lets
requests present no later, so completion times can only fall, and
`completion_HighMLP > completion_LowMLP` is **UNSAT**. The channel admits a new
request every `ADMISSION_GAP` cycles (`ADMISSION_GAP < LAT_MIN`):

- **Pipelined finite bandwidth (the MLP *benefit*).**
  `service_start[j] = max(present[j], service_start[j-1] + ADMISSION_GAP)`.
  Because admission is faster than service, requests **overlap in flight** — a
  wider window packs them tighter against the `ADMISSION_GAP` bound and finishes
  baseline work earlier. This is the latency hiding MLP exists to exploit. Service is
  `service_end[j] = service_start[j] + latency[j]`, where `latency[j]` is the
  shared solver-chosen draw in `[LAT_MIN, LAT_MAX]`.

- **Occupancy-based MSHR gating.** Because `latency[j]` varies, `mshr_release[j]`
  (`= service_end[j]`) is **not** monotone in `j` — completions reorder. So a
  request cannot be gated on a fixed prior index; it is gated on **occupancy**:
  `present[j] = max(arrival[j], min-over-slots slot_free)`, where the machine
  carries `window` interchangeable slot registers `slot_free[]` (the cycle each
  MSHR entry next frees). Request `j` presents once the **earliest** slot frees,
  then hands its own `mshr_release[j]` back into that slot. This is the exact
  order-statistic "≥ `window` entries occupied ⇒ stall" semantics; when latency is
  constant (releases in issue order) it collapses to gating on the request `window`
  slots back, the in-order-completion special case.

Without speculation, admission is in program order and every request
reaches the bus, so both machines see the identical admission sequence and the
identical `latency[j]`; only `window` shifts start
times, and a wider window presents no later. The pipelined channel with variable
latency is therefore monotone in `window` → **UNSAT** (verified: guard is UNSAT with
the shadow empty — variable latency / reordered completion does **not** by itself
break the dogma). This is the strict-generalization guard, and it is why the
falsification below is attributable to speculation alone. Recover the guard by
forcing the shadow empty: set `MAX_SHADOW=0` (equivalently, assert
`!squashed[i]` for all `i`).

**Wrong-path speculation (Strategy B) is the sole anti-MLP mechanism — and it
falsifies the dogma.** A single mispredicted branch `branch` is fetched and the
front-end speculatively issues the **shadow** of wrong-path requests after it
until the branch **resolves** at `resolve = service_end[branch]`, at which point
the shadow is **squashed**. The branch index `branch` and the wrong-path set
(shared bool tags `squashed[i]`, a contiguous block `{branch+1,…,shadow-end}`)
are part of the **shared workload** — both machines see the identical
misprediction. But the speculation *depth* — how many shadow requests actually
issue before `resolve` — is **per-machine and emerges from the schedule**:
`live[j] = ¬squashed[j] ∨ (service_start[j] < resolve)`. A wide window issues
deeper down the wrong path (more `live` shadow requests) than a narrow window,
which stalls MSHR-gated after one or two.

**The load-bearing resource is MSHR occupancy, not the bus.** A `live` shadow
request is an in-flight miss that holds its MSHR entry until `service_end`, and it
**cannot be un-sent** — an in-flight DRAM miss must keep its slot to sink the
returning fill (allocation is in-order at rename; the MSHR file does **not** skip).
So the wide machine's MSHR file is occupied by *dead wrong-path misses* at the
moment the **correct-path** tail needs slots (`present[j]` waits on the earliest of
the `window` occupied slots to free), pushing correct-path completion later. A
narrow shadow request that never issued (`!live`) frees its slot at squash `resolve`
instead — so the narrow window, precisely *because* it is throttled, never launches
those misses and its slots stay free for real work. The narrow window's MSHR limit
is acting as an **accidental wrong-path filter**. This attribution is empirical: the
effect is flat across an order of magnitude of channel bandwidth (`ADMISSION_GAP`
1→4), so it is **not** bus-admission contention — the bus admission chain does skip
non-live shadow requests
(`chan_free[j] = live[j-1] ? service_start[j-1]+ADMISSION_GAP : chan_free[j-1]`), but
zeroing that channel out leaves the deviation unchanged. `completion` counts
**correct-path completions only** (`squashed` requests never retire), so the wide
window can finish the *real* work later. Forcing every `squashed[i]` false
(e.g. `MAX_SHADOW=0`) ⇒ every `live` true ⇒ no skipping ⇒ the pure monotone pipelined
bus above.

**Why this is honest and not rigged.** The anti-MLP cost must *emerge* from the
schedule, not be injected. The wrong-path shadow is honest for exactly this
reason: `branch` and `squashed[]` are one shared quantity per physical request
(both machines mispredict the same branch and fetch the same wrong-path stream),
so the solver cannot give the wide machine a deeper misprediction. Only
`live`/`resolve`/`service_start` are per-machine, and the depth
(`#{i : squashed[i] ∧ live[i]}`) is decided by the **schedule**
(`service_start[i] < resolve`), which differs between machines only through
`window`. Do **not** implement this as "a fixed per-request waste tax on the wide
machine" or "make the shadow length depend on `window` directly" — that
reintroduces a
monotone-by-construction artifact the whole project exists to avoid. The shadow
set is shared; only issue-depth is per-machine, and only through
`service_start`/`resolve`.

**Do not add convex queueing contention as a falsifier.** A tempting second
anti-MLP mechanism is a channel crowding cost: an
`inflight[j] = #{ i<j : service_end[i] > service_start[j] }` count (requests
still in service when `j` starts), a convex two-knee penalty `Pen[j]` on it
(free below `C = LAT_MIN/ADMISSION_GAP`, `+PEN_LO` past `C`, `+PEN_HI`
more past `C2 = C+2`), fed into both completion
(`service_end = service_start + latency[j] + Pen`) and the next admission
(backpressure:
`service_start = max(present, service_start[j-1] + ADMISSION_GAP + Pen[j-1])`).
**Contention alone never breaks the dogma**: with the shadow empty it is UNSAT with
contention on *and* off. The free-concurrency `C = LAT_MIN/ADMISSION_GAP`
is generous, and
admission backpressure is negative feedback that *bounds* divergence, so the wide
window's latency-hiding benefit always covers its queueing cost. A *second,
independent* falsifier grounded in memory-controller queueing (rather than branch
misprediction) would need a much steeper near-saturation curve than that bounded
two-tier ramp to have any chance of flipping SAT — an open question, not a settled
result. Do not add it speculatively.

**Do not add a dependency subsystem as an amplifier.** Another tempting addition is
a shared data-dependency matrix `Dep[i][j]` (does request `j` consume `i`'s
result?), turned into a timing constraint through an
`Aeff[j] = max(arrival[j], max{ service_end[i]+1 : Dep[i][j] })` causality loop,
bounded to a reorder window `ROB_SIZE`, with the number of mutually-independent
requests in that window capped at `MAX_LSQ_MLP` (a second `Σ ite` sum). It does
no work on either anchor: the guard (empty shadow) stays **UNSAT** and the
falsifier still proves the **same maximal `Delta`** — with dependencies
available, Z3 could try to climb past that maximum through them and cannot. In principle
it *could* bear on the result — with speculation on, completion is not monotone
in `window`, so a dependency reading the machine's own `service_end[i]` can
propagate a wrong-path-induced delay onto a correct-path consumer — but that
amplifier does no measurable work at these bounds, and an LSQ cap is a pure
*restriction* (adding a constraint only narrows the feasible set, so it can only
hide counterexamples, never create one). Requests present directly at their
arrival `arrival[j]`. If a future witness turns up — e.g. a larger-`UNROLL_DEPTH`
or wider-`WINDOW_HIGH` regime — where a dependency changes an outcome, add it
reasoned from that concrete counterexample, not speculatively.

## Maximizing the deviation (worst case)

After the discovery query returns SAT, the engine does **not** stop at the first
counterexample — it searches for the workload that *maximizes* the deviation
`Delta = completion_HighMLP - completion_LowMLP`. This is done **without `z3::optimize`** (per the
design constraint), by incremental tightening on the standard solver:

1. Keep the best model found so far (`best`).
2. Assert `Delta >= best+1` directly (no push/pop, so learned lemmas are kept),
   `check()`.
3. SAT → adopt the larger witness, repeat. UNSAT → `best` is the **proved
   maximum**. UNKNOWN (timeout) → report `best` as a lower bound (not proven
   maximal).

Each `check()` inherits the solver timeout (60s by default, overridable via the
CLI argument — see Build & run), so the *final* probe (which is the hardest — it
tries to beat the true max) may exhaust it and the run reports "best found"
rather than a proof. The intermediate SAT steps are fast. Raising the timeout
(e.g. `./mlp 600`) gives the final probe more room to either climb higher or
prove maximality.

### Why not `z3::optimize`? (measured — it is far slower here)

The "no `z3::optimize`" rule is not stylistic; it is **empirically justified**.
`z3::optimize` *does* support a direct `maximize(Delta)` objective, and switching
to it was tried. It is a **severe performance regression on this model** — well
over 200× slower:

| Config | manual loop (`z3::solver`) | `z3::optimize` |
|---|---|---|
| `N=6`, guard (no shadow) | **UNSAT in 0.27 s** | **timeout (60 s)** |
| `N=6`, speculation on | **proved max in seconds** | **timeout (60 s)** |

The slowdown is in the optimize **core, not the objective search**: a control
build that registered **no** `maximize()` at all (pure `Delta>0` feasibility)
*still* timed out at 60s where the plain solver decides it in 0.27s. So no choice
of maximization strategy rescues it — `optsmt_engine=symba` also timed out.

**Likely cause (hypothesis, not confirmed):** `z3::optimize` runs a leaner
preprocessing pipeline than the default `z3::solver` and is conservative about
eliminating variables (to preserve the objective term and incremental model
events). This model is built almost entirely from **definitional equalities**
(`St_j == <ite-tree>`, `E_j == …`, hundreds of them); the plain solver's
`solve-eqs`/`simplify` tactics substitute those away and collapse the formula,
whereas optimize appears to keep them as free variables and search a vastly larger
space for the identical logical problem. (Not verified via `statistics()`; if
revisiting, dump decision/conflict counts from both engines to confirm.)

Two notes for anyone tempted to retry the switch:
- **Soundness is identical either way** — "max is `N`" means exactly "`Delta ≥ N+1`
  is UNSAT," which both approaches must establish. The manual loop is not a
  *stronger* proof, just the only one that finishes here.
- **A `z3::optimize` core lacks `reason_unknown()`**, and after a timeout its
  `get_model()` can return a **degenerate model that violates the assertions**
  (observed: all-zeros with `Delta=0`, despite `Delta>0` being asserted). Any
  retry must validate the witness before trusting it — the manual loop sidesteps
  this entirely by only ever adopting a `best` it has a satisfying model for.

## Status

Pipelined channel (`ADMISSION_GAP`) + variable memory latency `[LAT_MIN, LAT_MAX]`
(reordered completion) + occupancy-based MSHR gating +
**wrong-path speculation**, `4 vs 2` MSHRs, at unroll depth `N=12` (default config,
`MAX_SHADOW=8`, `RESOLVE_DELAY=3`). Wrong-path speculation is the single anti-MLP
mechanism and is always on; the guard (the pipelined channel with reordered
completion) is recovered only by forcing the shadow empty (`MAX_SHADOW=0` or
asserting `!squashed[i]`).

**Wrong-path speculation FALSIFIES the dogma.** A single mispredicted branch is
enough: a wide window issues more wrong-path misses into flight before the branch
resolves, occupying MSHR entries the correct-path tail needs, while the narrow
window is MSHR-gated and never launches those speculative misses — its throttle
acts as an accidental wrong-path filter.

### The transferable claim

The load-bearing finding is qualitative, not a cycle count (absolute cycles are
arbitrary units; the sign of `Delta` is the result). **A larger MSHR window is an
indiscriminate speculation amplifier.** It cannot distinguish correct-path from
wrong-path misses before branch resolution, so it commits its extra capacity to
*both*. When it guesses wrong, those speculative misses occupy the very MSHR
entries the correct-path tail needs — and an in-flight miss cannot be recalled
(the MSHR is the fill-matching structure; this is mainstream silicon behavior —
Intel Line Fill Buffers, AMD/Arm miss buffers all hold an outstanding miss to
completion, freeing only the core-level ROB/LQ entry at squash). A *smaller*
window, by throttling issue, never commits capacity to speculation it can't
afford, so its correct-path work is never crowded out. The narrower machine
finishes the *real* work sooner on the identical workload. This extrapolates to
real programs: **hard-to-predict branches guarding load-heavy (pointer-chasing,
irregular) code are where a big MSHR file can lose to a small one.**

### Current findings

- guard (no shadow) → **UNSAT** (strict-generalization guard: the pipelined channel
  with variable latency / reordered completion is monotone in `window`, so more MLP
  is never worse ✓). Proved at `N=6` and `N=8`; at `N=12` the UNSAT proof does not
  finish within a 120 s timeout (returns UNKNOWN), but the `N=6`/`N=8` proofs settle
  the sign — reordered completion alone does **not** break the dogma.
- speculation on, `N=12` (default `MAX_SHADOW=8`, `RESOLVE_DELAY=3`) →
  **SAT, `Delta ≥ 11`** — the falsifier. The maximization loop climbs to `Delta=11`
  and then exhausts the 120 s timeout on the next probe, so `11` is a **lower
  bound**, not a proved maximum; raise the CLI timeout to push it further or prove
  maximality.
- constant-latency corner (`LAT_MIN == LAT_MAX == 10`, in-order completion) →
  **SAT, `Delta = 3` (proved maximum)**. This is the in-order-completion baseline;
  variable latency (the default) *strengthens* the falsifier well past it.

**Parameter → behavior.** The one attribution anchor that is re-verified under the
current model:

| knob | result | reading |
|---|---|---|
| `MAX_SHADOW=0` (shadow off) | UNSAT | attribution: speculation is *the* cause |
| variable vs constant latency | `≥11` vs `3` | reordered completion *strengthens* the falsifier |

The `MAX_SHADOW=0` UNSAT is the load-bearing attribution: with the sole anti-MLP
mechanism disabled, the pipelined channel with reordered completion is monotone in
`window` and the dogma holds. The transferable claim below is mechanism-level and
does not depend on the exact `Delta`.

### Witness: wrong-path speculation (`N=12`, default config)

Best-found (not proved maximal — the maximality probe times out). Z3 mispredicts a
branch at `branch=6` with a shadow `squashed = {7,…,10}`, resolving at `resolve=64`
(identical on both machines — see the resolve caveat below).

- **HighMLP (window=4)** issues **4** live wrong-path misses before resolve
  (`service_start[j] < resolve`); those hold MSHR slots the correct-path tail needs.
- **LowMLP (window=2)** is MSHR-gated: it issues only **2** shadow misses before the
  window stalls the rest until after resolve.

The wide machine's extra in-flight wrong-path misses shove its correct-path tail
later, so `Delta = 11` (`completion_High=81` vs `completion_Low=70`) emerges purely
from `window` through the schedule (both machines see the identical shared
`latency[]`).

**Resolve caveat (the search's own choice, not a rig).** `resolve` is per-machine
and pinned to each machine's *own* `service_end[branch]` (see `pin_resolve`), so a
wide window that completes the condition-load earlier genuinely resolves the branch
earlier — the "more MLP resolves the branch faster" benefit is fully modeled and
available. But the solver, given free choice of `branch`, consistently parks it
where the condition-load completes *identically on both machines*, so
`resolve_High == resolve_Low` and the benefit is neutral. This is not suppression:
with unconstrained `branch` the worst case simply does not need the resolve
headwind, and forcing a late branch (to engage it) would be asserting a property of
the counterexample — the one thing this project forbids. The regime where
`resolve_High < resolve_Low` (a genuinely gated branch) is a *weaker* regime;
whether it yields a smaller counterexample or none is an open sub-question reachable
only by constraining `branch`.

## Optimizations (completed, model-preserving)

- **Solver tuning: Simplex-based arithmetic core** — selects Z3's Simplex core
  (`sol.set("arith.solver", 2)`) instead of the default LRA core (`=6`). Faster on
  this model because it is dominated by
  difference-logic-style definitional equalities (thousands of `service_start`/`service_end`/`slot_free`
  chains) that play to Simplex bound propagation's strengths. Model-preserving —
  search strategy only. NOTE: `arith.solver=1` (Bellman-Ford, diff-logic only) is
  faster still on the diff-logic subset but is **incomplete** here — the `Σ ite(...)`
  counting sum for the `shadow_len` cap is not difference logic, so it hard-aborts
  at the default `N=12` (`Overflow encountered when expanding vector`). Use `2`,
  not `1`.
- **Monotone tightening without push/pop** — the maximization loop asserts
  strictly increasing lower bounds on `Delta` directly into the solver (no push/pop).
  This **retains learned lemmas** across probes, which is a large win for the final,
  hardest UNSAT probe (the maximality proof), which would otherwise re-derive
  everything from a cold solver state.

## Future work (deferred, not modeled)

- **Same-line miss merging** — two misses to the same line share one MSHR entry.
  Miss-merging is the real-hardware reason an MSHR entry cannot be freed at squash
  (a merged secondary miss still needs the fill), so it reinforces the current
  no-un-send assumption; worth modeling to see if it changes the occupancy story.
  (Out-of-order completion is now modeled — variable `latency[]` reorders
  completions and gating is occupancy-based.)
- **Gated-branch regime** — the witness lives where `resolve_High == resolve_Low`
  (the condition-load completes identically on both machines). The regime where
  a wide window resolves the branch *earlier* (`resolve_High < resolve_Low`) is reachable
  only by constraining `branch`; characterize whether it yields a
  smaller counterexample or none. Reason from that constrained run, do not fold the
  constraint into the default model.

## Documentation policy

**Docs describe the code as it is now, not its history.** Git is the changelog.
Do not leave "used to be X," "removed Y," "earlier version had Z," or "renamed from
W" artifacts in any doc (`CLAUDE.md`, `README.md`, `TODO.md`) or in
code comments. When you change the model, rewrite the affected prose in place so it
reads as though the current design was always the design. The one exception is
*forward-looking* design guidance — "do not add X, because it does no work / breaks
honesty" — which is kept in present tense as a constraint on future edits, not as a
record of a past removal.

## Code style

- **Braces:** GNU Allman style — opening brace on the next line for all blocks,
  indented by two spaces. Single-statement if/else blocks may omit braces for
  readability (e.g., `if (cond) statement;`).
- **Function declarations:** return type on the *same* line as the function name
  (`static expr admit(...)`, not the type on its own line). Wrap long parameter lists
  onto continuation lines aligned under the first parameter.
- **Indentation:** Two spaces consistently (matching the existing `namespace cfg`
  block and throughout the file).
- **Line wrapping:** Keep lines reasonably compact; prefer semantic grouping over
  arbitrary 80-column boundaries.
- **Comments:** Minimal — only where the intent is non-obvious or a constraint is
  hidden. Do not repeat what the code already says; focus on the *why*.
