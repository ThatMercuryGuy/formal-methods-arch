# TODO — Surface *non-intuitive, constructible* counterexamples

Read `CLAUDE.md` (the "Critical modeling fact" section) and the README "Results"
section before starting. Strategy B (wrong-path speculation) is **done, measured,
and documented** — hand-verified with a proved maximal `Delta=5` at
`CONTENTION=0, SPEC=1, N=8`. It is the mechanism that **falsifies** the dogma; the
convex queueing penalty (`CONTENTION=1`) is a genuine MLP *cost* but does not
falsify on its own (contention alone is UNSAT — the benefit covers the cost and
backpressure bounds divergence).

---

## 0. The new goal (this changes the search objective)

The project has shifted from *"can Z3 falsify the dogma?"* (answered: yes, via
wrong-path speculation) to a sharper aim:

> **Find a workload Z3 synthesizes that maps onto a buildable real-life scenario
> AND that a human architect would not have intuitively predicted.**

This is a different objective from **Δ-max**. The largest-Δ witness is not
necessarily the most interesting one. Concretely:

- The current `SPEC=1` witness (wrong-path read steals a bus slot and
  forces a R→W turnaround) is **real but too intuitive** — "a doomed load steals
  bus bandwidth" is a first-order answer any architect would give. Good existence
  proof, weak as a *surprise*.
- A *non-intuitive* witness has a **causal chain of ≥3 hops** where the cause and
  the victim are **decoupled** — the harm doesn't come from the obvious local cost.
  Z3 is uniquely good at finding these because it doesn't reason locally.

There is a tension to manage explicitly:

- **Too constructible → intuitive** (current R→W story).
- **Too clever → not reproducible on hardware** (hinges on a 1-cycle knife-edge
  margin and a specific tag assignment that only Z3 can set up).
- **Sweet spot → a *structural* pattern** (not a timing accident) that survives
  perturbation and that you can describe as a real access pattern.

---

## 1. Next step: add a robustness filter to the search (highest priority)

Δ-max chases knife-edge timing artifacts (the current witness hinges on a
*one-cycle* margin, `St[6]=52 < R=53`). Those are real physics but **not
constructible** — you cannot hand a colleague a C program that reliably reproduces
a cycle-exact margin. Robust witnesses are both more surprising (structural, not
luck) and more constructible.

**Change the search objective** from "maximize Δ" to "find a witness whose
`T_HighMLP > T_LowMLP` ordering *survives perturbation*":

- After finding a SAT witness, re-check that the ordering still holds under a
  ±k-cycle perturbation of the arrival vector `A[]` (∀ over a small neighborhood,
  or just re-solve pinning each `A[i]` to witness±k and confirm still SAT/ordered).
- Adopt a witness only if the ordering is robust; discard cycle-exact flukes.
- This filters out timing knife-edges and surfaces *structural* counterexamples.

Implement as a post-discovery pass in the maximization loop (do **not** replace the
existing Δ-max loop — add a robustness gate on top, or a second search mode).

## 2. Mine the regimes where ≥3-hop chains live

The intuitive witnesses come from 2-hop chains (wrong-path req → bus slot → tail).
The surprising ones live in the **feedback and cross-thread** regimes:

- **Backpressure non-monotonicity** (`CONTENTION=1`). The closed loop already does
  counterintuitive things (the old completion-only `Δ=72` config became UNSAT once
  `Pen` fed admission). Hunt for a witness where the wide window is slower
  *specifically because* its contention penalty fed back into admission and
  **reordered which requests collide** — a chain "more MLP → earlier admission →
  higher inflight → penalty → later admission of a *different* request" that a human
  reasoning "more overlap = faster" would not trace. (Note: contention alone is
  UNSAT at the default config, so such a witness likely needs `SPEC=1` too — the
  interesting question is whether the feedback *amplifies* the speculation effect.)
- **Cross-thread interference** (`S=2`, already available). `inflight` spans all
  streams. Find a witness where the wide window's aggression on **stream A** floods
  the channel and delays **stream B's** critical request — the victim is not the
  speculating thread. Maps directly to real SMT / multicore bandwidth contention and
  is genuinely non-obvious.
- **Phase-dependent "helpful-looking" request.** A request that looks like it should
  help (arrives early, would prefetch) but lands at the wrong schedule phase and is
  *good for the narrow machine, bad for the wide one* — purely through timing phase.

## 3. For each candidate witness — construct the real scenario

A witness only counts for the new goal if you can **name the hardware access
pattern**. For each survivor of §1:

- Tell the story in architectural terms: pointer-chase colliding with a streaming
  write? an SMT co-runner? a specific branch+load idiom?
- If you **cannot** tell a buildable hardware story, **discard it regardless of Δ.**
- Cite the relevant prior art so the delta is honest:
  - Mutlu, Kim, Armstrong, Patt — wrong-path memory references (WMPI 2004 /
    IEEE TC 2005). The wrong-path-contention effect is known; our novelty is the
    monotonicity-in-`W` framing + the SMT *proof*.
  - Manne, Klauser, Grunwald — "Pipeline Gating: Speculation Control for Energy
    Reduction" (ISCA 1998) + Grunwald et al. "Confidence Estimation for Speculation
    Control" (ISCA 1998). Confidence-throttled speculation exists *for energy*; the
    open angle is **confidence-gated speculative MLP** (cap speculative MSHR/bus use
    while leaving correct-path MLP wide) — protecting correct-path *bandwidth*, not
    energy. Verify these against the PDFs before leaning on the distinction.

---

## Open items deferred from the modeling roadmap (not the current focus)

- **DRAM bank/row locality + FR-FCFS reordering** — a bank/row tag does nothing
  until service order is `W`-dependent (symbolic permutation — the expensive piece),
  so it only becomes honest physics *together with* reordering. Add the tag and
  reordering *together*, not separately.
- **Remaining SPEC sweep** — `RESOLVE_DELAY` variants; report whether each Δ is
  **proved maximal** or a timeout **lower bound**.
- **Re-confirm the `N=8` proved-max at `N=12`** for `CONTENTION=0, SPEC=1` — the
  proof is currently only for `N=8` (max-Δ is non-decreasing in `N`).
