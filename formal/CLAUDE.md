# CLAUDE.md

Guidance for working in this directory (`formal/`).

## What this is

A single-file Z3 (C++ API) Bounded Model Checking engine, `mlp.cpp`, that tests
the architectural dogma *"more Memory-Level Parallelism is always better."* It
unrolls `N` memory requests over two state machines (`System_HighMLP`,
`System_LowMLP`) that share one synthesized workload and differ only in their
MSHR / outstanding-miss window `W`. A standard `z3::solver` (NOT `z3::optimize`)
searches for a workload where `T_HighMLP > T_LowMLP`.

## Build & run

```
g++ -std=c++23 mlp.cpp -lz3 -o mlp -Ofast -march=native
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

Hand-verified against the model's own equations (first four HighMLP requests
recomputed by hand match the dumped timeline exactly — the SAT is real, not a
solver artifact). All 12 requests are writes (so `TT` never fires — this is pure
bank contention). Z3 puts requests 0–7 in **bank 0**, 8–11 in **bank 1**:

- **HighMLP (W=6)** never gates on its MSHR file, so it packs the bank-0 requests
  tight against the `G=2` admission gap — `nfly` for bank 0 climbs to **2–3**,
  exceeding the per-bank free concurrency `C=1`. The convex penalty `pen` row reads
  `0 0 3 3 3 3 3 3 …`, which feeds the `St` chain (backpressure) and pushes its tail
  to `T=94`.
- **LowMLP (W=2)** is MSHR-gated: its `A'` jumps (req 2: `A'=30` vs `Aeff=26`, held
  by `E[0]`) so the *same* bank-0 requests spread out in time, `nfly` stays pinned
  at **1**, `pen` is all-zero, and it finishes at `T=85`.

Same shared bank tags, same workload; `Delta = 94 − 85 = 9` emerges purely from `W`
through the schedule. Read the `Bk`, `nfly`, and `pen` rows together: the bank tag
decides *who collides*, `nfly` counts the same-bank collisions, and `pen` turns them
into delay that both slows completion (`E`) and back-pressures the next admission
(`St`).

## Future work (deferred, not modeled)

- **Wrong-path / pipeline-flush waste (Strategy B, planned next)** — a wide window
  issues deeper down a mispredicted path, consuming channel admission and MSHR
  slots on requests that are later squashed. An independent, anti-MLP mechanism;
  in-order, cheap to add on top of the current model.
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
