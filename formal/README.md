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
2. **MSHR gating** — a request cannot present to the channel until a slot frees:
   `A'[j] = max(Aeff[j], Rel[j-W])`. Slot release `Rel[j] = E[j]` (or early at
   squash `R` if the request never issued in `SPEC=1` mode). **Only axiom that
   reads `W`.**
3. **Pipelined channel + convex queueing + turnaround + backpressure.**
   The channel admits one request every `G < B` cycles (requests overlap for
   latency hiding) and adds a `TT`-cycle bubble on read/write direction switches.
   Service cost is convex in concurrent occupancy: `inflight[j] = #{ i<j : E[i] >
   St[j] }`. Penalty and admission:

   ```
   Pen[j] = PEN_LO·max(0, inflight-C) + PEN_HI·max(0, inflight-C2)
   E[j]   = St[j] + B + Pen[j]
   St[j]  = max(A'[j], St[j-1] + G + TT·switch[j] + Pen[j-1])
   ```

   The penalty `Pen[j]` delays both completion **and** the next admission
   (negative feedback). `C = B/G` is the bandwidth-delay product. *(`CONTENTION=0`
   forces `Pen ≡ 0`, dropping this entire queueing term — the channel reduces to a
   pure pipelined bus, `E[j] = St[j] + B` and `St[j] = max(A'[j], St[j-1] + G +
   TT·switch[j])`. See Results.)*
3½. **Wrong-path speculation** *(`SPEC=1` only).* A mispredicted branch `BR`
   triggers a shared shadow `Sq[]` until resolving at `R = E[BR]`, at which point
   it squashes. Per-machine issue depth is emergent:
   `Live[j] = ¬Sq[j] ∨ (St[j] < R)`. The bus skips non-live requests; total
   cycles count correct-path completions only.
4. **Timeline** — `T = max(E)` over correct-path requests.

## What makes the search non-trivial

A purely serial, work-conserving channel is **monotone in `W`** — a larger window
lets requests present no later, so completion times can only fall and the query is
vacuously UNSAT. Axiom 3 breaks that monotonicity with two opposing forces, and
wrong-path speculation adds a third independent one:

- **Pipelining gives MLP a benefit.** Because admission is every `G < B` cycles, a
  wider window packs requests tighter against the bandwidth bound and finishes the
  baseline work earlier — latency hiding.
- **Convex queueing gives MLP a cost.** The same packing drives more requests
  *concurrently in flight*, climbing the convex penalty.
- **Wrong-path speculation gives MLP an independent cost.** A wide window issues
  deeper down a mispredicted branch's shadow before it resolves, burning bus
  admissions (and read/write turnaround bubbles) on work that gets squashed,
  delaying the correct-path tail. A narrow, MSHR-gated window never reaches those
  shadow requests on the bus.

The honesty of the experiment rests on the anti-MLP costs being **emergent, not
injected**:

- **`inflight` is derived from the schedule (`St`/`E`), not a `W`-indexed window.**
  The backfire must therefore *emerge* from timing rather than being injected, and
  because the count spans **all streams** it captures cross-thread interference (an
  aggressive stream floods the channel and delays another stream's critical
  request).
- **The wrong-path shadow (`Sq[]`, `BR`) is shared** across both machines, so the
  solver mispredicts the *same* branch on both — only the per-machine *issue depth*
  (`#{ i : Sq[i] ∧ Live[i] }`, where `Live[j] = ¬Sq[j] ∨ St[j] < R`) can differ,
  and that difference comes solely through `W`.

Read/write turnaround (`TT`) is an additional emergent cost both machines pay
identically. Admission backpressure is *negative feedback*, so — counter-
intuitively — it **bounds** the deviation rather than amplifying it: a flooding
window admits later, which thins its own in-flight count. (An old deliberately-
pathological config drove a 72-cycle deviation when the penalty hit only
completion; with backpressure feeding admission the same config is UNSAT. The
falsifications below survive this feedback, so they are genuine contention effects,
not runaway artifacts.)

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
configurations the standard solver decides in well under a second (`N=6, SPEC=0`
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

g++ -std=c++23 -DCFG_SPEC=0 mlp.cpp -lz3 -o mlp -O3 -march=native  # no speculation
g++ -std=c++23 -DCFG_CONTENTION=0 mlp.cpp -lz3 -o mlp -O3 -march=native  # no queueing contention
```

`-DCFG_SPEC=0` turns off wrong-path speculation. `-DCFG_CONTENTION=0` forces
`Pen ≡ 0`, removing all queueing contention so the channel is a pure pipelined bus
+ MSHR gating + speculation — this **isolates wrong-path speculation** as the sole
anti-MLP mechanism. With both off (`SPEC=0, CONTENTION=0`) the pipelined bus is
monotone in `W` and the query is **UNSAT** (the strict-generalization guard). The
optional first CLI argument is the solver timeout in **seconds** (default 60,
`0` = unlimited); it bounds both the discovery query and each maximization probe.

## Results (6 vs 2 MSHRs)

See [RESULTS.md](RESULTS.md) for the full measured results, including the
wrong-path-speculation falsifier and its hand-verified witness.

## Configuration

All parameters live in `namespace cfg` at the top of `mlp.cpp`:

| Knob             | Meaning                                        | Default |
|------------------|------------------------------------------------|---------|
| `N`              | unroll depth (number of requests)              | 12      |
| `S`              | streams / hardware threads                     | 2       |
| `B`              | memory access latency per request (cycles)     | 10      |
| `ROB_SIZE`       | reorder-buffer dependency horizon              | 4       |
| `MAX_STREAM_MLP` | LSQ: max independent reqs per stream           | 3       |
| `G`              | channel inter-admission gap (`1/bw`, `<B`)     | 2       |
| `TT`             | read/write bus turnaround bubble (cycles)      | 4       |
| `C`              | free concurrency — **derived** `B/G` (floored at 1) | 5 |
| `C2`             | steeper convex knee — **derived** as `C+2`     | 7       |
| `PEN_LO`         | per-overlap cost in the `[C, C2)` regime       | 3       |
| `PEN_HI`         | additional per-overlap cost beyond `C2`        | 5       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                     | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                      | 2       |
| `SPEC`           | wrong-path speculation on (`1`) / off (`0`); `-DCFG_SPEC=0` | 1 |
| `CONTENTION`     | queueing contention on (`1`) / off (`0`, `Pen≡0`); `-DCFG_CONTENTION=0` | 1 |
| `MAX_SHADOW`     | cap on wrong-path shadow length (logged if it binds) | 4 |
| `RESOLVE_DELAY`  | branch resolves at `R = E[BR] + this`          | 0       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and rebuild.
