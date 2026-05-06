# Facts & Relations: Architecture

## What This Is

A framework for stating, testing, and reasoning about **falsifiable claims** in cache replacement microarchitecture. A "relation" is a mathematical implication — e.g., "if policy A has a higher hit rate than policy B on the same workload, then A causes fewer stall cycles (within tolerance ε)."

These relations come from textbook results, folklore, papers, and hypotheses we want to confirm or disprove via simulation.

## The Core Abstraction

Everything centers on one type: **`Relation`**.

```
∀ entities satisfying premises ⇒ consequent holds (within ε)
```

A Relation is built from:

| Component | What it is | Example |
|-----------|-----------|---------|
| **Entity** | A named participant (cache config, policy, workload) | `LLC_a`, `policy_x` |
| **MetricRef** | A measurable quantity on an entity | `HIT_RATE[LLC_a]` |
| **Constraint** | A comparison between expressions | `HR[a] >= HR[b]` |
| **Epsilon** | A named tolerance term (unknown, to be bounded) | `ε_1` |
| **Premise** | Condition(s) under which the claim applies | `Size[a] == Size[b]` |
| **Consequent** | The claimed outcome | `Stalls[a] <= Stalls[b] + ε_1` |

All of these are **immutable, frozen dataclasses** forming an expression AST.

## Three Backends, One Representation

The whole point of a structured AST (rather than strings or ad-hoc Python) is that multiple backends can consume the same `Relation` object:

```
                    ┌──────────────────────┐
                    │      Relation        │
                    │  (premises ⇒ cons.)  │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
     ┌────────────────┐  ┌──────────┐  ┌───────────────────┐
     │  Z3 Backend    │  │   Eval   │  │  Experiment Gen   │
     │                │  │ Backend  │  │                   │
     │ Symbolic       │  │          │  │ gem5 config       │
     │ consistency,   │  │ Check    │  │ generation,       │
     │ bound          │  │ against  │  │ sweep space       │
     │ tightening,    │  │ stats.txt│  │ derivation        │
     │ counterexample │  │          │  │                   │
     └────────────────┘  └──────────┘  └───────────────────┘
```

### Backend 1: Z3 (SMT Solving)

**Purpose:** Symbolic reasoning over the relation corpus.

**What it does:**
- Lowers each `Relation` to a Z3 `ForAll/Implies` formula
- `MetricRef` → Z3 Real-sorted constant (e.g., `z3.Real("HIT_RATE_LLC_a")`)
- `Epsilon` → existentially quantified Real (minimized for tight bounds)
- `Constraint` → Z3 comparison expression
- `Conjunction` → `z3.And(...)`

**Use cases:**
- Mutual consistency: "Are relations R1–R5 simultaneously satisfiable?"
- Counterexample search: "Is there a valid configuration where R3's consequent fails?"
- Bound tightening: "What's the minimum ε_1 such that R1 is satisfiable alongside R2?"
- Derived relations: "Given R1 and R3, does R7 follow?"

### Backend 2: Direct Evaluation

**Purpose:** Check relations against concrete gem5 simulation data.

**What it does:**
- Takes a stat dictionary: `{(MetricKind, entity_name): float}`
- Walks the AST, substituting measured values for `MetricRef` nodes
- Returns `True`/`False` for epsilon-free relations
- For relations with epsilons: computes the **empirical slack** (minimum ε that makes the consequent hold)

**Use cases:**
- Validation: "Does this relation hold on all SPEC2017 benchmarks?"
- Slack analysis: "Across 30 workloads, what's the distribution of ε_1?"
- Violation detection: "Which relations break on streaming workloads?"

### Backend 3: Experiment Generation

**Purpose:** Given a relation, produce the gem5 simulation runs needed to test it.

**What it does:**

1. **Entity extraction** — each entity becomes a distinct gem5 configuration (or run). Entity `kind` determines which gem5 subsystem to configure:
   - `kind="cache"` → cache size, associativity, replacement policy, latency
   - `kind="policy"` → replacement policy class + policy-specific knobs
   - `kind="workload"` → binary, input set, SimPoint, warm-up length

2. **Constraint interpretation** — premises tell us how to set parameters:
   - `EQ` constraints → shared parameter values across runs (e.g., same size)
   - Structural constraints (like `2 * Assoc[a] == Assoc[b]`) → parameterized sweep with linked values
   - `GE`/`LE` constraints → define which direction to sweep

3. **Metric mapping** — `MetricKind` → gem5 stat paths:
   - `HIT_RATE` → `system.l2.overallHits::total / system.l2.overallAccesses::total`
   - `STALLS` → `system.cpu.numCycles - system.cpu.committedInsts` (or similar)
   - `ASSOCIATIVITY` → config parameter, not a stat (set, not collected)

4. **Workload crossing** — unless the `domain` restricts to specific benchmarks, the generator crosses the config matrix with a full workload suite.

5. **Output** — a list of `ExperimentSpec` objects:
   ```python
   @dataclass
   class ExperimentSpec:
       gem5_config_overrides: dict    # config.ini keys to set
       stats_to_collect: list[str]    # gem5 stat paths to scrape
       relation_name: str             # which relation this tests
       entity_binding: dict           # entity_name -> config values
   ```

**The closed loop:**

```
Define Relation → Generate Experiments → Run gem5 → Collect Stats → Evaluate → Refine
       │                                                                          │
       └──────────────────────────────────────────────────────────────────────────┘
```

## Expression AST Nodes

```
Expr (union type)
├── Literal        — concrete float (2.0, 1024, 0.5)
├── MetricRef      — measured quantity on an entity
├── Epsilon        — named tolerance term
├── BinOp          — binary arithmetic (+, -, *, /)
└── UnaryOp        — unary (negation)
```

Each node is frozen/hashable. Backends walk the tree recursively — adding a new backend means writing one visitor function.

## Why Epsilons Are First-Class

Epsilons aren't just floats baked into inequalities. They're named AST nodes because each backend treats them differently:

| Backend | Epsilon semantics |
|---------|-------------------|
| Z3 | Existentially quantified variable to minimize |
| Eval | Computed slack (how much the data violates/satisfies) |
| Experiment Gen | Signals approximate relation → need more data points |

A relation with `free_epsilons=[]` is an exact claim (e.g., inclusion property for LRU). One with epsilons is approximate — the empirical question is "how small can ε be?"

## File Layout

```
facts_relations/
├── ARCHITECTURE.md      ← you are here
├── core.py              ← AST types, Relation, builder DSL
├── examples.py          ← the four initial relations encoded
├── z3_backend.py        ← (planned) lower Relations to Z3
├── eval_backend.py      ← (planned) evaluate against stat dicts
└── experiment_gen.py    ← (planned) derive gem5 configs from Relations
```

## Design Decisions

**Why frozen dataclasses, not mutable objects?**
Relations are values. You compare them, hash them, store them in sets. Immutability prevents accidental mutation and makes serialization trivial.

**Why a union-type AST, not class inheritance?**
Pattern matching (Python 3.10+ `match`) and explicit exhaustiveness. Each backend must handle every variant — no silent fallthrough.

**Why not just Z3 directly?**
Z3's API is great for solving but poor for:
- Attaching metadata (source, domain restrictions)
- Generating experiments (Z3 doesn't know what gem5 is)
- Evaluating against data (Z3 is symbolic, not numeric)

The AST is the shared language; Z3 is one consumer of it.

**Why not a logic programming language (Prolog, Datalog)?**
Integration cost. We need to call Z3, parse gem5 output, generate Python configs, and eventually do statistical analysis. Python is the glue language for all of these. The builder DSL gives us declarative syntax without leaving the Python ecosystem.

## What Counts as a Relation

A Relation IS:
- A falsifiable mathematical claim about the cache design space
- Universal (for-all) over its entities, given premises hold
- Testable via simulation (we can generate the experiments)
- Optionally approximate (with named epsilon slack)

A Relation is NOT:
- A simulation result (that's data to evaluate against)
- A gem5 config (that's what experiment gen produces)
- A proof (that's what Z3 attempts)
- A performance model (relations are qualitative orderings, not quantitative functions)

## Next Steps

1. **Z3 backend** — lower the four example relations and check mutual consistency
2. **Eval backend** — parse gem5 stats.txt and evaluate relations
3. **Experiment generator** — produce gem5 configs from relation structure
4. **Corpus expansion** — encode more relations from the literature and hypothesis generation
5. **Counterfactual studies** — once the pipeline works, ask "what if" questions by modifying premises
