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
— the data-dependency graph, the per-request stream assignment, the arrival
times, and the per-request bank tag — subject to physical pipeline constraints.
We then ask Z3 a single question with a standard solver (no optimizer):

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
- `Bank[i]` — which DRAM bank (locality class, `[0,NB)`) each request lands in.
  This is an **abstraction of the address**: only its equivalence class (same
  bank?) affects timing, so we carry the small tag directly rather than a raw
  address. It is **shared** across both machines and drives per-bank contention.
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
   flight **on the same bank** when `j` starts — `inflight[j] = #{ i<j : E[i] >
   St[j] AND Bank[i] == Bank[j] }`, measured in **time** and across **all streams**
   — under a **convex** curve: the first `C` same-bank overlaps are free (bank-level
   parallelism / bus pipelining), past `C` each costs `PEN_LO`, past the steeper
   knee `C2` each costs `PEN_HI` more. That penalty is
   `Pen[j] = PEN_LO*max(0, inflight-C) + PEN_HI*max(0, inflight-C2)`,
   and it does **two** things — it delays this request's completion *and* it
   back-pressures the next request's admission:
   `E[j]  = St[j] + B + Pen[j]`,
   `St[j] = max(A'[j], St[j-1] + G + TT*switch[j] + Pen[j-1])`.
   The backpressure term closes the loop: a request the channel is serving slowly
   holds the bus longer, so the next one admits later, and because `St` is a
   forward chain this **compounds**. `C` itself is **derived** — the bandwidth-delay
   product `B/G` is the number of *distinct banks* the channel keeps busy for free,
   so the *per-bank* free concurrency is `C = (B/G)/NB` (floored at 1) — not a
   hand-tuned constant.
4. **Timeline** — total cycles `T = max(E)`.

## What makes the search non-trivial

A purely serial, work-conserving channel is **monotone in `W`** — a larger
window lets requests present no later, so completion times can only fall, and the
discovery query is vacuously UNSAT. Axiom 3 breaks that monotonicity with the
genuine physics of a shared, pipelined channel:

- **Pipelining gives MLP a real benefit.** Because admission is every `G < B`
  cycles, a wider window packs requests tighter against the bandwidth bound and
  finishes the baseline work earlier — latency hiding.
- **Convex per-bank queueing gives MLP a real cost.** The same packing drives more
  requests *concurrently in flight on the same bank* (`inflight`), climbing the
  convex penalty curve. Crucially `inflight` is derived from the **schedule**
  (`St`/`E`) and counts across **all streams**, so the cost (a) is not a `W`-indexed
  constant — the backfire must *emerge* from timing, not be injected — and (b)
  captures **cross-thread interference**: an aggressive stream floods a bank and
  delays another stream's critical request.
- **Bank locality decides who collides.** Contention is counted *per bank* via the
  shared `Bank[]` tag, so the solver controls *where* requests land but not
  per-machine (the tag is one value per physical request, seen identically by both
  windows). The bandwidth-delay product `B/G` is spread over `NB` banks, so with
  enough banks the per-bank free concurrency `C = (B/G)/NB` falls to 1 — and a wide
  window that bunches same-bank requests then pays a penalty a throttled window
  spreads out and avoids. This is what falsifies the dogma; see *Example result*.
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

g++ -std=c++23 -DCFG_NB=3 mlp.cpp -lz3 -o mlp -Ofast -march=native  # sweep banks
```

The number of banks `NB` can be overridden at compile time with `-DCFG_NB=k`
(default `2`) without editing the source — this is the knob that moves the
SAT/UNSAT boundary (see *Example result*).

The optional first argument sets the solver timeout in **seconds** (default 60,
`0` = unlimited). It bounds both the discovery query and each maximization probe,
so a larger value lets the worst-case search push `Delta` further before giving
up.

## Example result (6 vs 2 MSHRs)

The outcome depends on the number of banks `NB`, because the per-bank free
concurrency is `C = (B/G)/NB`. With **many banks the dogma is falsifiable**;
with few banks it holds. Sweeping `NB` at `N=12` (all else default, `G=2`,
`PEN_LO=3`, `PEN_HI=5`, 600 s solver timeout):

| `NB` | per-bank `C = (B/G)/NB` | Result | Discovery time |
|------|--------------------------|--------|----------------|
| `1`  | `5` | **UNSAT** — dogma holds (== old locality-blind baseline) | ~78 s |
| `2` (default) | `2` | **UNSAT** — dogma holds | ~199 s |
| `3`  | `1` | **SAT, `Delta ≥ 9`** (lower bound; maximality probe timed out) | first SAT ~110 s |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # NB=3

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 9 cycles
  stopped (solver gave up proving a larger delta); reporting best found.

Conclusion: T_HighMLP = 94  >  T_LowMLP = 85   (delta = 9 cycles)
More memory-level parallelism made this workload SLOWER.
```

**Why `NB` is the deciding knob.** The channel's bandwidth-delay product `B/G = 5`
is the in-flight count it sustains for free — but spread across `NB` banks, the
*per-bank* free concurrency is `(B/G)/NB`. Once banks outnumber the BDP enough that
`C` floors at 1 (here `NB ≥ 3`), a wide window that bunches several requests onto
the same bank exceeds that bank's free regime and pays the convex penalty, while a
narrow MSHR-gated window spreads the same requests out in time and stays at
`inflight = 1`. **Real DRAM has 8–16 banks**, so `C = 1` is the ordinary regime,
not a contrived starved channel — which is what makes this a defensible
falsification rather than an artifact.

**`NB = 1` is the sanity check.** With one bank every request collides with every
other (the old locality-blind model) and `C = B/G = 5`; it reproduces the prior
UNSAT baseline in ~78 s, confirming the bank model is a strict generalization that
changes nothing at `NB = 1`.

### The `NB=3` witness (hand-verified)

All 12 requests are writes (so the `TT` turnaround never fires — this is *pure*
bank contention). Z3 places requests 0–7 in **bank 0** and 8–11 in **bank 1**:

- **HighMLP (W=6)** never gates on its MSHR file, so it packs the bank-0 requests
  tight against the `G=2` admission gap — `nfly` on bank 0 climbs to **2–3**,
  exceeding the per-bank free concurrency `C=1`. The `pen` row reads
  `0 0 3 3 3 3 3 3 …`, which feeds the `St` chain (backpressure) and pushes its
  tail to `T = 94`.
- **LowMLP (W=2)** is MSHR-gated: its presentation times `A'` jump (request 2 is
  held to `A'=30` by `E[0]`), so the *same* bank-0 requests spread out in time,
  `nfly` stays pinned at **1**, `pen` is all-zero, and it finishes at `T = 85`.

Same shared bank tags, same workload; `Delta = 94 − 85 = 9` emerges purely from the
window `W` through the schedule. The first four HighMLP requests were recomputed by
hand from the model's own equations and match the dumped timeline exactly, so the
SAT is a real counterexample, not a solver artifact. The engine prints the full
witness (arrivals, streams, read/write **and bank** tags, dependency matrix, and
both machines' `St`/`nfly`/`pen`/`E` timelines) so this mechanism can be read off
directly.

### A note on backpressure (earlier finding, still holds)

The convex penalty back-pressures admission (`Pen[j-1]` feeds `St[j]`), not only
completion. This is *negative feedback* — a contended request admits later, which
thins its own in-flight count — so it **bounds** the deviation rather than
amplifying it. The clearest evidence: an old deliberately-pathological config
(`G=6`, `C=0`, `C2=1`, `PEN_LO=8`, `PEN_HI=16`) drove a 72-cycle deviation when the
penalty hit only completion; with backpressure the same config is UNSAT. The
bank-locality falsification above is therefore *not* a backpressure runaway — it is
a genuine per-bank contention effect that survives the negative feedback.

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
| `C`              | per-bank free concurrency — **derived** as `(B/G)/NB` (floored at 1) | 2 |
| `C2`             | steeper convex knee — **derived** as `C+2`     | 4       |
| `PEN_LO`         | per-overlap cost in the `[C, C2)` regime       | 3       |
| `PEN_HI`         | additional per-overlap cost beyond `C2`        | 5       |
| `W_HIGH`         | MSHRs for `System_HighMLP`                     | 6       |
| `W_LOW`          | MSHRs for `System_LowMLP`                      | 2       |

To compare different MLP budgets (e.g. 6 vs 1), edit `W_HIGH` / `W_LOW` and
rebuild.
