# TODO — Surface *non-intuitive, constructible* counterexamples

Read `CLAUDE.md` (the "Critical modeling fact" section) and the README "Results"
section before starting. Strategy B (wrong-path speculation) is **done, measured,
and documented** — hand-verified with a proved maximal `Delta=5` at `N=8`. It is
the **only** anti-MLP mechanism in the model, the one that falsifies the dogma, and
it is always on. With the shadow forced empty (`MAX_SHADOW=0`) the channel is a
pure pipelined bus, monotone in `W`, and the query is UNSAT (the guard).

Two mechanisms are deliberately *not* modeled because they do no work on the
falsification — see "Why convex queueing contention is not a falsifier" and "Why a
dependency subsystem is not an amplifier" in RESULTS.md. Do not add either
speculatively; §4 tracks contention as a possible future *second* falsifier, an
open question rather than the current focus.

---

## 0. The goal (this is the search objective)

Beyond the existence proof (Z3 falsifies the dogma via wrong-path speculation), the
sharper aim is:

> **Find a workload Z3 synthesizes that maps onto a buildable real-life scenario
> AND that a human architect would not have intuitively predicted.**

This is a different objective from **Δ-max**. The largest-Δ witness is not
necessarily the most interesting one. Concretely:

- The current witness (wrong-path read steals a bus slot and
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
*one-cycle* margin, `St[6] < R`). Those are real physics but **not
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
The surprising ones live in the deeper interaction of speculation depth with the
schedule:

- **Speculation-depth vs. turnaround interplay.** The current witness spends its
  wasted admission on a single R→W bubble. Hunt for a witness where the wide
  window's *deeper* wrong-path issue reorders the read/write pattern of the
  **correct-path** tail — a chain "more MLP → deeper shadow → different `TT`
  bubble placement on real requests → later correct-path completion" that a human
  reasoning "wrong-path loads just waste one slot" would not trace.
- **Phase-dependent "helpful-looking" request.** A request that looks like it
  should help (arrives early, would prefetch) but lands at the wrong schedule
  phase and is *good for the narrow machine, bad for the wide one* — purely
  through timing phase and MSHR gating.
- **`RESOLVE_DELAY` regimes.** A later branch resolve (`RESOLVE_DELAY > 0`) widens
  the window in which the wide machine can issue shadow requests. Sweep it and
  look for a witness whose structure (not just Δ) changes qualitatively.

## 3. For each candidate witness — construct the real scenario

A witness only counts for the new goal if you can **name the hardware access
pattern**. For each survivor of §1:

- Tell the story in architectural terms: pointer-chase colliding with a streaming
  write? a specific branch+load idiom?
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

## 4. (Deferred) Contention as a *second* falsifier — open question

Convex queueing contention does not falsify on its own (UNSAT with the shadow
empty, contention on or off; RESULTS.md). To be a genuine second mechanism it would
have to flip SAT in isolation, which a bounded two-tier ramp does not. If this
direction is pursued:

- A *bounded* piecewise-linear ramp (free below `C = B/G`, `+PEN_LO` past `C`,
  `+PEN_HI` more past `C2 = C+2`, flat linear forever after) structurally
  under-models near-saturation regardless of constants. Real memory-controller
  queueing is closer to **hyperbolic** — latency grows toward a singularity as
  utilization approaches capacity (M/M/1-style).
- Add a third, much steeper (or superlinear-in-pieces) segment past a second knee
  `C3`, staying piecewise-linear so Z3's arithmetic core still handles it (avoid
  true nonlinearity — it defeats the Simplex/diff-logic engine per CLAUDE.md's
  solver notes).
- It must be justified by a **measured SAT** in isolation, not added speculatively.
  Absent that, speculation remains the sole mechanism and the model stays simple.

---

## (Tabled) Redesign the witness output for human interpretability

The current SAT-witness dump (`mlp.cpp` `main`) prints the raw internal timeline
variables — `A'`, `Cf`, `St`, `Live`, `E` — as terse numeric grids. It is
unintelligible without an AI walking through it; the goal is output a human can
interpret unaided.

**Hard constraint (why this is non-trivial):** the printer must **not** hardcode
the current model's story. Any prose that says "wrong-path read stole a slot and
forced a R→W turnaround" is a shadow reimplementation of the physics that silently
rots the moment the model changes (new mechanism, deleted turnaround, etc.). The
output must be a **pure data transform over the model's own named quantities** —
change the model and the view re-shapes itself. This kills the narrative-prose
option outright.

**What survives that constraint (agreed direction):**

- **Layer 1 — visualize the shared workload (KEEP, agreed).** Everything Z3 chose
  that is *identical* for both machines — `A`, `RW` (as R/W with turnaround
  positions), `Sq`/`BR` (wrong-path shadow) — printed once, in *domain* terms.
  This is interpretable precisely because it is not in the
  internal-variable currency; it's the input the human reasons about. Both machines
  share this (built once in `main`, passed to both `build_machine` calls), so
  everything downstream differs *only* through `W`.

- **Domain-level outcome = the bus schedule (leading candidate).** The internal
  timeline vars are plumbing; the thing that physically *happens* is which request
  occupies the bus, when, and which never get on. Render `St`/`E`/`Live` as bars on
  a shared cycle axis, one lane per machine, with the `R` resolve line and R/W
  turnaround bubbles marked (all derivable generically). You read the difference
  physically — High's bus carries extra wrong-path bars that shove the correct-path
  tail right — with no variable decoding. Still a pure transform of
  `St`/`E`/`Live`, so it survives model changes.

**Rejected during discussion:**
- Narrative/prose summary (Layer "A") — requires re-deriving semantics; rots.
- Per-request High−Low **delta grid** and the **argmax/finish-line** view
  (Layers 2 & 3) — deemed unhelpful: they still speak in the internal-variable
  currency (just diffed / max'd), which is exactly what's unintelligible today.

**Open questions to resolve before implementing:**
1. Is "what happened on the bus" the right domain-level outcome, or is the bus not
   the mental model to lead with?
2. How much annotation on the schedule (turnaround bubbles, `R` line) before it
   tips from "showing the data" into "telling the answer."
3. Fate of the raw internal grids: behind a `-v`/`--verbose` flag (leading choice),
   always-appended, or removed.

**Implementation note (generic machinery):** the `Timeline` struct is already the
schema. A field registry (`{name, &vec, INT|BOOL}`) driving the printer means
adding/removing a model quantity is a one-line edit and the output tracks it — no
per-field print code. Auto-hide any quantity that is uniform to keep the view lean.

---

## Open items deferred from the modeling roadmap (not the current focus)

- **DRAM bank/row locality + FR-FCFS reordering** — a bank/row tag does nothing
  until service order is `W`-dependent (symbolic permutation — the expensive piece),
  so it only becomes honest physics *together with* reordering. Add the tag and
  reordering *together*, not separately.
- **Remaining speculation sweep** — `RESOLVE_DELAY` variants; report whether each Δ
  is **proved maximal** or a timeout **lower bound**.
- **Re-confirm the `N=8` proved-max at `N=12`** — the proof is currently only for
  `N=8` (max-Δ is non-decreasing in `N`).
