# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A framework for encoding falsifiable claims about cache replacement microarchitecture as structured ASTs. Relations like "larger cache implies higher hit rate" are expressed as typed expression trees that three backends consume: Z3 (symbolic consistency/counterexample search), direct evaluation (check against gem5 stats), and experiment generation (produce gem5 configs to test the claim).

## Running

```bash
# Print the full corpus summary (no dependencies beyond stdlib)
python3 corpus.py

# Evaluate corpus against gem5 stats, writes results to eval_results.txt
python3 eval_backend.py

# Generate experiment plans, writes to experiment_plans.txt
python3 experiment_gen.py

# Type-check (no mypy config exists yet, but the code uses type annotations)
python3 -c "import core; import corpus; import eval_backend; import experiment_gen"
```

No third-party dependencies. Z3 backend (planned) will require `z3-solver`. Sample gem5 stats live in `../sample_workloads/`.

## Architecture

**`core.py`** — The entire type system: `Entity`, `MetricKind` (enum of ~18 measurable quantities), expression AST (`Literal`, `MetricRef`, `Epsilon`, `BinOp`, `UnaryOp`), `Constraint`, logical connectives (`Conjunction`, `Disjunction`), and the top-level `Relation` dataclass. Also contains builder DSL functions (`entity()`, `metric()`, `lit()`, `eps()`, `constraint()`, `conj()`, `relation()`, `add/sub/mul/div`).

**`corpus.py`** — 16 relations organized in groups A–G, using the builder DSL. `ALL_RELATIONS` list at the bottom is the canonical collection. The `assert len(ALL_RELATIONS) == 16` guard at the bottom must be updated when adding/removing relations.

**`eval_backend.py`** — Evaluates relations against gem5 stats dumps. Parses stats files, resolves MetricKinds to gem5 stat formulas (with fallbacks like hits = accesses - misses), walks the AST. For relations with free epsilons, computes the minimum epsilon needed to make the relation true (0 = already holds, positive = needs that much tolerance). Exact relations (no epsilon) just report True/False. Single-entity relations evaluate from one stats file; multi-entity relations require separate simulation runs bound via `EntityBinding`. Outputs to `eval_results.txt`.

**`experiment_gen.py`** — Extracts structured experiment plans from relations by walking the AST. For each relation, reports: how many gem5 runs are needed, which parameters are shared (from EQ premises), which form sweep axes (from inequality premises), complex constraints (ratio relationships, literal thresholds) printed as-is, and which metrics to collect from each run. Does not generate concrete parameter values or gem5 configs — the user decides what to simulate. Outputs to `experiment_plans.txt`.

**`corpus.md`** — Human-readable LaTeX descriptions of each relation.

**`system-relations.md`** — Describes coupled epsilon systems (transitive chains, interference coupling) where multiple relations share latent variables.

## Key Design Constraints

- All AST types are **frozen dataclasses** — never mutate, always construct new instances.
- `Expr` is a union type (`Union[Literal, MetricRef, BinOp, UnaryOp, Epsilon]`). Backends use `isinstance` dispatch (Python 3.8 compatible).
- Epsilons are first-class AST nodes, not float constants. Each backend interprets them differently (Z3: existential variable to minimize; eval: minimum epsilon needed to make relation true, 0 if it already holds; experiment-gen: signals approximate relation).
- Entity `kind` field (`"cache"`, `"policy"`, `"workload"`, `"interval"`) determines which gem5 subsystem the experiment generator maps it to.
- `bindings` on a Relation pair entities (e.g., cache→policy) without polluting the AST with fake binding metrics.

## Adding a New Relation

1. Define entities and epsilon(s) at module level in `corpus.py`
2. Use the builder DSL to construct the `Relation`
3. Add it to `ALL_RELATIONS` and update the assert count
4. Document it in `corpus.md` with LaTeX notation

## Planned but Not Yet Implemented

- `z3_backend.py` — Lower Relations to Z3 ForAll/Implies formulas
