# TODO — build order

Read `CLAUDE.md` first for the full model description, settled design decisions,
and (strict) coding principles. Build **one function at a time**, in this order,
verifying each layer before moving on. Do not dump large chunks.

## DONE

- [x] **Primitives**: `is_present`, `lru_line`, `updated_cache`. Verified on
  concrete values (move-to-front, insert-evict, victim-swap all correct).
- [x] **State scaffolding**: `Params`, `fresh_cache`, `init_empty`,
  `constrain_trace`. Verified (sentinels distinct/negative, trace bound enforced).

- [x] **Transitions**: `step_nine`, `step_victim`. Both take current-state
  lists, the access, and caller-allocated next-state lists; emit equality
  constraints tying next-state to `updated_cache(...)`; return the hit flags.
  - `step_nine` verified on concrete cases: L2-hit reorder / L3 untouched,
    L2-miss+L3-miss DRAM-fill, L2-miss+L3-hit promote (incl. `w3=2` reorder).
  - `step_victim` verified: L2-hit → victim unchanged; L2-miss+victim-miss →
    insert evictee; L2-miss+victim-hit → swap (accessed line out, L2 evictee in),
    incl. `v=2` cases.
  - **Shared-L2 spine PROVEN**: with a symbolic L2 and access fed to both step
    functions, `Or(l2_next_nine[i] != l2_next_victim[i])` is `unsat` — the two
    designs can never disagree on the L2 next-state. In `build_model` both also
    write the same `l2_traj[t+1]` vars, so identity holds by construction too.

## NEXT: first real run

Everything below the primitives is written and unit-verified. The only open item
is the end-to-end run + hand-check of the witness (see "Search + report").

## Cost (DONE, verified)

- [x] **`access_cost(l2_hit, mid_hit, params)`** → Z3 Int.
  `l2 + If(Not(l2_hit), l3 + If(Not(mid_hit), ld, 0), 0)`. One function for both
  designs (`mid_hit` = `l3_hit` for NINE, `victim_hit` for Victim). Verified all
  three tiers (1 / 11 / 111) for both mid_hit values; on an L2 hit `mid_hit` is
  correctly irrelevant.

## Assembly (DONE, verified)

- [x] **`build_model(params)`** — unroll `t = 0..N-1`:
  - Allocates ONE shared L2 trajectory (`l2_traj[0..N]`, same var list fed to
    both step functions), one NINE L3 trajectory, one victim trajectory, and the
    `access_sequence`.
  - `init_empty` on all caches at `t=0`; `constrain_trace(access_sequence, K)`.
  - Each timestep: calls `step_nine` and `step_victim` (both reading the SAME
    `l2_traj[t]` and both writing `l2_traj[t+1]`), accumulates `cost_nine` and
    `cost_victim` via `access_cost`.
  - Returns a `Bundle` dataclass: constraint list, `access_sequence`, all
    trajectories, both cost sums, per-step hit-flag vectors.
  - `gap` is `cost_victim - cost_nine`, used inline at the `maximize` site — NOT
    a standalone function (trivial one-liners are inlined per user preference).
- [x] Smoke test: `sat` for trace `[0,0,0,0]`, both costs 114, gap 0.

## Search + report (in progress)

- [x] **`solve_for_counterexample(bundle, params)`** — `z3.Optimize`; adds all
  constraints; asserts negated hypothesis `cost_victim > cost_nine`;
  `maximize(cost_victim - cost_nine)`; returns `(opt, opt.check())`.
- [x] **`report_result(opt, result, bundle, params)`**:
  - SAT: prints `access_sequence`, both full trajectories per timestep, `C_NINE`,
    `C_victim`, `gap`, and the derived integer
    `#(NINE-L3 hits among L2-miss steps) - #(victim hits among L2-miss steps)`,
    with the `gap == ld * that_integer` cross-check.
  - UNSAT: prints the bounded result with all params (H holds up to this N, K —
    NOT a general proof).
- [ ] First real run on the small config `Params(w2=2, w3=1, v=1, N=4, K=3,
  l2=1, l3=10, ld=100)` — run via `python3 model.py`.

## THEN: Sweep driver (last)

- [ ] **`main` / sweep** over:
  - `K` around the `w2` / `w2-1` capacity boundary.
  - `w3` vs `v`: start `w3 = v` (isolates the fill-source mechanism cleanly),
    then `w3 >> v`.
  - `N` increasing — CRITICAL: both designs start cold/empty with NO warm-up, so
    a single-N gap could be a cold-start transient. Report whether the gap grows,
    shrinks, or is flat as N increases, and **flag this prominently**.

## Verification checklist (from the plan)

1. `python3 model.py` on the small config runs and prints SAT witness or UNSAT.
2. Printed L2 trajectory is bit-for-bit identical between the two designs at every
   timestep (confirms the shared-L2 spine).
3. On any SAT result, hand-replay the printed access sequence through the printed
   trajectories to confirm `C_NINE`, `C_victim`, `gap`, and that the derived
   integer hit-count difference equals `gap / ld`.
4. Run the N-increasing sweep and report the gap trend (growing / flat /
   transient).
