"""
Lightweight relation definitions for cache microarchitecture claims.

Each relation is a dict with:
  - 'name': identifier (e.g., 'R1')
  - 'claim': callable(data: dict) -> bool, checks if the relation holds
  - 'epsilon_name': which epsilon slack variable this uses (or None)
  - 'epsilon_unit': human-readable unit of the slack/epsilon (e.g. 'hit-rate frac',
        'stall-cyc/tot-cyc', 'misses', 'writebacks'); shown in the epsilon table
  - 'requires': list of stat keys / config keys the claim needs

Pairing metadata (drives how eval.py assembles the data dict):
  - 'kind': how entities are sourced and paired:
        'single'       — one dataset; claim uses bare metric keys (hit_rate, ...)
        'interval'     — pair ROI windows (i, j) within one stats file; keys _a/_b
        'cross_config' — pair LLC config dirs by size; keys _a/_b
        'opt'          — pair OPT trace window vs policy window; keys _opt/_policy
    Any other value (or a metric we have no data for) -> UNEVALUABLE, reported
    honestly rather than silently dropped.
  - 'base_metrics': metric names each entity needs (suffix-free). eval.py resolves
        each against an entity's stats and appends the kind's suffix to form the
        keys the claim/premise read.
  - 'premise': callable(data: dict) -> bool, filters which pairs the claim applies
        to (multi-entity only; None for single-entity facts).
"""

# =============================================================================
# SINGLE-ENTITY RELATIONS (Facts)
# =============================================================================

F1_miss_rate_hit_rate_complement = {
    'name': 'F1',
    'description': 'miss_rate + hit_rate = 1',
    'kind': 'single',
    'base_metrics': ['miss_rate', 'hit_rate'],
    'premise': None,
    'claim': lambda d: abs((d.get('miss_rate', 0) + d.get('hit_rate', 0)) - 1.0) < 0.001,
    'epsilon_name': None,
    'requires': ['miss_rate', 'hit_rate'],
}

F2_hit_rate_bounded = {
    'name': 'F2',
    'description': 'hit_rate <= 1',
    'kind': 'single',
    'base_metrics': ['hit_rate'],
    'premise': None,
    'claim': lambda d: d.get('hit_rate', 0) <= 1.001,
    'epsilon_name': None,
    'requires': ['hit_rate'],
}

# =============================================================================
# MULTI-ENTITY RELATIONS (ordered comparisons)
# =============================================================================
# These take data as: {'entity_a': {...}, 'entity_b': {...}}
# The claim callable receives this dict and extracts what it needs.

R1_larger_cache_higher_hr = {
    'name': 'R1',
    'description': 'size[a] >= size[b] => hit_rate[a] >= hit_rate[b] + epsilon',
    'kind': 'cross_config',
    'base_metrics': ['hit_rate'],  # size_a/size_b supplied by the cross-config driver
    'premises': ['size_a >= size_b'],
    'premise': lambda d: d.get('size_a', 0) >= d.get('size_b', 0),
    'claim': lambda d: d.get('hit_rate_a', 0) >= d.get('hit_rate_b', 0),
    # slack: tolerance needed for the relaxed claim hit_rate_a >= hit_rate_b - eps.
    'slack': lambda d: max(0.0, d.get('hit_rate_b', 0) - d.get('hit_rate_a', 0)),
    'epsilon_name': 'eps_1',
    'epsilon_unit': 'hit-rate frac',
    'requires': ['hit_rate_a', 'hit_rate_b', 'size_a', 'size_b'],
}

R2_diminishing_returns_associativity = {
    'name': 'R2',
    'description': 'diminishing returns of associativity (concave in assoc)',
    # 3-entity associativity sweep at fixed size: no assoc-variant runs exist in
    # the current data, so this stays honestly UNEVALUABLE.
    'kind': 'unsupported',
    'base_metrics': ['hit_rate', 'size', 'assoc'],
    'premises': [
        'size_a == size_b == size_c',
        '2 * assoc_a == assoc_b',
        '2 * assoc_b == assoc_c',
    ],
    'premise': lambda d: True,
    'claim': lambda d: (d.get('hit_rate_c', 0) - d.get('hit_rate_b', 0)) <= \
                       (d.get('hit_rate_b', 0) - d.get('hit_rate_a', 0)),
    'epsilon_name': 'eps_2',
    'epsilon_unit': 'hit-rate frac',
    'requires': ['hit_rate_a', 'hit_rate_b', 'hit_rate_c', 'size_a', 'size_b', 'size_c', 'assoc_a', 'assoc_b', 'assoc_c'],
}

R3_opt_upper_bounds = {
    'name': 'R3',
    'description': 'OPT policy upper-bounds all demand-fetch policies',
    # OPT hit rate comes from the Belady trace (output-opt/<bench>/trace_llc_results.txt),
    # the policy hit rate from the matching gem5 stats.txt, paired window-by-window.
    # Geometry (size/assoc) is assumed equal by construction, so the premise is trivially true.
    'kind': 'opt',
    'base_metrics': ['hit_rate'],
    'premises': [
        'size[opt] == size[any]',
        'assoc[opt] == assoc[any]',
    ],
    'premise': lambda d: True,
    'claim': lambda d: d.get('hit_rate_opt', 0) >= d.get('hit_rate_policy', 0),
    # slack: tolerance for hit_rate_opt >= hit_rate_policy - eps.
    'slack': lambda d: max(0.0, d.get('hit_rate_policy', 0) - d.get('hit_rate_opt', 0)),
    'epsilon_name': 'eps_3',
    'epsilon_unit': 'hit-rate frac',
    'requires': ['hit_rate_opt', 'hit_rate_policy'],
}

R4_conflict_misses_decrease_with_assoc = {
    'name': 'R4',
    'description': 'assoc[hi] >= assoc[lo], size[hi] == size[lo] => conflict_misses[hi] <= conflict_misses[lo]',
    # Needs a 4C miss decomposition (conflict misses) and assoc-variant runs at fixed
    # size — neither is present in the current data.
    'kind': 'unsupported',
    'base_metrics': ['conflict_misses', 'assoc', 'size'],
    'premises': ['assoc_hi >= assoc_lo', 'size_hi == size_lo'],
    'premise': lambda d: True,
    'claim': lambda d: d.get('conflict_misses_hi', 0) <= d.get('conflict_misses_lo', 0),
    'epsilon_name': 'eps_4',
    'epsilon_unit': 'misses',
    'requires': ['conflict_misses_hi', 'conflict_misses_lo', 'assoc_hi', 'assoc_lo', 'size_hi', 'size_lo'],
}

R5_higher_hit_rate_fewer_stalls = {
    'name': 'R5',
    'description': 'hit_rate[a] >= hit_rate[b] => stalls[a] <= stalls[b]',
    # Paired across ROI windows of one run (same geometry, varying behavior).
    'kind': 'interval',
    'base_metrics': ['hit_rate', 'stalls'],
    'premises': ['hit_rate_a >= hit_rate_b'],
    'premise': lambda d: d.get('hit_rate_a', 0) >= d.get('hit_rate_b', 0),
    'claim': lambda d: d.get('stalls_a', 0) <= d.get('stalls_b', 0),
    # slack: tolerance (as a backend-stall fraction, 0-1) for stalls_a <= stalls_b + eps.
    'slack': lambda d: max(0.0, d.get('stalls_a', 0) - d.get('stalls_b', 0)),
    'epsilon_name': 'eps_5',
    'epsilon_unit': 'stall-cyc/tot-cyc',
    'requires': ['hit_rate_a', 'hit_rate_b', 'stalls_a', 'stalls_b'],
}

R6_critical_hit_rate_tighter_than_overall = {
    'name': 'R6',
    'description': 'critical_hit_rate[a] >= critical_hit_rate[b] => stalls[a] <= stalls[b] (tighter than R5)',
    # Paired across ROI windows. critical_hit_rate comes from the LSQ
    # criticalMissRate stat (present in the current data), falling back to
    # overall hit_rate only when that stat is absent.
    'kind': 'interval',
    'base_metrics': ['critical_hit_rate', 'stalls'],
    'premises': ['critical_hit_rate_a >= critical_hit_rate_b'],
    'premise': lambda d: d.get('critical_hit_rate_a', 0) >= d.get('critical_hit_rate_b', 0),
    'claim': lambda d: d.get('stalls_a', 0) <= d.get('stalls_b', 0),
    # slack: tolerance (as a backend-stall fraction, 0-1) for stalls_a <= stalls_b + eps.
    'slack': lambda d: max(0.0, d.get('stalls_a', 0) - d.get('stalls_b', 0)),
    'epsilon_name': 'eps_6',
    'epsilon_unit': 'stall-cyc/tot-cyc',
    'requires': ['critical_hit_rate_a', 'critical_hit_rate_b', 'stalls_a', 'stalls_b'],
}

R7_hit_rate_between_load_and_store = {
    'name': 'R7',
    'description': 'hit_rate between load_hit_rate and store_hit_rate',
    'kind': 'single',
    'base_metrics': ['hit_rate', 'load_hit_rate', 'store_hit_rate'],
    'premises': ['load_hit_rate >= store_hit_rate'],
    'premise': lambda d: d.get('load_hit_rate', 0) >= d.get('store_hit_rate', 0),
    'claim': lambda d: d.get('store_hit_rate', 0) <= d.get('hit_rate', 0) <= d.get('load_hit_rate', 0),
    'epsilon_name': None,
    'requires': ['hit_rate', 'load_hit_rate', 'store_hit_rate'],
}

R9_prefetch_coverage_reduces_demand_misses = {
    'name': 'R9',
    'description': 'prefetch_coverage[a] >= prefetch_coverage[b] => demand_hit_rate[a] >= demand_hit_rate[b]',
    # Needs prefetch-on vs prefetch-off runs with prefetch coverage stats — not present.
    'kind': 'unsupported',
    'base_metrics': ['demand_hit_rate', 'prefetch_coverage'],
    'premises': ['prefetch_coverage_a >= prefetch_coverage_b', 'same_geometry'],
    'premise': lambda d: d.get('prefetch_coverage_a', 0) >= d.get('prefetch_coverage_b', 0),
    'claim': lambda d: d.get('demand_hit_rate_a', 0) >= d.get('demand_hit_rate_b', 0),
    'epsilon_name': 'eps_9',
    'epsilon_unit': 'hit-rate frac',
    'requires': ['demand_hit_rate_a', 'demand_hit_rate_b', 'prefetch_coverage_a', 'prefetch_coverage_b'],
}

R10_low_prefetch_accuracy_hurts = {
    'name': 'R10',
    'description': 'prefetch_accuracy <= 0.25 => demand_hit_rate[prefetch] <= demand_hit_rate[no_prefetch]',
    # Needs prefetch-on vs prefetch-off runs with prefetch accuracy stats — not present.
    'kind': 'unsupported',
    'base_metrics': ['demand_hit_rate', 'prefetch_accuracy'],
    'premises': ['prefetch_accuracy <= 0.25'],
    'premise': lambda d: d.get('prefetch_accuracy', 1.0) <= 0.25,
    'claim': lambda d: d.get('demand_hit_rate_prefetch', 0) <= d.get('demand_hit_rate_no_prefetch', 0),
    'epsilon_name': 'eps_10',
    'epsilon_unit': 'hit-rate frac',
    'requires': ['demand_hit_rate_prefetch', 'demand_hit_rate_no_prefetch', 'prefetch_accuracy'],
}

R11_stores_hit_less_than_loads = {
    'name': 'R11',
    'description': 'write-allocate: store_hit_rate <= load_hit_rate',
    'kind': 'single',
    'base_metrics': ['store_hit_rate', 'load_hit_rate'],
    'premises': [],
    'premise': None,
    'claim': lambda d: d.get('store_hit_rate', 0) <= d.get('load_hit_rate', 0),
    # slack: tolerance for store_hit_rate <= load_hit_rate + eps.
    'slack': lambda d: max(0.0, d.get('store_hit_rate', 0) - d.get('load_hit_rate', 0)),
    'epsilon_name': 'eps_11',
    'epsilon_unit': 'hit-rate frac',
    'requires': ['store_hit_rate', 'load_hit_rate'],
}

R12_four_c_miss_decomposition = {
    'name': 'R12',
    'description': 'total_misses >= compulsory + capacity + conflict + coherence',
    # Needs a 4C miss decomposition gem5 does not emit — not present.
    'kind': 'unsupported',
    'base_metrics': ['total_misses', 'compulsory_misses', 'capacity_misses', 'conflict_misses', 'coherence_misses'],
    'premises': [],
    'premise': None,
    'claim': lambda d: d.get('total_misses', 0) >= (
        d.get('compulsory_misses', 0) +
        d.get('capacity_misses', 0) +
        d.get('conflict_misses', 0) +
        d.get('coherence_misses', 0)
    ),
    'epsilon_name': 'eps_12',
    'epsilon_unit': 'misses',
    'requires': ['total_misses', 'compulsory_misses', 'capacity_misses', 'conflict_misses', 'coherence_misses'],
}

R13_more_invalidations_more_coherence_misses = {
    'name': 'R13',
    'description': 'invalidations[a] >= invalidations[b] => coherence_misses[a] >= coherence_misses[b]',
    # Single-core runs: no coherence misses / invalidations to compare — not present.
    'kind': 'unsupported',
    'base_metrics': ['coherence_misses', 'invalidations'],
    'premises': ['invalidations_a >= invalidations_b', 'same_geometry'],
    'premise': lambda d: d.get('invalidations_a', 0) >= d.get('invalidations_b', 0),
    'claim': lambda d: d.get('coherence_misses_a', 0) >= d.get('coherence_misses_b', 0),
    'epsilon_name': 'eps_13',
    'epsilon_unit': 'misses',
    'requires': ['coherence_misses_a', 'coherence_misses_b', 'invalidations_a', 'invalidations_b'],
}

R14_more_dirty_lines_more_writebacks = {
    'name': 'R14',
    'description': 'store_hit_rate[a] >= store_hit_rate[b] AND evictions[a] >= evictions[b] => writebacks[a] >= writebacks[b]',
    # Pairs ROI windows: writebacks/evictions/store_hit_rate are all per-window LLC stats.
    'kind': 'interval',
    'base_metrics': ['writebacks', 'store_hit_rate', 'evictions'],
    'premises': ['store_hit_rate_a >= store_hit_rate_b', 'evictions_a >= evictions_b', 'size_a == size_b'],
    'premise': lambda d: d.get('store_hit_rate_a', 0) >= d.get('store_hit_rate_b', 0) and \
                         d.get('evictions_a', 0) >= d.get('evictions_b', 0),
    'claim': lambda d: d.get('writebacks_a', 0) >= d.get('writebacks_b', 0),
    # slack: tolerance (in writebacks) for writebacks_a >= writebacks_b - eps.
    'slack': lambda d: max(0.0, d.get('writebacks_b', 0) - d.get('writebacks_a', 0)),
    'epsilon_name': 'eps_14',
    'epsilon_unit': 'writebacks',
    'requires': ['writebacks_a', 'writebacks_b', 'store_hit_rate_a', 'store_hit_rate_b', 'evictions_a', 'evictions_b'],
}

# =============================================================================
# COLLECTION
# =============================================================================

ALL_RELATIONS = [
    F1_miss_rate_hit_rate_complement,
    F2_hit_rate_bounded,
    R1_larger_cache_higher_hr,
    R2_diminishing_returns_associativity,
    R3_opt_upper_bounds,
    R4_conflict_misses_decrease_with_assoc,
    R5_higher_hit_rate_fewer_stalls,
    R6_critical_hit_rate_tighter_than_overall,
    R7_hit_rate_between_load_and_store,
    R9_prefetch_coverage_reduces_demand_misses,
    R10_low_prefetch_accuracy_hurts,
    R11_stores_hit_less_than_loads,
    R12_four_c_miss_decomposition,
    R13_more_invalidations_more_coherence_misses,
    R14_more_dirty_lines_more_writebacks,
]

assert len(ALL_RELATIONS) == 15, f"Expected 15 relations, got {len(ALL_RELATIONS)}"
