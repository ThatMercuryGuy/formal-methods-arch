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
- `RW[i]` — whether each request is a read (`0`) or a write (`1`), which drives
  the bus turnaround bubbles.
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
3. **Pipelined channel + convex queueing + read/write turnaround + backpressure**
   — the channel admits a new request every `G` cycles (`G < B`, so requests
   **overlap in flight** — this is the latency hiding MLP exists to exploit), plus
   a `TT`-cycle turnaround bubble whenever the service direction switches
   read↔write. The service cost grows with how many earlier requests are still in
   flight when `j` starts — `inflight[j] = #{ i<j : E[i] > St[j] }`, measured in
   **time** and across **all streams** — under a **convex** curve: the first `C`
   overlaps are free (bank-level parallelism / bus pipelining), past `C` each
   costs `PEN_LO`, past the steeper knee `C2` each costs `PEN_HI` more. That
   penalty is `Pen[j] = PEN_LO*max(0, inflight-C) + PEN_HI*max(0, inflight-C2)`,
   and it does **two** things — it delays this request's completion *and* it
   back-pressures the next request's admission:
   `E[j]  = St[j] + B + Pen[j]`,
   `St[j] = max(A'[j], St[j-1] + G + TT*switch[j] + Pen[j-1])`.
   The backpressure term closes the loop: a request the channel is serving slowly
   holds the bus longer, so the next one admits later, and because `St` is a
   forward chain this **compounds**. `C` itself is **derived** — it is the
   bandwidth-delay product `B/G`, the in-flight count a pipelined channel sustains
   for free — not a hand-tuned constant.
4. **Timeline** — total cycles `T = max(E)`.

## What makes the search non-trivial

A purely serial, work-conserving channel is **monotone in `W`** — a larger
window lets requests present no later, so completion times can only fall, and the
discovery query is vacuously UNSAT. Axiom 3 breaks that monotonicity with the
genuine physics of a shared, pipelined channel:

- **Pipelining gives MLP a real benefit.** Because admission is every `G < B`
  cycles, a wider window packs requests tighter against the bandwidth bound and
  finishes the baseline work earlier — latency hiding.
- **Convex queueing gives MLP a real cost.** The same packing drives more
  requests *concurrently in flight* (`inflight`), climbing the convex penalty
  curve. Crucially `inflight` is derived from the **schedule** (`St`/`E`) and
  counts across **all streams**, so the cost (a) is not a `W`-indexed constant —
  the backfire must *emerge* from timing, not be injected — and (b) captures
  **cross-thread interference**: an aggressive stream floods the channel and
  delays another stream's critical request.
- **Read/write turnaround** adds a `TT`-cycle bubble on each direction switch; a
  tightly-packed wide window eats more of them than a throttled one.
- **Admission backpressure** feeds the convex penalty `Pen` forward into the next
  request's start time, not just the contended request's completion. This is
  *negative feedback* — a flooding window admits later, which thins out its own
  in-flight count — so it does **not** make the backfire easier; it bounds how far
  the deviation can run. (We expected the opposite; see *Example result*.)

The physics in Axioms 1–4 is **identical for both machines**; only the window
`W` differs. Whether more MLP helps or hurts is now genuinely workload- and
regime-dependent — that is exactly what Z3 is asked to decide, and what we
analyze in any witness it returns. (Notably, under the realistic default config
below the answer is **UNSAT** — see *Example result*.)

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

Under the realistic default config (pipelined channel `G=2`, free concurrency
derived as `C = B/G = 5`, convex knee `C2=7`, `PEN_LO=3`, `PEN_HI=5`), the dogma
**holds**:

```
Query: exists workload with  T_HighMLP > T_LowMLP ?

UNSAT: no counterexample. Within these bounds, more MLP is never worse -- the dogma holds.
```

This is the honest outcome: with the channel offering its full bandwidth-delay
product of free concurrency, the latency-hiding benefit of the wide window always
covers its added contention within these bounds. More MLP is never worse here.

### A result that overturned the expectation

Two changes were made specifically to make the cost side of the model more
faithful, on the hypothesis that they would make the backfire *easier* to find:

1. **`C` was derived rather than hand-set.** The free concurrency a pipelined
   channel sustains is the bandwidth-delay product `B/G` (here `10/2 = 5`), so the
   previous hand-set `C=2` understated the channel's own pipelining.
2. **The convex penalty was made to back-pressure admission**, not only
   completion — a flooding window should stall its own future issue.

Change 2 did the **opposite** of what we predicted. Backpressure is negative
feedback: a contended request admits later → its start time slides forward →
fewer earlier requests are still in flight when it starts → its `inflight` count
and penalty *fall*. The loop throttles the channel toward its sustainable rate
and **bounds** the deviation instead of amplifying it. The clearest evidence: the
old deliberately-pathological channel (`G=6`, `C=0`, `C2=1`, `PEN_LO=8`,
`PEN_HI=16`) previously drove a proved-maximal **72-cycle** deviation
(`154 > 82`); with the penalty also feeding admission, **the very same config is
now UNSAT**. That 72 was partly an artifact of charging latency without spacing
requests out — a more faithful channel does not allow it.

### Where the backfire survives

It survives only just below the free-concurrency boundary, and only modestly.
Sweeping `C` (everything else at default, backpressure on):

| `C` (free concurrency) | Result |
|------------------------|--------|
| `5` (default = `B/G`)  | **UNSAT** — dogma holds |
| `4`                    | **UNSAT** |
| `3`                    | **UNSAT** |
| `2`                    | **UNSAT** |
| `1`                    | SAT, proved max `Delta = 6` |
| `0` (no free parallelism) | SAT, proved max `Delta = 4` |

The boundary still sits between `C=1` and `C=2` — exactly where it was before
backpressure — confirming that the closed loop changed the *magnitude* of the
backfire (capping the 72 to a 6), not the *regime* in which it appears.

In the surviving `C=1` witness all eight requests are reads (so `TT` never
fires); the wide machine's `nfly` climbs `0 1 2 3` and its `pen` row reads
`0 0 3 6`, which feeds its `St` chain and pushes its tail to `E[7]=65`, while the
MSHR-gated narrow machine spreads its requests out, keeps `nfly` pinned at 1, pays
zero penalty, and finishes at `E[7]=59`. The engine prints the full witness
(arrivals, streams, read/write tags, dependency matrix, and both machines'
`St`/`nfly`/`pen`/`E` timelines) so this mechanism can be read off directly rather
than assumed.

## Configuration

All parameters live in `namespace cfg` at the top of `mlp.cpp`:

| Knob             | Meaning                                        | Default |
|------------------|------------------------------------------------|---------|
| `N`              | unroll depth (number of requests)              | 8       |
| `S`              | streams / hardware threads                     | 2       |
| `B`              | bank access latency per request (cycles)       | 10      |
| `ROB_SIZE`       | reorder-buffer dependency horizon              | 4       |
| `MAX_STREAM_MLP` | LSQ: max independent reqs per stream           | 3       |
| `G`              | channel inter-admission gap (`1/bw`, `<B`)     | 2       |
| `TT`             | read/write bus turnaround bubble (cycles)      | 4       |
| `C`              | free concurrency — **derived** as `B/G`        | 5       |
| `C2`             | steeper convex knee — **derived** as `C+2`     | 7       |
| `PEN_LO`         | per-overlap cost in the `[C, C2)` regime       | 3       |
| `PEN_HI`         | additional per-overlap cost beyond `C2`        | 5       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                     | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                      | 2       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and
rebuild.
