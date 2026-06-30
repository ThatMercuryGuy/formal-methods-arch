# Results (6 vs 2 MSHRs)

Two **independent** mechanisms can falsify the dogma. We isolate each.

### Bank contention alone (`SPEC=0`)

With speculation off, the outcome depends only on the number of banks, because the
per-bank free concurrency is `C = (B/G)/NB`. Sweeping `NB` at `N=12` (all else
default, 600 s timeout):

| `NB` | `C = (B/G)/NB` | Result | Discovery time |
|------|----------------|--------|----------------|
| `1`  | `5` | **UNSAT** — dogma holds (== old locality-blind baseline) | ~78 s |
| `2` (default) | `2` | **UNSAT** — dogma holds | ~199 s |
| `3`  | `1` | **SAT, `Delta ≥ 9`** (lower bound; maximality probe timed out) | first SAT ~110 s |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=0, NB=3

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 9 cycles
  stopped (timeout); reporting best found.

Conclusion: T_HighMLP = 98  >  T_LowMLP = 89   (delta = 9 cycles)
```

**`NB` is the deciding knob.** The bandwidth-delay product `B/G = 5` is shared
across `NB` banks, so once banks outnumber it enough that `C` floors at 1 (here
`NB ≥ 3`), a wide window that bunches requests onto one bank exceeds that bank's
free regime and pays the convex penalty, while a narrow MSHR-gated window spreads
the same requests out in time and stays at `inflight = 1`. **Real DRAM has 8–16
banks**, so `C = 1` is the ordinary regime, not a contrived starved channel — which
makes this a defensible falsification rather than an artifact. **`NB = 1` is the
sanity check**: one bank means every request collides with every other (the old
locality-blind model, `C = B/G = 5`), reproducing the prior UNSAT baseline and
confirming the bank model is a strict generalization.

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

The **headline run** is `NB=2` — where bank contention *alone* is UNSAT — with
speculation on, asking whether a wide window that issues deeper down a mispredicted
branch (burning bus and MSHR slots on requests that never retire) finishes the real
work later than a narrow one. **It does**, making wrong-path waste a second,
independent falsifier that breaks the dogma where bank contention could not.

| Config | Result | Notes |
|--------|--------|-------|
| `SPEC=0, NB=2` (guard) | **UNSAT** | reproduces the bank-only baseline exactly ✓ |
| `SPEC=0, NB=3` (guard) | **SAT, Δ≥9** | reproduces the bank-only baseline exactly ✓ |
| `SPEC=1, NB=2` (headline) | **SAT, Δ≥8** | lower bound; maximality probe hit the 600 s timeout |
| `SPEC=0, CONTENTION=0` (guard) | **UNSAT** | no penalty, no spec ⇒ monotone-in-W ✓ |
| `SPEC=1, CONTENTION=0` (isolation) | **SAT, Δ=5** | speculation alone, contention off; **proved maximal at `N=8`** (the maximality probe returned UNSAT) |

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=1, NB=2

SAT at delta = 1 cycles; maximizing...
  found larger delta = ... = 8 cycles
  stopped (timeout); reporting best found.

Conclusion: T_HighMLP = 88  >  T_LowMLP = 80   (delta = 8 cycles)
```

**The witness (hand-verified).** Z3 mispredicts a branch at `BR=2` with a 4-request
shadow `Sq = {3,4,5,6}`, resolving at `R = E[2] = 36` (identical for both machines —
the misprediction is *shared*; only the issue depth differs):

- **LowMLP (W=2)** is MSHR-gated, so its shadow requests cannot present until slots
  free: `A'[4]=36`, `A'[5]=41`, both `≥ R=36`. They reach the bus *after* the branch
  resolves and are killed in the issue queue (`Live=0`). **Only shadow request 3
  goes live → issue depth 1.**
- **HighMLP (W=6)** has no such gate: its shadow presents immediately (`A'[3..5]=31`)
  and packs against the `G=2` gap — `St[3]=31, St[4]=33, St[5]=35`, all `< 36`.
  **Three shadow requests reach the bus → issue depth 3.** (Request 6 just misses,
  `St=44 > 36`, pushed past resolve by a `TT=4` turnaround bubble plus a `Pen=3`
  contention penalty.)

Those three wasted admissions on the wide machine burn bus slots, a turnaround
bubble, and bank occupancy, delaying its **correct-path** tail to `T = 88` versus
the narrow window's `T = 80`. Same shared misprediction, same workload; `Delta = 8`
emerges purely from `W`. The `St`/`Live`/`Cf`/`Pen` rows were recomputed by hand and
match the dumped timeline exactly.

This was validated in the order the project requires: the strict-generalization
guard first (`SPEC=0` reproduces both bank-only baselines exactly — see the table),
then the headline run, then hand-verification of the witness.

**Isolating speculation from contention (`CONTENTION=0`).** To confirm the two
falsifiers are genuinely independent, `-DCFG_CONTENTION=0` forces `Pen ≡ 0`,
removing all bank/row contention so speculation is the only anti-MLP mechanism
left. The guard `SPEC=0, CONTENTION=0` is **UNSAT** (no penalty, no speculation ⇒
the channel is monotone in `W`). Turning speculation back on, `SPEC=1,
CONTENTION=0` is **SAT** — and at `N=8` the maximality probe **terminates**,
proving `Δ=5` is the exact maximum (not just a lower bound) at that depth:

```
Query: exists workload with  T_HighMLP > T_LowMLP ?     # SPEC=1, CONTENTION=0, N=8

SAT at delta = 1 cycles; maximizing...
  found larger delta = 5 cycles
  proved maximum: delta = 5 cycles.

Conclusion: T_HighMLP = 68  >  T_LowMLP = 63   (delta = 5 cycles)
```

In the witness the `nfly`/`pen` rows are identically zero for both machines, so
contention is genuinely off and the deviation is **purely speculative**. Z3
mispredicts a branch at `BR=4` with a 2-request shadow `Sq = {5,6}`, resolving at
`R = E[4] = 53` (shared — both machines mispredict identically). All the action is
on wrong-path **read** request 6:

- **LowMLP (W=2)** is MSHR-gated: request 6 cannot present until a slot frees at
  `A'[6]=53`, which is **not** before resolve (`St[6]=53 ≮ 53`), so it is killed in
  the issue queue (`Live=0`). **Issue depth 0** — it never touches the bus.
- **HighMLP (W=6)** has slots to spare: request 6 presents at `A'[6]=52` and reaches
  the bus at `St[6]=52`, squeaking in **one cycle before resolve** (`52 < 53`, so
  `Live=1`). **Issue depth 1.** That doomed read occupies a bus slot and, being a
  read immediately before correct-path write request 7, forces a `TT=4`
  read→write turnaround bubble: `St[7] = 52+G+TT = 58`, vs the narrow window's
  `St[7] = 53` with no bubble.

The single wasted admission delays the wide machine's **correct-path** tail to
`T = 68` versus the narrow window's `T = 63`; `Δ = 5` emerges purely from `W`. The
`St`/`A'`/`Live` rows were recomputed by hand and match the dumped timeline exactly.
This separates the two falsifiers cleanly: speculation does **not** need bank
contention to break the dogma. (The earlier `N=12` run reported the same value as a
timeout *lower bound* `Δ≥5`; the `N=8` run upgrades it to a *proved maximum* at that
depth — max-Δ is non-decreasing in `N`, so `N=12` could in principle climb higher.)
