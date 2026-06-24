# MLP Dogma Discovery Engine

A formal-methods experiment that asks a sharp question about computer
architecture:

> **Is more Memory-Level Parallelism (MLP) always better?**

Conventional wisdom says yes — wider outstanding-miss windows (more MSHRs) hide
memory latency and speed things up. This engine uses the **Z3 SMT solver** to
*search for a counterexample*: a memory-access workload on which a high-MLP core
is **slower** than a low-MLP core. Whether one exists within the bounds is left
to Z3; the engine makes no claim about the answer in advance.

## How it works

`mlp.cpp` is a single-file Bounded Model Checking (BMC) engine. It unrolls a
sequence of `N` memory requests and evaluates that one sequence on **two
mathematical CPU models** simultaneously:

| Machine          | MSHR window `W` |
|------------------|-----------------|
| `System_HighMLP` | 6               |
| `System_LowMLP`  | 2               |

Both machines run the **same workload** under the **same axioms**. The only
difference is the MSHR window `W`.

Crucially, the workload is **not hand-written**. Z3 synthesizes it from scratch
— the data-dependency graph, the per-request stream assignment, and the arrival
times — subject to physical pipeline constraints. We then ask Z3 a single
question with a standard solver (no optimizer):

> Does there exist a legal workload such that `T_HighMLP > T_LowMLP`?

If the answer is **SAT**, Z3 hands back a workload — and the engine then keeps
searching for the workload that **maximizes** the deviation (see *Finding the
worst case* below). If the answer is **UNSAT**, no such workload exists within
the bounds. We report whichever Z3 returns and analyze the witness; we do not
assume an outcome.

### What Z3 gets to choose (the symbolic workload)
- `A[i]` — program-order arrival time of each request.
- `K[i]` — which stream / hardware thread each request belongs to.
- `Dep[i][j]` — a boolean matrix: does request `j` consume request `i`'s result?

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
   outstanding-miss slot frees up: `A'[j] = max(Aeff[j], E[j-W])`.
3. **Finite-bandwidth channel + bank conflicts** — the channel admits a request
   every `G` cycles (`G < B`, so requests overlap in flight):
   `St[j] = max(A'[j], St[j-1] + G)`. A same-bank request still in flight when
   `j` starts costs a row-cycle penalty: `E[j] = St[j] + B + TRC * conflicts[j]`,
   where `conflicts[j] = #{ i<j : Bk[i]==Bk[j] && E[i] > St[j] }`.
4. **Timeline** — total cycles `T = max(E)`.

## What makes the search non-trivial

Axiom 3 models a **finite-bandwidth channel** (one admission every `G` cycles)
with a per-conflict row-cycle penalty (`TRC`) when same-bank requests overlap in
flight on the modeled timeline (`E[i] > St[j]`). This matters for one structural
reason: without a bandwidth-limited channel the model is **monotone in `W`** — a
larger window can never increase completion time — and the discovery query is
vacuously UNSAT. The finite-bandwidth channel is what makes `T_HighMLP >
T_LowMLP` even expressible.

The physics in Axioms 1–4 is **identical for both machines**; only the window
`W` differs. We make no claim here about whether a counterexample exists, or by
what mechanism one would arise if it does — that is exactly what Z3 is asked to
decide, and what we analyze in any witness it returns.

## Finding the worst case

A single counterexample only shows one workload exists. Once the discovery query
is SAT, the engine searches for the workload that **maximizes** the deviation
`Delta = T_HighMLP - T_LowMLP`, so the reported figure is the largest Z3 can
exhibit within the bounds rather than an arbitrary first hit.

This is done **without `z3::optimize`** (a deliberate design constraint), by
incrementally tightening the standard solver: remember the best model, assert
`Delta >= best + 1`, and re-solve. A SAT result is a strictly worse workload (we
adopt it and raise the floor); the first UNSAT proves the previous `Delta` was
the maximum achievable within the bounds. Each solve inherits the solver timeout
(60 s by default, overridable via the CLI argument), so if the final (hardest)
probe times out, the engine reports the best `Delta` found as a lower bound
rather than a proven maximum.

## Building and running

Requirements: a C++23 compiler and Z3 (with `z3++.h` / `libz3`).

```sh
g++ -std=c++23 mlp.cpp -lz3 -o mlp -Ofast -march=native
./mlp            # default 60 s solver timeout
./mlp 120        # raise the solver timeout to 120 s
./mlp 0          # no timeout (run the maximality probe to completion)
```

The optional first argument sets the solver timeout in **seconds** (default 60,
`0` = unlimited). It bounds both the discovery query and each maximization probe,
so a larger value lets the worst-case search push `Delta` further before giving
up.

## Example result (6 vs 2 MSHRs)

```
Query: exists workload with  T_HighMLP > T_LowMLP ?

SAT at delta = 1 cycles; maximizing...
  found larger delta = 3 cycles
  ...
  found larger delta = 21 cycles
  stopped (solver gave up proving a larger delta: canceled); reporting best found.

Conclusion: T_HighMLP = 85  >  T_LowMLP = 64   (delta = 21 cycles)
More memory-level parallelism made this workload SLOWER.
```

For this configuration Z3 first returns a workload with a 1-cycle deviation, then
drives it up to **21 cycles** (6-MSHR core: **85 cycles**, 2-MSHR core: **64**)
before the final maximality probe hits the 60 s timeout — so 21 is a
timeout-limited lower bound on the worst case here, not a proved maximum. These
are the numbers Z3 produced for this run; the engine prints the witness workload
(arrivals, stream assignment, dependency matrix) so the mechanism behind any
particular result can be read off and analyzed directly rather than assumed.

## Configuration

All parameters live in `namespace cfg` at the top of `mlp.cpp`:

| Knob             | Meaning                                   | Default |
|------------------|-------------------------------------------|---------|
| `N`              | unroll depth (number of requests)         | 8       |
| `S`              | streams / hardware threads                | 2       |
| `B`              | bank service latency per access (cycles)  | 10      |
| `ROB_SIZE`       | reorder-buffer dependency horizon         | 4       |
| `MAX_STREAM_MLP` | LSQ: max independent reqs per stream      | 3       |
| `NB`             | DRAM banks on the shared channel          | 2       |
| `G`              | channel inter-admission gap (`1/bw`, `<B`)| 2       |
| `TRC`            | bank-conflict row-cycle penalty (cycles)  | 8       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                 | 2       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and
rebuild.
