from z3 import *
from model import (Params, fresh_cache, init_empty, constrain_trace,
                   step_l2, step_nine, step_victim)


# Rebuild the unrolling with each logical constraint GROUP tracked under one
# named boolean (not per-slot), so an UNSAT core names which groups jointly force
# the contradiction. Reuses model.py's exact transitions — only the unroll loop
# is restated here to attach trackers.
def build_tracked(params):
    s = Solver()
    s.set(unsat_core=True)

    access_sequence = [Int(f"access_t{t}") for t in range(params.N)]
    l2 = [fresh_cache("L2", params.w2, t) for t in range(params.N + 1)]
    l3 = [fresh_cache("L3", params.w3, t) for t in range(params.N + 1)]
    vic = [fresh_cache("V", params.w3, t) for t in range(params.N + 1)]

    def track(label, cons):
        s.assert_and_track(And(cons) if len(cons) > 1 else cons[0], label)

    # Split the two constraint kinds constrain_trace emits so the core can tell
    # them apart: the physical [0,K) bound vs. the RGS symmetry reduction.
    track("trace_bound", [And(0 <= a, a < params.K) for a in access_sequence])

    frontier = access_sequence[0]
    rgs = [access_sequence[0] == 0]
    for a in access_sequence[1:]:
        rgs.append(a <= frontier + 1)
        frontier = If(a == frontier + 1, frontier + 1, frontier)
    track("RGS_symmetry", rgs)
    track("init_L2", init_empty(l2[0]))
    track("init_L3", init_empty(l3[0]))
    track("init_V", init_empty(vic[0]))

    dram_nine, dram_victim = 0, 0
    for t in range(params.N):
        access = access_sequence[t]

        l2_cons, l2_hit = step_l2(l2[t], access, l2[t + 1])
        nine_cons, l3_hit = step_nine(l3[t], access, l2_hit, l3[t + 1])
        victim_cons, v_hit = step_victim(l2[t], vic[t], access, l2_hit, vic[t + 1])

        track(f"l2_step_t{t}", l2_cons)
        track(f"nine_step_t{t}", nine_cons)
        track(f"victim_step_t{t}", victim_cons)

        dram_nine += If(Not(Or(l2_hit, l3_hit)), 1, 0)
        dram_victim += If(Not(Or(l2_hit, v_hit)), 1, 0)

    track("negated_hypothesis", [dram_victim > dram_nine])
    return s


if __name__ == "__main__":
    params = Params(w2=2, w3=3, N=8, K=6)
    print(f"params: {params}")

    s = build_tracked(params)
    result = s.check()
    print(f"result: {result}")

    if result == unsat:
        core = sorted(str(c) for c in s.unsat_core())
        print(f"\nunsat core: {len(core)} tracked groups needed for the contradiction")
        for name in core:
            print(f"  {name}")

        # Which timesteps / designs are load-bearing, at a glance.
        steps = {"l2": set(), "nine": set(), "victim": set()}
        for name in core:
            for kind in steps:
                if name.startswith(f"{kind}_step_t"):
                    steps[kind].add(int(name.split("_t")[-1]))
        print("\nby design (timesteps present in core):")
        for kind, ts in steps.items():
            print(f"  {kind:7s}: {sorted(ts)}")
        # Core membership is not a soundness test: assert_and_track returns a
        # jointly-unsat subset, not a minimal one, so a proof-participant like
        # RGS can appear even though it removes no counterexample. The real test
        # is that UNSAT survives with RGS dropped (verified separately).
        print(f"\ntrace_bound in core? {'trace_bound' in core}")
        print(f"RGS_symmetry in core? {'RGS_symmetry' in core}")
