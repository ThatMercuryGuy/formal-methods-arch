# TODO — Strategy B: validate the wrong-path / speculation model

Implementation brief for the next session. Read `CLAUDE.md` (the "Critical
modeling fact" wrong-path bullet, and the "Status → Strategy B" subsection)
before starting.

---

## 0. Where we are

Strategy B (**wrong-path / pipeline-flush speculation waste**) is **fully
implemented in `mlp.cpp`** and compiles clean at both `-DCFG_SPEC=0` and the
default `-DCFG_SPEC=1`. **It has NOT been run — no SAT/UNSAT result exists yet.**
Your job is to **run and validate it**, hand-verify any witness, and record the
outcome in the docs. Do **not** cite an outcome anywhere until the runs below
land and (if SAT) the witness is hand-checked.

> **Note:** the code may have been through a separate optimization/refactor pass
> before you got it. Do **not** trust line numbers in any old notes — verify
> behavior from the equations the binary prints, and confirm the §1 invariants
> still hold by inspecting the source. The SAT/UNSAT result is what matters, not
> the internal representation.
>
> **Optimization pass applied (Z3 solver-speed only, model-preserving):** (1)
> first-occurrence symmetry breaking now also on stream ids `K[i]` (when `S>1`)
> and (2) on read/write `RW[i]` (pin `RW[0]=0`, the global read↔write flip's Z₂
> quotient); (3) the `Dep[i][j]` matrix allocates only structurally-possible
> entries instead of all `N²` pinned `false`. All three are pure label-symmetry /
> dead-variable removals — read CLAUDE.md's "Label symmetry breaking" note for why
> they preserve `Delta`. Verified: pre/post builds prove the **identical maximum
> `Delta`** wherever maximization terminates (`N=6/7` `NB=3`→14, `NB=1`→5) and
> agree on UNSAT. They do **not** touch any §1 invariant.

**What the mechanism does (recap):** a shared mispredicted branch `BR` and a
contiguous wrong-path shadow `Sq[]` are part of the synthesized workload (both
machines see the *identical* misprediction). Each machine's speculation *depth*
— how many shadow requests reach the bus before the branch resolves at
`R = E[BR]` — **emerges** from its own schedule via `Live[j] = ¬Sq[j] ∨ (St[j] <
R)`. The bus admission chain skips non-live shadow; the MSHR file frees a shadow
slot at `R` only if it never issued (else at `E`); and `T` counts correct-path
completions only. A wide window issues deeper down the wrong path and can finish
the *real* work later — an anti-MLP mechanism independent of bank contention.

**The headline question:** at `NB=2` (where bank contention A1 *alone* is UNSAT),
does turning speculation on flip the query to **SAT**? If yes, speculation waste
is a second, independent falsifier of the dogma.

---

## 1. Invariants that must still hold (verify before trusting any result)

These are the honesty/correctness properties of the model. Confirm them by
reading the source and the printed witness — an optimizer must not have broken
any of them:

- `Sq[]` and `BR` are **shared** across both machines; only `Live`/`R`/`St`/`Cf`/
  `Rel` are per-machine. Never a per-machine shadow set or shadow length.
- Speculation depth **emerges** from `St[i] < R` — never written as a function of
  `W`. (Same discipline as A1's schedule-derived contention.)
- Bus chain **skips** not-bus-live wrong-path requests; the MSHR file does **not**
  skip (in-order allocation, only the release time changes). Different resources,
  handled differently.
- `Inflight` is masked by `Live`; `T` excludes `Sq` requests.
- Every modeled quantity is a named const asserted `== rhs` (prints in the
  witness; no solver slack). Model stays deterministic.
- `SPEC=0` must force every `Sq[i]` false ⇒ every `Live` true ⇒ no skipping ⇒
  the model is **identical** to the bank-only one.

---

## 2. Validation plan (do every step in order — this is how we trust the result)

Build line (defaults shown; vary `-DCFG_SPEC` / `-DCFG_NB`):
```
g++ -std=c++23 mlp.cpp -lz3 -o mlp -O3 -march=native
```

### Step 1 — Strict-generalization guard (NON-NEGOTIABLE, do first)

`SPEC=0` must reproduce the committed bank-only baselines **exactly**. If either
diverges, the generalization is broken — fix before trusting any `SPEC=1` result.

```
g++ -std=c++23 -DCFG_SPEC=0          mlp.cpp -lz3 -o mlp -O3 -march=native
./mlp 600     # NB=2 default  -> MUST be UNSAT (~199s)

g++ -std=c++23 -DCFG_SPEC=0 -DCFG_NB=3 mlp.cpp -lz3 -o mlp -O3 -march=native
./mlp 600     # NB=3          -> MUST be SAT, Delta >= 9
```

(If iterating is slow, drop to `N=8` to smoke-test, then re-confirm at the
committed `N=12`.)

### Step 2 — Headline run (the experiment)

```
g++ -std=c++23 mlp.cpp -lz3 -o mlp -O3 -march=native   # SPEC=1, NB=2
./mlp 600
```

- **SAT** ⇒ speculation waste independently falsifies the dogma.
  **Hand-verify the witness (MANDATORY):** recompute `St`/`Live`/`E` for the
  first few requests from the model's own printed equations (as was done for the
  A1 NB=3 witness — see CLAUDE.md "Status"). Confirm the mechanism:
  - the wide machine's printed **wrong-path issue depth** is **greater** than the
    narrow machine's, and
  - its correct-path tail (`T`) is **later**.
  If the witness does **not** show that, the SAT may be coming from a different
  (possibly buggy) channel — investigate before reporting. The dump already
  prints `Sq`/`BR`, per-machine `cf`/`live`/`R` rows, and the issue depth for
  exactly this purpose.
- **UNSAT** ⇒ an honest negative: speculation waste alone, in-order, does not
  break the dogma at `NB=2`. Record it; it strengthens the case for A2.

### Step 3 — Sweep (only if Step 2 is SAT)

Characterize where the backfire lives:
- `NB=1` + SPEC (does it survive with no bank contention at all?).
- `RESOLVE_DELAY` variations (models compare+redirect latency).
- Report whether the maximized `Delta` was **proved maximal** (final probe
  returned UNSAT) or is a timeout **lower bound** (be explicit, as with A1).

---

## 3. Tractability notes

Speculation adds the integer `BR`, `N` bools (`Sq`), and per-machine `Cf`/`Live`/
`Rel` consts; the `BR`-indexed `R` select is O(N) ite per machine and the
`Live`-masked inflight stays O(N²) with an extra conjunct. Expect solve times in
the same order as A1 (tens of seconds to a few minutes), possibly longer. If a
run hangs:

1. Confirm `MAX_SHADOW` (default 4) is in effect — the banner prints it. It caps
   the shadow length; relax only if an UNSAT looks suspiciously easy, and `log`
   the change.
2. Drop to `N=8` to iterate, then re-confirm at `N=12`.
3. Give the final maximization probe room with a generous CLI timeout
   (`./mlp 600` or more); report UNKNOWN honestly if it times out.

---

## 4. After a result lands — documentation (mirror the A1 write-up)

- **CLAUDE.md:** replace the "Status → Strategy B (IMPLEMENTED, VERIFICATION
  PENDING)" subsection with the measured outcome + the hand-verified witness
  (mirror the A1 NB=3 subsection). Update the "Done / Implemented" footer line.
- **README.md:** replace the "speculation experiment (implemented, verification
  pending)" box with the measured result and a short witness read-out.
- **Memory** (`bank-tag-falsifies-mlp-dogma`): update the Strategy B paragraph
  (currently "IMPLEMENTED, NOT YET RUN") with the outcome.
- **This file:** once B is validated and documented, the next roadmap item is
  **C** (tFAW/tRRD activate window — rides on the existing `Bank` tags) or **A2**
  (row-buffer + FR-FCFS reordering — the expensive one; row tags do nothing until
  service order is W-dependent). B is independent of both and does not block them.
