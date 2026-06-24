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
- `N`, `S`, `B` — unroll depth, streams (threads), per-access bank service latency.
- `ROB_SIZE`, `MAX_STREAM_MLP` — dependency-matrix pipeline bounds.
- `NB`, `G`, `TRC` — the shared-channel memory model (see critical note below):
  number of DRAM banks, channel inter-admission gap (`1/bandwidth`, must be
  `< B`), and the bank-conflict row-cycle penalty.
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

The model is made falsifiable by a **faithful finite-bandwidth memory channel**
in Axiom 3 (not a bookkeeping window tax — that was the original approach and is
deliberately retired):

- **Bandwidth admission, not strict FIFO.** `St[j] = max(A'[j], St[j-1] + G)`
  with `G < B`. The channel admits a request every `G` cycles, so requests
  genuinely **overlap in flight** — which is the entire point of MLP. (Pacing on
  the prior *start* + `G`, not the prior *end*, is what allows overlap; a
  strict `St[j] >= E[j-1]` FIFO is the UNSAT trap and must not return.)
- **Bank-conflict contention from genuine overlap.** DRAM is `NB` banks
  (synthesized per request as `Bk[i]`). `E[j] = St[j] + B + TRC * conflicts[j]`,
  where `conflicts[j]` counts earlier requests to the **same bank still in
  flight** when `j` starts (`Bk[i] == Bk[j] && E[i] > St[j]`). This is real
  queueing on the modeled timeline, *not* a count over an issue window.

`W` enters **only** through MSHR gating (`A'[j] = max(Aeff[j], E[j-W])`). A wider
window issues earlier → more same-bank requests are truly co-resident in flight
→ more bank conflicts; a narrow window self-throttles and dodges them. The
physics is identical for both machines; only `W` differs.

If you touch `build_machine`: keep the channel bandwidth-limited (overlap must be
possible) **and** keep contention keyed to genuine in-flight overlap (`E[i] >
St[j]`) rather than the bus end-time FIFO, or the discovery query goes vacuously
UNSAT. Do not reintroduce the window-count penalty `PEN * overlap`.

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

Refined finite-bandwidth model (`NB=2`, `G=2`, `TRC=8`, `B=10`), `6 vs 2` MSHRs:
- First counterexample → SAT, delta `1`.
- Maximization climbs to **delta = 21 cycles** (`T_HighMLP = 85 > T_LowMLP = 64`).
- At a 600s timeout (`./mlp 600`) the final probe for `>= 22` returns UNSAT, so
  **delta = 21 is a proved maximum** — no workload makes High-MLP slower by more.
  (At the default 60s timeout that probe times out and 21 is reported only as a
  timeout-limited lower bound; the larger budget closes the proof.)

The dogma still falsifies, and the margin is **physically believable** — a
genuine break-even driven by same-bank bursts the deep MSHR file pulls into
concurrent flight, paying row-cycle conflicts the throttled machine spaces out
and avoids. (This replaces the old unbounded window-tax model, whose 43-cycle
delta was an inflated artifact.)

### Worst-case witness (the proved-maximal workload)

The `delta = 21` counterexample is a clean instance of DRAM bank-conflict
physics, *not* a modeling artifact:

- **HighMLP (W=6)** never gates — six MSHRs exceed anything the unroll needs, so
  `E[j-6]` is always stale and the machine issues as early as the `G=2` channel
  allows. That aggression lands four requests in a bank whose prior access is
  still draining (`j=2,3,4,7`, each `St[j]` one or two cycles short of the
  predecessor's `E[i]`), paying `TRC=8` four times → `T = 85`.
- **LowMLP (W=2)** is throttled by its shallow MSHR file, which incidentally
  spaces same-bank requests exactly one row-service apart (`St[j] == E[i]`, and
  the conflict test `E[i] > St[j]` is strict, so equality is a near-miss). It
  pays **zero** conflicts → `T = 64`.

The throttled machine accidentally implements a bank-aware issue schedule; the
aggressive one collides repeatedly. Delta is 21 rather than the naive `4*8 = 32`
because LowMLP's gating gives back ~11 cycles of latency it would otherwise have
saved — the honest net break-even.
