# CLAUDE.md

Guidance for working in this directory (`formal/`).

## What this is

A single-file Z3 (C++ API) Bounded Model Checking engine, `mlp.cpp`, that tests
the architectural dogma *"more Memory-Level Parallelism is always better."* It
unrolls `N` memory requests over two state machines (`System_HighMLP`,
`System_LowMLP`) that share one synthesized workload and differ only in their
MSHR / outstanding-miss window `W`. A standard `z3::solver` (NOT `z3::optimize` —
the latter was measured 200×+ slower on this model; see "Why not `z3::optimize`?")
searches for a workload where `T_HighMLP > T_LowMLP`.

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
(e.g. `SPEC=1/N=8` proves its maximum in ~45s vs ~68s on the default) because the
model is dominated by difference-logic-style definitional equalities (`St`/`E`/`Aeff`
chains) that Simplex bound propagation handles well.

**Do NOT set `arith.solver=1`.** Value `1` is Z3's Bellman-Ford *difference-logic-
only* engine. It is genuinely faster on the pure diff-logic subset, and earlier
notes here mislabeled it as "the Simplex core" — but it is **incomplete for this
model**, which also contains genuine non-difference-logic constraints: the
`Σ ite(...)` counting sums (the `shadow_len` cap and the LSQ per-stream count).
Bellman-Ford cannot represent those. At small `N` it merely warns
(`smt.diff_logic: non-diff logic expression ...`), but at the default `N=12` those
sums grow and it **hard-aborts** with `Overflow encountered when expanding vector`.
Simplex (`=2`) is the complete, non-crashing engine that earlier notes intended.

## Where to change things

All knobs are in `namespace cfg` at the top of `mlp.cpp`:
- `N`, `S`, `B` — unroll depth (currently `12`), streams (threads), per-request
  memory access latency.
- `ROB_SIZE`, `MAX_STREAM_MLP` — dependency-matrix pipeline bounds.
- `G` — channel inter-admission gap (`1/bandwidth`, `< B`): the channel admits a
  new request every `G` cycles, so requests overlap in flight (latency hiding).
- `TT` — read/write bus turnaround bubble added on each direction switch.
- `C`, `C2`, `PEN_LO`, `PEN_HI` — the **convex** queueing-delay curve (see
  critical note below): the first `C` concurrently-in-flight requests are free,
  past `C` each costs `PEN_LO`, past the steeper knee `C2` each costs `PEN_HI`
  more. **`C` is derived, not hand-set:** `C := B/G` floored at 1 — the
  bandwidth-delay product `B/G` is the number of requests the channel keeps busy
  for free. `C2 := C+2`. Change `C` by changing `B`/`G`, not by typing a magic
  number.
- `W_HIGH`, `W_LOW` — the MSHR windows of the two machines (the only knob that
  differs between them). Currently `6` vs `2`.
- `SPEC` — wrong-path speculation master switch (Strategy B). `1` (default)
  models a mispredicted branch and its squashed shadow; `0` reduces the model
  **exactly** to the pre-Strategy-B one (the strict-generalization guard).
  Overridable: `g++ -DCFG_SPEC=0 ...`.
- `CONTENTION` — queueing-contention master switch. `1` (default) keeps the full
  `inflight→Pen` chain (convex queueing + admission backpressure). `0` forces
  `Pen≡0`, removing all queueing contention: the channel reduces to a pure
  pipelined bus (`G` + `TT`) + MSHR gating + speculation. This **isolates
  wrong-path speculation** as the sole anti-MLP mechanism. Overridable:
  `g++ -DCFG_CONTENTION=0 ...`.
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
only fall, and `T_HighMLP > T_LowMLP` is vacuously **UNSAT**. The model breaks
that monotonicity with the genuine physics of a shared, **pipelined** channel
(Axiom 3), folded in as **identical physics for both machines** — only `W`
differs:

- **Pipelined finite bandwidth (the MLP *benefit*).** The channel admits a new
  request every `G` cycles (`G < B`):
  `St[j] = max(A'[j], St[j-1] + G + TT*switch[j])`, where `switch[j]` is 1 when
  the read/write direction changes from `j-1`. Because admission is faster than
  service, requests **overlap in flight** — a wider window packs them tighter
  against the `G` bound and finishes baseline work earlier. This is the latency
  hiding MLP exists to exploit.
- **Convex queueing delay (the MLP *cost*), measured in TIME.**
  `inflight[j] = #{ i<j : E[i] > St[j] }` — how many earlier requests are still in
  service when `j` starts. Service cost is **convex** in it: the first `C` overlaps
  are free, past `C` each costs `PEN_LO`, past `C2` each costs `PEN_HI` more. This
  penalty is named `Pen[j]` and applied to completion:
  `Pen[j] = PEN_LO*max(0,inflight-C) + PEN_HI*max(0,inflight-C2)`,
  `E[j] = St[j] + B + Pen[j]`.
- **Admission backpressure (the closed loop) — `Pen` feeds `St`, not just `E`.**
  The convex penalty is *not* a dead-end term on completion. It also feeds forward
  into the next request's admission:
  `St[j] = max(A'[j], St[j-1] + G + TT*switch[j] + Pen[j-1])`.
  A request the channel is serving slowly holds the resource longer, so the next
  request admits later — and because `St` is a forward chain, this **compounds**
  across all later requests. This is genuine negative feedback: a contended
  request admits later → later `St` → fewer earlier requests still in flight when
  it starts → *lower* `inflight` → smaller penalty. The loop throttles the
  channel toward its sustainable rate, exactly as a real shared bus does.
- **Wrong-path speculation waste (Strategy B, `SPEC=1`) — an independent anti-MLP
  mechanism.** A single mispredicted branch `BR` is fetched and the
  front-end speculatively issues the **shadow** of wrong-path requests after it
  until the branch **resolves** at `R = E[BR]`, at which point the shadow is
  **squashed**. The branch index `BR` and the wrong-path set (shared bool tags
  `Sq[i]`, a contiguous block `{BR+1,…,SE}`) are part of the **shared workload**
  — both machines see the identical misprediction. But the speculation *depth*
  — how many shadow requests actually reach the bus before `R` — is **per-machine
  and emerges from the schedule**: `Live[j] = ¬Sq[j] ∨ (St[j] < R)`. A wide
  window issues deeper down the wrong path (more `Live` shadow requests), each
  eating a `G`-cycle admission slot, a possible `TT` bubble, and queue occupancy,
  all of which delay the **correct-path** tail. The bus admission chain **skips**
  non-live shadow requests (a request killed in the issue queue before `R` costs
  the bus nothing — `Cf[j] = Live[j-1] ? St[j-1]+gap : Cf[j-1]`), so a narrow
  window's late shadow requests never reach the bus and cost it nothing. The MSHR
  file does **not** skip (allocation is in-order at rename): a shadow request
  frees its slot at squash `R` only if it never issued, otherwise it holds to `E`
  (an in-flight DRAM miss must keep its slot to sink the returning fill — you
  cannot un-send it). `T` counts **correct-path completions only** (`Sq` requests
  never retire), so the wide window can finish the *real* work later. `SPEC=0`
  forces every `Sq[i]` false ⇒ every `Live` true ⇒ no skipping ⇒ the model is
  **identical** to the pre-Strategy-B one.

**Disabling queueing contention (`CONTENTION=0`) — isolating speculation.** `Pen`
is the *only* path by which the `inflight` count touches any timing quantity, so
forcing `Pen≡0` cleanly removes the entire queueing-contention subsystem: the
channel collapses to a pure pipelined bus (`G` + `TT`) + MSHR gating + speculation.
Use it to test whether wrong-path speculation *alone* falsifies the dogma, with
contention removed as a confound. `(CONTENTION=0, SPEC=0)` is a strict-
generalization guard — with no penalty and no speculation the channel is monotone
in `W` (a wider window presents no later, completions only fall), so it must be
**UNSAT** (verified).

**Why this is honest and not rigged.** The old model added `PEN * overlap` where
`overlap` counted siblings in the *index* window `[j-W, j)` — a quantity
**monotone in `W` by construction**, so the wide machine *could not* pay less and
the "discovery" was essentially an arithmetic identity. The `inflight` count is
derived from the **schedule** (`St`/`E`), not from a `W`-indexed window, and spans
**all streams** — so (a) the backfire must *emerge* from timing rather than being
injected, and (b) it captures **cross-thread interference** (an aggressive stream
floods the channel and delays another stream's critical request). The read/write
turnaround `TT` is a third emergent cost. Do **not** revert to an index-window
penalty term — that reintroduces the monotone-by-design artifact.

**Label symmetry breaking (solver speed only, model-preserving).** Two workload
tags are pure interchangeable labels, so the solver would otherwise explore many
relabelings of the same physical workload. Both are broken with a first-occurrence
canonical form, sound because each is read *only* through an equality/disequality,
so relabeling preserves every quantity (including `Delta`) and excludes no
physically-distinct workload:
- **Stream ids** (`K[i]`, when `S>1`) — read only via `K[i]==K[j]` (dependency
  matching and the LSQ per-stream count); no timing reads a stream's absolute id.
- **Read/write** (`RW[i]`) — read only via the turnaround disequality
  `RW[j]!=RW[j-1]`, invariant under a global read↔write flip, so `RW[0]` is pinned
  to `0` (first request is a read WLOG), keeping one representative of each
  flip-pair. This is the standard Z₂ quotient.

They prune redundant models; do **not** mistake them for modeling changes.
(Likewise, the `Dep[i][j]` matrix only allocates the structurally-possible entries
— strictly upper-triangular and within `ROB_SIZE` — instead of allocating all `N²`
and pinning the impossible ones `false`; model-identical, fewer booleans.)

The **wrong-path shadow is honest for the same reason**: `BR` and `Sq[]`
are one shared quantity per physical request (both machines mispredict the same
branch and fetch the same wrong-path stream), so the solver cannot give the wide
machine a deeper misprediction. Only `Live`/`R`/`St` are per-machine, and the
depth (`#{i : Sq[i] ∧ Live[i]}`) is decided by the **schedule** (`St[i] < R`),
which differs between machines only through `W`. Do **not** implement this as "a
fixed per-request waste tax on the wide machine" or "make the shadow length
depend on `W` directly" — that reintroduces the monotone-by-construction artifact
the whole project exists to avoid. The shadow set is shared; only issue-depth is
per-machine, and only through `St`/`R`.

**Backpressure caps the divergence — do not expect it to break the dogma.** It is
tempting to think feeding `Pen` into admission must make "more MLP is worse"
*easier* (the wide window floods the channel and stalls its own issue). The
opposite happens: backpressure is negative feedback, so it *bounds* the very
divergence the old completion-only penalty allowed to run away. Concretely, the
old pathological config (`G=6, C=0, C2=1, PEN_LO=8, PEN_HI=16`) drove a
proved-maximal `Delta=72` when the penalty only hit `E`; with the penalty also
feeding `St` the same config is **UNSAT**. That `72` was partly an artifact of
penalizing latency *without spacing requests out*. The closed loop is the more
faithful physics and it makes the dogma harder, not easier, to falsify.

## Maximizing the deviation (worst case)

After the discovery query returns SAT, the engine does **not** stop at the first
counterexample — it searches for the workload that *maximizes* the deviation
`Delta = T_HighMLP - T_LowMLP`. This is done **without `z3::optimize`** (per the
design constraint), by incremental tightening on the standard solver:

1. Keep the best model found so far (`best`).
2. `push()`, assert `Delta >= best+1`, `check()`.
3. SAT → adopt the larger witness, `pop()`, re-pin `Delta >= best` permanently,
   repeat. UNSAT → `best` is the **proved maximum**. UNKNOWN (timeout) → report
   `best` as a lower bound (not proven maximal).

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
| `N=6, SPEC=0` | **UNSAT in 0.27 s** | **timeout (60 s)** |
| `N=6, SPEC=1` | **proved max `Delta=5` in seconds** | **timeout (60 s)** |

The slowdown is in the optimize **core, not the objective search**: a control
build that registered **no** `maximize()` at all (pure `Delta>0` feasibility)
*still* timed out at 60s where the plain solver decides it in 0.27s. So no choice
of maximization strategy rescues it — `optsmt_engine=symba` also timed out.

**Likely cause (hypothesis, not confirmed):** `z3::optimize` runs a leaner
preprocessing pipeline than the default `z3::solver` and is conservative about
eliminating variables (to preserve the objective term and incremental model
events). This model is built almost entirely from **definitional equalities**
(`Aeff_j == <ite-tree>`, `St_j == …`, hundreds of them); the plain solver's
`solve-eqs`/`simplify` tactics substitute those away and collapse the formula,
whereas optimize appears to keep them as free variables and search a vastly larger
space for the identical logical problem. (Not verified via `statistics()`; if
revisiting, dump decision/conflict counts from both engines to confirm.)

Two notes for anyone tempted to retry the switch:
- **Soundness is identical either way** — "max is `N`" means exactly "`Delta ≥ N+1`
  is UNSAT," which both approaches must establish. The manual loop is not a
  *stronger* proof, just the only one that finishes here. (Earlier docs/comments
  hinting the manual loop is more "auditable" or "trustworthy" overstate it; the
  real reason is performance.)
- **A `z3::optimize` core lacks `reason_unknown()`**, and after a timeout its
  `get_model()` can return a **degenerate model that violates the assertions**
  (observed: all-zeros with `Delta=0`, despite `Delta>0` being asserted). Any
  retry must validate the witness before trusting it — the manual loop sidesteps
  this entirely by only ever adopting a `best` it has a satisfying model for.

## Status

Pipelined-channel + convex-queueing + R/W-turnaround model **with admission
backpressure** (`inflight` spans all in-flight requests) and **derived
free-concurrency `C = B/G`**, plus **wrong-path speculation**, `6 vs 2`
MSHRs, at unroll depth `N=8` (default config). Two anti-MLP mechanisms are live:
convex queueing contention (`CONTENTION=1`) and speculation waste (`SPEC=1`).
Either can be isolated with `-DCFG_CONTENTION=0` / `-DCFG_SPEC=0`.

**Wrong-path speculation FALSIFIES the dogma.** A single mispredicted branch is
enough, even with contention removed: a wide window issues deeper down the wrong
path before the branch resolves, wasting bus admissions and delaying the
correct-path tail, while the narrow window is MSHR-gated and never reaches the
shadow requests on the bus. Convex queueing contention, by contrast, is a genuine
MLP *cost* but does **not** falsify the dogma on its own — the free-concurrency
`C = B/G = 5` is generous and admission backpressure bounds the divergence, so the
wide window's latency-hiding benefit always covers its queueing cost (UNSAT).

### Current findings (all proved or hand-verified)

- `CONTENTION=0, SPEC=0, N=12` → **UNSAT** (strict-generalization guard: the pure
  pipelined bus is monotone in `W`, so more MLP is never worse ✓)
- `CONTENTION=1, SPEC=0, N=12` → **UNSAT** (contention alone cannot falsify: the
  benefit covers the queueing cost; backpressure bounds divergence)
- `CONTENTION=0, SPEC=1, N=8` → **SAT, `Delta = 5` (proved maximum)** — speculation
  in isolation, the falsifier

The isolation result is the cleanest: with all queueing contention removed and
speculation as the sole anti-MLP mechanism, a mispredicted branch falsifies the
dogma with a **proved** maximum deviation of 5 cycles. A wide window (W=6) issues
deeper into the wrong path before the branch resolves, wasting bus admissions and
delaying correct-path completion by 5 cycles versus the narrow window (W=2), which
is MSHR-gated and never reaches the shadow requests on the bus.

### Proved-maximal witness: wrong-path speculation (`CONTENTION=0, SPEC=1, N=8`)

Hand-verified and proved maximal. Z3 mispredicts a branch at `BR=4` with a 2-request
shadow `Sq = {5,6}`, resolving at `R = 53`.

- **HighMLP (W=6)** issues one wrong-path read to the bus before resolve (`St[6]=52 < 53`).
- **LowMLP (W=2)** is MSHR-gated: its shadow requests `A' = 53, 58` (both `≥ R`) so
  they never reach the bus (killed in the issue queue); **zero shadow requests go live**.

That single wasted admission on the wide machine forces a read→write turnaround bubble,
delaying correct-path completion to `T = 68` versus `T = 63`. `Delta = 5` emerges
purely from `W` through the schedule. **This is proved maximal** — no configuration
at `N=8` with this setup yields `Delta ≥ 6`.

## Optimizations (completed, model-preserving)

- **Solver tuning: Simplex-based arithmetic core** — selects Z3's Simplex core
  (`sol.set("arith.solver", 2)`) instead of the default LRA core (`=6`). Faster on
  this model (e.g. `SPEC=1/N=8`: ~45s vs ~68s) because it is dominated by
  difference-logic-style definitional equalities (thousands of `St`/`E`/`Aeff`
  chains) that play to Simplex bound propagation's strengths. Model-preserving —
  search strategy only. NOTE: `arith.solver=1` (Bellman-Ford, diff-logic only) is
  faster still on the diff-logic subset but is **incomplete** here — the `Σ ite(...)`
  counting sums are not difference logic, so it hard-aborts at the default `N=12`
  (`Overflow encountered when expanding vector`). Use `2`, not `1`.
- **Monotone tightening without push/pop** — the maximization loop now asserts
  strictly increasing lower bounds on `Delta` directly into the solver (no push/pop).
  This **retains learned lemmas** across probes, which is a large win for the final,
  hardest UNSAT probe (the maximality proof), which would otherwise re-derive
  everything from a cold solver state.

## Future work (deferred, not modeled)

- **Out-of-order MSHR completion + same-line miss merging** (gating currently
  assumes in-order completion via `E[j-W]`).
- **Remaining sweep (complete maximality at larger `N`)** — `RESOLVE_DELAY`
  variants; report whether Δ-max is proved or a timeout lower bound.

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

## Completed (no longer deferred)

- **Strategy B: wrong-path speculation** — the `SPEC`/`BR`/`Sq`/`Live`/`Cf`/`Rel`
  machinery is in `mlp.cpp`, with proved maximal `Delta=5` at `CONTENTION=0, SPEC=1,
  N=8` (hand-verified). One of two independent anti-MLP mechanisms (the other is
  convex queueing contention).
