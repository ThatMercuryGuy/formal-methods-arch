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
- `N`, `S`, `B` — unroll depth, streams (threads), per-access bus latency.
- `ROB_SIZE`, `MAX_STREAM_MLP` — dependency-matrix pipeline bounds.
- `PEN` — the per-overlap bus-contention penalty (see critical note below): each
  independent sibling miss inside the MLP window adds `PEN` cycles of service.
- `W_HIGH`, `W_LOW` — the MSHR windows of the two machines (the only knob that
  differs between them). Currently `6` vs `2`.

Changing the comparison (e.g. "6 vs 2 MSHRs") means editing `W_HIGH` / `W_LOW`
only — nothing else.

## Critical modeling fact — do not regress this

The bare hardware axioms (causality / MSHR gating / serialization / max) are
**monotone in `W`**: with a shared workload, more MLP can only lower completion
times. With them alone the query `T_HighMLP > T_LowMLP` is provably **UNSAT**
and the engine discovers nothing. This was observed empirically — an early
version returned UNSAT.

The model is made falsifiable by **shared-resource contention** folded into
Axiom 3 as a per-overlap penalty:

- **Strict FIFO serialization.** `St[j] = max(A'[j], E[j-1])` — a single FIFO
  bus / DRAM channel: request `j` cannot start service until its predecessor on
  the bus has finished.
- **Window-level contention penalty.** `E[j] = St[j] + B + PEN * overlap[j]`,
  where `overlap[j]` counts the earlier **independent** (no true-dep) sibling
  misses inside this machine's MLP window `[j-W, j)` that the MSHR file lets be
  outstanding concurrently with `j`. Each such sibling contends for the shared
  channel (bank conflicts / arbitration) and adds `PEN` cycles.

The penalty is **identical physics for both machines**; the only thing that
differs is the window width `W`. A wider MSHR file exposes more concurrent
siblings → more contention; the narrow window self-throttles and dodges it. `W`
enters both through MSHR gating (`A'[j] = max(Aeff[j], E[j-W])`) and through the
`overlap` window bound.

Key subtlety — **contention must be measured at the MSHR/issue level (the
window), not at the bus level.** A strict index-order FIFO bus can never let two
requests overlap *on the bus* (`St[j] >= E[j-1]`), so a bus-level overlap count
is monotone in `W` and the query goes vacuously **UNSAT**. The `overlap` count
over `[j-W, j)` is what makes the dogma falsifiable; do not move it to the bus
timeline.

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

FIFO + window-tax model (`B=10`, `PEN=4`), `6 vs 2` MSHRs:
- First counterexample → SAT, delta `35`.
- Maximization climbs `35 → 52 → 55 → 56`, and the final probe for `>= 57`
  returns UNSAT, so **delta = 56 is a proved maximum** — no workload within the
  bounds makes High-MLP slower by more. This finishes well inside the default
  60s timeout (a few seconds at `./mlp 30`).

The dogma falsifies: more MLP is worse on the discovered workload. The mechanism
is the window-level contention tax — the deep MSHR file exposes many independent
siblings concurrently and pays `PEN` per overlap, while the shallow window
self-throttles and avoids most of it.

### Worst-case witness (the proved-maximal workload)

The `delta = 56` counterexample (`T_HighMLP = 170 > T_LowMLP = 114`):

- **HighMLP (W=6)** never gates on its MSHR file — six slots exceed anything the
  unroll needs — so it pulls the full window of independent siblings into
  concurrent flight and pays the `PEN * overlap` tax on each, inflating its
  per-access service times and the FIFO tail.
- **LowMLP (W=2)** is throttled by its shallow window: it can only ever count a
  small `overlap`, so it pays far fewer penalty cycles even though MSHR gating
  delays some issues.

The aggressive machine's larger concurrent-sibling count is exactly what its
extra MLP buys, and it is precisely what costs it the contention penalty — the
net is that more MLP makes this workload slower by 56 cycles.
