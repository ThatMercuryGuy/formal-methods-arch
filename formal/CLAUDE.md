# CLAUDE.md

Guidance for working in this directory (`formal/`).

## What this is

A single-file Z3 (C++ API) Bounded Model Checking engine, `mlp.cpp`, that tests
the architectural dogma *"more Memory-Level Parallelism is always better."* It
unrolls `N` memory requests over two state machines (`System_HighMLP`,
`System_LowMLP`) that share one synthesized workload and differ only in their
MSHR / outstanding-miss window `W`. A standard `z3::solver` (NOT `z3::optimize` —
the latter was measured 200×+ slower on this model; see "Why not `z3::optimize`?")
searches for a workload where `T_HighMLP > T_LowMLP`. Wrong-path speculation is
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

**Solver tuning.** The code sets `sol.set("arith.solver", 2)`, which selects Z3's
Simplex-based arithmetic core instead of the default LRA core (`=6`). This is a
**search strategy only** (model-preserving), and is faster on this model
(e.g. `N=12` proves its maximum in ~3.1s vs ~3.9s on the default) because the
model is dominated by difference-logic-style definitional equalities (`St`/`E`
chains) that Simplex bound propagation handles well.

**Do NOT set `arith.solver=1`.** Value `1` is Z3's Bellman-Ford *difference-logic-
only* engine. It is genuinely faster on the pure diff-logic subset, but it is
**incomplete for this model**, which also contains a genuine non-difference-logic
constraint: the `Σ ite(...)` counting sum for the `shadow_len` cap. Bellman-Ford
cannot represent it. At small `N` it merely warns (`smt.diff_logic: non-diff logic
expression ...`), but at the default `N=12` that sum grows and it **hard-aborts**
with `Overflow encountered when expanding vector`. Simplex (`=2`) is the complete,
non-crashing engine.

## Where to change things

All knobs are in `namespace cfg` at the top of `mlp.cpp`:
- `N`, `B` — unroll depth (currently `12`), per-request memory access latency.
- `G` — channel inter-admission gap (`1/bandwidth`, `< B`): the channel admits a
  new request every `G` cycles, so requests overlap in flight (latency hiding).
- `TT` — read/write bus turnaround bubble added on each direction switch.
- `W_HIGH`, `W_LOW` — the MSHR windows of the two machines (the only knob that
  differs between them). Currently `6` vs `2`.
- `MAX_SHADOW` — cap on the number of wrong-path (squashed) requests, for
  tractability. Default `4`. The binary prints the cap so it is never a silent
  truncation of the search space.
- `RESOLVE_DELAY` — branch resolves at `R = E[BR] + RESOLVE_DELAY`. Default `0`
  (resolve when the condition-load completes); a small constant models
  compare+redirect latency.

Changing the comparison (e.g. "6 vs 2 MSHRs") means editing `W_HIGH` / `W_LOW`
only — nothing else.

## Critical modeling fact — do not regress this

A purely serial, work-conserving channel is **monotone in `W`**: with a shared
workload, a larger window lets requests present no later, so completion times can
only fall, and `T_HighMLP > T_LowMLP` is vacuously **UNSAT**. Crucially, the
**pipelined bus does not break this on its own.** The channel admits a new request
every `G` cycles (`G < B`) and adds a `TT` turnaround bubble on read/write
direction switches:

- **Pipelined finite bandwidth (the MLP *benefit*).**
  `St[j] = max(A'[j], St[j-1] + G + TT*switch[j])`, where `switch[j]` is 1 when
  the read/write direction changes from `j-1`. Because admission is faster than
  service, requests **overlap in flight** — a wider window packs them tighter
  against the `G` bound and finishes baseline work earlier. This is the latency
  hiding MLP exists to exploit. Service is a flat `E[j] = St[j] + B`.

Without speculation, admission is in program order and every request
reaches the bus, so both machines see the identical admission sequence and the
same turnaround pattern; only `W` shifts start times, and a wider window presents
no later. The pure pipelined bus is therefore monotone in `W` → **UNSAT**. This
is the strict-generalization guard, and it is why the falsification below is
attributable to speculation alone. Recover the guard by forcing the shadow empty:
set `MAX_SHADOW=0` (equivalently, assert `!Sq[i]` for all `i`).

**Wrong-path speculation (Strategy B) is the sole anti-MLP mechanism — and it
falsifies the dogma.** A single mispredicted branch `BR` is fetched and the
front-end speculatively issues the **shadow** of wrong-path requests after it
until the branch **resolves** at `R = E[BR]`, at which point the shadow is
**squashed**. The branch index `BR` and the wrong-path set (shared bool tags
`Sq[i]`, a contiguous block `{BR+1,…,SE}`) are part of the **shared workload**
— both machines see the identical misprediction. But the speculation *depth*
— how many shadow requests actually reach the bus before `R` — is **per-machine
and emerges from the schedule**: `Live[j] = ¬Sq[j] ∨ (St[j] < R)`. A wide window
issues deeper down the wrong path (more `Live` shadow requests), each eating a
`G`-cycle admission slot and a possible `TT` bubble, delaying the **correct-path**
tail. The bus admission chain **skips** non-live shadow requests (a request killed
in the issue queue before `R` costs the bus nothing —
`Cf[j] = Live[j-1] ? St[j-1]+gap : Cf[j-1]`), so a narrow window's late shadow
requests never reach the bus and cost it nothing. The MSHR file does **not** skip
(allocation is in-order at rename): a shadow request frees its slot at squash `R`
only if it never issued, otherwise it holds to `E` (an in-flight DRAM miss must
keep its slot to sink the returning fill — you cannot un-send it). `T` counts
**correct-path completions only** (`Sq` requests never retire), so the wide window
can finish the *real* work later. Forcing every `Sq[i]` false (e.g. `MAX_SHADOW=0`)
⇒ every `Live` true ⇒ no skipping ⇒ the pure monotone pipelined bus above.

**Why this is honest and not rigged.** The anti-MLP cost must *emerge* from the
schedule, not be injected. The wrong-path shadow is honest for exactly this
reason: `BR` and `Sq[]` are one shared quantity per physical request (both
machines mispredict the same branch and fetch the same wrong-path stream), so the
solver cannot give the wide machine a deeper misprediction. Only `Live`/`R`/`St`
are per-machine, and the depth (`#{i : Sq[i] ∧ Live[i]}`) is decided by the
**schedule** (`St[i] < R`), which differs between machines only through `W`. Do
**not** implement this as "a fixed per-request waste tax on the wide machine" or
"make the shadow length depend on `W` directly" — that reintroduces a
monotone-by-construction artifact the whole project exists to avoid. The shadow
set is shared; only issue-depth is per-machine, and only through `St`/`R`.

**Do not add convex queueing contention as a falsifier.** A tempting second
anti-MLP mechanism is a channel crowding cost: an `inflight[j] = #{ i<j : E[i] >
St[j] }` count (requests still in service when `j` starts), a convex two-knee
penalty `Pen[j]` on it (free below `C = B/G`, `+PEN_LO` past `C`, `+PEN_HI` more
past `C2 = C+2`), fed into both completion (`E = St + B + Pen`) and the next
admission (backpressure: `St = max(A', St[j-1] + G + TT*switch + Pen[j-1])`).
**Contention alone never breaks the dogma**: with the shadow empty it is UNSAT with
contention on *and* off. The free-concurrency `C = B/G = 5` is generous, and
admission backpressure is negative feedback that *bounds* divergence, so the wide
window's latency-hiding benefit always covers its queueing cost. A *second,
independent* falsifier grounded in memory-controller queueing (rather than branch
misprediction) would need a much steeper near-saturation curve than that bounded
two-tier ramp to have any chance of flipping SAT — an open question, not a settled
result. Do not add it speculatively.

**Do not add a dependency subsystem as an amplifier.** Another tempting addition is
a shared data-dependency matrix `Dep[i][j]` (does request `j` consume `i`'s
result?), turned into a timing constraint through an `Aeff[j] = max(A[j], max{
E[i]+1 : Dep[i][j] })` causality loop, bounded to a reorder window `ROB_SIZE`, with
the number of mutually-independent requests in that window capped at `MAX_LSQ_MLP`
(a second `Σ ite` sum). It does no work on either anchor: the guard (empty shadow)
stays **UNSAT** and the falsifier still proves the **same maximal `Delta=5`** —
with dependencies available, Z3 could try to reach `Delta≥6` through them and
cannot. In principle it *could* bear on the result — with speculation on,
completion is not monotone in `W`, so a dependency reading the machine's own `E[i]`
can propagate a wrong-path-induced delay onto a correct-path consumer — but that
amplifier does no measurable work at these bounds, and an LSQ cap is a pure
*restriction* (adding a constraint only narrows the feasible set, so it can only
hide counterexamples, never create one). Requests present directly at their arrival
`A[j]`. If a future witness turns up — e.g. a `RESOLVE_DELAY>0` or larger-`N`
regime — where a dependency changes an outcome, add it reasoned from that concrete
counterexample, not speculatively.

**Label symmetry breaking (solver speed only, model-preserving).** A workload
tag is a pure interchangeable label, so the solver would otherwise explore many
relabelings of the same physical workload. It is broken with a first-occurrence
canonical form, sound because it is read *only* through an equality/disequality,
so relabeling preserves every quantity (including `Delta`) and excludes no
physically-distinct workload:
- **Read/write** (`RW[i]`) — read only via the turnaround disequality
  `RW[j]!=RW[j-1]`, invariant under a global read↔write flip, so `RW[0]` is pinned
  to `0` (first request is a read WLOG), keeping one representative of each
  flip-pair. This is the standard Z₂ quotient.

They prune redundant models; do **not** mistake them for modeling changes.

## Maximizing the deviation (worst case)

After the discovery query returns SAT, the engine does **not** stop at the first
counterexample — it searches for the workload that *maximizes* the deviation
`Delta = T_HighMLP - T_LowMLP`. This is done **without `z3::optimize`** (per the
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
| `N=6`, speculation on | **proved max `Delta=5` in seconds** | **timeout (60 s)** |

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

Pipelined-channel (`G` + R/W turnaround `TT`) + MSHR gating + **wrong-path
speculation**, `6 vs 2` MSHRs, at unroll depth `N=12` (default config). Wrong-path
speculation is the single anti-MLP mechanism and is always on; the guard (the pure
pipelined bus) is recovered only by forcing the shadow empty (`MAX_SHADOW=0` or
asserting `!Sq[i]`).

**Wrong-path speculation FALSIFIES the dogma.** A single mispredicted branch is
enough: a wide window issues deeper down the wrong path before the branch
resolves, wasting bus admissions and delaying the correct-path tail, while the
narrow window is MSHR-gated and never reaches the shadow requests on the bus.

### Current findings (all proved or hand-verified)

- guard (no shadow), `N=12` → **UNSAT** (strict-generalization guard: the pure
  pipelined bus is monotone in `W`, so more MLP is never worse ✓)
- speculation on, `N=12` → **SAT, `Delta = 5` (proved maximum, ~3 s)** — the falsifier

With speculation as the sole anti-MLP mechanism, a mispredicted branch falsifies
the dogma with a **proved** maximum deviation of 5 cycles. A wide window (W=6)
issues deeper into the wrong path before the branch resolves, wasting bus
admissions and delaying correct-path completion by 5 cycles versus the narrow
window (W=2), which is MSHR-gated and never reaches the shadow requests on the bus.

### Proved-maximal witness: wrong-path speculation (`N=12`)

Hand-verified and proved maximal. Z3 mispredicts a branch at `BR=2` with a 4-request
shadow `Sq = {3,4,5,6}`, resolving at `R=26`.

- **HighMLP (W=6)** issues **all four** wrong-path requests to the bus before
  resolve (`St[j] < R`); wrong-path issue depth = 4.
- **LowMLP (W=2)** is MSHR-gated: it issues only **one** shadow request before the
  window stalls the rest until after resolve; wrong-path issue depth = 1.

Those three extra wasted admissions on the wide machine shove its correct-path tail
later, so that `Delta = 5` (`T_High=65` vs `T_Low=60`) emerges purely from `W`
through the schedule. **This is proved maximal** — no configuration at `N=12` with
this setup yields `Delta ≥ 6`. (Absolute `T` values depend on `B`/`G`/`TT`; the
load-bearing quantity is `Delta`.)

## Optimizations (completed, model-preserving)

- **Solver tuning: Simplex-based arithmetic core** — selects Z3's Simplex core
  (`sol.set("arith.solver", 2)`) instead of the default LRA core (`=6`). Faster on
  this model because it is dominated by
  difference-logic-style definitional equalities (thousands of `St`/`E`
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

- **Out-of-order MSHR completion + same-line miss merging** (gating currently
  assumes in-order completion via `E[j-W]`).
- **Remaining sweep (complete maximality at larger `N`)** — `RESOLVE_DELAY`
  variants; report whether Δ-max is proved or a timeout lower bound.

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
- **Indentation:** Two spaces consistently (matching the existing `namespace cfg`
  block and throughout the file).
- **Line wrapping:** Keep lines reasonably compact; prefer semantic grouping over
  arbitrary 80-column boundaries.
- **Comments:** Minimal — only where the intent is non-obvious or a constraint is
  hidden. Do not repeat what the code already says; focus on the *why*.
