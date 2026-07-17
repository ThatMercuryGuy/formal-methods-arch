from dataclasses import dataclass

from z3 import *


@dataclass
class Params:
    w2: int   # L2 ways (shared by both designs)
    w3: int   # L3 ways (the SAME physical L3, run as NINE or as victim cache)

    N: int    # trace length (bounded horizon)
    K: int    # number of distinct line labels available to the trace

    l2: int   # L2 lookup latency
    l3: int   # L3 / victim-cache lookup latency (equal by design choice)
    ld: int   # DRAM latency


def fresh_cache(name, num_ways, timestep):
    return [Int(f"{name}_t{timestep}_slot{i}") for i in range(num_ways)]


# Cold start: every slot holds a distinct negative sentinel, so no slot can
# match a real line label (which are non-negative) until something is inserted.
def init_empty(cache_state):
    return [slot == -(i + 1) for i, slot in enumerate(cache_state)]


# The trace is the only free variable Z3 searches over: each access is an
# unconstrained line label in [0, K). No assumption about workload shape.
def constrain_trace(access_sequence, K):
    return [And(0 <= access, access < K) for access in access_sequence]


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


# NINE transition. L2 always installs the access. L3 is probed and updated only
# on an L2 miss (promote on L3 hit, DRAM-fill on L3 miss); on an L2 hit L3 is
# left unchanged. Constrains the caller-allocated l2_next / l3_next lists.
def step_nine(l2_now, l3_now, access, l2_next, l3_next):
    l2_hit = is_present(l2_now, access)
    l3_hit = is_present(l3_now, access)

    l2_after = updated_cache(l2_now, access, access)
    l3_after = updated_cache(l3_now, access, access)

    constraints = [l2_next[i] == l2_after[i] for i in range(len(l2_now))]
    constraints += [l3_next[i] == If(l2_hit, l3_now[i], l3_after[i])
                    for i in range(len(l3_now))]

    return constraints, l2_hit, l3_hit


# Victim transition. The L2 update is identical to step_nine (shared-L2 spine).
# The victim cache changes only on an L2 miss: on a victim hit the accessed line
# is swapped out of the victim cache and the line evicted from L2 takes its
# place; on a victim miss the line evicted from L2 is inserted. On an L2 hit the
# victim cache is unchanged.
def step_victim(l2_now, victim_now, access, l2_next, victim_next):
    l2_hit = is_present(l2_now, access)
    victim_hit = is_present(victim_now, access)
    evicted_from_l2 = lru_line(l2_now)

    l2_after = updated_cache(l2_now, access, access)

    swap = updated_cache(victim_now, access, evicted_from_l2)
    insert_evictee = updated_cache(victim_now, evicted_from_l2, evicted_from_l2)

    constraints = [l2_next[i] == l2_after[i] for i in range(len(l2_now))]
    constraints += [victim_next[i] == If(l2_hit, victim_now[i],
                                          If(victim_hit, swap[i], insert_evictee[i]))
                    for i in range(len(victim_now))]

    return constraints, l2_hit, victim_hit


# Cumulative lookup cost of one access: pay l2 always; on an L2 miss also pay l3
# for the mid-level probe; on a mid-level miss also pay ld (DRAM always
# resolves). mid_hit is l3_hit for NINE, victim_hit for Victim.
def access_cost(l2_hit, mid_hit, params):
    return params.l2 + If(Not(l2_hit),
                          params.l3 + If(Not(mid_hit), params.ld, 0),
                          0)


# Everything build_model produces, gathered for the search and the report.
@dataclass
class Bundle:
    constraints: list

    access_sequence: list

    l2_traj: list       # shared L2 states, length N+1
    l3_traj: list       # NINE L3 states, length N+1
    victim_traj: list   # victim-cache states, length N+1

    cost_nine: object
    cost_victim: object

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
    cost_nine, cost_victim = 0, 0

    for t in range(params.N):
        access = access_sequence[t]

        nine_cons, l2_hit, l3_hit = step_nine(
            l2_traj[t], l3_traj[t], access, l2_traj[t + 1], l3_traj[t + 1])
        victim_cons, _, victim_hit = step_victim(
            l2_traj[t], victim_traj[t], access, l2_traj[t + 1], victim_traj[t + 1])

        constraints += nine_cons + victim_cons

        l2_hits.append(l2_hit)
        l3_hits.append(l3_hit)
        victim_hits.append(victim_hit)

        cost_nine += access_cost(l2_hit, l3_hit, params)
        cost_victim += access_cost(l2_hit, victim_hit, params)

    return Bundle(constraints, access_sequence, l2_traj, l3_traj, victim_traj,
                  cost_nine, cost_victim, l2_hits, l3_hits, victim_hits)


# Assert the negated hypothesis (Victim strictly costlier than NINE). First a
# plain SAT feasibility check (cheap); optimization is far more expensive, so
# only if a counterexample provably exists do we build an Optimize and maximize
# the gap. UNSAT on the feasibility check means H holds up to this (N, K).
def solve_for_counterexample(bundle, params):
    feasible = Solver()
    feasible.add(bundle.constraints)
    feasible.add(bundle.cost_victim > bundle.cost_nine)
    if feasible.check() == unsat:
        return feasible, unsat

    opt = Optimize()
    opt.add(bundle.constraints)
    opt.add(bundle.cost_victim > bundle.cost_nine)
    opt.maximize(bundle.cost_victim - bundle.cost_nine)
    return opt, opt.check()


def _row(state, model):
    return [model.eval(slot).as_long() for slot in state]


# Print the full witness (SAT) or the bounded result (UNSAT). On SAT the derived
# integer #(NINE-L3 hits on L2-miss steps) - #(victim hits on L2-miss steps) is
# computed from the model and cross-checked against gap / ld.
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

    c_nine = model.eval(bundle.cost_nine).as_long()
    c_victim = model.eval(bundle.cost_victim).as_long()
    gap = c_victim - c_nine

    # Derived, hand-checkable quantity: on L2-miss steps only, how many more
    # times NINE's L3 hit than the victim cache did. gap must equal ld times it.
    nine_mid_hits = sum(1 for t in range(params.N)
                        if not is_true(model.eval(bundle.l2_hits[t]))
                        and is_true(model.eval(bundle.l3_hits[t])))
    victim_mid_hits = sum(1 for t in range(params.N)
                          if not is_true(model.eval(bundle.l2_hits[t]))
                          and is_true(model.eval(bundle.victim_hits[t])))
    hit_diff = nine_mid_hits - victim_mid_hits

    print(f"\nC_NINE   = {c_nine}")
    print(f"C_victim = {c_victim}")
    print(f"gap      = {gap}")
    print(f"NINE-L3 hits on L2-miss steps   = {nine_mid_hits}")
    print(f"victim   hits on L2-miss steps  = {victim_mid_hits}")
    print(f"hit-count difference            = {hit_diff}")
    print(f"check gap == ld * hit_diff      = {gap == params.ld * hit_diff} "
          f"({params.ld} * {hit_diff} = {params.ld * hit_diff})")


if __name__ == "__main__":
    params = Params(w2=4, w3=8, N=12, K=10, l2=10, l3=25, ld=250)
    bundle = build_model(params)
    opt, result = solve_for_counterexample(bundle, params)
    report_result(opt, result, bundle, params)
