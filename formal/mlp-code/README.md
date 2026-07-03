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
- `RW[i]` — read (`0`) or write (`1`); drives the bus turnaround bubbles.
- `BR`, `Sq[i]` — the index of a mispredicted branch and the contiguous **shadow**
  of wrong-path requests after it. Both are **shared** across the machines, so only
  the per-machine *issue depth* — how far each window gets down the wrong path
  before the branch resolves — can differ.

### The hardware axioms (applied identically to both machines)

1. **Program order** — requests present in program order (arrivals `A[i]` are
   non-decreasing).
2. **MSHR gating** — a request cannot present to the channel until a slot frees:
   `A'[j] = max(A[j], Rel[j-W])`. Slot release `Rel[j] = E[j]` (or early at
   squash `R` if the request never issued). **Only axiom that reads `W`.**
3. **Pipelined channel + turnaround.** The channel admits one request every
   `G < B` cycles (requests overlap for latency hiding) and adds a `TT`-cycle
   bubble on read/write direction switches. Service is flat:

   ```
   E[j]  = St[j] + B
   St[j] = max(A'[j], St[j-1] + G + TT·switch[j])
   ```

   With the shadow forced empty this is the whole channel: a pure pipelined bus,
   which is **monotone in `W`** (see below) and therefore UNSAT.
3½. **Wrong-path speculation.** A mispredicted branch `BR`
   triggers a shared shadow `Sq[]` until resolving at `R = E[BR]`, at which point
   it squashes. Per-machine issue depth is emergent:
   `Live[j] = ¬Sq[j] ∨ (St[j] < R)`. The bus skips non-live requests; total
   cycles count correct-path completions only.
4. **Timeline** — `T = max(E)` over correct-path requests.

## What makes the search non-trivial

A purely serial, work-conserving channel is **monotone in `W`** — a larger window
lets requests present no later, so completion times can only fall and the query is
vacuously UNSAT. The **pipelined bus of axiom 3 does not break this on its own**:
with speculation off, every request reaches the bus in program order and both
machines see the identical admission and turnaround sequence, so a wider window
only presents requests no later. The no-shadow model is therefore UNSAT — the
strict-generalization guard.

**Wrong-path speculation breaks monotonicity.** A wide window issues deeper down a
mispredicted branch's shadow before it resolves, burning bus admissions (and
read/write turnaround bubbles) on work that gets squashed, delaying the
correct-path tail. A narrow, MSHR-gated window never reaches those shadow requests
on the bus. This is the single anti-MLP mechanism in the model, and it is what
falsifies the dogma.

The honesty of the experiment rests on the anti-MLP cost being **emergent, not
injected**: the wrong-path shadow (`Sq[]`, `BR`) is **shared** across both
machines, so the solver mispredicts the *same* branch on both — only the
per-machine *issue depth* (`#{ i : Sq[i] ∧ Live[i] }`, where
`Live[j] = ¬Sq[j] ∨ St[j] < R`) can differ, and that difference comes solely
through `W`. Read/write turnaround (`TT`) is an additional cost both machines pay
identically.

### Not modeled: convex queueing contention

A tempting second anti-MLP mechanism is a channel crowding cost — a
schedule-derived `inflight[j] = #{ i<j : E[i] > St[j] }` count, a convex two-knee
penalty on it (`PEN_LO`/`PEN_HI` past knees `C = B/G` and `C2`), fed into both
completion and the next admission (backpressure). It does no work on the
falsification: **contention alone never breaks the dogma** — with the shadow empty
the query is UNSAT whether contention is on or off. The free-concurrency
`C = B/G = 5` is generous and admission backpressure is *negative feedback* that
**bounds** the deviation rather than amplifying it (a flooding window admits later,
thinning its own in-flight count), so the wide window's latency-hiding benefit
always covers its queueing cost. Contention as a *second, independent* falsifier
(grounded in memory-controller queueing rather than branch misprediction) would
require a much steeper near-saturation curve than that bounded two-tier ramp, and
remains an open question rather than a settled result.

### Not modeled: a dependency subsystem

Another tempting addition is a shared data-dependency matrix `Dep[i][j]` (does
request `j` consume `i`'s result?), turned into a timing constraint through a
causality loop `Aeff[j] = max(A[j], max{ E[i]+1 : Dep[i][j] })`, bounded to a
reorder window `ROB_SIZE`, with the concurrently-independent requests in that
window capped at `MAX_LSQ_MLP`. It does no work on either anchor: with dependencies
available the guard (no shadow) stays **UNSAT** and the falsifier still proves the
**same maximal `Delta=5`** — Z3 has dependencies on hand to reach `Delta≥6` and
cannot. An LSQ cap is moreover a pure *restriction* — adding a
constraint only narrows the search, so it can hide counterexamples but never create
one. Requests present directly at their arrival `A[j]`. If a future regime
(`RESOLVE_DELAY>0`, larger `N`) yields a witness that turns on a dependency, add it
from that concrete counterexample.

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
configurations the standard solver decides in well under a second (the `N=6`
no-shadow guard, UNSAT in 0.27 s) all time out at 60 s under `z3::optimize`. The regression is in the
optimize *core*, not the objective search — even a no-objective feasibility check
(`N=6`) timed out — most likely because optimize skips the preprocessing
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
```

Wrong-path speculation is the model's sole anti-MLP mechanism and is always on.
The strict-generalization guard (the pure pipelined bus, monotone in `W` and
therefore **UNSAT**) is recovered by forcing the shadow empty: set `MAX_SHADOW = 0`
in `namespace cfg`, or assert `!Sq[i]` for all `i`, and rebuild. The optional first
CLI argument is the solver timeout in **seconds** (default 60, `0` = unlimited); it
bounds both the discovery query and each maximization probe.

## Results (6 vs 2 MSHRs)

Two anchors, both at the default `N=12` (proved, not timeout lower bounds):

- **Guard — empty shadow (`MAX_SHADOW=0`) → UNSAT.** The pure pipelined bus is
  monotone in `W`, so more MLP is never worse. This is the strict-generalization
  guard.
- **Speculation on → SAT, `Delta = 5` (proved maximum, ~3 s).** Wrong-path
  speculation falsifies the dogma.

**Hand-verified witness.** Z3 mispredicts a branch at `BR=2` with a 4-request
shadow `Sq = {3,4,5,6}`, resolving at `R=26`. The wide machine (W=6) issues **all
four** shadow requests to the bus before resolve (`St[j] < R`), while the narrow
machine (W=2) is MSHR-gated and issues only **one**. Those three extra wasted
admissions shove the wide machine's correct-path tail 5 cycles later
(`T_High=65 > T_Low=60`). `Delta = 5` emerges purely from `W` through the schedule
and is **proved maximal** — no workload at `N=12` yields `Delta ≥ 6`. (Absolute `T`
depends on `B`/`G`/`TT`; the load-bearing quantity is `Delta`.)

## Configuration

All parameters live in `namespace cfg` at the top of `mlp.cpp`:

| Knob             | Meaning                                        | Default |
|------------------|------------------------------------------------|---------|
| `N`              | unroll depth (number of requests)              | 12      |
| `B`              | memory access latency per request (cycles)     | 10      |
| `G`              | channel inter-admission gap (`1/bw`, `<B`)     | 2       |
| `TT`             | read/write bus turnaround bubble (cycles)      | 4       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                     | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                      | 2       |
| `MAX_SHADOW`     | cap on wrong-path shadow length (logged if it binds; `0` recovers the guard) | 4 |
| `RESOLVE_DELAY`  | branch resolves at `R = E[BR] + this`          | 0       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and rebuild.
