"""
Encoding of the initial four cache replacement relations using the core AST.

This demonstrates how mathematical claims translate to the representation
and shows what each backend would do with them.
"""

from core import (
    entity, metric, lit, eps, constraint, conj, relation,
    add, sub, mul, div,
    MetricKind as M, CmpOp,
)


# --- Relation 1: Higher hit rate implies fewer stalls (within epsilon) ---
#
#   HitRate[t_a] >= HitRate[t_b] => Stalls[t_a] <= Stalls[t_b] + ε_1
#
# Experiment generation notes:
#   - t_a and t_b are two replacement policies applied to the same cache.
#   - We need at least two gem5 runs: same cache size/assoc/workload,
#     different replacement policies.
#   - Collect: overallHits/overallAccesses (hit rate), cpu.numCycles stall
#     breakdown.

t_a = entity("t_a", kind="policy")
t_b = entity("t_b", kind="policy")
e1 = eps("1")

hit_rate_implies_stalls = relation(
    name="hit_rate_implies_fewer_stalls",
    premises=[
        constraint(metric(M.HIT_RATE, t_a), CmpOp.GE, metric(M.HIT_RATE, t_b))
    ],
    consequent=constraint(
        metric(M.STALLS, t_a), CmpOp.LE, add(metric(M.STALLS, t_b), e1)
    ),
    entities=[t_a, t_b],
    free_epsilons=[e1],
    source="folklore / intuition",
    domain="single-core, same workload, same cache geometry",
)


# --- Relation 2: Critical hit rate is a tighter predictor of stalls ---
#
#   CriticalHitRate[t_a] >= CHR[t_b] => Stalls[t_a] <= Stalls[t_b] + ε_2
#
# This is the same shape as R1 but with critical hit rate. The hypothesis
# is that ε_2 < ε_1 (critical HR is more predictive). We can test this
# by running both relations against the same gem5 data and comparing slack.

e2 = eps("2")

critical_hit_rate_implies_stalls = relation(
    name="critical_hit_rate_implies_fewer_stalls",
    premises=[
        constraint(metric(M.CRITICAL_HIT_RATE, t_a), CmpOp.GE,
                   metric(M.CRITICAL_HIT_RATE, t_b))
    ],
    consequent=constraint(
        metric(M.STALLS, t_a), CmpOp.LE, add(metric(M.STALLS, t_b), e2)
    ),
    entities=[t_a, t_b],
    free_epsilons=[e2],
    source="folklore / Qureshi ISCA'06 insight",
    domain="single-core, same workload, same cache geometry",
)


# --- Relation 3: Larger cache implies higher hit rate ---
#
#   Size[LLC_a] >= Size[LLC_b] => HR[LLC_a] >= HR[LLC_b] + ε_3
#
# Experiment generation notes:
#   - LLC_a and LLC_b are caches with different sizes.
#   - Same replacement policy, same workload, same associativity.
#   - Sweep: size in {256KB, 512KB, 1MB, 2MB, 4MB, 8MB, ...}
#   - This is the inclusion property and holds exactly (ε_3 >= 0)
#     only for LRU. For other policies, the epsilon may be negative
#     (Belady anomaly). The domain restriction matters here.

llc_a = entity("LLC_a", kind="cache")
llc_b = entity("LLC_b", kind="cache")
e3 = eps("3")

larger_cache_higher_hr = relation(
    name="larger_cache_implies_higher_hit_rate",
    premises=[
        constraint(metric(M.SIZE, llc_a), CmpOp.GE, metric(M.SIZE, llc_b))
    ],
    consequent=constraint(
        metric(M.HIT_RATE, llc_a), CmpOp.GE, add(metric(M.HIT_RATE, llc_b), e3)
    ),
    entities=[llc_a, llc_b],
    free_epsilons=[e3],
    source="inclusion property (exact for LRU)",
    domain="LRU or stack-based policies; same workload and assoc",
)


# --- Relation 4: Diminishing returns of associativity ---
#
#   Assoc[LLC_a] = A/2 ∧ Assoc[LLC_b] = A ∧ Assoc[LLC_c] = 2A
#   ∧ Size[a] = Size[b] = Size[c]
#   => (HR[LLC_c] - HR[LLC_b]) <= (HR[LLC_b] - HR[LLC_a]) + ε_12
#
# This says going from A to 2A ways gives less benefit than going from
# A/2 to A ways. It's the "diminishing returns of associativity" principle.
#
# Experiment generation notes:
#   - Three cache configs, identical sizes, associativity in {A/2, A, 2A}.
#   - Need to sweep A itself: A in {4, 8, 16} gives triples (2,4,8), (4,8,16), (8,16,32).
#   - Same replacement policy, same workload for all three.
#   - Collect hit rates for each.

llc_c = entity("LLC_c", kind="cache")
e12 = eps("12")

# We use a symbolic literal for A — in practice, the experiment generator
# will expand this into concrete sweeps. For the AST we just express the
# structural constraint.
A = entity("A_param", kind="parameter")

diminishing_assoc_returns = relation(
    name="diminishing_returns_of_associativity",
    premises=[
        conj(
            constraint(metric(M.SIZE, llc_a), CmpOp.EQ, metric(M.SIZE, llc_b)),
            constraint(metric(M.SIZE, llc_b), CmpOp.EQ, metric(M.SIZE, llc_c)),
            # Associativity ordering is structural; expressed as constraints
            # The exact "A/2, A, 2A" is captured by the relation between the three
            constraint(
                mul(lit(2), metric(M.ASSOCIATIVITY, llc_a)),
                CmpOp.EQ,
                metric(M.ASSOCIATIVITY, llc_b)
            ),
            constraint(
                mul(lit(2), metric(M.ASSOCIATIVITY, llc_b)),
                CmpOp.EQ,
                metric(M.ASSOCIATIVITY, llc_c)
            ),
        )
    ],
    consequent=constraint(
        sub(metric(M.HIT_RATE, llc_c), metric(M.HIT_RATE, llc_b)),
        CmpOp.LE,
        add(sub(metric(M.HIT_RATE, llc_b), metric(M.HIT_RATE, llc_a)), e12)
    ),
    entities=[llc_a, llc_b, llc_c],
    free_epsilons=[e12],
    source="diminishing returns / concavity of miss-rate curve",
    domain="fixed size, LRU-family, single workload",
)


if __name__ == "__main__":
    print("=== Encoded Relations ===\n")
    for r in [hit_rate_implies_stalls, critical_hit_rate_implies_stalls,
              larger_cache_higher_hr, diminishing_assoc_returns]:
        print(r)
        print(f"  entities: {r.entities}")
        print(f"  epsilons: {r.free_epsilons}")
        print(f"  domain:   {r.domain}")
        print()
