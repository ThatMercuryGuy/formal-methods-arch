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

For relations with free epsilons, the key output is the **empirical epsilon
bound** — the value of ε that makes the consequent exactly true for this data.
  - Negative: relation holds with that much margin.
  - Zero: holds exactly at the boundary.
  - Positive: violated by that amount.

Relations without free epsilons (exact/definitional facts like F1, F2) just
report True/False — there is no bound to compute.

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

Metrics not derivable from stats (SIZE, ASSOCIATIVITY, COMPULSORY_MISSES,
CONFLICT_MISSES, CAPACITY_MISSES, COHERENCE_MISSES, CRITICAL_HIT_RATE,
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
    # result.slack["1"] gives the empirical epsilon bound

Running as a script:

    python3 eval_backend.py

    Evaluates all corpus relations against stats files in ../sample_workloads/
    and writes results to eval_results.txt.

================================================================================
OUTPUT FORMAT
================================================================================

EvalResult fields:
    relation_name    — which relation was evaluated
    premises_hold    — True/False/None (None = couldn't check)
    consequent_holds — True/False/None
    slack            — {epsilon_name: float} for relations with free epsilons.
                       Sign convention: negative = holds, positive = violated.
                       None for exact relations (no epsilon to solve for).
    missing_metrics  — list of what couldn't be resolved

EvalResult statuses:
    HOLDS                      — consequent satisfied (slack <= 0 if epsilons)
    VIOLATED                   — consequent broken (slack > 0)
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
        cycles = stats.get("board.processor.cores.core.numCycles")
        insts = stats.get("simInsts")
        if cycles is None or insts is None:
            return None
        return cycles - insts

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
        return config.get(kind) if config else None

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
    """
    entity_name: str
    stats: dict[str, float]
    component_prefix: str
    config: dict[MetricKind, float] = field(default_factory=dict)


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
    match expr:
        case Literal(value=v):
            return v

        case MetricRef(metric=m):
            ent_name = m.entity.name
            binding = bindings.get(ent_name)
            if binding is None:
                return None
            return resolve_metric(m.kind, binding.stats, binding.component_prefix, binding.config)

        case Epsilon(name=n):
            if epsilon_values and n in epsilon_values:
                return epsilon_values[n]
            return 0.0

        case BinOp(op=op, left=left, right=right):
            l = eval_expr(left, bindings, epsilon_values)
            r = eval_expr(right, bindings, epsilon_values)
            if l is None or r is None:
                return None
            match op:
                case Op.ADD: return l + r
                case Op.SUB: return l - r
                case Op.MUL: return l * r
                case Op.DIV: return l / r if r != 0 else None

        case UnaryOp(op=Op.NEG, operand=operand):
            v = eval_expr(operand, bindings, epsilon_values)
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
    match c.op:
        case CmpOp.GE: return lhs >= rhs
        case CmpOp.GT: return lhs > rhs
        case CmpOp.LE: return lhs <= rhs
        case CmpOp.LT: return lhs < rhs
        case CmpOp.EQ: return abs(lhs - rhs) < 1e-9
        case CmpOp.NE: return abs(lhs - rhs) >= 1e-9
    return None


def eval_premise(
    p: Premise,
    bindings: dict[str, EntityBinding],
    epsilon_values: dict[str, float] | None = None,
) -> bool | None:
    """Evaluate a premise (Constraint, Conjunction, or Disjunction)."""
    match p:
        case Constraint() as c:
            return eval_constraint(c, bindings, epsilon_values)
        case Conjunction(constraints=cs):
            results = [eval_constraint(c, bindings, epsilon_values) for c in cs]
            if None in results:
                return None
            return all(results)
        case Disjunction(constraints=cs):
            results = [eval_constraint(c, bindings, epsilon_values) for c in cs]
            if None in results:
                return None
            return any(results)
    return None


# =============================================================================
# SLACK COMPUTATION
# =============================================================================

def compute_slack(
    consequent: Constraint,
    bindings: dict[str, EntityBinding],
    free_epsilons: list[str],
) -> dict[str, float] | None:
    """Compute empirical slack for each free epsilon.

    For a constraint like `lhs <= rhs + ε`, slack = lhs - rhs.
    Positive slack means the relation is violated by that amount.
    Negative slack means the relation holds with margin.

    For constraints with a single epsilon, we solve directly.
    For multiple epsilons we report the total residual under the first epsilon.
    """
    if not free_epsilons:
        return None

    lhs_val = eval_expr(consequent.lhs, bindings, {e: 0.0 for e in free_epsilons})
    rhs_val = eval_expr(consequent.rhs, bindings, {e: 0.0 for e in free_epsilons})

    if lhs_val is None or rhs_val is None:
        return None

    match consequent.op:
        case CmpOp.LE:
            # lhs <= rhs + ε  →  ε >= lhs - rhs
            residual = lhs_val - rhs_val
        case CmpOp.GE:
            # lhs >= rhs - ε  →  ε >= rhs - lhs
            # lhs >= rhs + ε  →  ε <= lhs - rhs (epsilon tightens the bound)
            residual = rhs_val - lhs_val
        case CmpOp.EQ:
            residual = abs(lhs_val - rhs_val)
        case _:
            residual = lhs_val - rhs_val

    # Assign all residual to the first epsilon (single-epsilon relations
    # are the common case; multi-epsilon needs the Z3 backend for proper decomposition)
    result = {}
    result[free_epsilons[0]] = residual
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

    match consequent.op:
        case CmpOp.LE | CmpOp.LT:
            return lhs_val - rhs_val
        case CmpOp.GE | CmpOp.GT:
            return rhs_val - lhs_val
        case CmpOp.EQ:
            return abs(lhs_val - rhs_val)
        case CmpOp.NE:
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
    slack: dict[str, float] | None
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
        if self.slack:
            for name, val in self.slack.items():
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
        b.stats is first.stats and b.component_prefix == first.component_prefix
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
            slack=None,
            missing_metrics=missing,
        )

    # Refuse to evaluate multi-entity relations with identical data
    if _is_degenerate(relation, bindings_dict):
        return EvalResult(
            relation_name=relation.name,
            premises_hold=None,
            consequent_holds=None,
            slack=None,
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
            slack=None,
        )

    # Evaluate consequent
    free_eps_names = [e.name for e in relation.free_epsilons]

    if free_eps_names:
        # Slack mode: compute empirical epsilon
        slack = compute_slack(relation.consequent, bindings_dict, free_eps_names)
        if slack is None:
            return EvalResult(
                relation_name=relation.name,
                premises_hold=premises_hold,
                consequent_holds=None,
                slack=None,
                missing_metrics=["could not resolve consequent metrics"],
            )
        # Relation holds if all slacks are <= 0 (no violation)
        holds = all(v <= 1e-9 for v in slack.values())
        return EvalResult(
            relation_name=relation.name,
            premises_hold=premises_hold,
            consequent_holds=holds,
            slack=slack,
        )
    else:
        # Exact relation (no free epsilons): just True/False
        holds = eval_constraint(relation.consequent, bindings_dict)
        return EvalResult(
            relation_name=relation.name,
            premises_hold=premises_hold,
            consequent_holds=holds,
            slack=None,
            missing_metrics=["could not resolve consequent metrics"] if holds is None else [],
        )


def evaluate_corpus(
    relations: list[Relation],
    entity_bindings: list[EntityBinding],
) -> list[EvalResult]:
    """Evaluate all relations in a corpus against the same bindings."""
    return [evaluate_relation(r, entity_bindings) for r in relations]


# =============================================================================
# MAIN: DEMO AGAINST SAMPLE WORKLOADS
# =============================================================================

if __name__ == "__main__":
    import sys
    from corpus import ALL_RELATIONS

    sample_dir = Path(__file__).parent.parent / "sample_workloads"
    output_path = Path(__file__).parent / "eval_results.txt"

    stats_files = sorted(sample_dir.glob("*.txt"))
    if not stats_files:
        print(f"No stats files found in {sample_dir}")
        exit(1)

    CACHE_LEVELS = {
        "LLC": "board.cache_hierarchy.llcache",
    }

    lines: list[str] = []

    for stats_path in stats_files:
        stats = parse_stats_file(stats_path)
        lines.append(f"{'=' * 60}")
        lines.append(f"Stats file: {stats_path.name} ({len(stats)} stats parsed)")
        lines.append(f"{'=' * 60}")
        lines.append("")

        # Identify single-entity relations (evaluable from one stats file)
        single_entity = [r for r in ALL_RELATIONS if len(r.entities) == 1]
        multi_entity = [r for r in ALL_RELATIONS if len(r.entities) > 1]

        lines.append(f"--- Single-entity relations (evaluable per cache level) ---")
        lines.append("")

        for level_name, prefix in CACHE_LEVELS.items():
            lines.append(f"  [{level_name}] ({prefix})")
            for r in single_entity:
                ent_name = r.entities[0].name
                binding = EntityBinding(
                    entity_name=ent_name,
                    stats=stats,
                    component_prefix=prefix,
                )
                result = evaluate_relation(r, [binding])
                lines.append(f"    {repr(result)}")
            lines.append("")

        lines.append(f"--- Multi-entity relations (need separate runs) ---")
        lines.append("")
        for r in multi_entity:
            ent_names = ", ".join(e.name for e in r.entities)
            lines.append(f"  [{r.name}] entities: {ent_names}")
            lines.append(f"    NOT EVALUABLE from single stats file")
        lines.append("")
        lines.append("")

    output_text = "\n".join(lines)
    output_path.write_text(output_text)
    print(f"Results written to {output_path}")
