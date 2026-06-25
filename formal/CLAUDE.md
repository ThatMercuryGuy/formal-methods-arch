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
- `N`, `S`, `B` — unroll depth, streams (threads), per-request bank latency.
- `ROB_SIZE`, `MAX_STREAM_MLP` — dependency-matrix pipeline bounds.
- `G` — channel inter-admission gap (`1/bandwidth`, `< B`): the channel admits a
  new request every `G` cycles, so requests overlap in flight (latency hiding).
- `TT` — read/write bus turnaround bubble added on each direction switch.
- `C`, `C2`, `PEN_LO`, `PEN_HI` — the **convex** queueing-delay curve (see
  critical note below): the first `C` concurrently-in-flight requests are free,
  past `C` each costs `PEN_LO`, past the steeper knee `C2` each costs `PEN_HI`
  more. **`C` is derived, not hand-set:** `C := B/G` (the bandwidth-delay product
  — the in-flight count a pipelined channel sustains for free), and `C2 := C+2`.
  Change them by changing `B`/`G`, not by typing a magic number.
- `W_HIGH`, `W_LOW` — the MSHR windows of the two machines (the only knob that
  differs between them). Currently `6` vs `2`.

Changing the comparison (e.g. "6 vs 2 MSHRs") means editing `W_HIGH` / `W_LOW`
only — nothing else.

## Critical modeling fact — do not regress this

A purely serial, work-conserving channel is **monotone in `W`**: with a shared
workload, a larger window lets requests present no later, so completion times can
only fall, and `T_HighMLP > T_LowMLP` is vacuously **UNSAT**. The model breaks
that monotonicity with the genuine physics of a shared, **pipelined** channel
(Axiom 3), folded in as **identical physics for both machines** — only `W`
differs:

- **Pipelined finite bandwidth (the MLP *benefit*).** The channel admits a new
  request every `G` cycles (`G < B`):
  `St[j] = max(A'[j], St[j-1] + G + TT*switch[j])`, where `switch[j]` is 1 when
  the read/write direction changes from `j-1`. Because admission is faster than
  service, requests **overlap in flight** — a wider window packs them tighter
  against the `G` bound and finishes baseline work earlier. This is the latency
  hiding MLP exists to exploit.
- **Convex queueing delay (the MLP *cost*), measured in TIME.** `inflight[j] =
  #{ i<j : E[i] > St[j] }` — how many earlier requests are still in service when
  `j` starts. Service cost is **convex** in it: the first `C` overlaps are free
  (bank-level parallelism), past `C` each costs `PEN_LO`, past `C2` each costs
  `PEN_HI` more. This penalty is named `Pen[j]` and applied to completion:
  `Pen[j] = PEN_LO*max(0,inflight-C) + PEN_HI*max(0,inflight-C2)`,
  `E[j] = St[j] + B + Pen[j]`.
- **Admission backpressure (the closed loop) — `Pen` feeds `St`, not just `E`.**
  The convex penalty is *not* a dead-end term on completion. It also feeds forward
  into the next request's admission:
  `St[j] = max(A'[j], St[j-1] + G + TT*switch[j] + Pen[j-1])`.
  A request the channel is serving slowly holds the resource longer, so the next
  request admits later — and because `St` is a forward chain, this **compounds**
  across all later requests. This is genuine negative feedback: a contended
  request admits later → later `St` → fewer earlier requests still in flight when
  it starts → *lower* `inflight` → smaller penalty. The loop throttles the
  channel toward its sustainable rate, exactly as a real shared bus does.

**Why this is honest and not rigged.** The old model added `PEN * overlap` where
`overlap` counted siblings in the *index* window `[j-W, j)` — a quantity
**monotone in `W` by construction**, so the wide machine *could not* pay less and
the "discovery" was essentially an arithmetic identity. The new `inflight` count
is derived from the **schedule** (`St`/`E`), not from a `W`-indexed window, and
spans **all streams** — so (a) the backfire must *emerge* from timing rather than
being injected, and (b) it captures **cross-thread interference** (an aggressive
stream floods the channel and delays another stream's critical request). The
read/write turnaround `TT` is a third emergent cost. Do **not** revert to an
index-window penalty term — that reintroduces the monotone-by-design artifact.

Key consequence — **under the realistic default config the query is UNSAT, and
that is the correct result, not a bug.** With `C = B/G = 5` requests of free
concurrency, the wide window's latency-hiding benefit always covers its
contention cost within the bounds. The backfire only emerges in a starved regime
(small `C`). See Status.

**Backpressure caps the divergence — do not expect it to break the dogma.** It is
tempting to think feeding `Pen` into admission must make "more MLP is worse"
*easier* (the wide window floods the channel and stalls its own issue). The
opposite happens: backpressure is negative feedback, so it *bounds* the very
divergence the old completion-only penalty allowed to run away. Concretely, the
old pathological config (`G=6, C=0, C2=1, PEN_LO=8, PEN_HI=16`) drove a
proved-maximal `Delta=72` when the penalty only hit `E`; with the penalty also
feeding `St` the same config is **UNSAT**. That `72` was partly an artifact of
penalizing latency *without spacing requests out*. The closed loop is the more
faithful physics and it makes the dogma harder, not easier, to falsify.

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

Pipelined-channel + convex-queueing + R/W-turnaround model **with admission
backpressure** and **derived free-concurrency `C = B/G`**, `6 vs 2` MSHRs.

**Default (realistic) config — `G=2, TT=4, C=B/G=5, C2=7, PEN_LO=3, PEN_HI=5`:**
the query is **UNSAT**. Within the bounds, more MLP is never worse. **This is the
honest result: the dogma holds under realistic physics.**

**Two changes landed since the previous status, and the second overturned the
expected outcome:**

1. **`C` is now derived (`C := B/G = 5`), not hand-set to `2`.** The free
   concurrency a pipelined channel sustains *is* the bandwidth-delay product, so
   the old `C=2` understated the channel's own pipelining. This alone moves the
   default deeper into the UNSAT region.
2. **The convex penalty now back-pressures admission** (`Pen[j-1]` added to the
   `St[j]` gap), not just completion. The expectation was that letting a wide
   window stall its own issue would make the backfire *easier* to find. It does
   the opposite: backpressure is **negative feedback**, so it bounds the
   divergence the old completion-only penalty let run away.

**The SAT/UNSAT boundary still sits between `C=1` and `C=2`** — backpressure did
*not* move it. Sweeping `C` with both changes in place (other defaults fixed):

| `C` | result |
|-----|--------|
| `5` (default = `B/G`) | **UNSAT** — dogma holds |
| `4` | **UNSAT** |
| `3` | **UNSAT** |
| `2` | **UNSAT** |
| `1` | SAT, proved max `Delta = 6` |
| `0` | SAT, proved max `Delta = 4` |

**The old pathological channel is now UNSAT.** `G=6, C=0, C2=1, PEN_LO=8,
PEN_HI=16` previously drove a proved-maximal `Delta=72` (`154 > 82`). With the
penalty also feeding `St`, the same config is **UNSAT**: the runaway tail was an
artifact of penalizing latency without spacing requests out. Backpressure throttles
the flooding machine toward the channel's sustainable rate, capping the deviation.

### Worst-case witness (the `C=1`, `Delta=6` proved-maximal workload)

The surviving backfire is small and lives just below the boundary. At `C=1` (all
8 requests are reads, so `TT` never fires) Z3 drives a proved-maximal `Delta=6`:

- **HighMLP (W=6)** never gates on its MSHR file, so it packs requests; its `nfly`
  climbs `0 1 2 3`, and the convex penalty `pen` row reads `0 0 3 6` — that penalty
  feeds the `St` chain (`St[3]=23=max(A'=22, St[2]+G+pen[2]=18+2+3)`), pushing its
  tail to `E[7]=65`.
- **LowMLP (W=2)** is MSHR-gated: its `A'` jumps (`24, 26, …`) so requests spread
  out, `nfly` stays pinned at **1**, `pen` is all-zero, and it finishes at
  `E[7]=59`.

So the wide machine's extra temporal in-flight count is what costs it — but with
backpressure the cost no longer compounds without bound, so the maximum deviation
is a modest `6` rather than the old `72`. Read the `nfly` and `pen` rows together
to see the mechanism: `nfly` drives `pen`, and `pen` now feeds both `E` (this
request finishes later) and the next `St` (the next request admits later).

## Future work (deferred, not modeled)

- **FR-FCFS / reordering memory controller** — the canonical academic backfire
  mechanism; the channel here services in index order (`St` non-decreasing). With
  backpressure now capping the convex-contention backfire, reordering/row-buffer
  effects are the most likely remaining route to a realistic-regime SAT.
- **Explicit DRAM row-buffer / banks / `tRC`/`tFAW`** — the scalar `inflight`
  penalty is a locality-blind proxy for these; it cannot express one stream
  evicting another's open row.
- **Out-of-order MSHR completion + same-line miss merging** (gating currently
  assumes in-order completion via `E[j-W]`).
