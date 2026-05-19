"""
Corpus of 17 cache replacement facts and relations.

Organized into thematic groups:
  A. Unconditional Facts (3)
  B. Cache Size & Associativity Properties (2)
  C. Policy Comparison (1)
  D. Associativity Effects (1)
  E. Temporal / Interval Relations (2)
  F. Hit Rate Decomposition (5)
  G. Coherence Effects (3)
"""

from core import (
    entity, metric, lit, eps, constraint, conj, relation,
    add, sub, mul, div,
    MetricKind as M, CmpOp,
)


# =============================================================================
# REUSABLE ENTITIES
# =============================================================================

# Policies (kind="policy") — first-class entities bound to caches via `bindings`
p_opt = entity("OPT", kind="policy")

# Block size constant (bytes) used across many relations
BLOCK_SIZE = lit(64)


# =============================================================================
# GROUP A: UNCONDITIONAL FACTS (3)
# =============================================================================

# --- F1: MissRate + HitRate == 1.0 ---
#
#   MissRate[C] + HitRate[C] == 1.0
#
# Definitional identity. Holds for any cache, any policy, any workload.

C_f1 = entity("C", kind="cache")

F1_miss_rate_hit_rate_complement = relation(
    name="miss_rate_hit_rate_complement",
    premises=[],
    consequent=constraint(
        add(metric(M.MISS_RATE, C_f1), metric(M.HIT_RATE, C_f1)),
        CmpOp.EQ,
        lit(1.0)
    ),
    entities=[C_f1],
    source="definition",
    domain="any cache, any policy, any workload",
)


# --- F2: Hit rate is bounded in [0, 1] ---
#
#   0 <= HitRate[C] <= 1
#
# A rate by definition. This seems trivial but matters for Z3:
# without this bound, the solver can propose negative hit rates
# or rates > 1 as "counterexamples." Grounding fact.

C_f2 = entity("C", kind="cache")

F2_hit_rate_bounded = relation(
    name="hit_rate_bounded",
    premises=[],
    consequent=constraint(
        metric(M.HIT_RATE, C_f2),
        CmpOp.LE,
        lit(1.0)
    ),
    entities=[C_f2],
    source="definition (rate)",
    domain="any cache, any policy, any workload",
)


# --- F3: Compulsory misses are policy-independent ---
#
#   CompulsoryMisses[C_a] == CompulsoryMisses[C_b]
#
# Given same workload and same cache size, compulsory misses depend only
# on the trace's unique blocks, not on how eviction decisions are made.

C_a_f3 = entity("C_a", kind="cache")
C_b_f3 = entity("C_b", kind="cache")
p_any_a = entity("P_a", kind="policy")
p_any_b = entity("P_b", kind="policy")

F3_compulsory_misses_policy_independent = relation(
    name="compulsory_misses_policy_independent",
    premises=[
        conj(
            constraint(metric(M.SIZE, C_a_f3), CmpOp.EQ, metric(M.SIZE, C_b_f3)),
            constraint(metric(M.ASSOCIATIVITY, C_a_f3), CmpOp.EQ,
                       metric(M.ASSOCIATIVITY, C_b_f3)),
        )
    ],
    consequent=constraint(
        metric(M.COMPULSORY_MISSES, C_a_f3),
        CmpOp.EQ,
        metric(M.COMPULSORY_MISSES, C_b_f3)
    ),
    entities=[C_a_f3, C_b_f3, p_any_a, p_any_b],
    bindings=[(C_a_f3, p_any_a), (C_b_f3, p_any_b)],
    source="3C miss model (Hill & Smith 1989)",
    domain="same workload, same cache geometry, any two policies",
)


# =============================================================================
# GROUP B: CACHE SIZE & ASSOCIATIVITY PROPERTIES (2)
# =============================================================================

# --- R1: Larger cache implies higher hit rate ---
#
#   Size[LLC_a] >= Size[LLC_b] => HitRate[LLC_a] >= HitRate[LLC_b] + ε_1

llc_a = entity("LLC_a", kind="cache")
llc_b = entity("LLC_b", kind="cache")
p_r1 = entity("P", kind="policy")
e1 = eps("1")

R1_larger_cache_higher_hr = relation(
    name="larger_cache_implies_higher_hit_rate",
    premises=[
        constraint(metric(M.SIZE, llc_a), CmpOp.GE, metric(M.SIZE, llc_b))
    ],
    consequent=constraint(
        metric(M.HIT_RATE, llc_a), CmpOp.GE, add(metric(M.HIT_RATE, llc_b), e1)
    ),
    entities=[llc_a, llc_b, p_r1],
    bindings=[(llc_a, p_r1), (llc_b, p_r1)],
    free_epsilons=[e1],
    source="inclusion property (exact for stack algorithms)",
    domain="any policy; same workload and assoc",
)


# --- R2: Diminishing returns of associativity ---
#
#   Assoc[a] = A/2 ∧ Assoc[b] = A ∧ Assoc[c] = 2A ∧ Size equal
#     => (HR[c] - HR[b]) <= (HR[b] - HR[a]) + ε_2

llc_a_r2 = entity("LLC_a", kind="cache")
llc_b_r2 = entity("LLC_b", kind="cache")
llc_c_r2 = entity("LLC_c", kind="cache")
p_r2 = entity("P", kind="policy")
e2 = eps("2")

R2_diminishing_assoc_returns = relation(
    name="diminishing_returns_of_associativity",
    premises=[
        conj(
            constraint(metric(M.SIZE, llc_a_r2), CmpOp.EQ, metric(M.SIZE, llc_b_r2)),
            constraint(metric(M.SIZE, llc_b_r2), CmpOp.EQ, metric(M.SIZE, llc_c_r2)),
            constraint(
                mul(lit(2), metric(M.ASSOCIATIVITY, llc_a_r2)),
                CmpOp.EQ,
                metric(M.ASSOCIATIVITY, llc_b_r2)
            ),
            constraint(
                mul(lit(2), metric(M.ASSOCIATIVITY, llc_b_r2)),
                CmpOp.EQ,
                metric(M.ASSOCIATIVITY, llc_c_r2)
            ),
        )
    ],
    consequent=constraint(
        sub(metric(M.HIT_RATE, llc_c_r2), metric(M.HIT_RATE, llc_b_r2)),
        CmpOp.LE,
        add(sub(metric(M.HIT_RATE, llc_b_r2), metric(M.HIT_RATE, llc_a_r2)), e2)
    ),
    entities=[llc_a_r2, llc_b_r2, llc_c_r2, p_r2],
    bindings=[(llc_a_r2, p_r2), (llc_b_r2, p_r2), (llc_c_r2, p_r2)],
    free_epsilons=[e2],
    source="diminishing returns / concavity of miss-rate curve",
    domain="any policy, fixed size, single workload",
)


# =============================================================================
# GROUP C: POLICY COMPARISON (1)
# =============================================================================

# --- R3: OPT upper-bounds all policies ---
#
#   HitRate[C_opt] >= HitRate[C_any]
#
# Belady's OPT achieves the highest possible hit rate for any
# demand-fetch replacement policy on the same trace.

C_opt = entity("C_opt", kind="cache")
C_any = entity("C_any", kind="cache")
p_any_r3 = entity("P_any", kind="policy")
e3 = eps("3")

R3_opt_upper_bounds_all_policies = relation(
    name="opt_upper_bounds_all_policies",
    premises=[
        conj(
            constraint(metric(M.SIZE, C_opt), CmpOp.EQ, metric(M.SIZE, C_any)),
            constraint(metric(M.ASSOCIATIVITY, C_opt), CmpOp.EQ,
                       metric(M.ASSOCIATIVITY, C_any)),
        )
    ],
    consequent=constraint(
        metric(M.HIT_RATE, C_opt),
        CmpOp.GE,
        sub(metric(M.HIT_RATE, C_any), e3)
    ),
    entities=[C_opt, C_any, p_opt, p_any_r3],
    bindings=[(C_opt, p_opt), (C_any, p_any_r3)],
    free_epsilons=[e3],
    source="Belady 1966",
    domain="same cache geometry, same workload, demand-fetch only",
)


# =============================================================================
# GROUP D: ASSOCIATIVITY EFFECTS (1)
# =============================================================================

# --- R4: Conflict misses decrease with associativity ---
#
#   Assoc[C_high] >= Assoc[C_low] ∧ Size equal
#     => ConflictMisses[C_high] <= ConflictMisses[C_low] + ε

C_hi_r4 = entity("C_hiassoc", kind="cache")
C_lo_r4 = entity("C_loassoc", kind="cache")
e4 = eps("4")

R4_conflict_misses_decrease_with_associativity = relation(
    name="conflict_misses_decrease_with_associativity",
    premises=[
        conj(
            constraint(metric(M.ASSOCIATIVITY, C_hi_r4), CmpOp.GE,
                       metric(M.ASSOCIATIVITY, C_lo_r4)),
            constraint(metric(M.SIZE, C_hi_r4), CmpOp.EQ, metric(M.SIZE, C_lo_r4)),
        )
    ],
    consequent=constraint(
        metric(M.CONFLICT_MISSES, C_hi_r4),
        CmpOp.LE,
        add(metric(M.CONFLICT_MISSES, C_lo_r4), e4)
    ),
    entities=[C_hi_r4, C_lo_r4],
    free_epsilons=[e4],
    source="Hill & Smith 1989 (3C model)",
    domain="any policy (same for both), same workload",
)


# =============================================================================
# GROUP E: TEMPORAL / INTERVAL RELATIONS (2)
# =============================================================================

# --- R5: Higher hit rate implies fewer stalls ---
#
#   HitRate[t_a] >= HitRate[t_b] => Stalls[t_a] <= Stalls[t_b] + ε_5

t_a = entity("t_a", kind="interval")
t_b = entity("t_b", kind="interval")
e5 = eps("5")

R5_hit_rate_implies_fewer_stalls = relation(
    name="hit_rate_implies_fewer_stalls",
    premises=[
        constraint(metric(M.HIT_RATE, t_a), CmpOp.GE, metric(M.HIT_RATE, t_b))
    ],
    consequent=constraint(
        metric(M.STALLS, t_a), CmpOp.LE, add(metric(M.STALLS, t_b), e5)
    ),
    entities=[t_a, t_b],
    free_epsilons=[e5],
    source="folklore / intuition",
    domain="single-core, same cache geometry, intervals large enough for metric stability",
)


# --- R6: Critical hit rate is a tighter predictor ---
#
#   CriticalHitRate[t_a] >= CHR[t_b] => Stalls[t_a] <= Stalls[t_b] + ε_6

e6 = eps("6")

R6_critical_hit_rate_implies_fewer_stalls = relation(
    name="critical_hit_rate_implies_fewer_stalls",
    premises=[
        constraint(metric(M.CRITICAL_HIT_RATE, t_a), CmpOp.GE,
                   metric(M.CRITICAL_HIT_RATE, t_b))
    ],
    consequent=constraint(
        metric(M.STALLS, t_a), CmpOp.LE, add(metric(M.STALLS, t_b), e6)
    ),
    entities=[t_a, t_b],
    free_epsilons=[e6],
    source="folklore / Qureshi ISCA'06 insight",
    domain="single-core, same cache geometry, intervals large enough for metric stability",
)


# =============================================================================
# GROUP F: HIT RATE DECOMPOSITION (5)
# =============================================================================

# --- R7: Overall hit rate is a weighted mix of load and store hit rates ---
#
#   HitRate[C] is between LoadHitRate[C] and StoreHitRate[C]

C_r7 = entity("C", kind="cache")

R7_hit_rate_between_load_store = relation(
    name="hit_rate_between_load_and_store",
    premises=[
        constraint(metric(M.LOAD_HIT_RATE, C_r7), CmpOp.GE,
                   metric(M.STORE_HIT_RATE, C_r7))
    ],
    consequent=constraint(
        metric(M.HIT_RATE, C_r7),
        CmpOp.GE,
        metric(M.STORE_HIT_RATE, C_r7)
    ),
    entities=[C_r7],
    source="weighted average property",
    domain="any policy, any workload with both loads and stores",
)

# --- R9: Prefetch coverage + demand miss rate relationship ---
#
#   Higher prefetch coverage => lower demand miss rate

C_a_r9 = entity("C_a", kind="cache")
C_b_r9 = entity("C_b", kind="cache")
e9 = eps("9")

R9_prefetch_coverage_reduces_demand_misses = relation(
    name="prefetch_coverage_reduces_demand_misses",
    premises=[
        conj(
            constraint(metric(M.PREFETCH_COVERAGE, C_a_r9), CmpOp.GE,
                       metric(M.PREFETCH_COVERAGE, C_b_r9)),
            constraint(metric(M.SIZE, C_a_r9), CmpOp.EQ, metric(M.SIZE, C_b_r9)),
            constraint(metric(M.ASSOCIATIVITY, C_a_r9), CmpOp.EQ,
                       metric(M.ASSOCIATIVITY, C_b_r9)),
        )
    ],
    consequent=constraint(
        metric(M.DEMAND_HIT_RATE, C_a_r9),
        CmpOp.GE,
        sub(metric(M.DEMAND_HIT_RATE, C_b_r9), e9)
    ),
    entities=[C_a_r9, C_b_r9],
    free_epsilons=[e9],
    source="definition of coverage: prefetches that prevent demand misses",
    domain="same geometry, same workload, same replacement policy",
)

# --- R10: Low prefetch accuracy => effective capacity reduction ---
#
#   PrefetchAccuracy[C] low => MissRate[C] increases relative to no-prefetch

C_r10 = entity("C_prefetch", kind="cache")
C_nopf = entity("C_noprefetch", kind="cache")
e10 = eps("10")

R10_low_prefetch_accuracy_hurts = relation(
    name="low_prefetch_accuracy_increases_misses",
    premises=[
        conj(
            constraint(metric(M.PREFETCH_ACCURACY, C_r10), CmpOp.LE, lit(0.25)),
            constraint(metric(M.SIZE, C_r10), CmpOp.EQ, metric(M.SIZE, C_nopf)),
            constraint(metric(M.ASSOCIATIVITY, C_r10), CmpOp.EQ,
                       metric(M.ASSOCIATIVITY, C_nopf)),
        )
    ],
    consequent=constraint(
        metric(M.DEMAND_HIT_RATE, C_r10),
        CmpOp.LE,
        add(metric(M.DEMAND_HIT_RATE, C_nopf), e10)
    ),
    entities=[C_r10, C_nopf],
    free_epsilons=[e10],
    source="cache pollution from inaccurate prefetches",
    domain="same geometry, same workload, same replacement policy",
)

# --- R11: Stores hit less than loads ---
#
#   StoreHitRate[C] <= LoadHitRate[C] + ε

C_r11 = entity("C", kind="cache")
e11 = eps("11")

R11_stores_hit_less_than_loads = relation(
    name="stores_hit_less_than_loads",
    premises=[],
    consequent=constraint(
        metric(M.STORE_HIT_RATE, C_r11),
        CmpOp.LE,
        add(metric(M.LOAD_HIT_RATE, C_r11), e11)
    ),
    entities=[C_r11],
    free_epsilons=[e11],
    source="first-write misses on newly allocated data",
    domain="write-allocate cache, any policy",
)


# =============================================================================
# GROUP G: COHERENCE EFFECTS (3)
# =============================================================================


# --- R12: Coherence misses add to total misses ---
#
#   MissCount[C] >= CapacityMisses[C] + ConflictMisses[C] + CompulsoryMisses[C]
#                   + CoherenceMisses[C] - ε

C_r12 = entity("C", kind="cache")
e12 = eps("12")

R12_4c_decomposition = relation(
    name="four_c_miss_decomposition",
    premises=[],
    consequent=constraint(
        metric(M.MISS_COUNT, C_r12),
        CmpOp.GE,
        sub(
            add(
                add(metric(M.CAPACITY_MISSES, C_r12), metric(M.CONFLICT_MISSES, C_r12)),
                add(metric(M.COMPULSORY_MISSES, C_r12), metric(M.COHERENCE_MISSES, C_r12))
            ),
            e12
        )
    ),
    entities=[C_r12],
    free_epsilons=[e12],
    source="4C miss model extension",
    domain="multicore with coherence protocol",
)

# --- R13: More sharing => more coherence misses ---

C_a_r13 = entity("C_a", kind="cache")
C_b_r13 = entity("C_b", kind="cache")
W_a_r13 = entity("W_a", kind="workload")
W_b_r13 = entity("W_b", kind="workload")
e13 = eps("13")

R13_more_sharing_more_coherence_misses = relation(
    name="more_sharing_implies_more_coherence_misses",
    premises=[
        conj(
            constraint(metric(M.INVALIDATIONS, C_a_r13), CmpOp.GE,
                       metric(M.INVALIDATIONS, C_b_r13)),
            constraint(metric(M.SIZE, C_a_r13), CmpOp.EQ, metric(M.SIZE, C_b_r13)),
            constraint(metric(M.ASSOCIATIVITY, C_a_r13), CmpOp.EQ,
                       metric(M.ASSOCIATIVITY, C_b_r13)),
        )
    ],
    consequent=constraint(
        metric(M.COHERENCE_MISSES, C_a_r13),
        CmpOp.GE,
        sub(metric(M.COHERENCE_MISSES, C_b_r13), e13)
    ),
    entities=[C_a_r13, C_b_r13, W_a_r13, W_b_r13],
    free_epsilons=[e13],
    source="invalidations are the mechanism of coherence misses",
    domain="same geometry, multicore",
)

# --- R14: Writebacks increase with eviction of dirty lines ---
#
#   Higher store hit rate (more modified lines) => more writebacks on eviction

C_a_r14 = entity("C_a", kind="cache")
C_b_r14 = entity("C_b", kind="cache")
e14 = eps("14")

R14_more_dirty_lines_more_writebacks = relation(
    name="more_dirty_lines_more_writebacks",
    premises=[
        conj(
            constraint(metric(M.STORE_HIT_RATE, C_a_r14), CmpOp.GE,
                       metric(M.STORE_HIT_RATE, C_b_r14)),
            constraint(metric(M.SIZE, C_a_r14), CmpOp.EQ, metric(M.SIZE, C_b_r14)),
            constraint(metric(M.EVICTIONS, C_a_r14), CmpOp.GE,
                       metric(M.EVICTIONS, C_b_r14)),
        )
    ],
    consequent=constraint(
        metric(M.WRITEBACKS, C_a_r14),
        CmpOp.GE,
        sub(metric(M.WRITEBACKS, C_b_r14), e14)
    ),
    entities=[C_a_r14, C_b_r14],
    free_epsilons=[e14],
    source="dirty evictions require writeback",
    domain="write-back cache, same geometry",
)


# =============================================================================
# CORPUS COLLECTION
# =============================================================================

ALL_RELATIONS = [
    # Group A: Unconditional Facts
    F1_miss_rate_hit_rate_complement,
    F2_hit_rate_bounded,
    F3_compulsory_misses_policy_independent,
    # Group B: Cache Size & Associativity Properties
    R1_larger_cache_higher_hr,
    R2_diminishing_assoc_returns,
    # Group C: Policy Comparison
    R3_opt_upper_bounds_all_policies,
    # Group D: Associativity Effects
    R4_conflict_misses_decrease_with_associativity,
    # Group E: Temporal / Interval
    R5_hit_rate_implies_fewer_stalls,
    R6_critical_hit_rate_implies_fewer_stalls,
    # Group F: Hit Rate Decomposition
    R7_hit_rate_between_load_store,
    R9_prefetch_coverage_reduces_demand_misses,
    R10_low_prefetch_accuracy_hurts,
    R11_stores_hit_less_than_loads,
    # Group G: Coherence Effects
    R12_4c_decomposition,
    R13_more_sharing_more_coherence_misses,
    R14_more_dirty_lines_more_writebacks,
]

assert len(ALL_RELATIONS) == 16, f"Expected 16, got {len(ALL_RELATIONS)}"


if __name__ == "__main__":
    print(f"=== Cache Replacement Corpus: {len(ALL_RELATIONS)} relations ===\n")

    facts = [r for r in ALL_RELATIONS if not r.premises]
    conditionals = [r for r in ALL_RELATIONS if r.premises and not r.expected_violable]
    violable = [r for r in ALL_RELATIONS if r.expected_violable]

    print(f"  Unconditional facts: {len(facts)}")
    print(f"  Conditional relations: {len(conditionals)}")
    print(f"  Expected-violable: {len(violable)}")
    print()

    for i, r in enumerate(ALL_RELATIONS, 1):
        tag = ""
        if not r.premises:
            tag = " [FACT]"
        elif r.expected_violable:
            tag = " [VIOLABLE]"
        print(f"{i:2d}. {r}{tag}")
        if r.bindings:
            binds = ", ".join(f"{c} uses {p}" for c, p in r.bindings)
            print(f"      bindings: {binds}")
        print()
