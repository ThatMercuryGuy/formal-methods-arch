# TODO — build order

Read `CLAUDE.md` first for the full model description, settled design decisions,
and (strict) coding principles. Build **one function at a time**, in this order,
verifying each layer before moving on. Do not dump large chunks.

## DONE

- [x] **Primitives**: `is_present`, `lru_line`, `updated_cache`. Verified on
  concrete values (move-to-front, insert-evict, victim-swap all correct).
- [x] **State scaffolding**: `Params`, `fresh_cache`, `init_empty`,
  `constrain_trace`. Verified (sentinels distinct/negative, trace bound enforced).

## NEXT: Transitions (in progress — nothing committed yet)

Two functions. Each takes the current-state lists, the access, and the
caller-allocated next-state lists; emits equality constraints tying next-state to
`updated_cache(...)`; and returns the hit flags. Design: caller allocates the
next-state vars with `fresh_cache`, the step function constrains them.

- [ ] **`step_nine(l2_now, l3_now, access, l2_next, l3_next)`**
  - `l2_hit = is_present(l2_now, access)`; `l3_hit = is_present(l3_now, access)`.
  - L2 always updates: `l2_next == updated_cache(l2_now, access, access)`.
  - L3 updates only on an L2 miss (promote on hit / DRAM-fill on miss), else
    unchanged: `l3_next[i] == If(l2_hit, l3_now[i], updated_cache(l3_now, access, access)[i])`.
  - Return the constraint list plus `l2_hit`, `l3_hit`.
  - NOTE: an earlier draft of this exact function was written but the user halted
    before committing it — re-derive it, don't assume the draft was correct.

- [ ] **`step_victim(l2_now, victim_now, access, l2_next, victim_next)`**
  - `l2_hit = is_present(l2_now, access)`; `victim_hit = is_present(victim_now, access)`.
  - `evicted_from_l2 = lru_line(l2_now)`.
  - L2 update is IDENTICAL to `step_nine`'s (shared-L2 spine):
    `l2_next == updated_cache(l2_now, access, access)`.
  - Victim cache 3-way update:
    - L2 hit → victim unchanged (`victim_next == victim_now`).
    - L2 miss & victim hit → **swap**:
      `updated_cache(victim_now, line_to_find=access, line_to_insert=evicted_from_l2)`.
    - L2 miss & victim miss → **insert evictee**:
      `updated_cache(victim_now, line_to_find=evicted_from_l2, line_to_insert=evicted_from_l2)`.
    - Encode the 3-way choice with nested `If` on `l2_hit` / `victim_hit`.
  - Return the constraint list plus `l2_hit`, `victim_hit`.
  - VERIFY: on a shared L2 fed to both step functions, the L2 trajectory is
    identical (sanity-check the shared-L2 claim).

## THEN: Cost

- [ ] **`access_cost(l2_hit, mid_hit, params)`** → Z3 Int.
  `l2 + If(Not(l2_hit), l3 + If(Not(mid_hit), ld, 0), 0)`. One function for both
  designs (`mid_hit` = `l3_hit` for NINE, `victim_hit` for Victim). Verify all
  three tiers for both mid_hit values.

## THEN: Assembly

- [ ] **`build_model(params)`** — unroll `t = 0..N-1`:
  - Allocate ONE shared L2 trajectory (`l2[0..N]`), one NINE L3 trajectory, one
    victim trajectory, and the `access_sequence`.
  - `init_empty` on all caches at `t=0`; `constrain_trace(access_sequence, K)`.
  - Each timestep: call `step_nine` and `step_victim` (both reading the SAME
    `l2[t]`), accumulate `cost_nine` and `cost_victim` via `access_cost`.
  - Return a bundle: constraint list, `access_sequence`, all trajectories, both
    cost sums, per-step hit-flag vectors.
- [ ] **`gap_expression(bundle)`** → `cost_victim - cost_nine`.
- [ ] Smoke test: satisfiable for *some* trace with no objective yet.

## THEN: Search + report

- [ ] **`solve_for_counterexample(bundle, params)`** — `z3.Optimize`; add all
  constraints; assert negated hypothesis `cost_victim > cost_nine`;
  `maximize(gap_expression(bundle))`; `check()`.
- [ ] **`report_result(opt, bundle, params)`**:
  - SAT: print `access_sequence`, both full trajectories per timestep, `C_NINE`,
    `C_victim`, `gap`, and the derived integer
    `#(NINE-L3 hits among L2-miss steps) - #(victim hits among L2-miss steps)`.
    Confirm `gap == ld * that_integer`.
  - UNSAT: print the bounded result with all params (H holds up to this N, K —
    NOT a general proof).
- [ ] First real run on a small config, e.g. `Params(w2=2, w3=1, v=1, N=4, K=3,
  l2=1, l3=10, ld=100)`.

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
