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

If the answer is **SAT**, Z3 hands back the exact adversarial workload.

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
2. **MSHR gating** — a request cannot present to the bus until an
   outstanding-miss slot frees up: `A'[j] = max(Aeff[j], E[j-W])`.
3. **FIFO serialization + contention** — single in-order bus channel,
   `E[j] = St[j] + B + PEN * overlap[j]`.
4. **Timeline** — total cycles `T = max(E)`.

## Why high-MLP can lose: contention

The key physics is in Axiom 3. A wider MSHR window lets more *independent*
sibling misses sit on the shared DRAM channel at once. That concurrency is not
free — bank conflicts and arbitration add a penalty (`PEN` cycles) per
overlapping sibling. The low-MLP machine, by issuing fewer misses at a time,
sidesteps this contention.

So there is a genuine trade-off: high-MLP presents requests to the bus *earlier*
but pays *more contention*. The dogma holds only when the first effect wins.
Z3's job is to find a workload where the second effect wins instead.

(Without this contention term the model is monotone in `W` — more MLP could only
ever help — and the search would be vacuously UNSAT. The contention is identical
physics for both machines; only the window `W` differs.)

## Building and running

Requirements: a C++23 compiler and Z3 (with `z3++.h` / `libz3`).

```sh
g++ -std=c++23 mlp.cpp -lz3 -o mlp -Ofast -march=native
./mlp
```

## Example result (6 vs 2 MSHRs)

```
Query: exists workload with  T_HighMLP > T_LowMLP ?

SAT -- discovered a workload where High-MLP is SLOWER.
...
Conclusion: T_HighMLP = 159  >  T_LowMLP = 116   (delta = 43 cycles)
More memory-level parallelism made this workload SLOWER.
```

Z3 autonomously discovered a dependency graph and stream layout where the
6-MSHR core takes **159 cycles** and the 2-MSHR core takes **116** — a 43-cycle
penalty for having *more* parallelism. The dogma is false in general.

## Configuration

All parameters live in `namespace cfg` at the top of `mlp.cpp`:

| Knob             | Meaning                                   | Default |
|------------------|-------------------------------------------|---------|
| `N`              | unroll depth (number of requests)         | 8       |
| `S`              | streams / hardware threads                | 2       |
| `B`              | fixed bus latency per access (cycles)     | 10      |
| `ROB_SIZE`       | reorder-buffer dependency horizon         | 4       |
| `MAX_STREAM_MLP` | LSQ: max independent reqs per stream      | 3       |
| `PEN`            | per-overlap contention penalty (cycles)   | 4       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                 | 2       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and
rebuild.
