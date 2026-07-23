from dataclasses import dataclass
from z3 import *

@dataclass
class Params:
    w2: int   # L2 ways (shared by both designs)
    w3: int   # L3 ways (the SAME physical L3, run as NINE or as victim cache)

    N: int    # trace length (bounded horizon)
    K: int    # number of distinct line labels available to the trace

    # Diagnostic override for NINE's L3 ways; None means w3_nine == w3.
    w3_nine: int = None


# Cold start: slot i holds the sentinel K + i, which no real line label
# (in [0, K)) can equal.
def init_empty(num_ways, K, width):
    return [BitVecVal(K + i, width) for i in range(num_ways)]


# Each access is a line label in [0, K), plus the canonical (restricted-growth)
# labeling — an exact S_K symmetry reduction.
def constrain_trace(access_sequence, K):
    constraints = [ULT(access, K) for access in access_sequence]

    frontier = access_sequence[0]
    constraints.append(access_sequence[0] == 0)
    for access in access_sequence[1:]:
        constraints.append(ULE(access, frontier + 1))
        frontier = If(access == frontier + 1, frontier + 1, frontier)

    return constraints


def is_present(cache_state, line_label):
    return Or([slot == line_label for slot in cache_state])


def lru_line(cache_state):
    return cache_state[-1]


# Strict-LRU update: remove line_to_find if present, and place line_to_insert
# at the MRU position. If line_to_find is absent, the LRU entry is evicted.
def updated_cache(cache_state, line_to_find, line_to_insert):
    new_state = [line_to_insert]
    found_above = BoolVal(False)
    for k in range(1, len(cache_state)):
        found_above = Or(found_above, cache_state[k - 1] == line_to_find)
        new_state.append(If(found_above, cache_state[k], cache_state[k - 1]))
    return new_state


# Transitions return the next state as If-terms over the current state; the
# access symbols are the formula's only free variables (see CLAUDE.md).

# L2 transition, shared by both designs (one L2 trajectory feeds both).
def step_l2(l2_now, access):
    l2_hit = is_present(l2_now, access)
    l2_next = updated_cache(l2_now, access, access)
    return l2_next, l2_hit


def step_nine(l3_now, access, l2_hit):
    l3_hit = is_present(l3_now, access)
    l3_after = updated_cache(l3_now, access, access)

    l3_next = [If(l2_hit, l3_now[i], l3_after[i]) for i in range(len(l3_now))]

    return l3_next, l3_hit


def step_victim(l2_now, victim_now, access, l2_hit):
    victim_hit = is_present(victim_now, access)
    evicted_from_l2 = lru_line(l2_now)

    line_to_find = If(victim_hit, access, evicted_from_l2)
    victim_after = updated_cache(victim_now, line_to_find, evicted_from_l2)

    victim_next = [If(l2_hit, victim_now[i], victim_after[i])
                   for i in range(len(victim_now))]

    return victim_next, victim_hit


# Everything build_model produces, gathered for the search and the report.
@dataclass
class Bundle:
    constraints: list

    access_sequence: list

    l2_traj: list       # shared L2 states, length N+1
    l3_traj: list       # NINE L3 states, length N+1
    victim_traj: list   # victim-cache states, length N+1

    dram_nine: object
    dram_victim: object

    l2_hits: list       # per-step l2_hit flags, length N
    l3_hits: list       # per-step NINE L3 hit flags, length N
    victim_hits: list   # per-step victim-cache hit flags, length N



def build_model(params):
    max_ways = max(params.w2, params.w3,
                   params.w3 if params.w3_nine is None else params.w3_nine)
    width = (params.K + max_ways - 1).bit_length()

    access_sequence = [BitVec(f"access_t{t}", width) for t in range(params.N)]

    w3_nine = params.w3 if params.w3_nine is None else params.w3_nine

    l2_traj = [init_empty(params.w2, params.K, width)]
    l3_traj = [init_empty(w3_nine, params.K, width)]
    victim_traj = [init_empty(params.w3, params.K, width)]

    constraints = constrain_trace(access_sequence, params.K)

    l2_hits, l3_hits, victim_hits = [], [], []

    # A DRAM lookup happens exactly when neither L2 nor the mid level holds
    # the access; these sums are the two designs' DRAM-lookup counts.
    dram_nine, dram_victim = 0, 0

    for t in range(params.N):
        access = access_sequence[t]

        l2_next, l2_hit = step_l2(l2_traj[t], access)
        l3_next, l3_hit = step_nine(l3_traj[t], access, l2_hit)
        victim_next, victim_hit = step_victim(
            l2_traj[t], victim_traj[t], access, l2_hit)

        l2_traj.append(l2_next)
        l3_traj.append(l3_next)
        victim_traj.append(victim_next)

        l2_hits.append(l2_hit)
        l3_hits.append(l3_hit)
        victim_hits.append(victim_hit)

        dram_nine += If(Not(Or(l2_hit, l3_hit)), 1, 0)
        dram_victim += If(Not(Or(l2_hit, victim_hit)), 1, 0)

    return Bundle(constraints, access_sequence, l2_traj, l3_traj, victim_traj,
                  dram_nine, dram_victim, l2_hits, l3_hits, victim_hits)


# Assert the negated hypothesis (Victim strictly costlier than NINE); on SAT,
# maximize the gap by binary search on one incremental solver.
def solve_for_counterexample(bundle, params):
    gap = bundle.dram_victim - bundle.dram_nine

    solver = Solver()
    solver.add(bundle.constraints)
    solver.add(gap >= 1)
    if solver.check() == unsat:
        return None, unsat

    best_model = solver.model()
    lo, hi = 1, params.N
    while lo < hi:
        mid = (lo + hi + 1) // 2
        solver.push()
        solver.add(gap >= mid)
        if solver.check() == sat:
            best_model = solver.model()
            lo = mid
        else:
            hi = mid - 1
        solver.pop()

    return best_model, sat


def _row(state, model):
    return [model.eval(slot).as_long() for slot in state]


# Print the full witness (SAT) or the bounded result (UNSAT). On SAT the
# derived hit-count difference is cross-checked against gap.
def report_result(model, result, bundle, params):
    print(f"params: {params}")

    if result == unsat:
        print("UNSAT: no trace makes Victim cost more than NINE.")
        print(f"Hypothesis C_victim <= C_NINE holds for ALL traces of length "
              f"N={params.N} over K={params.K} labels.")
        return

    trace = [model.eval(a).as_long() for a in bundle.access_sequence]
    print(f"SAT: counterexample trace = {trace}")

    print(f"\nper-timestep trajectories (position 0 = MRU; "
          f"values >= K={params.K} are empty-slot sentinels):")
    print(f"  t0 (init): L2={_row(bundle.l2_traj[0], model)} "
          f"L3={_row(bundle.l3_traj[0], model)} V={_row(bundle.victim_traj[0], model)}")
    for t in range(params.N):
        l2_hit = is_true(model.eval(bundle.l2_hits[t]))
        l3_hit = is_true(model.eval(bundle.l3_hits[t]))
        v_hit = is_true(model.eval(bundle.victim_hits[t]))
        print(f"  t{t + 1}: access={trace[t]} "
              f"L2={_row(bundle.l2_traj[t + 1], model)} "
              f"L3={_row(bundle.l3_traj[t + 1], model)} "
              f"V={_row(bundle.victim_traj[t + 1], model)}  "
              f"[l2_hit={l2_hit} l3_hit={l3_hit} v_hit={v_hit}]")

    dram_nine = model.eval(bundle.dram_nine).as_long()
    dram_victim = model.eval(bundle.dram_victim).as_long()
    gap = dram_victim - dram_nine

    nine_mid_hits = sum(1 for t in range(params.N)
                        if not is_true(model.eval(bundle.l2_hits[t]))
                        and is_true(model.eval(bundle.l3_hits[t])))
    victim_mid_hits = sum(1 for t in range(params.N)
                          if not is_true(model.eval(bundle.l2_hits[t]))
                          and is_true(model.eval(bundle.victim_hits[t])))
    hit_diff = nine_mid_hits - victim_mid_hits

    print(f"\nDRAM lookups NINE   = {dram_nine}")
    print(f"DRAM lookups victim = {dram_victim}")
    print(f"gap                 = {gap}")
    print(f"NINE-L3 hits on L2-miss steps   = {nine_mid_hits}")
    print(f"victim   hits on L2-miss steps  = {victim_mid_hits}")
    print(f"hit-count difference            = {hit_diff}")
    print(f"check gap == hit_diff           = {gap == hit_diff}")

if __name__ == "__main__":
    # Coverage-parity experiment: NINE's L3 gets w2 + w3 ways, matching the
    # victim design's exclusive union. K > w2 + w3 keeps capacity pressure on
    # both. Any remaining gap is attributable to recency dynamics alone.
    params = Params(w2=3, w3=6, N=12, K=10, w3_nine=9)
    bundle = build_model(params)
    model, result = solve_for_counterexample(bundle, params)
    report_result(model, result, bundle, params)
