# MLP Dogma Discovery Engine

A formal-methods experiment that asks a sharp question about computer
architecture:

> **Is more Memory-Level Parallelism (MLP) always better?**

Conventional wisdom says yes — wider outstanding-miss windows (more MSHRs) hide
memory latency and speed things up. This engine uses the **Z3 SMT solver** to
*search for a counterexample*: a memory-access workload on which a high-MLP core
is provably **slower** than a low-MLP core. It finds one.

## How it works

`mlp.cpp` is a single-file Bounded Model Checking (BMC) engine. It unrolls a
sequence of `N` memory requests and evaluates that one sequence on **two
mathematical CPU models** simultaneously:

| Machine          | MSHR window `W` | Behavior                |
|------------------|-----------------|-------------------------|
| `System_HighMLP` | 6               | aggressive, many misses in flight |
| `System_LowMLP`  | 2               | throttled, few misses in flight   |

Both machines run the **same workload**. The only difference is `W`.

Crucially, the workload is **not hand-written**. Z3 synthesizes it from scratch
— the data-dependency graph, the per-request stream assignment, and the arrival
times — subject to physical pipeline constraints. We then ask Z3 a single
question with a standard solver (no optimizer):

> Does there exist a legal workload such that `T_HighMLP > T_LowMLP`?

If the answer is **SAT**, Z3 hands back the exact adversarial workload — and the
engine then keeps searching for the workload that **maximizes** the slowdown
(see *Finding the worst case* below).

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

## Why high-MLP can lose: bank contention

The key physics is in Axiom 3, and it is modeled the way real hardware behaves —
not as a bookkeeping tax. The memory channel has **finite bandwidth** (one
admission every `G` cycles), so a wide MSHR window genuinely puts more requests
*in flight at the same time*. When two of those in-flight requests hit the **same
DRAM bank**, the bank cannot serve both open rows at once and pays a row-cycle
penalty (`TRC`). The low-MLP machine issues fewer misses at a time, so fewer
same-bank requests are ever co-resident — it sidesteps the conflicts.

So there is a genuine trade-off: high-MLP presents requests *earlier* but, by
crowding the banks, pays *more conflicts*. The dogma holds only when the first
effect wins. Z3's job is to find a workload where the second effect wins instead.

(Contention arises from **genuine in-flight overlap** on the modeled timeline
(`E[i] > St[j]`), not from counting an issue window. Without a bandwidth-limited
channel the model is monotone in `W` — more MLP could only ever help — and the
search would be vacuously UNSAT. The physics is identical for both machines; only
the window `W` differs.)

## Finding the worst case

A single counterexample only proves the dogma is *false*; it says nothing about
*how* false. So once the discovery query is SAT, the engine searches for the
workload that **maximizes** the deviation `Delta = T_HighMLP - T_LowMLP`.

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

Z3 first finds a workload with a 1-cycle slowdown, then drives it up to a
**21-cycle** worst case (6-MSHR core: **85 cycles**, 2-MSHR core: **64**) before
the final maximality probe hits the 60 s timeout — so 21 is a timeout-limited
lower bound on the worst case here. The slowdown comes from same-bank access
bursts the deep MSHR file pulls into concurrent flight, paying row-cycle
conflicts the throttled core spaces out and avoids. With a faithful
finite-bandwidth channel the effect is real but bounded — a genuine break-even,
not the inflated artifact an unbounded contention tax would produce. The dogma is
false in general.

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
