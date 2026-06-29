# MLP Dogma Discovery Engine

A formal-methods experiment that asks a sharp computer-architecture question:

> **Is more Memory-Level Parallelism (MLP) always better?**

Conventional wisdom says yes — wider outstanding-miss windows (more MSHRs) hide
memory latency and speed things up. This engine uses the **Z3 SMT solver** to
*search for a counterexample*: a memory-access workload on which a high-MLP core
is **slower** than a low-MLP core. Whether one exists within the bounds is left
to Z3; the engine assumes no answer in advance.

## How it works

`mlp.cpp` is a single-file Bounded Model Checking (BMC) engine. It unrolls a
sequence of `N` memory requests and evaluates that one sequence on **two
mathematical CPU models** that differ only in their MSHR window `W`:

| Machine          | MSHR window `W` |
|------------------|-----------------|
| `System_HighMLP` | 6               |
| `System_LowMLP`  | 2               |

Both run the **same workload** under the **same axioms**; only `W` differs.

Crucially, the workload is **not hand-written** — Z3 synthesizes it from scratch,
subject to physical pipeline constraints. We then pose a single question to a
standard solver (no optimizer):

> Does there exist a legal workload such that `T_HighMLP > T_LowMLP`?

**SAT** means Z3 found such a workload, and the engine then searches for the one
that **maximizes** the deviation (see *Finding the worst case*). **UNSAT** means
no such workload exists within the bounds. We report whichever Z3 returns and
analyze the witness.

### What Z3 chooses (the symbolic workload)
- `A[i]` — program-order arrival time of each request.
- `K[i]` — which stream / hardware thread each request belongs to.
- `RW[i]` — read (`0`) or write (`1`); drives the bus turnaround bubbles.
- `Bank[i]` — which DRAM bank (locality class `[0,NB)`) the request lands in.
  This is an **abstraction of the address**: only its equivalence class (same
  bank?) affects timing, so we carry the small tag rather than a raw address. It
  is **shared** across both machines and drives per-bank contention.
- `Dep[i][j]` — a boolean matrix: does request `j` consume request `i`'s result?
- `BR`, `Sq[i]` — *(speculation, `SPEC=1` only)* the index of a mispredicted
  branch and the contiguous **shadow** of wrong-path requests after it. Both are
  **shared** across the machines, so only the per-machine *issue depth* — how far
  each window gets down the wrong path before the branch resolves — can differ.

### The physical pipeline bounds (keep the search realistic)
- **Strictly upper-triangular** dependencies — a request can only depend on an
  earlier one.
- **ROB horizon** — dependencies cannot span more than `ROB_SIZE` slots.
- **Stream matching** — a true dependency requires matching stream ids.
- **LSQ capacity** (`MAX_STREAM_MLP`) — a single stream can have at most
  `MAX_STREAM_MLP` concurrently-independent requests in flight.

### The hardware axioms (applied identically to both machines)

1. **Causality** — a dependent request waits for its producer to retire;
   otherwise it respects program order.
2. **MSHR gating** — a request cannot present to the channel until an
   outstanding-miss slot frees: `A'[j] = max(Aeff[j], Rel[j-W])`. The slot
   release `Rel[j]` is `E[j]`, except a squashed wrong-path request (`SPEC=1`)
   that never issued, which frees early at the squash cycle `R`. This is the
   **only** axiom that reads `W`.
3. **Pipelined channel + convex queueing + turnaround + backpressure** — the core
   physics. The channel admits a new request every `G` cycles (`G < B`, so
   requests **overlap in flight** — the latency hiding MLP exists to exploit),
   plus a `TT`-cycle bubble whenever the read/write direction switches. Service
   cost grows with how many earlier requests are still in flight **on the same
   bank** when `j` starts — `inflight[j] = #{ i<j : E[i] > St[j] ∧ Bank[i] ==
   Bank[j] }` — under a **convex** curve: the first `C` same-bank overlaps are
   free, past `C` each costs `PEN_LO`, past the steeper knee `C2` each costs
   `PEN_HI` more:

   ```
   Pen[j] = PEN_LO·max(0, inflight-C) + PEN_HI·max(0, inflight-C2)
   E[j]   = St[j] + B + Pen[j]                       # completion
   St[j]  = max(A'[j], St[j-1] + G + TT·switch[j] + Pen[j-1])   # next admission
   ```

   `Pen` does **two** things: it delays this request's completion *and*
   back-pressures the next request's admission. That backpressure closes a
   **negative-feedback** loop — a request the channel serves slowly holds the bus
   longer, so the next admits later, and because `St` is a forward chain this
   compounds. `C` is **derived, not tuned**: the bandwidth-delay product `B/G` is
   the number of distinct banks the channel keeps busy for free, so the *per-bank*
   free concurrency is `C = (B/G)/NB` (floored at 1).
3½. **Wrong-path speculation** *(`SPEC=1` only; `SPEC=0` removes it entirely and
   reduces the model to exactly Axioms 1–4)*. A mispredicted branch `BR` is fetched
   and the front-end speculatively issues the wrong-path shadow `Sq[]` until the
   branch resolves at `R = E[BR]`, then squashes it. The depth each machine reaches
   is **emergent**: `Live[j] = ¬Sq[j] ∨ (St[j] < R)` marks whether request `j`
   actually occupies the bus on *this* machine. The bus admission chain **skips**
   non-live shadow requests (one killed before it issues costs the bus nothing),
   and `inflight` and the MSHR release are likewise masked by `Live`. Total cycles
   count **correct-path completions only**, so a wide window that issues deeper
   down the wrong path can delay the real work a narrow window finishes sooner.
4. **Timeline** — total cycles `T = max(E)` over **correct-path** requests
   (squashed wrong-path requests excluded; with `SPEC=0` there are none, so this
   is just `max(E)`).

## What makes the search non-trivial

A purely serial, work-conserving channel is **monotone in `W`** — a larger window
lets requests present no later, so completion times can only fall and the query is
vacuously UNSAT. Axiom 3 breaks that monotonicity with two opposing forces:

- **Pipelining gives MLP a benefit.** Because admission is every `G < B` cycles, a
  wider window packs requests tighter against the bandwidth bound and finishes the
  baseline work earlier — latency hiding.
- **Convex per-bank queueing gives MLP a cost.** The same packing drives more
  requests *concurrently in flight on the same bank*, climbing the convex penalty.

The honesty of the experiment rests on two design choices:

- **`inflight` is derived from the schedule (`St`/`E`), not a `W`-indexed window.**
  The backfire must therefore *emerge* from timing rather than being injected, and
  because the count spans **all streams** it captures cross-thread interference (an
  aggressive stream floods a bank and delays another stream's critical request).
- **The bank tag is one shared value per physical request**, seen identically by
  both machines, so the solver controls *where* requests land but cannot declare a
  pair "conflicting" for the wide machine and "not" for the narrow one. Which
  requests actually collide is decided by the schedule, which differs only through
  `W`.

Read/write turnaround (`TT`) is a third emergent cost. Admission backpressure is
*negative feedback*, so — counter-intuitively — it **bounds** the deviation rather
than amplifying it: a flooding window admits later, which thins its own in-flight
count. (An old deliberately-pathological config drove a 72-cycle deviation when the
penalty hit only completion; with backpressure feeding admission the same config is
UNSAT. The falsifications below survive this feedback, so they are genuine
contention effects, not runaway artifacts.)

## Finding the worst case

A single counterexample shows only that one workload exists. Once the discovery
query is SAT, the engine searches for the workload that **maximizes**
`Delta = T_HighMLP - T_LowMLP`, so the reported figure is the largest Z3 can
exhibit within the bounds rather than an arbitrary first hit.

This is done **without `z3::optimize`**, by incrementally tightening the standard
solver: remember the best model, assert `Delta >= best + 1`, re-solve. SAT yields a
strictly worse workload (adopt it, raise the floor); the first UNSAT proves the
previous `Delta` maximal. Each solve inherits the solver timeout, so if the final
(hardest) probe times out, the engine reports the best `Delta` as a **lower bound**.

Avoiding `z3::optimize` is **measured, not stylistic.** Z3 offers a direct
`maximize(Delta)` objective, but switching to it was over 200× slower here:
configurations the standard solver decides in well under a second (`N=6, NB=1`
UNSAT in 0.27 s) all time out at 60 s under `z3::optimize`. The regression is in the
optimize *core*, not the objective search — even a no-objective feasibility check
timed out — most likely because optimize skips the preprocessing
(`solve-eqs`/`simplify`) that collapses this model's hundreds of definitional
equalities. The maximality proof is equally sound either way (`max = N` ⟺ `Delta ≥
N+1` is UNSAT); the manual loop is simply the only one that finishes.

## Building and running

Requirements: a C++23 compiler and Z3 (with `z3++.h` / `libz3`).

```sh
g++ -std=c++23 mlp.cpp -lz3 -o mlp -O3 -march=native
./mlp            # default 60 s solver timeout
./mlp 120        # raise the solver timeout to 120 s
./mlp 0          # no timeout (run the maximality probe to completion)

g++ -std=c++23 -DCFG_NB=3   mlp.cpp -lz3 -o mlp -O3 -march=native  # sweep banks
g++ -std=c++23 -DCFG_SPEC=0 mlp.cpp -lz3 -o mlp -O3 -march=native  # no speculation
```

`-DCFG_NB=k` (default `2`) sets the number of banks without editing the source —
the knob that moves the SAT/UNSAT boundary. `-DCFG_SPEC=0` turns off wrong-path
speculation, reducing the model **exactly** to the bank-only one. The optional
first CLI argument is the solver timeout in **seconds** (default 60, `0` =
unlimited); it bounds both the discovery query and each maximization probe.

## Results (6 vs 2 MSHRs)

Two **independent** mechanisms can falsify the dogma. We isolate each.

### Bank contention alone (`SPEC=0`)

With speculation off, the outcome depends only on the number of banks, because the
per-bank free concurrency is `C = (B/G)/NB`. Sweeping `NB` at `N=12` (all else
default, 600 s timeout):

| `NB` | `C = (B/G)/NB` | Result | Discovery time |
|------|----------------|--------|----------------|
| `1`  | `5` | **UNSAT** — dogma holds (== old locality-blind baseline) | ~78 s |
| `2` (default) | `2` | **UNSAT** — dogma holds | ~199 s |
| `3`  | `1` | **SAT, `Delta ≥ 9`** (lower bound; maximality probe timed out) | first SAT ~110 s |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=0, NB=3

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 9 cycles
  stopped (timeout); reporting best found.

Conclusion: T_HighMLP = 98  >  T_LowMLP = 89   (delta = 9 cycles)
```

**`NB` is the deciding knob.** The bandwidth-delay product `B/G = 5` is shared
across `NB` banks, so once banks outnumber it enough that `C` floors at 1 (here
`NB ≥ 3`), a wide window that bunches requests onto one bank exceeds that bank's
free regime and pays the convex penalty, while a narrow MSHR-gated window spreads
the same requests out in time and stays at `inflight = 1`. **Real DRAM has 8–16
banks**, so `C = 1` is the ordinary regime, not a contrived starved channel — which
makes this a defensible falsification rather than an artifact. **`NB = 1` is the
sanity check**: one bank means every request collides with every other (the old
locality-blind model, `C = B/G = 5`), reproducing the prior UNSAT baseline and
confirming the bank model is a strict generalization.

**The `NB=3` witness.** Z3 places bank-0 requests `{0,1,2,3,8,9,10,11}` and bank-1
requests `{4,5,6,7}`:

- **HighMLP (W=6)** never gates on its MSHR file, so it packs same-bank requests
  tight against the `G=2` gap — `nfly` climbs `0 1 2 3`, exceeding `C=1`. With
  `C2=3` the penalty row reads `0 0 3 6` per bank-run (`3·max(0,nfly−1) +
  5·max(0,nfly−3)`), which feeds the `St` chain (backpressure) and pushes the tail
  to `T = 98`.
- **LowMLP (W=2)** is MSHR-gated: its presentation times `A'` jump (request 2 held
  to `A'=31` by `E[0]`), so the *same* requests spread out in time, `nfly` stays
  pinned at **1**, `pen` is all-zero, and it finishes at `T = 89`.

Same shared bank tags, same workload; `Delta = 98 − 89 = 9` emerges purely from
`W` through the schedule. The `nfly`/`pen`/`E` arithmetic was checked by hand
against the model's equations, so the SAT is a real counterexample.

### Wrong-path speculation (`SPEC=1`, the default)

The **headline run** is `NB=2` — where bank contention *alone* is UNSAT — with
speculation on, asking whether a wide window that issues deeper down a mispredicted
branch (burning bus and MSHR slots on requests that never retire) finishes the real
work later than a narrow one. **It does**, making wrong-path waste a second,
independent falsifier that breaks the dogma where bank contention could not.

| Config | Result | Notes |
|--------|--------|-------|
| `SPEC=0, NB=2` (guard) | **UNSAT** | reproduces the bank-only baseline exactly ✓ |
| `SPEC=0, NB=3` (guard) | **SAT, Δ≥9** | reproduces the bank-only baseline exactly ✓ |
| `SPEC=1, NB=2` (headline) | **SAT, Δ≥8** | lower bound; maximality probe hit the 600 s timeout |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=1, NB=2

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 8 cycles
  stopped (timeout); reporting best found.

Conclusion: T_HighMLP = 88  >  T_LowMLP = 80   (delta = 8 cycles)
```

**The witness (hand-verified).** Z3 mispredicts a branch at `BR=2` with a 4-request
shadow `Sq = {3,4,5,6}`, resolving at `R = E[2] = 36` (identical for both machines —
the misprediction is *shared*; only the issue depth differs):

- **LowMLP (W=2)** is MSHR-gated, so its shadow requests cannot present until slots
  free: `A'[4]=36`, `A'[5]=41`, both `≥ R=36`. They reach the bus *after* the branch
  resolves and are killed in the issue queue (`Live=0`). **Only shadow request 3
  goes live → issue depth 1.**
- **HighMLP (W=6)** has no such gate: its shadow presents immediately (`A'[3..5]=31`)
  and packs against the `G=2` gap — `St[3]=31, St[4]=33, St[5]=35`, all `< 36`.
  **Three shadow requests reach the bus → issue depth 3.** (Request 6 just misses,
  `St=44 > 36`, pushed past resolve by a `TT=4` turnaround bubble plus a `Pen=3`
  contention penalty.)

Those three wasted admissions on the wide machine burn bus slots, a turnaround
bubble, and bank occupancy, delaying its **correct-path** tail to `T = 88` versus
the narrow window's `T = 80`. Same shared misprediction, same workload; `Delta = 8`
emerges purely from `W`. The `St`/`Live`/`Cf`/`Pen` rows were recomputed by hand and
match the dumped timeline exactly.

This was validated in the order the project requires: the strict-generalization
guard first (`SPEC=0` reproduces both bank-only baselines exactly — see the table),
then the headline run, then hand-verification of the witness.

## Configuration

All parameters live in `namespace cfg` at the top of `mlp.cpp`:

| Knob             | Meaning                                        | Default |
|------------------|------------------------------------------------|---------|
| `N`              | unroll depth (number of requests)              | 12      |
| `S`              | streams / hardware threads                     | 2       |
| `B`              | bank access latency per request (cycles)       | 10      |
| `ROB_SIZE`       | reorder-buffer dependency horizon              | 4       |
| `MAX_STREAM_MLP` | LSQ: max independent reqs per stream           | 3       |
| `G`              | channel inter-admission gap (`1/bw`, `<B`)     | 2       |
| `TT`             | read/write bus turnaround bubble (cycles)      | 4       |
| `NB`             | DRAM banks (locality classes); `-DCFG_NB=k`    | 2       |
| `C`              | per-bank free concurrency — **derived** `(B/G)/NB` (floored at 1) | 2 |
| `C2`             | steeper convex knee — **derived** as `C+2`     | 4       |
| `PEN_LO`         | per-overlap cost in the `[C, C2)` regime       | 3       |
| `PEN_HI`         | additional per-overlap cost beyond `C2`        | 5       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                     | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                      | 2       |
| `SPEC`           | wrong-path speculation on (`1`) / off (`0`); `-DCFG_SPEC=0` | 1 |
| `MAX_SHADOW`     | cap on wrong-path shadow length (logged if it binds) | 4 |
| `RESOLVE_DELAY`  | branch resolves at `R = E[BR] + this`          | 0       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and rebuild.
