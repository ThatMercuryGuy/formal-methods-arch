"""
Evaluation backend: check Relations against gem5 stats dumps and compute
empirical epsilon bounds.

================================================================================
OVERVIEW
================================================================================

Given a gem5 stats.txt file and one or more entity bindings, this module:

1. Parses the stats file into a flat {stat_name: float} dictionary.
2. Resolves MetricKind values from raw gem5 stats (including derived metrics
   like HIT_RATE = hits/accesses, STALLS = cycles - instructions).
3. Walks the Relation's expression AST, substituting concrete values.
4. Reports whether the relation holds, is violated, or can't be evaluated.

For relations with free epsilons, the key output is the **empirical epsilon** —
the minimum ε needed to make the relation true for this data.
  - Zero: relation already holds without any tolerance.
  - Positive: relation would not hold without this much epsilon.

Relations without free epsilons (exact/definitional facts like F1, F2) just
report True/False — there is no epsilon to compute.

================================================================================
EVALUATION MODES
================================================================================

Single-entity relations (F1, F2, R7, R11, R12):
    Require one stats file. Bind the relation's entity to a gem5 component
    prefix (e.g., "board.cache_hierarchy.llcache").

Multi-entity relations (R1–R6, R9, R10, R13, R14):
    Compare two or more cache configurations. Each entity must be bound to
    stats from a *separate* simulation run (same workload, different config).
    Cannot be evaluated from a single stats file.

================================================================================
METRIC RESOLUTION
================================================================================

Metrics are resolved via `resolve_metric()`. Each MetricKind maps to a formula
over raw gem5 stat keys:

    HIT_RATE        = overallHits::total / overallAccesses::total
                      (fallback: (accesses - misses) / accesses)
    MISS_RATE       = overallMisses::total / overallAccesses::total
    DEMAND_HIT_RATE = demandHits::total / demandAccesses::total
    MISS_COUNT      = overallMisses::total
    EVICTIONS       = replacements
    WRITEBACKS      = writebacks::total
    LOAD_HIT_RATE   = ReadReq.hits::total / ReadReq.accesses::total
    STORE_HIT_RATE  = WriteReq.hits::total / WriteReq.accesses::total
    STALLS          = numCycles - simInsts (approximate)
    CRITICAL_HIT_RATE = 1 - board.processor.switch.core.lsq0.criticalMissRate

Metrics not derivable from stats (SIZE, ASSOCIATIVITY, COMPULSORY_MISSES,
CONFLICT_MISSES, CAPACITY_MISSES, COHERENCE_MISSES,
PREFETCH_ACCURACY, PREFETCH_COVERAGE) must be supplied via the `config` dict
on EntityBinding, or the relation is marked unevaluable.

================================================================================
USAGE
================================================================================

Single-entity evaluation:

    from eval_backend import parse_stats_file, EntityBinding, evaluate_relation
    from corpus import F1_miss_rate_hit_rate_complement

    stats = parse_stats_file("path/to/stats.txt")
    binding = EntityBinding(
        entity_name="C",
        stats=stats,
        component_prefix="board.cache_hierarchy.llcache",
    )
    result = evaluate_relation(F1_miss_rate_hit_rate_complement, [binding])

Multi-entity evaluation (comparing two LLC configs):

    from corpus import R1_larger_cache_higher_hr
    from core import MetricKind

    stats_big = parse_stats_file("run_4MB/stats.txt")
    stats_small = parse_stats_file("run_2MB/stats.txt")

    bindings = [
        EntityBinding("LLC_a", stats_big, "board.cache_hierarchy.llcache",
                      config={MetricKind.SIZE: 4 * 1024 * 1024}),
        EntityBinding("LLC_b", stats_small, "board.cache_hierarchy.llcache",
                      config={MetricKind.SIZE: 2 * 1024 * 1024}),
        EntityBinding("P", stats_big, "board.cache_hierarchy.llcache"),
    ]
    result = evaluate_relation(R1_larger_cache_higher_hr, bindings)
    # result.epsilon["1"] gives the minimum epsilon needed (0 if relation holds)

Running as a script:

    python3 eval_backend.py

    Evaluates all corpus relations against the multi-ROI gem5 stats dumps in
    ../../gem5-configs/output/ (single-entity, interval, cross-config, and R11
    sections) and writes results to eval_results.txt.

================================================================================
OUTPUT FORMAT
================================================================================

EvalResult fields:
    relation_name    — which relation was evaluated
    premises_hold    — True/False/None (None = couldn't check)
    consequent_holds — True/False/None
    epsilon          — {epsilon_name: float} for relations with free epsilons.
                       0 = holds as-is, positive = minimum epsilon to make it true.
                       None for exact relations (no epsilon to solve for).
    missing_metrics  — list of what couldn't be resolved

EvalResult statuses:
    HOLDS                      — consequent satisfied (epsilon = 0)
    VIOLATED                   — consequent needs nonzero epsilon to hold
    VACUOUS (premises not met) — premises false, relation doesn't apply to this data
    UNEVALUABLE                — missing metrics or entity bindings
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional
from pathlib import Path

from core import (
    Expr, Literal, MetricRef, BinOp, UnaryOp, Epsilon,
    Op, CmpOp, Constraint, Conjunction, Disjunction,
    Premise, Relation, MetricKind, Entity,
)


# =============================================================================
# STATS PARSER
# =============================================================================

def parse_stats_file(path: str | Path) -> dict[str, float]:
    """Parse a gem5 stats.txt into {stat_name: value}."""
    stats: dict[str, float] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("---") or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            key = parts[0]
            val_str = parts[1]
            try:
                val = float(val_str)
            except ValueError:
                continue
            if val != val and val_str.lower() == "nan":
                continue
            if val_str.lower() in ("inf", "-inf"):
                continue
            stats[key] = val
    return stats


def _parse_stat_line(line: str) -> tuple[str, float] | None:
    """Parse a single stats line into (key, value) or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    val_str = parts[1]
    try:
        val = float(val_str)
    except ValueError:
        return None
    if val != val and val_str.lower() == "nan":
        return None
    if val_str.lower() in ("inf", "-inf"):
        return None
    return (parts[0], val)


def parse_stats_file_windows(path: str | Path) -> list[dict[str, float]]:
    """Parse a gem5 stats.txt with multiple ROI dumps into per-window dicts.

    Each window is delimited by:
        '---------- Begin Simulation Statistics ----------'
        '---------- End Simulation Statistics   ----------'

    Returns a list where windows[i] is the stats dict for the i-th ROI dump.
    If the file has no delimiters, returns a one-element list with all stats.
    """
    BEGIN = "---------- Begin Simulation Statistics ----------"
    END = "---------- End Simulation Statistics   ----------"

    windows: list[dict[str, float]] = []
    current: dict[str, float] = {}
    inside = False

    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped == BEGIN:
                inside = True
                current = {}
                continue
            if stripped == END:
                if current:
                    windows.append(current)
                inside = False
                continue
            if inside:
                parsed = _parse_stat_line(stripped)
                if parsed:
                    current[parsed[0]] = parsed[1]

    if not windows:
        return [parse_stats_file(path)]
    return windows


def parse_opt_trace_results(path: str | Path) -> list[dict[str, float]]:
    """Parse a Belady-OPT trace_llc_results.txt into per-ROI hit-rate dicts.

    The file is a whitespace-delimited table with a header row:
        roi  hits  misses  hit_rate  warmup  roi_accesses

    Returns a list where result[i] holds the OPT stats for the i-th ROI window:
        {"hits": ..., "misses": ..., "hit_rate": ..., "accesses": ...}

    The i-th entry lines up with the i-th ROI window in the matching gem5
    stats.txt, so OPT hit rate can be compared against the actual policy's
    hit rate window-by-window.
    """
    rows: list[dict[str, float]] = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 6:
                continue
            # skip the header row (non-numeric first column)
            try:
                roi = int(parts[0])
            except ValueError:
                continue
            try:
                hits = float(parts[1])
                misses = float(parts[2])
                hit_rate = float(parts[3])
                accesses = float(parts[5])
            except ValueError:
                continue
            rows.append({
                "roi": roi,
                "hits": hits,
                "misses": misses,
                "hit_rate": hit_rate,
                "accesses": accesses,
            })
    return rows


def opt_window_stats(opt_row: dict[str, float], prefix: str) -> dict[str, float]:
    """Build a synthetic gem5-style stats dict for one OPT ROI window.

    Lets the standard resolve_metric(HIT_RATE) path work on OPT data by
    exposing the trace's hits/accesses under the LLC overall* stat keys.
    """
    return {
        f"{prefix}.overallHits::total": opt_row["hits"],
        f"{prefix}.overallMisses::total": opt_row["misses"],
        f"{prefix}.overallAccesses::total": opt_row["accesses"],
    }


# =============================================================================
# METRIC RESOLUTION
# =============================================================================

def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _get(stats: dict[str, float], prefix: str, suffix: str) -> float | None:
    key = f"{prefix}.{suffix}" if prefix else suffix
    return stats.get(key)


def resolve_metric(
    kind: MetricKind,
    stats: dict[str, float],
    prefix: str,
    config: dict[MetricKind, float] | None = None,
) -> float | None:
    """Resolve a MetricKind to a float from gem5 stats.

    Args:
        kind: which metric to resolve
        stats: parsed gem5 stats dict
        prefix: component path prefix (e.g. "board.cache_hierarchy.llcache")
        config: manually supplied parameters (SIZE, ASSOCIATIVITY, etc.)
    """
    if config and kind in config:
        return config[kind]

    if kind == MetricKind.HIT_RATE:
        hits = _get(stats, prefix, "overallHits::total")
        accesses = _get(stats, prefix, "overallAccesses::total")
        if hits is None and accesses is not None:
            misses = _get(stats, prefix, "overallMisses::total")
            if misses is not None:
                hits = accesses - misses
        return _safe_div(hits, accesses)

    elif kind == MetricKind.MISS_RATE:
        misses = _get(stats, prefix, "overallMisses::total")
        accesses = _get(stats, prefix, "overallAccesses::total")
        return _safe_div(misses, accesses)

    elif kind == MetricKind.DEMAND_HIT_RATE:
        hits = _get(stats, prefix, "demandHits::total")
        accesses = _get(stats, prefix, "demandAccesses::total")
        if hits is None and accesses is not None:
            misses = _get(stats, prefix, "demandMisses::total")
            if misses is not None:
                hits = accesses - misses
        return _safe_div(hits, accesses)

    elif kind == MetricKind.MISS_COUNT:
        return _get(stats, prefix, "overallMisses::total")

    elif kind == MetricKind.EVICTIONS:
        return _get(stats, prefix, "replacements")

    elif kind == MetricKind.WRITEBACKS:
        return _get(stats, prefix, "writebacks::total")

    elif kind == MetricKind.LOAD_HIT_RATE:
        hits = _get(stats, prefix, "ReadReq.hits::total")
        accesses = _get(stats, prefix, "ReadReq.accesses::total")
        if hits is None and accesses is not None:
            misses = _get(stats, prefix, "ReadReq.misses::total")
            if misses is not None:
                hits = accesses - misses
        return _safe_div(hits, accesses)

    elif kind == MetricKind.STORE_HIT_RATE:
        hits = _get(stats, prefix, "WriteReq.hits::total")
        accesses = _get(stats, prefix, "WriteReq.accesses::total")
        if hits is None and accesses is not None:
            misses = _get(stats, prefix, "WriteReq.misses::total")
            if misses is not None:
                hits = accesses - misses
        return _safe_div(hits, accesses)

    elif kind == MetricKind.STALLS:
        cpi = stats.get("board.processor.switch.core.cpi")
        if cpi is not None:
            return cpi
        cycles = stats.get("board.processor.switch.core.numCycles")
        if cycles is None:
            cycles = stats.get("board.processor.cores.core.numCycles")
        insts = stats.get("simInsts")
        if cycles is None or insts is None:
            return None
        return cycles / insts if insts > 0 else None

    elif kind == MetricKind.SIZE:
        return config.get(kind) if config else None

    elif kind == MetricKind.ASSOCIATIVITY:
        return config.get(kind) if config else None

    elif kind == MetricKind.INVALIDATIONS:
        return _get(stats, prefix, "invalidations::total")

    elif kind == MetricKind.COHERENCE_MISSES:
        return _get(stats, prefix, "coherenceMisses::total")

    elif kind == MetricKind.CONFLICT_MISSES:
        return config.get(kind) if config else None

    elif kind == MetricKind.CAPACITY_MISSES:
        return config.get(kind) if config else None

    elif kind == MetricKind.COMPULSORY_MISSES:
        return config.get(kind) if config else None

    elif kind == MetricKind.PREFETCH_ACCURACY:
        return config.get(kind) if config else None

    elif kind == MetricKind.PREFETCH_COVERAGE:
        return config.get(kind) if config else None

    elif kind == MetricKind.CRITICAL_HIT_RATE:
        if config and kind in config:
            return config[kind]
        cmr = stats.get("board.processor.switch.core.lsq0.criticalMissRate")
        if cmr is not None:
            return 1.0 - cmr
        return None

    return None


# =============================================================================
# ENTITY BINDING
# =============================================================================

@dataclass
class EntityBinding:
    """Binds an abstract entity name to concrete gem5 stats.

    Args:
        entity_name: matches Entity.name in the Relation (e.g. "C", "LLC_a")
        stats: parsed stats dict (from parse_stats_file)
        component_prefix: gem5 component path (e.g. "board.cache_hierarchy.llcache")
        config: manually supplied design parameters (SIZE, ASSOCIATIVITY, etc.)
        window_index: which ROI window this binding comes from (None for legacy)
    """
    entity_name: str
    stats: dict[str, float]
    component_prefix: str
    config: dict[MetricKind, float] = field(default_factory=dict)
    window_index: int | None = field(default=None)


# =============================================================================
# AST EVALUATOR
# =============================================================================

def eval_expr(
    expr: Expr,
    bindings: dict[str, EntityBinding],
    epsilon_values: dict[str, float] | None = None,
) -> float | None:
    """Recursively evaluate an Expr to a float.

    Returns None if any metric can't be resolved.
    """
    if isinstance(expr, Literal):
        return expr.value

    elif isinstance(expr, MetricRef):
        ent_name = expr.metric.entity.name
        binding = bindings.get(ent_name)
        if binding is None:
            return None
        return resolve_metric(expr.metric.kind, binding.stats, binding.component_prefix, binding.config)

    elif isinstance(expr, Epsilon):
        if epsilon_values and expr.name in epsilon_values:
            return epsilon_values[expr.name]
        return 0.0

    elif isinstance(expr, BinOp):
        l = eval_expr(expr.left, bindings, epsilon_values)
        r = eval_expr(expr.right, bindings, epsilon_values)
        if l is None or r is None:
            return None
        if expr.op == Op.ADD:
            return l + r
        elif expr.op == Op.SUB:
            return l - r
        elif expr.op == Op.MUL:
            return l * r
        elif expr.op == Op.DIV:
            return l / r if r != 0 else None

    elif isinstance(expr, UnaryOp) and expr.op == Op.NEG:
        v = eval_expr(expr.operand, bindings, epsilon_values)
        return -v if v is not None else None

    return None


def eval_constraint(
    c: Constraint,
    bindings: dict[str, EntityBinding],
    epsilon_values: dict[str, float] | None = None,
) -> bool | None:
    """Evaluate a constraint. Returns None if metrics unavailable."""
    lhs = eval_expr(c.lhs, bindings, epsilon_values)
    rhs = eval_expr(c.rhs, bindings, epsilon_values)
    if lhs is None or rhs is None:
        return None
    if c.op == CmpOp.GE:
        return lhs >= rhs
    elif c.op == CmpOp.GT:
        return lhs > rhs
    elif c.op == CmpOp.LE:
        return lhs <= rhs
    elif c.op == CmpOp.LT:
        return lhs < rhs
    elif c.op == CmpOp.EQ:
        return abs(lhs - rhs) < 1e-9
    elif c.op == CmpOp.NE:
        return abs(lhs - rhs) >= 1e-9
    return None


def eval_premise(
    p: Premise,
    bindings: dict[str, EntityBinding],
    epsilon_values: dict[str, float] | None = None,
) -> bool | None:
    """Evaluate a premise (Constraint, Conjunction, or Disjunction)."""
    if isinstance(p, Constraint):
        return eval_constraint(p, bindings, epsilon_values)
    elif isinstance(p, Conjunction):
        results = [eval_constraint(c, bindings, epsilon_values) for c in p.constraints]
        if None in results:
            return None
        return all(results)
    elif isinstance(p, Disjunction):
        results = [eval_constraint(c, bindings, epsilon_values) for c in p.constraints]
        if None in results:
            return None
        return any(results)
    return None


# =============================================================================
# EPSILON COMPUTATION
# =============================================================================

def compute_epsilon(
    consequent: Constraint,
    bindings: dict[str, EntityBinding],
    free_epsilons: list[str],
) -> dict[str, float] | None:
    """Compute the minimum epsilon needed to make the consequent true.

    Returns 0 if the relation already holds, positive if it needs tolerance.

    For constraints with a single epsilon, we solve directly.
    For multiple epsilons we report the total residual under the first epsilon.
    """
    if not free_epsilons:
        return None

    lhs_val = eval_expr(consequent.lhs, bindings, {e: 0.0 for e in free_epsilons})
    rhs_val = eval_expr(consequent.rhs, bindings, {e: 0.0 for e in free_epsilons})

    if lhs_val is None or rhs_val is None:
        return None

    if consequent.op == CmpOp.LE:
        residual = lhs_val - rhs_val
    elif consequent.op == CmpOp.GE:
        residual = rhs_val - lhs_val
    elif consequent.op == CmpOp.EQ:
        residual = abs(lhs_val - rhs_val)
    else:
        residual = lhs_val - rhs_val

    result = {}
    result[free_epsilons[0]] = max(0.0, residual)
    for e in free_epsilons[1:]:
        result[e] = 0.0
    return result


def compute_residual(
    consequent: Constraint,
    bindings: dict[str, EntityBinding],
) -> float | None:
    """Compute the residual for an exact (epsilon-free) constraint.

    Returns how far the data is from satisfying/violating the constraint:
      - For lhs <= rhs: residual = lhs - rhs (negative = holds with margin)
      - For lhs >= rhs: residual = rhs - lhs (negative = holds with margin)
      - For lhs == rhs: residual = |lhs - rhs|

    Positive means violated, negative means holds with that much margin.
    """
    lhs_val = eval_expr(consequent.lhs, bindings)
    rhs_val = eval_expr(consequent.rhs, bindings)

    if lhs_val is None or rhs_val is None:
        return None

    if consequent.op in (CmpOp.LE, CmpOp.LT):
        return lhs_val - rhs_val
    elif consequent.op in (CmpOp.GE, CmpOp.GT):
        return rhs_val - lhs_val
    elif consequent.op == CmpOp.EQ:
        return abs(lhs_val - rhs_val)
    elif consequent.op == CmpOp.NE:
        return -abs(lhs_val - rhs_val)
    return None


# =============================================================================
# RELATION EVALUATION
# =============================================================================

@dataclass
class EvalResult:
    """Result of evaluating a single Relation against data."""
    relation_name: str
    premises_hold: bool | None
    consequent_holds: bool | None
    epsilon: dict[str, float] | None
    missing_metrics: list[str] = field(default_factory=list)

    def __repr__(self):
        if self.premises_hold is False:
            status = "VACUOUS (premises not met)"
        elif self.consequent_holds is True:
            status = "HOLDS"
        elif self.consequent_holds is False:
            status = "VIOLATED"
        else:
            status = "UNEVALUABLE"
        parts = [f"[{self.relation_name}] {status}"]
        if self.epsilon:
            for name, val in self.epsilon.items():
                parts.append(f"  ε_{name} = {val:.6f}")
        if self.missing_metrics:
            parts.append(f"  missing: {', '.join(self.missing_metrics)}")
        return "\n".join(parts)


def _is_degenerate(relation: Relation, bindings_dict: dict[str, EntityBinding]) -> bool:
    """Detect if a multi-entity relation has all entities bound to the same data.

    A comparative relation (e.g., HR[A] >= HR[B]) is meaningless when A and B
    resolve to identical stats — the bound is trivially 0.
    """
    if len(relation.entities) <= 1:
        return False

    bound_entities = [b for name, b in bindings_dict.items()
                      if any(e.name == name for e in relation.entities)]
    if len(bound_entities) <= 1:
        return False

    first = bound_entities[0]
    return all(
        b.stats is first.stats
        and b.component_prefix == first.component_prefix
        and b.window_index == first.window_index
        for b in bound_entities[1:]
    )


def evaluate_relation(
    relation: Relation,
    entity_bindings: list[EntityBinding],
) -> EvalResult:
    """Evaluate a Relation against bound entity data.

    Args:
        relation: the Relation to evaluate
        entity_bindings: list of EntityBinding mapping entity names to stats
    """
    bindings_dict = {b.entity_name: b for b in entity_bindings}
    missing: list[str] = []

    # Check which entities are bound
    for ent in relation.entities:
        if ent.name not in bindings_dict:
            missing.append(f"entity:{ent.name}")

    if missing:
        return EvalResult(
            relation_name=relation.name,
            premises_hold=None,
            consequent_holds=None,
            epsilon=None,
            missing_metrics=missing,
        )

    # Refuse to evaluate multi-entity relations with identical data
    if _is_degenerate(relation, bindings_dict):
        return EvalResult(
            relation_name=relation.name,
            premises_hold=None,
            consequent_holds=None,
            epsilon=None,
            missing_metrics=["degenerate: all entities bound to same data (need separate runs)"],
        )

    # Evaluate premises
    premises_hold: bool | None = True
    if relation.premises:
        for p in relation.premises:
            result = eval_premise(p, bindings_dict)
            if result is None:
                premises_hold = None
                break
            if not result:
                premises_hold = False
                break

    # If premises don't hold, the relation is vacuously true
    if premises_hold is False:
        return EvalResult(
            relation_name=relation.name,
            premises_hold=False,
            consequent_holds=None,
            epsilon=None,
        )

    # Evaluate consequent
    free_eps_names = [e.name for e in relation.free_epsilons]

    if free_eps_names:
        eps = compute_epsilon(relation.consequent, bindings_dict, free_eps_names)
        if eps is None:
            return EvalResult(
                relation_name=relation.name,
                premises_hold=premises_hold,
                consequent_holds=None,
                epsilon=None,
                missing_metrics=["could not resolve consequent metrics"],
            )
        holds = all(v <= 1e-9 for v in eps.values())
        return EvalResult(
            relation_name=relation.name,
            premises_hold=premises_hold,
            consequent_holds=holds,
            epsilon=eps,
        )
    else:
        holds = eval_constraint(relation.consequent, bindings_dict)
        return EvalResult(
            relation_name=relation.name,
            premises_hold=premises_hold,
            consequent_holds=holds,
            epsilon=None,
            missing_metrics=["could not resolve consequent metrics"] if holds is None else [],
        )


def evaluate_corpus(
    relations: list[Relation],
    entity_bindings: list[EntityBinding],
) -> list[EvalResult]:
    """Evaluate all relations in a corpus against the same bindings."""
    return [evaluate_relation(r, entity_bindings) for r in relations]


# =============================================================================
# INTERVAL (MULTI-ROI) EVALUATION
# =============================================================================

@dataclass
class IntervalPairResult:
    """Result for one (window_i, window_j) pair evaluation."""
    window_a: int
    window_b: int
    premises_hold: bool | None
    consequent_holds: bool | None
    epsilon: dict[str, float] | None
    metric_values: dict[str, float]


@dataclass
class IntervalEvalSummary:
    """Aggregate results across all window pairs for one benchmark."""
    relation_name: str
    benchmark: str
    num_windows: int
    total_pairs: int
    premises_met_pairs: int
    holds_pairs: int
    violated_pairs: int
    unevaluable_pairs: int
    min_epsilon: float | None
    max_epsilon: float | None
    mean_epsilon: float | None
    pair_results: list[IntervalPairResult]


# =============================================================================
# CROSS-CONFIG (LLC SIZE) EVALUATION
# =============================================================================

@dataclass
class ConfigPairResult:
    """Result for one (config_a, config_b) pair at a specific window."""
    config_a: str
    config_b: str
    size_a_bytes: int
    size_b_bytes: int
    window_index: int
    premises_hold: bool | None
    consequent_holds: bool | None
    epsilon: dict[str, float] | None
    hit_rate_a: float | None
    hit_rate_b: float | None


@dataclass
class CrossConfigEvalSummary:
    """Aggregate results across all config pairs and windows for one benchmark."""
    relation_name: str
    benchmark: str
    configs: list[str]
    num_windows: int
    total_evaluations: int
    premises_met: int
    holds_count: int
    violated_count: int
    unevaluable_count: int
    min_epsilon: float | None
    max_epsilon: float | None
    mean_epsilon: float | None
    pair_results: list[ConfigPairResult]


def evaluate_interval_relation(
    relation: Relation,
    windows: list[dict[str, float]],
    component_prefix: str,
    benchmark_name: str = "",
    config: dict[MetricKind, float] | None = None,
    premise_metric: MetricKind = MetricKind.HIT_RATE,
) -> IntervalEvalSummary:
    """Evaluate an interval relation across all ordered pairs of ROI windows.

    For relations like R5 where premise is directional (HR[t_a] >= HR[t_b]),
    we evaluate all ordered pairs (i, j) where i != j. The premise filters
    which pairs are applicable.
    """
    assert len(relation.entities) == 2
    ent_a_name = relation.entities[0].name
    ent_b_name = relation.entities[1].name

    pair_results: list[IntervalPairResult] = []
    epsilons_where_premises_met: list[float] = []

    for i in range(len(windows)):
        for j in range(len(windows)):
            if i == j:
                continue

            binding_a = EntityBinding(
                entity_name=ent_a_name,
                stats=windows[i],
                component_prefix=component_prefix,
                config=config or {},
                window_index=i,
            )
            binding_b = EntityBinding(
                entity_name=ent_b_name,
                stats=windows[j],
                component_prefix=component_prefix,
                config=config or {},
                window_index=j,
            )

            result = evaluate_relation(relation, [binding_a, binding_b])

            metrics: dict[str, float] = {}
            for ent_name, win_idx in [(ent_a_name, i), (ent_b_name, j)]:
                w = windows[win_idx]
                hr = resolve_metric(premise_metric, w, component_prefix, config)
                cpi = resolve_metric(MetricKind.STALLS, w, component_prefix, config)
                if hr is not None:
                    metrics[f"HR_{ent_name}(w{win_idx})"] = hr
                if cpi is not None:
                    metrics[f"CPI_{ent_name}(w{win_idx})"] = cpi

            pair_results.append(IntervalPairResult(
                window_a=i,
                window_b=j,
                premises_hold=result.premises_hold,
                consequent_holds=result.consequent_holds,
                epsilon=result.epsilon,
                metric_values=metrics,
            ))

            if result.premises_hold is True and result.epsilon:
                for v in result.epsilon.values():
                    epsilons_where_premises_met.append(v)

    premises_met = sum(1 for p in pair_results if p.premises_hold is True)
    holds = sum(1 for p in pair_results
                if p.premises_hold is True and p.consequent_holds is True)
    violated = sum(1 for p in pair_results
                   if p.premises_hold is True and p.consequent_holds is False)
    unevaluable = sum(1 for p in pair_results
                      if p.premises_hold is True and p.consequent_holds is None)

    return IntervalEvalSummary(
        relation_name=relation.name,
        benchmark=benchmark_name,
        num_windows=len(windows),
        total_pairs=len(pair_results),
        premises_met_pairs=premises_met,
        holds_pairs=holds,
        violated_pairs=violated,
        unevaluable_pairs=unevaluable,
        min_epsilon=min(epsilons_where_premises_met) if epsilons_where_premises_met else None,
        max_epsilon=max(epsilons_where_premises_met) if epsilons_where_premises_met else None,
        mean_epsilon=(sum(epsilons_where_premises_met) / len(epsilons_where_premises_met)
                      if epsilons_where_premises_met else None),
        pair_results=pair_results,
    )


def format_interval_summary(summary: IntervalEvalSummary) -> str:
    """Format an IntervalEvalSummary as a human-readable text block."""
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    lines.append(f"[{summary.relation_name}] Benchmark: {summary.benchmark} "
                 f"({summary.num_windows} windows, {summary.total_pairs} pairs)")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"  Premises met: {summary.premises_met_pairs}/{summary.total_pairs} pairs")
    lines.append(f"  Consequent holds (eps=0): {summary.holds_pairs}/{summary.premises_met_pairs}")
    lines.append(f"  Violated (eps>0): {summary.violated_pairs}/{summary.premises_met_pairs}")
    if summary.unevaluable_pairs:
        lines.append(f"  Unevaluable: {summary.unevaluable_pairs}")
    lines.append("")

    if summary.min_epsilon is not None:
        lines.append(f"  Epsilon statistics (CPI units, across premise-met pairs):")
        lines.append(f"    min:  {summary.min_epsilon:.6f}")
        lines.append(f"    max:  {summary.max_epsilon:.6f}")
        lines.append(f"    mean: {summary.mean_epsilon:.6f}")
        lines.append("")

    lines.append(f"  Per-pair detail (premises met only):")
    for pr in summary.pair_results:
        if pr.premises_hold is not True:
            continue
        eps_str = ""
        if pr.epsilon:
            eps_val = list(pr.epsilon.values())[0]
            status = "HOLDS" if eps_val <= 1e-9 else f"VIOLATED eps={eps_val:.6f}"
            eps_str = f" -> {status}"
        mv = pr.metric_values
        hr_a = next((v for k, v in mv.items() if k.startswith("HR_") and f"w{pr.window_a}" in k), None)
        hr_b = next((v for k, v in mv.items() if k.startswith("HR_") and f"w{pr.window_b}" in k), None)
        cpi_a = next((v for k, v in mv.items() if k.startswith("CPI_") and f"w{pr.window_a}" in k), None)
        cpi_b = next((v for k, v in mv.items() if k.startswith("CPI_") and f"w{pr.window_b}" in k), None)
        lines.append(f"    (w{pr.window_a}, w{pr.window_b}): "
                     f"HR=[{hr_a:.4f}, {hr_b:.4f}] "
                     f"CPI=[{cpi_a:.4f}, {cpi_b:.4f}]"
                     f"{eps_str}")
    lines.append("")
    return "\n".join(lines)


def format_cross_benchmark_table(summaries: list[IntervalEvalSummary]) -> str:
    """Format a cross-benchmark summary table."""
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    if summaries:
        lines.append(f"CROSS-BENCHMARK SUMMARY: {summaries[0].relation_name}")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"  {'Benchmark':<16} {'Pairs':>5} {'Prem-Met':>8} "
                 f"{'Holds':>5} {'Violated':>8} {'Min-Eps':>10} {'Max-Eps':>10}")
    lines.append(f"  {'-'*16} {'-'*5} {'-'*8} {'-'*5} {'-'*8} {'-'*10} {'-'*10}")

    total_premises_met = 0
    total_holds = 0
    all_eps: list[float] = []

    for s in summaries:
        min_e = f"{s.min_epsilon:.6f}" if s.min_epsilon is not None else "N/A"
        max_e = f"{s.max_epsilon:.6f}" if s.max_epsilon is not None else "N/A"
        lines.append(f"  {s.benchmark:<16} {s.total_pairs:>5} {s.premises_met_pairs:>8} "
                     f"{s.holds_pairs:>5} {s.violated_pairs:>8} {min_e:>10} {max_e:>10}")
        total_premises_met += s.premises_met_pairs
        total_holds += s.holds_pairs
        for pr in s.pair_results:
            if pr.premises_hold is True and pr.epsilon:
                all_eps.extend(pr.epsilon.values())

    lines.append("")
    if total_premises_met > 0:
        pct = 100.0 * total_holds / total_premises_met
        lines.append(f"  Total pairs where premises hold: {total_premises_met}")
        lines.append(f"  Relation holds (eps=0): {total_holds} ({pct:.1f}%)")
    if all_eps:
        lines.append(f"  Global max epsilon: {max(all_eps):.6f}")
        lines.append(f"  Global mean epsilon: {sum(all_eps)/len(all_eps):.6f}")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# CROSS-CONFIG EVALUATION FUNCTIONS
# =============================================================================

def discover_llc_configs(benchmark_dir: Path, benchmark_name: str) -> dict[str, Path]:
    """Discover available LLC config stats files for a benchmark.

    Returns: {config_name: stats_path} for configs that have valid stats.
    """
    configs: dict[str, Path] = {}

    default_stats = benchmark_dir / "stats.txt"
    if default_stats.exists():
        configs["default"] = default_stats

    for variant_dir in sorted(benchmark_dir.iterdir()):
        if variant_dir.is_dir() and variant_dir.name.startswith("llc_"):
            variant_stats = variant_dir / benchmark_name / "stats.txt"
            if variant_stats.exists():
                configs[variant_dir.name] = variant_stats

    return configs


def evaluate_cross_config_relation(
    relation: Relation,
    config_windows: dict[str, list[dict[str, float]]],
    config_sizes: dict[str, int],
    component_prefix: str,
    benchmark_name: str = "",
) -> CrossConfigEvalSummary:
    """Evaluate a cross-config relation across all ordered config pairs and windows.

    For R1: Size[LLC_a] >= Size[LLC_b] => HitRate[LLC_a] >= HitRate[LLC_b] + epsilon

    Evaluates all ordered pairs (config_a, config_b) where size_a >= size_b,
    at each window index independently.
    """
    cache_entities = [e for e in relation.entities if e.kind == "cache"]
    policy_entities = [e for e in relation.entities if e.kind == "policy"]
    assert len(cache_entities) == 2
    ent_a_name = cache_entities[0].name
    ent_b_name = cache_entities[1].name
    policy_name = policy_entities[0].name if policy_entities else None

    num_windows = min(len(ws) for ws in config_windows.values())
    config_names = sorted(config_windows.keys(), key=lambda c: config_sizes[c])

    pair_results: list[ConfigPairResult] = []
    epsilons_collected: list[float] = []

    for i, cfg_a in enumerate(config_names):
        for j, cfg_b in enumerate(config_names):
            if i == j:
                continue
            if config_sizes[cfg_a] < config_sizes[cfg_b]:
                continue

            for w in range(num_windows):
                stats_a = config_windows[cfg_a][w]
                stats_b = config_windows[cfg_b][w]

                binding_a = EntityBinding(
                    entity_name=ent_a_name,
                    stats=stats_a,
                    component_prefix=component_prefix,
                    config={MetricKind.SIZE: config_sizes[cfg_a]},
                    window_index=w,
                )
                binding_b = EntityBinding(
                    entity_name=ent_b_name,
                    stats=stats_b,
                    component_prefix=component_prefix,
                    config={MetricKind.SIZE: config_sizes[cfg_b]},
                    window_index=w,
                )

                bindings = [binding_a, binding_b]

                if policy_name:
                    binding_p = EntityBinding(
                        entity_name=policy_name,
                        stats=stats_a,
                        component_prefix=component_prefix,
                        config={},
                    )
                    bindings.append(binding_p)

                result = evaluate_relation(relation, bindings)

                hr_a = resolve_metric(MetricKind.HIT_RATE, stats_a, component_prefix)
                hr_b = resolve_metric(MetricKind.HIT_RATE, stats_b, component_prefix)

                pair_results.append(ConfigPairResult(
                    config_a=cfg_a,
                    config_b=cfg_b,
                    size_a_bytes=config_sizes[cfg_a],
                    size_b_bytes=config_sizes[cfg_b],
                    window_index=w,
                    premises_hold=result.premises_hold,
                    consequent_holds=result.consequent_holds,
                    epsilon=result.epsilon,
                    hit_rate_a=hr_a,
                    hit_rate_b=hr_b,
                ))

                if result.premises_hold is True and result.epsilon:
                    for v in result.epsilon.values():
                        epsilons_collected.append(v)

    premises_met = sum(1 for p in pair_results if p.premises_hold is True)
    holds = sum(1 for p in pair_results
                if p.premises_hold is True and p.consequent_holds is True)
    violated = sum(1 for p in pair_results
                   if p.premises_hold is True and p.consequent_holds is False)
    unevaluable = sum(1 for p in pair_results
                      if p.premises_hold is True and p.consequent_holds is None)

    return CrossConfigEvalSummary(
        relation_name=relation.name,
        benchmark=benchmark_name,
        configs=config_names,
        num_windows=num_windows,
        total_evaluations=len(pair_results),
        premises_met=premises_met,
        holds_count=holds,
        violated_count=violated,
        unevaluable_count=unevaluable,
        min_epsilon=min(epsilons_collected) if epsilons_collected else None,
        max_epsilon=max(epsilons_collected) if epsilons_collected else None,
        mean_epsilon=(sum(epsilons_collected) / len(epsilons_collected)
                      if epsilons_collected else None),
        pair_results=pair_results,
    )


def format_cross_config_summary(summary: CrossConfigEvalSummary) -> str:
    """Format a CrossConfigEvalSummary as human-readable text."""
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    lines.append(f"[{summary.relation_name}] Benchmark: {summary.benchmark}")
    lines.append(f"  Configs: {', '.join(summary.configs)} | {summary.num_windows} windows")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"  Total evaluations: {summary.total_evaluations}")
    lines.append(f"  Premises met: {summary.premises_met}/{summary.total_evaluations}")
    lines.append(f"  Consequent holds (eps=0): {summary.holds_count}/{summary.premises_met}")
    lines.append(f"  Violated (eps>0): {summary.violated_count}/{summary.premises_met}")
    if summary.unevaluable_count:
        lines.append(f"  Unevaluable: {summary.unevaluable_count}")
    lines.append("")

    if summary.min_epsilon is not None:
        lines.append(f"  Epsilon statistics (hit rate units, across premise-met pairs):")
        lines.append(f"    min:  {summary.min_epsilon:.6f}")
        lines.append(f"    max:  {summary.max_epsilon:.6f}")
        lines.append(f"    mean: {summary.mean_epsilon:.6f}")
        lines.append("")

    lines.append(f"  Per-pair detail (premises met only):")
    for pr in summary.pair_results:
        if pr.premises_hold is not True:
            continue
        eps_str = ""
        if pr.epsilon:
            eps_val = list(pr.epsilon.values())[0]
            status = "HOLDS" if eps_val <= 1e-9 else f"VIOLATED eps={eps_val:.6f}"
            eps_str = f" -> {status}"
        hr_a_s = f"{pr.hit_rate_a:.4f}" if pr.hit_rate_a is not None else "N/A"
        hr_b_s = f"{pr.hit_rate_b:.4f}" if pr.hit_rate_b is not None else "N/A"
        size_a_mb = pr.size_a_bytes / (1024 * 1024)
        size_b_mb = pr.size_b_bytes / (1024 * 1024)
        lines.append(f"    ({pr.config_a} vs {pr.config_b}, w{pr.window_index}): "
                     f"Size=[{size_a_mb:.0f}MB, {size_b_mb:.0f}MB] "
                     f"HR=[{hr_a_s}, {hr_b_s}]{eps_str}")
    lines.append("")
    return "\n".join(lines)


def format_cross_config_benchmark_table(summaries: list[CrossConfigEvalSummary]) -> str:
    """Format a cross-benchmark summary table for cross-config evaluation."""
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    if summaries:
        lines.append(f"CROSS-CONFIG CROSS-BENCHMARK SUMMARY: {summaries[0].relation_name}")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"  {'Benchmark':<16} {'Evals':>5} {'Prem-Met':>8} "
                 f"{'Holds':>5} {'Violated':>8} {'Min-Eps':>10} {'Max-Eps':>10}")
    lines.append(f"  {'-'*16} {'-'*5} {'-'*8} {'-'*5} {'-'*8} {'-'*10} {'-'*10}")

    total_premises_met = 0
    total_holds = 0
    all_eps: list[float] = []

    for s in summaries:
        min_e = f"{s.min_epsilon:.6f}" if s.min_epsilon is not None else "N/A"
        max_e = f"{s.max_epsilon:.6f}" if s.max_epsilon is not None else "N/A"
        lines.append(f"  {s.benchmark:<16} {s.total_evaluations:>5} {s.premises_met:>8} "
                     f"{s.holds_count:>5} {s.violated_count:>8} {min_e:>10} {max_e:>10}")
        total_premises_met += s.premises_met
        total_holds += s.holds_count
        for pr in s.pair_results:
            if pr.premises_hold is True and pr.epsilon:
                all_eps.extend(pr.epsilon.values())

    lines.append("")
    if total_premises_met > 0:
        pct = 100.0 * total_holds / total_premises_met
        lines.append(f"  Total pairs where premises hold: {total_premises_met}")
        lines.append(f"  Relation holds (eps=0): {total_holds} ({pct:.1f}%)")
    if all_eps:
        lines.append(f"  Global max epsilon: {max(all_eps):.6f}")
        lines.append(f"  Global mean epsilon: {sum(all_eps)/len(all_eps):.6f}")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# OPT (R3: OPT UPPER-BOUNDS ALL POLICIES) EVALUATION
# =============================================================================

@dataclass
class OptWindowResult:
    """Result for R3 at one ROI window: OPT vs actual policy hit rate."""
    window_index: int
    premises_hold: bool | None
    consequent_holds: bool | None
    epsilon: dict[str, float] | None
    hit_rate_opt: float | None
    hit_rate_actual: float | None


@dataclass
class OptEvalSummary:
    """Aggregate R3 results across all ROI windows for one benchmark."""
    relation_name: str
    benchmark: str
    num_windows: int
    premises_met: int
    holds_count: int
    violated_count: int
    unevaluable_count: int
    min_epsilon: float | None
    max_epsilon: float | None
    mean_epsilon: float | None
    window_results: list[OptWindowResult]


def evaluate_opt_relation(
    relation: Relation,
    actual_windows: list[dict[str, float]],
    opt_rows: list[dict[str, float]],
    component_prefix: str,
    benchmark_name: str = "",
) -> OptEvalSummary:
    """Evaluate R3 (OPT upper-bounds the actual policy) per ROI window.

    For each window i, C_any is bound to the *same* gem5 stats used for R5
    (the actual replacement policy's LLC hits/accesses), and C_opt is bound
    to the Belady-OPT trace's hits/accesses for that ROI. Both caches share
    the same geometry, so the SIZE/ASSOC equality premises are satisfied by
    supplying matching dummy config values.

        Size[C_opt]=Size[C_any] ∧ Assoc[C_opt]=Assoc[C_any]
            => HitRate[C_opt] >= HitRate[C_any] - ε_3
    """
    cache_entities = [e for e in relation.entities if e.kind == "cache"]
    assert len(cache_entities) == 2
    # C_opt is the first cache entity, C_any the second (per corpus R3 def)
    opt_name = cache_entities[0].name
    any_name = cache_entities[1].name
    policy_entities = [e for e in relation.entities if e.kind == "policy"]

    # Matching geometry: identical dummy SIZE/ASSOC for both caches.
    geom = {MetricKind.SIZE: 1.0, MetricKind.ASSOCIATIVITY: 1.0}

    num_windows = min(len(actual_windows), len(opt_rows))
    window_results: list[OptWindowResult] = []
    epsilons_collected: list[float] = []

    for w in range(num_windows):
        actual_stats = actual_windows[w]
        opt_stats = opt_window_stats(opt_rows[w], component_prefix)

        binding_opt = EntityBinding(
            entity_name=opt_name,
            stats=opt_stats,
            component_prefix=component_prefix,
            config=dict(geom),
            window_index=w,
        )
        binding_any = EntityBinding(
            entity_name=any_name,
            stats=actual_stats,
            component_prefix=component_prefix,
            config=dict(geom),
            window_index=w,
        )
        bindings = [binding_opt, binding_any]

        # Bind policy entities so the relation's entity list resolves. No
        # metric in R3 references a policy, so the bound stats are irrelevant.
        for pe in policy_entities:
            bindings.append(EntityBinding(
                entity_name=pe.name,
                stats=actual_stats,
                component_prefix=component_prefix,
                config={},
            ))

        result = evaluate_relation(relation, bindings)

        hr_opt = resolve_metric(MetricKind.HIT_RATE, opt_stats, component_prefix)
        hr_actual = resolve_metric(MetricKind.HIT_RATE, actual_stats, component_prefix)

        window_results.append(OptWindowResult(
            window_index=w,
            premises_hold=result.premises_hold,
            consequent_holds=result.consequent_holds,
            epsilon=result.epsilon,
            hit_rate_opt=hr_opt,
            hit_rate_actual=hr_actual,
        ))

        if result.premises_hold is True and result.epsilon:
            for v in result.epsilon.values():
                epsilons_collected.append(v)

    premises_met = sum(1 for r in window_results if r.premises_hold is True)
    holds = sum(1 for r in window_results
                if r.premises_hold is True and r.consequent_holds is True)
    violated = sum(1 for r in window_results
                   if r.premises_hold is True and r.consequent_holds is False)
    unevaluable = sum(1 for r in window_results
                      if r.premises_hold is True and r.consequent_holds is None)

    return OptEvalSummary(
        relation_name=relation.name,
        benchmark=benchmark_name,
        num_windows=num_windows,
        premises_met=premises_met,
        holds_count=holds,
        violated_count=violated,
        unevaluable_count=unevaluable,
        min_epsilon=min(epsilons_collected) if epsilons_collected else None,
        max_epsilon=max(epsilons_collected) if epsilons_collected else None,
        mean_epsilon=(sum(epsilons_collected) / len(epsilons_collected)
                      if epsilons_collected else None),
        window_results=window_results,
    )


def format_opt_summary(summary: OptEvalSummary) -> str:
    """Format an OptEvalSummary as human-readable text."""
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    lines.append(f"[{summary.relation_name}] Benchmark: {summary.benchmark} "
                 f"({summary.num_windows} windows)")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"  Premises met: {summary.premises_met}/{summary.num_windows}")
    lines.append(f"  Consequent holds (eps=0): {summary.holds_count}/{summary.premises_met}")
    lines.append(f"  Violated (eps>0): {summary.violated_count}/{summary.premises_met}")
    if summary.unevaluable_count:
        lines.append(f"  Unevaluable: {summary.unevaluable_count}")
    lines.append("")

    if summary.min_epsilon is not None:
        lines.append(f"  Epsilon statistics (hit rate units, across premise-met windows):")
        lines.append(f"    min:  {summary.min_epsilon:.6f}")
        lines.append(f"    max:  {summary.max_epsilon:.6f}")
        lines.append(f"    mean: {summary.mean_epsilon:.6f}")
        lines.append("")

    lines.append(f"  Per-window detail:")
    for wr in summary.window_results:
        eps_str = ""
        if wr.epsilon:
            eps_val = list(wr.epsilon.values())[0]
            status = "HOLDS" if eps_val <= 1e-9 else f"VIOLATED eps={eps_val:.6f}"
            eps_str = f" -> {status}"
        opt_s = f"{wr.hit_rate_opt:.4f}" if wr.hit_rate_opt is not None else "N/A"
        act_s = f"{wr.hit_rate_actual:.4f}" if wr.hit_rate_actual is not None else "N/A"
        lines.append(f"    w{wr.window_index}: "
                     f"HR_opt={opt_s}  HR_actual={act_s}{eps_str}")
    lines.append("")
    return "\n".join(lines)


def format_opt_benchmark_table(summaries: list[OptEvalSummary]) -> str:
    """Format a cross-benchmark summary table for R3 OPT evaluation."""
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    if summaries:
        lines.append(f"OPT CROSS-BENCHMARK SUMMARY: {summaries[0].relation_name}")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"  {'Benchmark':<16} {'Windows':>7} {'Prem-Met':>8} "
                 f"{'Holds':>5} {'Violated':>8} {'Min-Eps':>10} {'Max-Eps':>10}")
    lines.append(f"  {'-'*16} {'-'*7} {'-'*8} {'-'*5} {'-'*8} {'-'*10} {'-'*10}")

    total_premises_met = 0
    total_holds = 0
    all_eps: list[float] = []

    for s in summaries:
        min_e = f"{s.min_epsilon:.6f}" if s.min_epsilon is not None else "N/A"
        max_e = f"{s.max_epsilon:.6f}" if s.max_epsilon is not None else "N/A"
        lines.append(f"  {s.benchmark:<16} {s.num_windows:>7} {s.premises_met:>8} "
                     f"{s.holds_count:>5} {s.violated_count:>8} {min_e:>10} {max_e:>10}")
        total_premises_met += s.premises_met
        total_holds += s.holds_count
        for wr in s.window_results:
            if wr.premises_hold is True and wr.epsilon:
                all_eps.extend(wr.epsilon.values())

    lines.append("")
    if total_premises_met > 0:
        pct = 100.0 * total_holds / total_premises_met
        lines.append(f"  Total windows where premises hold: {total_premises_met}")
        lines.append(f"  Relation holds (eps=0): {total_holds} ({pct:.1f}%)")
    if all_eps:
        lines.append(f"  Global max epsilon: {max(all_eps):.6f}")
        lines.append(f"  Global mean epsilon: {sum(all_eps)/len(all_eps):.6f}")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# MAIN: DEMO AGAINST SAMPLE WORKLOADS
# =============================================================================

if __name__ == "__main__":
    import sys
    from corpus import (
        ALL_RELATIONS,
        R1_larger_cache_higher_hr,
        R3_opt_upper_bounds_all_policies,
        R5_hit_rate_implies_fewer_stalls,
        R6_critical_hit_rate_implies_fewer_stalls,
        R11_stores_hit_less_than_loads,
    )

    output_path = Path(__file__).resolve().parent / "eval_results.txt"
    lines: list[str] = []

    multi_roi_dir = Path(__file__).resolve().parent.parent / "gem5-configs" / "output"

    if multi_roi_dir.exists():
        benchmark_dirs = sorted(d for d in multi_roi_dir.iterdir() if d.is_dir())
    else:
        benchmark_dirs = []

    LLC_PREFIX = "board.cache_hierarchy.llcache"

    # --- Section 1: single-entity evaluation against gem5 output benchmarks ---

    lines.append("SINGLE-ENTITY RELATION EVALUATION")
    lines.append(f"Source: {multi_roi_dir}")
    lines.append("")

    single_entity = [r for r in ALL_RELATIONS if len(r.entities) == 1]

    if benchmark_dirs:
        for bench_dir in benchmark_dirs:
            stats_path = bench_dir / "stats.txt"
            if not stats_path.exists():
                continue

            windows = parse_stats_file_windows(stats_path)
            lines.append(f"{'=' * 60}")
            lines.append(f"Benchmark: {bench_dir.name} ({len(windows)} windows)")
            lines.append(f"{'=' * 60}")
            lines.append("")

            for w_idx, stats in enumerate(windows):
                lines.append(f"  --- Window {w_idx} ---")
                for r in single_entity:
                    ent_name = r.entities[0].name
                    binding = EntityBinding(
                        entity_name=ent_name,
                        stats=stats,
                        component_prefix=LLC_PREFIX,
                    )
                    result = evaluate_relation(r, [binding])
                    lines.append(f"    {repr(result)}")
                lines.append("")
    else:
        lines.append("  No benchmark directories found, skipping.")
        lines.append("")

    # --- Section 2: Multi-ROI interval evaluation ---
    lines.append("")
    lines.append("INTERVAL RELATION EVALUATION (Multi-ROI)")
    lines.append(f"Source: {multi_roi_dir}")
    lines.append(f"STALLS metric mapped to CPI (board.processor.switch.core.cpi)")
    lines.append("")

    if benchmark_dirs:
        interval_relations = [
            (R5_hit_rate_implies_fewer_stalls, MetricKind.HIT_RATE),
            (R6_critical_hit_rate_implies_fewer_stalls, MetricKind.CRITICAL_HIT_RATE),
        ]

        for rel, prem_metric in interval_relations:
            summaries: list[IntervalEvalSummary] = []

            for bench_dir in benchmark_dirs:
                stats_path = bench_dir / "stats.txt"
                if not stats_path.exists():
                    continue

                windows = parse_stats_file_windows(stats_path)
                if len(windows) < 2:
                    continue

                summary = evaluate_interval_relation(
                    rel, windows, LLC_PREFIX,
                    benchmark_name=bench_dir.name,
                    premise_metric=prem_metric,
                )
                summaries.append(summary)
                lines.append(format_interval_summary(summary))

            if summaries:
                lines.append(format_cross_benchmark_table(summaries))
    else:
        lines.append("  No benchmark directories found, skipping interval evaluation.")
        lines.append("")

    # --- Section 3: Cross-config (LLC size) evaluation for R1 ---
    LLC_CONFIG_SIZES: dict[str, int] = {
        "default": 8 * 1024 * 1024,
        "llc_4MiB": 4 * 1024 * 1024,
        "llc_16MiB": 16 * 1024 * 1024,
    }

    lines.append("")
    lines.append("CROSS-CONFIG RELATION EVALUATION (R1: Larger Cache => Higher Hit Rate)")
    lines.append(f"Source: {multi_roi_dir}")
    lines.append(f"Configs: 4MiB, 8MiB (default), 16MiB")
    lines.append(f"Evaluation: per-window across all ordered config pairs")
    lines.append("")

    if benchmark_dirs:
        r1_summaries: list[CrossConfigEvalSummary] = []

        for bench_dir in benchmark_dirs:
            bench_name = bench_dir.name
            configs = discover_llc_configs(bench_dir, bench_name)

            if len(configs) < 2:
                continue

            config_windows: dict[str, list[dict[str, float]]] = {}
            for cfg_name, stats_path in configs.items():
                windows = parse_stats_file_windows(stats_path)
                if windows:
                    config_windows[cfg_name] = windows

            if len(config_windows) < 2:
                continue

            config_sizes = {k: LLC_CONFIG_SIZES[k] for k in config_windows
                            if k in LLC_CONFIG_SIZES}
            config_windows = {k: v for k, v in config_windows.items() if k in config_sizes}

            if len(config_windows) < 2:
                continue

            summary = evaluate_cross_config_relation(
                R1_larger_cache_higher_hr,
                config_windows,
                config_sizes,
                LLC_PREFIX,
                benchmark_name=bench_name,
            )
            r1_summaries.append(summary)
            lines.append(format_cross_config_summary(summary))

        if r1_summaries:
            lines.append(format_cross_config_benchmark_table(r1_summaries))
    else:
        lines.append("  No benchmark directories found, skipping cross-config evaluation.")
        lines.append("")

    # --- Section 4: R11 (Stores Hit Less Than Loads) at L1D ---
    L1D_PREFIX = "board.cache_hierarchy.l1dcaches"

    lines.append("")
    lines.append("R11 EVALUATION: Stores Hit Less Than Loads (L1D)")
    lines.append(f"Source: {multi_roi_dir}")
    lines.append(f"Target: L1D ({L1D_PREFIX})")
    lines.append(f"Relation: StoreHitRate[C] <= LoadHitRate[C] + ε_11")
    lines.append("")

    if benchmark_dirs:
        r11_results: dict[str, list[dict]] = {}

        for bench_dir in benchmark_dirs:
            stats_path = bench_dir / "stats.txt"
            if not stats_path.exists():
                continue

            windows = parse_stats_file_windows(stats_path)
            lines.append(f"{'=' * 60}")
            lines.append(f"Benchmark: {bench_dir.name} ({len(windows)} windows)")
            lines.append(f"{'=' * 60}")
            lines.append("")

            bench_results = []
            for w_idx, stats in enumerate(windows):
                ent_name = R11_stores_hit_less_than_loads.entities[0].name
                binding = EntityBinding(
                    entity_name=ent_name,
                    stats=stats,
                    component_prefix=L1D_PREFIX,
                )
                result = evaluate_relation(R11_stores_hit_less_than_loads, [binding])

                load_hr = resolve_metric(MetricKind.LOAD_HIT_RATE, stats, L1D_PREFIX)
                store_hr = resolve_metric(MetricKind.STORE_HIT_RATE, stats, L1D_PREFIX)
                load_str = f"{load_hr:.6f}" if load_hr is not None else "N/A"
                store_str = f"{store_hr:.6f}" if store_hr is not None else "N/A"

                eps_str = ""
                eps_val = None
                if result.epsilon:
                    eps_val = list(result.epsilon.values())[0]
                    status = "HOLDS" if eps_val <= 1e-9 else f"VIOLATED eps={eps_val:.6f}"
                    eps_str = f" -> {status}"
                elif result.consequent_holds is True:
                    eps_str = " -> HOLDS"
                    eps_val = 0.0
                elif result.consequent_holds is False:
                    eps_str = " -> VIOLATED"

                lines.append(f"  Window {w_idx}: LoadHR={load_str}  StoreHR={store_str}{eps_str}")
                bench_results.append({
                    "window": w_idx,
                    "load_hr": load_hr,
                    "store_hr": store_hr,
                    "holds": result.consequent_holds is True,
                    "eps": eps_val,
                })

            r11_results[bench_dir.name] = bench_results
            lines.append("")

        # Summary table for R11 across benchmarks
        lines.append(f"{'=' * 70}")
        lines.append("R11 CROSS-BENCHMARK SUMMARY")
        lines.append(f"{'=' * 70}")
        lines.append("")
        lines.append(f"  {'Benchmark':<16} {'Windows':>7} {'Holds':>5} {'Violated':>8} "
                     f"{'Min-Eps':>10} {'Max-Eps':>10} {'Mean-Eps':>10}")
        lines.append(f"  {'-'*16} {'-'*7} {'-'*5} {'-'*8} {'-'*10} {'-'*10} {'-'*10}")

        total_holds = 0
        total_windows = 0
        all_eps: list[float] = []

        for bench_name in sorted(r11_results.keys()):
            results = r11_results[bench_name]
            num_windows = len(results)
            holds = sum(1 for r in results if r["holds"])
            violated = num_windows - holds
            eps_vals = [r["eps"] for r in results if r["eps"] is not None]

            min_e = f"{min(eps_vals):.6f}" if eps_vals else "N/A"
            max_e = f"{max(eps_vals):.6f}" if eps_vals else "N/A"
            mean_e = f"{sum(eps_vals)/len(eps_vals):.6f}" if eps_vals else "N/A"

            lines.append(f"  {bench_name:<16} {num_windows:>7} {holds:>5} {violated:>8} "
                         f"{min_e:>10} {max_e:>10} {mean_e:>10}")

            total_holds += holds
            total_windows += num_windows
            all_eps.extend(eps_vals)

        lines.append("")
        lines.append(f"  Total windows: {total_windows}")
        lines.append(f"  Relation holds (eps=0): {total_holds}/{total_windows} ({100.0*total_holds/total_windows:.1f}%)")
        lines.append(f"  Relation violated (eps>0): {total_windows - total_holds}/{total_windows}")
        if all_eps:
            lines.append(f"  Global min epsilon: {min(all_eps):.6f}")
            lines.append(f"  Global max epsilon: {max(all_eps):.6f}")
            lines.append(f"  Global mean epsilon: {sum(all_eps)/len(all_eps):.6f}")
        lines.append("")
    else:
        lines.append("  No benchmark directories found, skipping R11 evaluation.")
        lines.append("")

    # --- Section 5: R3 (OPT Upper-Bounds All Policies) ---
    # OPT hit rates come from the per-ROI Belady trace results in output-opt/.
    # The actual-policy hit rates reuse the SAME gem5 stats as R5 (the LLC
    # overall hits/accesses from gem5-configs/output/), compared window-by-window.
    opt_dir = Path(__file__).resolve().parent.parent / "output-opt"

    lines.append("")
    lines.append("OPT RELATION EVALUATION (R3: OPT Upper-Bounds All Policies)")
    lines.append(f"OPT source: {opt_dir} (trace_llc_results.txt per benchmark)")
    lines.append(f"Actual-policy source: {multi_roi_dir} (same LLC hit rates as R5)")
    lines.append(f"Relation: HitRate[C_opt] >= HitRate[C_any] - ε_3 (per ROI window)")
    lines.append("")

    if benchmark_dirs and opt_dir.exists():
        opt_summaries: list[OptEvalSummary] = []

        for bench_dir in benchmark_dirs:
            stats_path = bench_dir / "stats.txt"
            opt_path = opt_dir / bench_dir.name / "trace_llc_results.txt"
            if not stats_path.exists() or not opt_path.exists():
                continue

            actual_windows = parse_stats_file_windows(stats_path)
            opt_rows = parse_opt_trace_results(opt_path)
            if not actual_windows or not opt_rows:
                continue

            summary = evaluate_opt_relation(
                R3_opt_upper_bounds_all_policies,
                actual_windows,
                opt_rows,
                LLC_PREFIX,
                benchmark_name=bench_dir.name,
            )
            opt_summaries.append(summary)
            lines.append(format_opt_summary(summary))

        if opt_summaries:
            lines.append(format_opt_benchmark_table(opt_summaries))
    else:
        lines.append("  No OPT trace results found, skipping R3 evaluation.")
        lines.append("")

    # --- Write combined output ---
    output_path.write_text("\n".join(lines))
    print(f"Results written to {output_path}")
