from z3 import *
from model import Params, build_model


# Track each logical constraint GROUP under one named boolean so an UNSAT core
# names which groups jointly force the contradiction. The term-DAG encoding
# (model.py) bakes the whole transition system into the dram sums, so the only
# trackable groups are the two trace-constraint kinds and the negated hypothesis.
def build_tracked(params):
    bundle = build_model(params)

    s = Solver()
    s.set(unsat_core=True)

    def track(label, cons):
        s.assert_and_track(And(cons) if len(cons) > 1 else cons[0], label)

    access_sequence = bundle.access_sequence

    # Split the two constraint kinds constrain_trace emits so the core can tell
    # them apart: the physical [0,K) bound vs. the RGS symmetry reduction.
    track("trace_bound", [ULT(a, params.K) for a in access_sequence])

    frontier = access_sequence[0]
    rgs = [access_sequence[0] == 0]
    for a in access_sequence[1:]:
        rgs.append(ULE(a, frontier + 1))
        frontier = If(a == frontier + 1, frontier + 1, frontier)
    track("RGS_symmetry", rgs)

    track("negated_hypothesis", [bundle.dram_victim > bundle.dram_nine])

    return s


if __name__ == "__main__":
    params = Params(w2=2, w3=4, N=7, K=7)
    print(f"params: {params}")

    s = build_tracked(params)
    result = s.check()
    print(f"result: {result}")

    if result == unsat:
        core = sorted(str(c) for c in s.unsat_core())
        print(f"\nunsat core: {len(core)} tracked groups needed for the contradiction")
        for name in core:
            print(f"  {name}")

        # Core membership is not a soundness test: assert_and_track returns a
        # jointly-unsat subset, not a minimal one, so a proof-participant like
        # RGS can appear even though it removes no counterexample. The real test
        # is that UNSAT survives with RGS dropped (verified separately).
        print(f"\ntrace_bound in core?        {'trace_bound' in core}")
        print(f"RGS_symmetry in core?       {'RGS_symmetry' in core}")
        print(f"negated_hypothesis in core? {'negated_hypothesis' in core}")
