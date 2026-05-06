"""
Facts & Relations: A representation for microarchitectural cache replacement claims.

================================================================================
OVERVIEW
================================================================================

This module defines an expression AST and a Relation type that together let us
state claims like:

    "If cache A has hit rate >= cache B's hit rate, then A's stall cycles
     are <= B's stall cycles, up to some tolerance epsilon."

These claims arise from cache replacement research — some are well-known
(monotone inclusion for LRU), some are folklore, some are hypotheses we want
to confirm or refute. The representation is designed to support three workflows:

================================================================================
WORKFLOW 1: SMT SOLVING (Z3)
================================================================================

Purpose:
    Check whether a set of relations is mutually consistent, derive implied
    bounds, or find counterexamples symbolically.

How it works:
    Each Relation lowers to a Z3 ForAll/Implies formula. MetricRefs become
    uninterpreted Real-sorted constants (or functions if parameterized).
    Epsilons become existentially quantified Reals with sign constraints.
    The Z3 backend (see z3_backend.py) walks the AST and builds z3.ExprRef
    trees.

Example use:
    - "Given relations R1..R5, is there a configuration where all premises
       hold but a consequent is violated?" (counterexample search)
    - "What is the tightest epsilon_1 such that R1 is satisfiable alongside
       R2?" (bound tightening)

================================================================================
WORKFLOW 2: DIRECT EVALUATION AGAINST GEM5 STATS
================================================================================

Purpose:
    Given a gem5 stats.txt (or parsed stat dictionary), check whether a
    relation holds empirically for that particular simulation run.

How it works:
    A Relation with no free epsilons (or with epsilons set to 0) is just a
    predicate: plug in measured values and get True/False. The eval backend
    (see eval_backend.py) takes a dict mapping (MetricKind, entity_name) ->
    float, substitutes into the AST, and evaluates.

    For relations WITH free epsilons, evaluation computes the "slack": the
    minimum epsilon that would make the consequent hold. This tells us how
    tight the bound is empirically.

Example use:
    - Run gem5 with two replacement policies on SPEC2017 benchmarks.
    - Parse stats.txt to get hit rates and stall counts.
    - Evaluate each relation: does it hold? What's the empirical slack?
    - Flag relations that are violated (potential counterexamples or
      relations that need stronger preconditions).

================================================================================
WORKFLOW 3: EXPERIMENT GENERATION FROM RELATIONS
================================================================================

Purpose:
    Given a Relation, automatically determine what gem5 configurations must
    be simulated to test it, then emit runnable gem5 config scripts.

How it works:
    A Relation's entities and premises describe the experimental conditions.
    The experiment generator inspects:

    1. ENTITIES — what distinct cache/system configurations are needed.
       e.g., if the relation talks about LLC_a and LLC_b with different
       associativities, we need at least two gem5 runs with those configs.

    2. PREMISES (parameter constraints) — what parameter values to set.
       Equality constraints (Size[LLC_a] == Size[LLC_b]) become shared
       config values. Inequality constraints (Assoc[LLC_a] >= Assoc[LLC_b])
       become parameterized sweeps.

    3. METRICS referenced in the consequent — which gem5 stats to collect.
       The generator maps MetricKind -> gem5 stat names (e.g.,
       HIT_RATE -> "system.l2.overallHits::total / system.l2.overallAccesses::total").

    4. WORKLOAD binding — relations are universal over workloads unless
       the domain restricts them. The generator crosses the config matrix
       with a workload suite (e.g., SPEC2017, GAP, PARSEC).

    Output is a list of gem5 run specifications:
        - Python config script (or config.ini overrides)
        - Expected stat paths to scrape
        - Which relation(s) this run is evidence for/against

    This closes the loop: define a relation -> generate experiments ->
    run gem5 -> collect stats -> evaluate relation -> refine.

================================================================================
DESIGN PRINCIPLES
================================================================================

Frozen dataclasses everywhere:
    Relations and expressions are immutable values. This makes them hashable
    (can be set members, dict keys), safe to share across threads, and
    trivially serializable. Transform by constructing new instances.

Expression AST rather than strings:
    Structured trees let each backend (Z3, eval, experiment-gen) walk the
    same representation without parsing. Adding a new backend means writing
    one recursive visitor.

Epsilons as first-class nodes:
    Tolerance terms aren't just floats jammed into the inequality — they're
    named, tracked, and can be solved for. This matters because:
    - In Z3 mode, they're existential variables we minimize.
    - In eval mode, they're the computed slack.
    - In experiment-gen mode, their presence signals that the relation is
      approximate (not exact), which affects how many data points we need
      to estimate the bound.

Entities carry a "kind" tag:
    A relation might involve entities of different kinds (a cache config
    AND a workload AND a replacement policy). The kind tag lets the
    experiment generator know which gem5 knobs each entity maps to:
    - kind="cache" -> cache size, associativity, latency params
    - kind="policy" -> replacement policy class, policy-specific params
    - kind="workload" -> benchmark binary, input set, simpoint

================================================================================
WHAT A RELATION CAPTURES
================================================================================

Formally, a Relation is:

    ∀ entities satisfying premises: consequent holds (within epsilon)

The premises are a conjunction (AND) of constraints over entity parameters
and metrics. The consequent is a single constraint (possibly involving
multiple metrics and epsilons).

Crucially, a Relation is NOT:
    - A simulation result (that's a data point against which we evaluate)
    - A gem5 config (that's what the experiment generator produces)
    - A proof (that's what Z3 attempts or refutes)

It IS a falsifiable claim about the cache replacement design space.

================================================================================
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Union, Optional
import json


# =============================================================================
# ENTITIES
# =============================================================================
# An entity is a named participant in a relation. It represents an abstract
# configuration point — NOT a concrete parameter setting. The premises of the
# relation constrain entity parameters; the consequent makes claims about
# entity metrics.
#
# For experiment generation: each distinct entity in a relation corresponds
# to a distinct gem5 simulation run (or a distinct configuration within a
# multi-config run). The entity's "kind" determines which gem5 subsystem
# it maps to.

@dataclass(frozen=True)
class Entity:
    name: str
    kind: str  # "cache", "policy", "workload", "core", "memory"

    def __repr__(self):
        return f"{self.kind}:{self.name}"


# =============================================================================
# METRICS
# =============================================================================
# A metric is a quantity we can measure from gem5 stats or constrain
# symbolically. Each MetricKind will eventually map to:
#   - A gem5 stat expression (for collection)
#   - A Z3 sort (Real for rates/latencies, Int for counts)
#   - Value bounds (hit rate in [0,1], size > 0, etc.)

class MetricKind(Enum):
    HIT_RATE = auto()           # overall hit rate (hits / accesses)
    CRITICAL_HIT_RATE = auto()  # hit rate weighted by criticality (on critical path)
    STALLS = auto()             # pipeline stall cycles due to cache misses
    SIZE = auto()               # cache size in bytes (a design parameter, not measured)
    ASSOCIATIVITY = auto()      # set associativity (ways)
    LATENCY = auto()            # access latency in cycles
    MISS_RATE = auto()          # 1 - hit_rate (redundant but common in literature)
    IPC = auto()                # instructions per cycle (whole-core metric)
    BANDWIDTH = auto()          # memory bandwidth consumed (bytes/cycle)
    EVICTIONS = auto()          # number of evictions over a trace window

    def __repr__(self):
        return self.name


@dataclass(frozen=True)
class Metric:
    """A measurable quantity bound to a specific entity."""
    kind: MetricKind
    entity: Entity

    def __repr__(self):
        return f"{self.kind.name}[{self.entity}]"


# =============================================================================
# EXPRESSION AST
# =============================================================================
# The expression tree is the core of the representation. Every numeric
# quantity in a relation — whether a measured stat, a design parameter,
# a literal constant, or a tolerance — is an Expr node.
#
# Backends traverse this tree:
#   - Z3 backend: Expr -> z3.ArithRef
#   - Eval backend: Expr -> float (given a stat dict)
#   - Printer: Expr -> LaTeX or Unicode string

class Op(Enum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    NEG = "neg"


class CmpOp(Enum):
    """Comparison operators for constraints."""
    GE = ">="
    GT = ">"
    LE = "<="
    LT = "<"
    EQ = "=="
    NE = "!="

    @property
    def flip(self) -> CmpOp:
        """Swap sides: (a >= b) becomes (b <= a)."""
        return {
            CmpOp.GE: CmpOp.LE, CmpOp.LE: CmpOp.GE,
            CmpOp.GT: CmpOp.LT, CmpOp.LT: CmpOp.GT,
            CmpOp.EQ: CmpOp.EQ, CmpOp.NE: CmpOp.NE,
        }[self]


# The Expr union type. Each variant is a leaf or interior node.
Expr = Union["Literal", "MetricRef", "BinOp", "UnaryOp", "Epsilon"]


@dataclass(frozen=True)
class Literal:
    """A concrete numeric constant (e.g., 2, 0.5, 1024)."""
    value: float

    def __repr__(self):
        return str(self.value)


@dataclass(frozen=True)
class MetricRef:
    """
    A reference to a metric on an entity.

    In eval mode: resolved to a float by looking up (metric.kind, metric.entity.name)
    in the stats dictionary.

    In Z3 mode: becomes a Real-sorted constant named like "HIT_RATE_LLC_a".

    In experiment-gen mode: signals that this metric must be collected for
    the entity's corresponding gem5 run.
    """
    metric: Metric

    def __repr__(self):
        return repr(self.metric)


@dataclass(frozen=True)
class Epsilon:
    """
    A named tolerance/slack term.

    Epsilons represent the "wiggle room" in approximate relations. They serve
    different roles depending on the backend:

    Z3 mode:
        An existentially quantified Real. We typically ask Z3 to minimize it,
        giving us the tightest bound consistent with other constraints.

    Eval mode:
        Computed as the residual slack. If the consequent says
        "Stalls[a] <= Stalls[b] + epsilon", evaluation computes
        epsilon = Stalls[a] - Stalls[b]. If negative, the relation holds
        with room to spare; if positive, epsilon is the violation magnitude.

    Experiment-gen mode:
        A nonzero epsilon signals the relation is approximate, meaning we
        need multiple workloads/configs to estimate the bound distribution
        rather than checking a single point.
    """
    name: str

    def __repr__(self):
        return f"ε_{self.name}"


@dataclass(frozen=True)
class BinOp:
    """Binary arithmetic operation on two sub-expressions."""
    op: Op
    left: Expr
    right: Expr

    def __repr__(self):
        return f"({self.left} {self.op.value} {self.right})"


@dataclass(frozen=True)
class UnaryOp:
    """Unary operation (currently just negation)."""
    op: Op
    operand: Expr

    def __repr__(self):
        return f"({self.op.value} {self.operand})"


# =============================================================================
# CONSTRAINTS
# =============================================================================
# A constraint is the atomic unit of logical content: "this expression
# relates to that expression by this comparison." Premises and consequents
# are both built from constraints.

@dataclass(frozen=True)
class Constraint:
    """
    A comparison: lhs op rhs.

    Examples:
        HIT_RATE[LLC_a] >= HIT_RATE[LLC_b]
        STALLS[policy_x] <= STALLS[policy_y] + ε_1
        SIZE[LLC_a] == SIZE[LLC_b]
    """
    lhs: Expr
    op: CmpOp
    rhs: Expr

    def __repr__(self):
        return f"{self.lhs} {self.op.value} {self.rhs}"


# =============================================================================
# LOGICAL CONNECTIVES
# =============================================================================
# Premises can be single constraints or compound (conjunction/disjunction).
# In practice, most premises are conjunctions (all conditions must hold).
# Disjunctions appear in case-split relations ("either A or B implies C").

class LogicOp(Enum):
    AND = "∧"
    OR = "∨"


@dataclass(frozen=True)
class Conjunction:
    """All constraints must hold simultaneously."""
    constraints: tuple[Constraint, ...]

    def __repr__(self):
        return " ∧ ".join(repr(c) for c in self.constraints)


@dataclass(frozen=True)
class Disjunction:
    """At least one constraint must hold."""
    constraints: tuple[Constraint, ...]

    def __repr__(self):
        return " ∨ ".join(repr(c) for c in self.constraints)


Premise = Union[Constraint, Conjunction, Disjunction]


# =============================================================================
# RELATION
# =============================================================================

@dataclass(frozen=True)
class Relation:
    """
    A falsifiable claim about the cache replacement design space.

    Semantics:
        For all assignments to entities satisfying `premises`,
        `consequent` holds (possibly within free_epsilons tolerance).

    Fields:
        name:           Human-readable identifier for this relation.
        premises:       Tuple of conditions that must hold for the claim
                        to apply. These constrain entity parameters (size,
                        associativity) or establish orderings (HR[a] >= HR[b]).
        consequent:     The claimed consequence — a single Constraint.
        entities:       All entities referenced. The experiment generator
                        creates one gem5 config per entity.
        free_epsilons:  Tolerance terms whose values are unknown. In eval
                        mode these become measured slack; in Z3 mode they
                        become variables to solve for.
        source:         Provenance — "folklore", "doi:10.1145/...", "hypothesis".
        domain:         Applicability conditions that aren't formal premises
                        but restrict when this relation is meaningful
                        (e.g., "LRU-family policies", "SPEC-like workloads").

    Experiment generation contract:
        Given a Relation, the experiment generator must produce a set of
        gem5 runs such that:
          (a) Each entity maps to at least one run.
          (b) Premise equality constraints (SIZE[a] == SIZE[b]) are encoded
              as shared config values across runs.
          (c) Premise inequality constraints define the sweep space.
          (d) All MetricRefs in the consequent are mapped to gem5 stats
              that will be collected.
        The generator returns a list of ExperimentSpec objects (see
        experiment_gen.py) which can be serialized to gem5 Python configs.
    """
    name: str
    premises: tuple[Premise, ...]
    consequent: Constraint
    entities: tuple[Entity, ...] = field(default_factory=tuple)
    free_epsilons: tuple[Epsilon, ...] = field(default_factory=tuple)
    source: str = ""
    domain: str = ""

    def __repr__(self):
        prems = " ∧ ".join(repr(p) for p in self.premises)
        return f"[{self.name}] {prems} ⇒ {self.consequent}"


# =============================================================================
# BUILDER HELPERS
# =============================================================================
# These functions provide a concise DSL for constructing relations without
# the verbosity of direct dataclass instantiation. The intent is that
# encoding a new relation from a paper or hypothesis reads almost like
# the mathematical statement.

def entity(name: str, kind: str = "cache") -> Entity:
    return Entity(name, kind)


def metric(kind: MetricKind, ent: Entity) -> MetricRef:
    return MetricRef(Metric(kind, ent))


def lit(value: float) -> Literal:
    return Literal(value)


def eps(name: str) -> Epsilon:
    return Epsilon(name)


def constraint(lhs: Expr, op: CmpOp, rhs: Expr) -> Constraint:
    return Constraint(lhs, op, rhs)


def conj(*cs: Constraint) -> Conjunction:
    return Conjunction(cs)


def relation(
    name: str,
    premises: list[Premise],
    consequent: Constraint,
    entities: list[Entity] | None = None,
    free_epsilons: list[Epsilon] | None = None,
    source: str = "",
    domain: str = "",
) -> Relation:
    ents = tuple(entities) if entities else ()
    epss = tuple(free_epsilons) if free_epsilons else ()
    return Relation(name, tuple(premises), consequent, ents, epss, source, domain)


# Arithmetic combinators for building Expr trees
def add(a: Expr, b: Expr) -> BinOp:
    return BinOp(Op.ADD, a, b)

def sub(a: Expr, b: Expr) -> BinOp:
    return BinOp(Op.SUB, a, b)

def mul(a: Expr, b: Expr) -> BinOp:
    return BinOp(Op.MUL, a, b)

def div(a: Expr, b: Expr) -> BinOp:
    return BinOp(Op.DIV, a, b)
