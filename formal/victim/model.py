from dataclasses import dataclass
from z3 import *

@dataclass
class Params:
    w2: int   # L2 ways (shared by both designs)
    w3: int   # L3 ways (the SAME physical L3, run as NINE or as victim cache)

    N: int    # trace length (bounded horizon)
    K: int    # number of distinct line labels available to the trace


def fresh_cache(name, num_ways, timestep):
    return [Int(f"{name}_t{timestep}_slot{i}") for i in range(num_ways)]


# Cold start: every slot holds a distinct negative sentinel, so no slot can
# match a real line label (which are non-negative) until something is inserted.
def init_empty(cache_state):
    return [slot == -(i + 1) for i, slot in enumerate(cache_state)]


# Each access is a line label in [0, K), plus a canonical labeling (restricted-
# growth string: first access 0, each new label the smallest unused one) — an
# exact S_K symmetry reduction that fixes only the gauge, never the pattern.
def constrain_trace(access_sequence, K):
    constraints = [And(0 <= access, access < K) for access in access_sequence]

    frontier = access_sequence[0]
    constraints.append(access_sequence[0] == 0)
    for access in access_sequence[1:]:
        constraints.append(access <= frontier + 1)
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


# L2 transition, shared by both designs. L2 always installs the access and never
# reads a lower level, so one L2 trajectory (and its l2_hit) feeds NINE and
# victim alike. Constrains the caller-allocated l2_next list.
def step_l2(l2_now, access, l2_next):
    l2_hit = is_present(l2_now, access)
    l2_after = updated_cache(l2_now, access, access)
    return [l2_next[i] == l2_after[i] for i in range(len(l2_now))], l2_hit


def step_nine(l3_now, access, l2_hit, l3_next):
    l3_hit = is_present(l3_now, access)
    l3_after = updated_cache(l3_now, access, access)

    constraints = [l3_next[i] == If(l2_hit, l3_now[i], l3_after[i])
                   for i in range(len(l3_now))]

    return constraints, l3_hit


def step_victim(l2_now, victim_now, access, l2_hit, victim_next):
    victim_hit = is_present(victim_now, access)
    evicted_from_l2 = lru_line(l2_now)

    line_to_find = If(victim_hit, access, evicted_from_l2)
    victim_after = updated_cache(victim_now, line_to_find, evicted_from_l2)

    constraints = [victim_next[i] == If(l2_hit, victim_now[i], victim_after[i])
                   for i in range(len(victim_now))]

    return constraints, victim_hit


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


# Unroll both designs over one shared symbolic trace of length N. One L2
# trajectory feeds both step functions (shared-L2 spine); the NINE L3 and the
# victim cache evolve independently. Costs accumulate per timestep.
def build_model(params):
    access_sequence = [Int(f"access_t{t}") for t in range(params.N)]

    l2_traj = [fresh_cache("L2", params.w2, t) for t in range(params.N + 1)]
    l3_traj = [fresh_cache("L3", params.w3, t) for t in range(params.N + 1)]
    victim_traj = [fresh_cache("V", params.w3, t) for t in range(params.N + 1)]

    constraints = constrain_trace(access_sequence, params.K)
    constraints += init_empty(l2_traj[0])
    constraints += init_empty(l3_traj[0])
    constraints += init_empty(victim_traj[0])

    l2_hits, l3_hits, victim_hits = [], [], []

    # A DRAM lookup happens exactly when neither L2 nor the mid level holds the
    # access; these sums are the two designs' DRAM-lookup counts.
    dram_nine, dram_victim = 0, 0

    for t in range(params.N):
        access = access_sequence[t]

        l2_cons, l2_hit = step_l2(l2_traj[t], access, l2_traj[t + 1])
        nine_cons, l3_hit = step_nine(
            l3_traj[t], access, l2_hit, l3_traj[t + 1])
        victim_cons, victim_hit = step_victim(
            l2_traj[t], victim_traj[t], access, l2_hit, victim_traj[t + 1])

        constraints += l2_cons + nine_cons + victim_cons

        l2_hits.append(l2_hit)
        l3_hits.append(l3_hit)
        victim_hits.append(victim_hit)

        dram_nine += If(Not(Or(l2_hit, l3_hit)), 1, 0)
        dram_victim += If(Not(Or(l2_hit, victim_hit)), 1, 0)

    return Bundle(constraints, access_sequence, l2_traj, l3_traj, victim_traj,
                  dram_nine, dram_victim, l2_hits, l3_hits, victim_hits)


# Assert the negated hypothesis (Victim strictly costlier than NINE). First a
# plain SAT feasibility check (cheap); optimization is far more expensive, so
# only if a counterexample provably exists do we build an Optimize and maximize
# the gap. UNSAT on the feasibility check means H holds up to this (N, K).
def solve_for_counterexample(bundle):
    feasible = Solver()
    feasible.add(bundle.constraints)
    feasible.add(bundle.dram_victim > bundle.dram_nine)
    if feasible.check() == unsat:
        return feasible, unsat

    opt = Optimize()
    opt.add(bundle.constraints)
    opt.add(bundle.dram_victim > bundle.dram_nine)
    opt.maximize(bundle.dram_victim - bundle.dram_nine)
    return opt, opt.check()


def _row(state, model):
    return [model.eval(slot).as_long() for slot in state]


# Print the full witness (SAT) or the bounded result (UNSAT). On SAT the derived
# integer #(NINE-L3 hits on L2-miss steps) - #(victim hits on L2-miss steps) is
# computed from the model and cross-checked against gap.
def report_result(opt, result, bundle, params):
    print(f"params: {params}")

    if result == unsat:
        print("UNSAT: no trace makes Victim cost more than NINE.")
        print(f"Hypothesis C_victim <= C_NINE holds for ALL traces of length "
              f"N={params.N} over K={params.K} labels.")
        return

    model = opt.model()

    trace = [model.eval(a).as_long() for a in bundle.access_sequence]
    print(f"SAT: counterexample trace = {trace}")

    print("\nper-timestep trajectories (position 0 = MRU):")
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

    # Derived, hand-checkable quantity: on L2-miss steps only, how many more
    # times NINE's L3 hit than the victim cache did. gap (a raw DRAM-lookup
    # count difference) must equal this hit-count difference exactly.
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
    # K > w2 + w3 so neither L3 can cache the whole alphabet: NINE's L3 evicts
    # real lines (K > w3) and the victim's exclusive union w2+w3 is also under K,
    # so both designs face genuine capacity pressure.
    params = Params(w2=2, w3=3, N=10, K=6)
    bundle = build_model(params)
    opt, result = solve_for_counterexample(bundle)
    report_result(opt, result, bundle, params)
