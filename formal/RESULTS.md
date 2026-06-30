# Results (6 vs 2 MSHRs)

Two **independent** mechanisms can falsify the dogma. We isolate each.

### Bank contention alone (`SPEC=0`)

With speculation off, the outcome depends only on the number of banks, because the
per-bank free concurrency is `C = (B/G)/NB`. Sweeping `NB` at `N=12` (all else
default, 600 s timeout):

| `NB` | `C = (B/G)/NB` | Result | Discovery time |
|------|----------------|--------|----------------|
| `3`  | `1` | **SAT, `Delta ≥ 9`** (lower bound; maximality probe timed out) | first SAT ~110 s |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=0, NB=3

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 9 cycles
  stopped (timeout); reporting best found.

Conclusion: T_HighMLP = 98  >  T_LowMLP = 89   (delta = 9 cycles)
```

**The critical knob is `NB`.** The bandwidth-delay product `B/G = 5` is shared
across `NB` banks, so once `C = (B/G)/NB` floors at 1 (here `NB ≥ 3`), a wide
window that bunches same-bank requests exceeds that bank's free regime and pays
the convex penalty, while a narrow MSHR-gated window spreads requests in time
and stays at `inflight = 1`. **Real DRAM has 8–16 banks**, so `C = 1` is the
ordinary regime, not contrived.

**The `NB=3` witness.** Z3 places bank-0 requests `{0,1,2,3,8,9,10,11}` and bank-1
requests `{4,5,6,7}`:

- **HighMLP (W=6)** never gates on its MSHR file, so it packs same-bank requests
  tight against the `G=2` gap — `nfly` climbs `0 1 2 3`, exceeding `C=1`. With
  `C2=3` the penalty row reads `0 0 3 6` per bank-run (`3·max(0,nfly−1) +
  5·max(0,nfly−3)`), which feeds the `St` chain (backpressure) and pushes the tail
  to `T = 98`.
- **LowMLP (W=2)** is MSHR-gated: its presentation times `A'` jump (request 2 held
  to `A'=31` by `E[0]`), so the *same* requests spread out in time, `nfly` stays
  pinned at **1**, `pen` is all-zero, and it finishes at `T = 89`.

Same shared bank tags, same workload; `Delta = 98 − 89 = 9` emerges purely from
`W` through the schedule. The `nfly`/`pen`/`E` arithmetic was checked by hand
against the model's equations, so the SAT is a real counterexample.

### Wrong-path speculation (`SPEC=1`, the default)

The **headline run** (`SPEC=1, NB=2`) shows that wrong-path waste independently
falsifies the dogma where bank contention alone could not: a wide window issues
deeper down a mispredicted branch, burning bus and MSHR slots on requests that
never retire, delaying correct-path completion.

| Config | Result |
|--------|--------|
| `SPEC=1, NB=2` (headline) | **SAT, Δ≥8** |
| `SPEC=1, CONTENTION=0` (isolation) | **SAT, Δ=5** — proved maximal at `N=8` |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=1, NB=2

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 8 cycles
  stopped (timeout); reporting best found.

Conclusion: T_HighMLP = 88  >  T_LowMLP = 80   (delta = 8 cycles)
```

**Hand-verified witness.** Z3 mispredicts a branch at `BR=2` with a 4-request
shadow `Sq = {3,4,5,6}`, resolving at `R = 36`. The wide machine (W=6) issues
three shadow requests to the bus before the branch resolves (`St[3..5] = 31, 33,
35`), while the narrow machine (W=2) is MSHR-gated and issues only one. Those
three wasted admissions burn bus slots and delay correct-path completion to
`T = 88` versus `T = 80`.

**Isolating speculation from contention.** With `-DCFG_CONTENTION=0`, bank/row
contention is removed entirely (all penalties zeroed). Speculation *alone* still
falsifies the dogma at `N=8`:

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=1, CONTENTION=0, N=8

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
`T = 63`. This proves the two falsifiers are independent: speculation breaks the
dogma without bank contention.
