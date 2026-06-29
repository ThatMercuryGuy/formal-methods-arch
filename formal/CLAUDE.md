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

## Where to change things

All knobs are in `namespace cfg` at the top of `mlp.cpp`:
- `N`, `S`, `B` — unroll depth (currently `12`), streams (threads), per-request
  bank latency.
- `ROB_SIZE`, `MAX_STREAM_MLP` — dependency-matrix pipeline bounds.
- `G` — channel inter-admission gap (`1/bandwidth`, `< B`): the channel admits a
  new request every `G` cycles, so requests overlap in flight (latency hiding).
- `TT` — read/write bus turnaround bubble added on each direction switch.
- `NB` — number of DRAM banks (locality classes). The workload carries a per-
  request bank tag `Bank[i] ∈ [0,NB)` and contention is counted **per bank** (see
  critical note). `NB=1` collapses to a single bank = the old locality-blind
  model exactly. Overridable at compile time without editing the source:
  `g++ -DCFG_NB=3 ...`. Default `2`.
- `C`, `C2`, `PEN_LO`, `PEN_HI` — the **convex** queueing-delay curve (see
  critical note below): the first `C` concurrently-in-flight **same-bank**
  requests are free, past `C` each costs `PEN_LO`, past the steeper knee `C2` each
  costs `PEN_HI` more. **`C` is derived, not hand-set:** `C := (B/G)/NB` floored
  at 1 — the bandwidth-delay product `B/G` is the number of *distinct banks* the
  channel keeps busy for free, so the *per-bank* free concurrency is that spread
  over `NB` banks. `C2 := C+2`. Change `C` by changing `B`/`G`/`NB`, not by
  typing a magic number.
- `W_HIGH`, `W_LOW` — the MSHR windows of the two machines (the only knob that
  differs between them). Currently `6` vs `2`.
- `SPEC` — wrong-path speculation master switch (Strategy B). `1` (default)
  models a mispredicted branch and its squashed shadow; `0` reduces the model
  **exactly** to the pre-Strategy-B one (the strict-generalization guard, the
  analog of `NB=1` for A1). Overridable: `g++ -DCFG_SPEC=0 ...`.
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
- **Convex queueing delay (the MLP *cost*), measured in TIME and PER BANK.**
  `inflight[j] = #{ i<j : E[i] > St[j] AND Bank[i] == Bank[j] }` — how many
  earlier requests are still in service **on the same bank** when `j` starts.
  Service cost is **convex** in it: the first `C` same-bank overlaps are free
  (bank-level parallelism), past `C` each costs `PEN_LO`, past `C2` each costs
  `PEN_HI` more. This penalty is named `Pen[j]` and applied to completion:
  `Pen[j] = PEN_LO*max(0,inflight-C) + PEN_HI*max(0,inflight-C2)`,
  `E[j] = St[j] + B + Pen[j]`. The bank tag `Bank[i]` is an **abstraction of the
  address** — only its equivalence class (same bank? same row?) affects timing,
  so we carry the small finite-domain tag directly instead of a raw address (no
  modular arithmetic → far better Z3 termination). It is **shared** across both
  machines, so the solver cannot rig contention per-machine.
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
- **Wrong-path speculation waste (Strategy B, `SPEC=1`) — an independent
  anti-MLP mechanism.** A single mispredicted branch `BR` is fetched and the
  front-end speculatively issues the **shadow** of wrong-path requests after it
  until the branch **resolves** at `R = E[BR]`, at which point the shadow is
  **squashed**. The branch index `BR` and the wrong-path set (shared bool tags
  `Sq[i]`, a contiguous block `{BR+1,…,SE}`) are part of the **shared workload**
  — both machines see the identical misprediction. But the speculation *depth*
  — how many shadow requests actually reach the bus before `R` — is **per-machine
  and emerges from the schedule**: `Live[j] = ¬Sq[j] ∨ (St[j] < R)`. A wide
  window issues deeper down the wrong path (more `Live` shadow requests), each
  eating a `G`-cycle admission slot, a possible `TT` bubble, and bank occupancy,
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

**Why this is honest and not rigged.** The old model added `PEN * overlap` where
`overlap` counted siblings in the *index* window `[j-W, j)` — a quantity
**monotone in `W` by construction**, so the wide machine *could not* pay less and
the "discovery" was essentially an arithmetic identity. The new `inflight` count
is derived from the **schedule** (`St`/`E`), not from a `W`-indexed window, and
spans **all streams** — so (a) the backfire must *emerge* from timing rather than
being injected, and (b) it captures **cross-thread interference** (an aggressive
stream floods the channel and delays another stream's critical request). The
read/write turnaround `TT` is a third emergent cost. Do **not** revert to an
index-window penalty term — that reintroduces the monotone-by-design artifact.

The **bank tag is honest for the same reason**: it is a single shared quantity per
physical request (the same request lands in one bank, seen identically by High and
Low), so the solver cannot declare a pair "conflicting" for the wide machine and
"not" for the narrow one — a free per-machine conflict *matrix* could do that, a
shared tag cannot. Which requests actually collide on a bank is therefore decided
by the **schedule** (`St`/`E`), which differs between machines only through `W`.
The bank ids are symmetry-broken (first-occurrence order) purely for solver speed;
because all timing reads banks only through the *equality* relation, relabeling
preserves every quantity, so the symmetry break excludes no physically-distinct
workload. Do **not** replace the shared tag with a per-machine conflict matrix.

**Label symmetry breaking (solver speed only, model-preserving).** Three workload
tags are pure interchangeable labels, so the solver would otherwise explore many
relabelings of the same physical workload. All three are broken with the identical
first-occurrence canonical form, sound for the same reason as the bank break — each
is read *only* through an equality/disequality, so relabeling preserves every
quantity (including `Delta`) and excludes no physically-distinct workload:
- **Bank ids** (`Bank[i]`, when `NB>1`) — read only via `Bank[i]==Bank[j]`.
- **Stream ids** (`K[i]`, when `S>1`) — read only via `K[i]==K[j]` (dependency
  matching and the LSQ per-stream count); no timing reads a stream's absolute id.
- **Read/write** (`RW[i]`) — read only via the turnaround disequality
  `RW[j]!=RW[j-1]`, invariant under a global read↔write flip, so `RW[0]` is pinned
  to `0` (first request is a read WLOG), keeping one representative of each
  flip-pair. This is the standard Z₂ quotient, not an `NB`-style growth constraint.

These were verified outcome-preserving: at tractable depths where the maximization
*terminates*, pre- and post-break builds prove the **identical maximum `Delta`**
(e.g. `N=6/7, NB=3 → Delta=14`; `NB=1 → Delta=5`) and agree on every UNSAT. They
prune redundant models; do **not** mistake them for modeling changes. (Likewise,
the `Dep[i][j]` matrix only allocates the structurally-possible entries — strictly
upper-triangular and within `ROB_SIZE` — instead of allocating all `N²` and pinning
the impossible ones `false`; model-identical, fewer booleans.)

The **wrong-path shadow is honest for exactly the same reason**: `BR` and `Sq[]`
are one shared quantity per physical request (both machines mispredict the same
branch and fetch the same wrong-path stream), so the solver cannot give the wide
machine a deeper misprediction. Only `Live`/`R`/`St` are per-machine, and the
depth (`#{i : Sq[i] ∧ Live[i]}`) is decided by the **schedule** (`St[i] < R`),
which differs between machines only through `W`. Do **not** implement this as "a
fixed per-request waste tax on the wide machine" or "make the shadow length
depend on `W` directly" — that reintroduces the monotone-by-construction artifact
the whole project exists to avoid. The shadow set is shared; only issue-depth is
per-machine, and only through `St`/`R`.

Key consequence — **the SAT/UNSAT boundary is the per-bank free concurrency
`C = (B/G)/NB`.** With few banks (`NB ≤ 2`, so `C ≥ 2`) the wide window's latency-
hiding benefit covers its contention cost and the query is UNSAT. With **many
banks (`NB ≥ 3`, so `C = 1`)** the per-bank free concurrency falls to one and the
dogma is **falsifiable** — a wide window that bunches same-bank requests pays a
convex penalty a throttled window spreads out and avoids. This is a *realistic*
regime (real DRAM has 8–16 banks), not a contrived starved channel. See Status.

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

| Config (SPEC=0) | manual loop (`z3::solver`) | `z3::optimize` |
|---|---|---|
| `N=6, NB=1` | **UNSAT in 0.27 s** | **timeout (60 s)** |
| `N=6, NB=3` | **proved max `Delta=5` in seconds** | **timeout (60 s)** |

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
backpressure**, **per-bank locality contention** (the bank-tag proxy, `NB`
banks), and **derived per-bank free-concurrency `C = (B/G)/NB`**, `6 vs 2` MSHRs.

**The bank-tag model FALSIFIES the dogma in a realistic many-bank regime.** This
overturned the previous status (which reported UNSAT for the default and expected
locality-aware contention to *not* break the dogma — that expectation was wrong).

Sweeping `NB` at `N=12` (all other defaults `G=2, TT=4, C2=C+2, PEN_LO=3,
PEN_HI=5`; 600s solver timeout each):

| `NB` | per-bank `C = (B/G)/NB` | result | discovery time |
|------|--------------------------|--------|----------------|
| `1` | `5` | **UNSAT** — dogma holds (== old locality-blind baseline ✓) | ~78s |
| `2` (default) | `2` | **UNSAT** — dogma holds | ~199s |
| `3` | `1` | **SAT, `Delta ≥ 9`** (lower bound — final maximization probe hit the 600s timeout; not yet proved maximal) | ~110s to first SAT |

**`NB=1` reproduces the old single-channel model exactly** (every `Bank[i]==0`, so
the same-bank filter is vacuous and `C=B/G=5`): UNSAT in ~78s, matching the prior
baseline. This is the strict-generalization sanity check — A1 changes nothing at
`NB=1`.

**The boundary is `C=1`, reached naturally at `NB≥3`.** The bandwidth-delay product
`B/G=5` is shared across `NB` banks, so per-bank free concurrency is `(B/G)/NB`.
Once banks outnumber the BDP enough that `C` floors at 1, a wide window that bunches
same-bank requests exceeds the free regime and pays the convex penalty; a throttled
window spreads the same requests in time and stays at `inflight=1`. Real DRAM has
8–16 banks, so `C=1` is the **ordinary** regime, not a contrived starved channel —
which makes this a more defensible falsification than the old hand-set `C=1`.

(Note: `C` floors at 1, so `NB ≥ B/G` cannot reach the `C=0` starved regime via
`NB` alone — by design; a bank always serves ≥1 request for free.)

### Worst-case witness (the `NB=3`, `Delta=9` lower-bound workload)

Hand-verified against the model's own equations (the `nfly`/`pen`/`E` arithmetic
matches the dumped timeline exactly — the SAT is real, not a solver artifact). Z3
puts requests `{0,1,2,3,8,9,10,11}` in **bank 0** and `{4,5,6,7}` in **bank 1**
(workload is mixed read/write). Run output: `out_spec0_nb3.txt`.

- **HighMLP (W=6)** never gates on its MSHR file, so it packs same-bank requests
  tight against the `G=2` admission gap — `nfly` climbs `0 1 2 3` within a bank-run,
  exceeding the per-bank free concurrency `C=1`. With `C2=3` the convex penalty `pen`
  row reads `0 0 3 6` per run (`PEN_LO*max(0,nfly-1) + PEN_HI*max(0,nfly-3)`), which
  feeds the `St` chain (backpressure) and pushes its tail to `T=98`.
- **LowMLP (W=2)** is MSHR-gated: its `A'` jumps (req 2: `A'=31`, held by `E[0]`) so
  the *same* requests spread out in time, `nfly` stays pinned at **1**, `pen` is
  all-zero, and it finishes at `T=89`.

Same shared bank tags, same workload; `Delta = 98 − 89 = 9` emerges purely from `W`
through the schedule. Read the `Bk`, `nfly`, and `pen` rows together: the bank tag
decides *who collides*, `nfly` counts the same-bank collisions, and `pen` turns them
into delay that both slows completion (`E`) and back-pressures the next admission
(`St`).

### Strategy B (wrong-path speculation) — MEASURED, SAT, hand-verified

The `SPEC`/`BR`/`Sq`/`Live`/`Cf`/`Rel`/`R` machinery is implemented in `mlp.cpp`
(default `SPEC=1`) and the runs have **landed**. **Wrong-path waste independently
falsifies the dogma** at `NB=2`, where bank contention alone is UNSAT — a second,
independent falsifier. Validated in the required order: strict-generalization guards
first, then the headline run, then hand-verification.

| Config | Result | Run output |
|--------|--------|------------|
| `SPEC=0, NB=2` (guard) | **UNSAT** — reproduces bank-only baseline exactly ✓ | `out_spec0_nb2.txt` |
| `SPEC=0, NB=3` (guard) | **SAT, `Delta ≥ 9`** — reproduces bank-only baseline exactly ✓ | `out_spec0_nb3.txt` |
| `SPEC=1, NB=2` (headline) | **SAT, `Delta ≥ 8`** (lower bound; maximization hit 600s timeout) | `out_spec1_nb2.txt` |

**Headline witness (`SPEC=1, NB=2`, hand-verified).** Z3 mispredicts a branch at
`BR=2` with a 4-request shadow `Sq={3,4,5,6}`, resolving at `R = E[2] = 36`
(identical for both machines — the misprediction is *shared*; only the per-machine
issue depth differs):

- **LowMLP (W=2)** is MSHR-gated: shadow requests cannot present until slots free
  (`A'[4]=36`, `A'[5]=41`, both `≥ R=36`), so they reach the bus *after* resolve and
  are killed in the issue queue (`Live=0`). **Only shadow req 3 goes live → issue
  depth 1.**
- **HighMLP (W=6)** has no such gate: its shadow presents immediately (`A'[3..5]=31`)
  and packs against `G=2` — `St[3]=31, St[4]=33, St[5]=35`, all `< 36`. **Three
  shadow requests reach the bus → issue depth 3.** (Req 6 just misses, `St=44 > 36`,
  pushed past resolve by a `TT=4` turnaround bubble plus a `Pen=3` penalty.)

Those three wasted admissions on the wide machine burn bus slots, a turnaround
bubble, and bank occupancy, delaying its **correct-path** tail to `T=88` vs the
narrow window's `T=80`. `Delta = 8` emerges purely from `W`. The
`St`/`Live`/`Cf`/`Pen` rows were recomputed by hand and match the dump exactly.

Remaining sweep (deferred, not yet run): `NB=1`+SPEC and `RESOLVE_DELAY` variants;
report whether Δ-max is **proved** or a timeout **lower bound**.

## Future work (deferred, not modeled)

- **Row-buffer hit/miss + FR-FCFS reordering (Strategy A2)** — the bank tag now
  models *which bank* a request hits; a `row` tag would model *which row* within
  it (hit vs. activate/precharge). But under the current **in-order** service
  (`St` non-decreasing), row hit/miss is identical for both machines, so a row tag
  buys no divergence until the controller **reorders** (FR-FCFS) — which makes
  service order itself `W`-dependent (symbolic permutation, the expensive piece).
  Add the row tag and reordering together, not separately.
- **`tFAW`/`tRRD` activate window (Strategy C)** — a rolling cap of ≤4 row
  activations per window; rides on the existing bank tags. A second independent
  "physics throttles parallelism" mechanism.
- **Out-of-order MSHR completion + same-line miss merging** (gating currently
  assumes in-order completion via `E[j-W]`).

**Done (no longer deferred):** explicit DRAM banks — the scalar locality-blind
`inflight` is now a per-bank count via the shared `Bank[]` tag (Strategy A1). This
is what falsified the dogma at `NB≥3`; see Status.

**Done (no longer deferred):** wrong-path / pipeline-flush waste (Strategy B) — the
`SPEC`/`BR`/`Sq`/`Live`/`Cf`/`Rel` machinery is in `mlp.cpp`, measured **SAT** at
`SPEC=1, NB=2` (a second independent falsifier) and hand-verified. See the Strategy B
subsection under Status.
