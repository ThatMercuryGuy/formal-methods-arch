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
./mlp
```

Z3 4.x with `z3++.h` is installed system-wide (`/usr/include`, `libz3.so`).
g++ 15.2 (full C++23). There is no test harness or build script — compile and
run directly. The model is deterministic; the same config yields the same model.

## Where to change things

All knobs are in `namespace cfg` at the top of `mlp.cpp`:
- `N`, `S`, `B` — unroll depth, streams (threads), bus latency.
- `ROB_SIZE`, `MAX_STREAM_MLP` — dependency-matrix pipeline bounds.
- `W_HIGH`, `W_LOW` — the MSHR windows of the two machines (the only knob that
  differs between them). Currently `6` vs `2`.
- `PEN` — per-overlap contention penalty (see critical note below).

Changing the comparison (e.g. "6 vs 2 MSHRs") means editing `W_HIGH` / `W_LOW`
only — nothing else.

## Critical modeling fact — do not regress this

The bare hardware axioms (causality / MSHR gating / FIFO / max) are **monotone
in `W`**: more MLP can only lower completion times. With them alone the query
`T_HighMLP > T_LowMLP` is provably **UNSAT** and the engine discovers nothing.
This was observed empirically — an early version returned UNSAT.

The model is made falsifiable by **shared-channel contention** in Axiom 3:
`E[j] = St[j] + B + PEN * overlap[j]`, where `overlap[j]` counts earlier
*independent* sibling misses inside the machine's own `W`-window. This penalty
is identical physics for both machines; only `W` differs, so a wider window
exposes more concurrent siblings → more contention. Contention is measured at
the **MSHR/issue level (the window), not the bus level** — a strict index-order
FIFO bus never lets two requests overlap on the bus (`St[j] >= E[j-1]`), so a
bus-level overlap count is always zero and reintroduces the UNSAT trap.

If you touch `build_machine`, keep the contention term tied to `W`, or the
discovery query goes vacuously UNSAT.

## Status

`6 vs 2` MSHRs → SAT, `T_HighMLP = 159 > T_LowMLP = 116` (43-cycle penalty).
`6 vs 1` MSHRs → SAT, delta 52 cycles. The dogma falsifies cleanly.
