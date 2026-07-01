# Results (6 vs 2 MSHRs)

The model has two **independent** anti-MLP mechanisms: convex queueing contention
(`CONTENTION=1`) and wrong-path speculation (`SPEC=1`). Each can be isolated.

### Guard: no contention, no speculation (`CONTENTION=0, SPEC=0`)

With both mechanisms off the channel is a pure pipelined bus, which is **monotone
in `W`** — a wider window lets requests present no later, so completion times can
only fall. The query is therefore **UNSAT**: within these bounds, more MLP is never
worse.

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # CONTENTION=0, SPEC=0, N=12

UNSAT: no counterexample. Within these bounds, more MLP is never worse.
```

### Wrong-path speculation in isolation (`CONTENTION=0, SPEC=1`)

With queueing contention removed entirely (`Pen ≡ 0`), wrong-path speculation
**alone** still falsifies the dogma: a wide window issues deeper down a
mispredicted branch, burning bus and MSHR slots on requests that never retire,
delaying correct-path completion.

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # CONTENTION=0, SPEC=1, N=8

SAT at delta = 1 cycles; maximizing...
  found larger delta = 5 cycles
  proved maximum: delta = 5 cycles.

Conclusion: T_HighMLP = 68  >  T_LowMLP = 63   (delta = 5 cycles)
```

**Hand-verified witness.** Z3 mispredicts a branch at `BR=4` with a 2-request
shadow `Sq = {5,6}`, resolving at `R = 53`. The wide machine (W=6) issues one
shadow read to the bus before resolve (`St[6]=52 < 53`), while the narrow machine
(W=2) is MSHR-gated and issues zero. That single wasted admission forces a
read→write turnaround bubble, delaying correct-path completion to `T = 68` versus
`T = 63`. `Delta = 5` emerges purely from `W` through the schedule, and is **proved
maximal** — no workload at `N=8` yields `Delta ≥ 6`. This proves speculation breaks
the dogma without needing queueing contention.

### Convex queueing contention alone (`SPEC=0`, contention on) → UNSAT

With speculation off but contention on, the query is **UNSAT**: the wide window's
latency-hiding benefit covers its queueing cost, so more MLP is never worse. Two
reasons the convex penalty cannot backfire on its own here:

- The free-concurrency `C = B/G = 5` is generous — a wide window has to pack five
  requests concurrently before paying anything, and the bandwidth-delay product
  makes that packing exactly the latency-hiding win.
- **Admission backpressure bounds the divergence.** Feeding `Pen` into the next
  admission is negative feedback: a flooding window admits later, which thins its
  own in-flight count. (An old completion-only penalty drove a proved-maximal
  `Delta=72`; once `Pen` also fed admission the same config became UNSAT.)

So contention is a genuine physical *cost* of MLP, but under in-order service and
this bandwidth it never outweighs the benefit. **Wrong-path speculation is the
mechanism that actually falsifies the dogma** (above).
