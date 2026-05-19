"""
Experiment generation: extract structured experiment plans from Relations.

For each relation in the corpus, inspects the AST to determine:
  - How many distinct gem5 configs are needed
  - Which parameters must be shared (EQ premises between entities)
  - Which parameters form sweep axes (inequality premises)
  - Which metrics to collect from each run
  - Any complex constraints (ratio relationships, literal thresholds)

Writes plans to experiment_plans.txt.

Usage:
    python3 experiment_gen.py
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from core import (
    Expr, Literal, MetricRef, BinOp, UnaryOp, Epsilon,
    Op, CmpOp, Constraint, Conjunction, Disjunction,
    Premise, Relation, MetricKind, Entity,
)
from corpus import ALL_RELATIONS


# =============================================================================
# PLAN DATASTRUCTURES
# =============================================================================

@dataclass
class SharedParam:
    """An EQ constraint between MetricRefs on different entities."""
    kind: MetricKind
    entities: list[str]

    def __repr__(self):
        return f"{self.kind.name}: {' == '.join(self.entities)}"


@dataclass
class SweepAxis:
    """An inequality constraint between MetricRefs on different entities."""
    kind: MetricKind
    higher: str
    lower: str
    op: CmpOp

    def __repr__(self):
        return f"{self.kind.name}: {self.higher} {self.op.value} {self.lower}"


@dataclass
class ComplexConstraint:
    """A premise constraint that isn't a simple MetricRef-to-MetricRef comparison."""
    text: str

    def __repr__(self):
        return self.text


@dataclass
class MetricToCollect:
    """A metric that must be scraped from a specific entity's run."""
    kind: MetricKind
    entity_name: str

    def __repr__(self):
        return f"{self.kind.name} from {self.entity_name}"


@dataclass
class ExperimentPlan:
    """Full experiment plan for a single relation."""
    relation_name: str
    configs: list[tuple[str, str]]  # (entity_name, entity_kind)
    bindings: list[tuple[str, str]]  # (cache_name, policy_name)
    shared: list[SharedParam]
    sweeps: list[SweepAxis]
    complex_constraints: list[ComplexConstraint]
    metrics: list[MetricToCollect]
    free_epsilons: list[str]
    domain: str


# =============================================================================
# AST WALKERS
# =============================================================================

def collect_metric_refs(expr: Expr) -> list[MetricRef]:
    """Recursively collect all MetricRef nodes from an expression."""
    match expr:
        case MetricRef() as m:
            return [m]
        case BinOp(left=l, right=r):
            return collect_metric_refs(l) + collect_metric_refs(r)
        case UnaryOp(operand=o):
            return collect_metric_refs(o)
        case Literal() | Epsilon():
            return []
    return []


def classify_constraint(c: Constraint) -> SharedParam | SweepAxis | ComplexConstraint:
    """Classify a premise constraint by its structure.

    - MetricRef == MetricRef on different entities → SharedParam
    - MetricRef >= MetricRef on different entities → SweepAxis
    - Anything else → ComplexConstraint
    """
    lhs_refs = collect_metric_refs(c.lhs)
    rhs_refs = collect_metric_refs(c.rhs)

    if (len(lhs_refs) == 1 and len(rhs_refs) == 1
            and isinstance(c.lhs, MetricRef) and isinstance(c.rhs, MetricRef)):
        lm = lhs_refs[0].metric
        rm = rhs_refs[0].metric
        if lm.entity.name != rm.entity.name and lm.kind == rm.kind:
            if c.op == CmpOp.EQ:
                return SharedParam(kind=lm.kind, entities=[lm.entity.name, rm.entity.name])
            elif c.op in (CmpOp.GE, CmpOp.GT):
                return SweepAxis(kind=lm.kind, higher=lm.entity.name, lower=rm.entity.name, op=c.op)
            elif c.op in (CmpOp.LE, CmpOp.LT):
                return SweepAxis(kind=lm.kind, higher=rm.entity.name, lower=lm.entity.name, op=c.op.flip)

    return ComplexConstraint(text=repr(c))


def extract_plan(relation: Relation) -> ExperimentPlan:
    """Extract an ExperimentPlan from a Relation by walking its AST."""
    configs = [(e.name, e.kind) for e in relation.entities]
    bindings = [(c.name, p.name) for c, p in relation.bindings]

    shared: list[SharedParam] = []
    sweeps: list[SweepAxis] = []
    complex_constraints: list[ComplexConstraint] = []

    for premise in relation.premises:
        constraints: list[Constraint] = []
        match premise:
            case Constraint() as c:
                constraints = [c]
            case Conjunction(constraints=cs):
                constraints = list(cs)
            case Disjunction(constraints=cs):
                constraints = list(cs)

        for c in constraints:
            result = classify_constraint(c)
            match result:
                case SharedParam() as s:
                    shared.append(s)
                case SweepAxis() as sw:
                    sweeps.append(sw)
                case ComplexConstraint() as cc:
                    complex_constraints.append(cc)

    # Merge shared params that reference the same MetricKind into one entry
    shared = _merge_shared(shared)

    # Collect metrics from the consequent
    refs = collect_metric_refs(relation.consequent.lhs) + collect_metric_refs(relation.consequent.rhs)
    seen: set[tuple[str, str]] = set()
    metrics: list[MetricToCollect] = []
    for ref in refs:
        key = (ref.metric.kind.name, ref.metric.entity.name)
        if key not in seen:
            seen.add(key)
            metrics.append(MetricToCollect(kind=ref.metric.kind, entity_name=ref.metric.entity.name))

    free_epsilons = [e.name for e in relation.free_epsilons]

    return ExperimentPlan(
        relation_name=relation.name,
        configs=configs,
        bindings=bindings,
        shared=shared,
        sweeps=sweeps,
        complex_constraints=complex_constraints,
        metrics=metrics,
        free_epsilons=free_epsilons,
        domain=relation.domain,
    )


def _merge_shared(params: list[SharedParam]) -> list[SharedParam]:
    """Merge SharedParams with the same MetricKind into one entry with all entities."""
    by_kind: dict[MetricKind, list[str]] = {}
    for p in params:
        if p.kind not in by_kind:
            by_kind[p.kind] = []
        for e in p.entities:
            if e not in by_kind[p.kind]:
                by_kind[p.kind].append(e)
    return [SharedParam(kind=k, entities=ents) for k, ents in by_kind.items()]


# =============================================================================
# FORMATTER
# =============================================================================

def format_plan(plan: ExperimentPlan) -> str:
    """Format an ExperimentPlan as human-readable text."""
    lines: list[str] = []
    sep = "=" * 72
    lines.append(sep)
    lines.append(f"  {plan.relation_name}")
    lines.append(sep)
    lines.append("")

    run_entities = [(n, k) for n, k in plan.configs if k in ("cache", "interval")]
    lines.append(f"Gem5 runs needed: {len(run_entities)}")
    for name, kind in run_entities:
        lines.append(f"  {name} ({kind})")
    binding_entities = [(n, k) for n, k in plan.configs if k not in ("cache", "interval")]
    if binding_entities:
        lines.append(f"Bound entities (not separate runs):")
        for name, kind in binding_entities:
            lines.append(f"  {name} ({kind})")
    lines.append("")

    if plan.bindings:
        lines.append("Bindings:")
        for cache, policy in plan.bindings:
            lines.append(f"  {cache} uses {policy}")
        lines.append("")

    lines.append("Shared (EQ constraints from premises):")
    if plan.shared:
        for s in plan.shared:
            lines.append(f"  {s}")
    else:
        lines.append("  [none]")
    lines.append("")

    lines.append("Sweep axes (inequality constraints):")
    if plan.sweeps:
        for sw in plan.sweeps:
            lines.append(f"  {sw}")
    else:
        lines.append("  [none]")
    lines.append("")

    if plan.complex_constraints:
        lines.append("Other premise constraints:")
        for cc in plan.complex_constraints:
            lines.append(f"  {cc}")
        lines.append("")

    lines.append("Metrics to collect:")
    for m in plan.metrics:
        lines.append(f"  {m}")
    lines.append("")

    if plan.free_epsilons:
        eps_str = ", ".join(f"ε_{e}" for e in plan.free_epsilons)
        lines.append(f"Free epsilons: {eps_str} (computed by eval_backend)")
    else:
        lines.append("Free epsilons: [none — exact relation]")
    lines.append("")

    lines.append(f"Domain: {plan.domain}")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    output_path = Path(__file__).parent / "experiment_plans.txt"

    lines: list[str] = []
    lines.append(f"Experiment Plans for {len(ALL_RELATIONS)} relations")
    lines.append(f"{'=' * 72}")
    lines.append("")

    multi = [r for r in ALL_RELATIONS if len(r.entities) > 1]
    single = [r for r in ALL_RELATIONS if len(r.entities) <= 1]

    lines.append(f"Multi-entity (need separate gem5 runs): {len(multi)}")
    lines.append(f"Single-entity (one stats file suffices): {len(single)}")
    lines.append("")
    lines.append("")

    for r in ALL_RELATIONS:
        plan = extract_plan(r)
        lines.append(format_plan(plan))

    output_text = "\n".join(lines)
    output_path.write_text(output_text)
    print(f"Plans written to {output_path}")
