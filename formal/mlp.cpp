/*
 * Bounded Model Checking engine for the "more MLP is always better" dogma.
 * Unrolls N memory requests on two machines (System_HighMLP, System_LowMLP) that
 * share the same synthesized workload but differ only in MSHR window W.
 * Seeks workloads where T_HighMLP > T_LowMLP by modeling pipelined finite bandwidth,
 * convex queueing delay, admission backpressure, R/W turnaround, and
 * wrong-path speculation waste.
 *
 * Build: g++ -std=c++23 mlp.cpp -lz3 -o mlp -O3 -march=native
 */

#include <z3++.h>
#include <vector>
#include <string>
#include <iostream>
#include <iomanip>

using z3::expr;
using z3::context;
using z3::solver;

// ---------------------------------------------------------------------------
// Tunable parameters of the unrolled model.
// ---------------------------------------------------------------------------
namespace cfg
{
  constexpr int N             = 12;   // Unroll depth (number of memory requests).
  constexpr int S             = 2;    // Hardware threads -> number of distinct streams.
  constexpr int B             = 10;   // Memory access latency per request (cycles).
  constexpr int ROB_SIZE      = 4;    // Reorder-buffer horizon: deps span <= ROB_SIZE.
  constexpr int MAX_STREAM_MLP= 3;    // LSQ: max concurrently-independent reqs per stream.
  constexpr int HORIZON       = 64;   // Upper bound for synthesized arrival times.

  // ---- Pipelined finite-bandwidth channel (shared physics, both machines) --
  constexpr int G             = 2;    // Channel inter-admission gap (1/bandwidth), G < B.
  constexpr int TT            = 4;    // Read/write bus turnaround bubble (direction switch).

  /* Convex queueing-delay curve. First C concurrently-in-flight requests are free,
   * past C each costs PEN_LO, past the steeper knee C2 each costs PEN_HI more.
   * C = B/G is NOT hand-picked: it is the bandwidth-delay product, the number of
   * requests the channel keeps busy for free. C2 := C+2. Change C by changing
   * B/G, not by typing a magic number. */
  constexpr int C             = (B / G) > 0 ? (B / G) : 1;
  constexpr int C2            = C + 2;
  constexpr int PEN_LO        = 3;
  constexpr int PEN_HI        = 5;

  /* Contention master switch. 1 (default) keeps the full inflight→Pen chain
   * (convex queueing + admission backpressure). 0 forces Pen≡0, removing all
   * queueing contention: the channel reduces to a pure pipelined bus (G admission
   * gap + TT turnaround) + MSHR gating + speculation. This isolates wrong-path
   * speculation as the sole anti-MLP mechanism. Override:
   * g++ -DCFG_CONTENTION=0 ... */
#ifndef CFG_CONTENTION
#define CFG_CONTENTION 1
#endif
  constexpr int CONTENTION    = CFG_CONTENTION;

  constexpr int W_HIGH        = 6;   // System_HighMLP (aggressive).
  constexpr int W_LOW         = 2;   // System_LowMLP (throttled).

  /* Wrong-path speculation (Strategy B). Mispredicted branch BR → shadow of wrong-path
   * requests after it. Shadow is shared workload (both machines); issue depth emerges
   * from St[j] < R only. Wide window issues deeper → wastes bus/MSHR/queue slots on
   * shadow, delays correct-path tail. T counts correct-path completions only.
   * SPEC=0 → strict-generalization guard, recovers old model exactly. */
#ifndef CFG_SPEC
#define CFG_SPEC 1
#endif
  constexpr int SPEC          = CFG_SPEC;
  constexpr int MAX_SHADOW    = 4;   // Shadow-length cap (logged if binds).
  constexpr int RESOLVE_DELAY = 0;   // R = E[BR] + this; 0 = resolve at condition-load.

  static_assert(G < B, "channel must pipeline: admission gap G must be < B");
  static_assert(C < C2, "convex knee C2 must lie beyond the free regime C");
}

// ---------------------------------------------------------------------------
// Small helpers.
// ---------------------------------------------------------------------------
static inline expr
zmax(const expr& a, const expr& b)
{
  return z3::ite(a >= b, a, b);
}

static inline expr
b2i(context& c, const expr& b)
{
  return z3::ite(b, c.int_val(1), c.int_val(0));
}

struct Timeline
{
  std::vector<expr> Aeff;     // effective arrival (causality)
  std::vector<expr> Aprime;   // channel presentation (MSHR gating)
  std::vector<expr> Cf;       // channel-free time (skips non-live shadow)
  std::vector<expr> St;       // service start (admission + turnaround)
  std::vector<expr> Live;     // reaches bus? (bool)
  std::vector<expr> Inflight; // earlier requests still in service
  std::vector<expr> Pen;      // convex queueing penalty
  std::vector<expr> E;        // service end
  std::vector<expr> Rel;      // MSHR release time (E or R if squashed)
  expr              R;        // branch-resolve cycle
  expr              T;        // system cycles (max correct-path E)
  explicit Timeline(context& c) : R(c.int_val(0)), T(c.int_val(0)) {}
};

// Build per-machine timeline (W-parameterized on shared workload).
static Timeline
build_machine(context& c, solver& sol,
              const std::string& tag, int W,
              const std::vector<std::vector<expr>>& Dep,
              const std::vector<expr>& A,
              const std::vector<expr>& RW,
              const expr& BR,
              const std::vector<expr>& Sq)
{
  using namespace cfg;
  Timeline tl(c);
  tl.Aeff.reserve(N); tl.Aprime.reserve(N); tl.Cf.reserve(N); tl.St.reserve(N);
  tl.Live.reserve(N); tl.Inflight.reserve(N); tl.Pen.reserve(N);
  tl.E.reserve(N); tl.Rel.reserve(N);

  // Branch-resolve cycle R = E[BR] + RESOLVE_DELAY (pinned after E is built)
  expr R = c.int_const(("R_" + tag).c_str());
  tl.R = R;

  for (int j = 0; j < N; ++j)
    {
      // Axiom 1: Causality — Aeff[j] = max(A[j], max over dependencies)
      expr aeff_rhs = A[j];
      for (int i = 0; i < j; ++i)
        aeff_rhs = z3::ite(Dep[i][j], zmax(aeff_rhs, tl.E[i] + 1), aeff_rhs);
      expr Aeff_j = c.int_const(("Aeff_" + tag + "_" + std::to_string(j)).c_str());
      sol.add(Aeff_j == aeff_rhs);
      tl.Aeff.push_back(Aeff_j);

      // Axiom 2: MSHR gating — A'[j] = max(Aeff[j], Rel[j-W])
      expr Aprime_j = c.int_const(("Aprime_" + tag + "_" + std::to_string(j)).c_str());
      if (j - W >= 0)
        sol.add(Aprime_j == zmax(Aeff_j, tl.Rel[j - W]));
      else
        sol.add(Aprime_j == Aeff_j);
      tl.Aprime.push_back(Aprime_j);

      // Axiom 3: Pipelined admission + backpressure + turnaround
      // St[j] = max(A'[j], St[j-1] + G + TT*switch + Pen[j-1])
      expr Cf_j = c.int_const(("Cf_" + tag + "_" + std::to_string(j)).c_str());
      expr St_j = c.int_const(("St_" + tag + "_" + std::to_string(j)).c_str());
      if (j == 0) {
        sol.add(Cf_j == Aprime_j);
        sol.add(St_j == Aprime_j);
      }
      else
        {
          expr sw = b2i(c, RW[j] != RW[j - 1]);
          expr gap = c.int_val(G) + c.int_val(TT) * sw + tl.Pen[j - 1];
          sol.add(Cf_j == z3::ite(tl.Live[j - 1], tl.St[j - 1] + gap, tl.Cf[j - 1]));
          sol.add(St_j == zmax(Aprime_j, Cf_j));
        }
      tl.Cf.push_back(Cf_j);
      tl.St.push_back(St_j);

      // Bus liveness: Live[j] = ¬Sq[j] ∨ (St[j] < R)
      expr Live_j = c.bool_const(("Live_" + tag + "_" + std::to_string(j)).c_str());
      sol.add(Live_j == (!Sq[j] || (St_j < R)));
      tl.Live.push_back(Live_j);

      // Inflight: count earlier live requests still serving when j starts.
      // With contention off there is no queueing, so the count is inert (0).
      expr inflight = c.int_val(0);
      if (CONTENTION) {
        for (int i = 0; i < j; ++i)
          inflight = inflight + b2i(c, tl.Live[i] && (tl.E[i] > St_j));
      }
      expr Inflight_j = c.int_const(("Inflight_" + tag + "_" + std::to_string(j)).c_str());
      sol.add(Inflight_j == inflight);
      tl.Inflight.push_back(Inflight_j);

      // Convex penalty: Pen[j] = PEN_LO*max(0,inflight-C) + PEN_HI*max(0,inflight-C2).
      // CONTENTION=0 forces Pen≡0, dropping the queueing chain entirely: Pen=0 falls
      // out of both E[j] (completion) and the backpressure gap (admission).
      expr Pen_j = c.int_const(("Pen_" + tag + "_" + std::to_string(j)).c_str());
      if (CONTENTION) {
        expr over1 = zmax(c.int_val(0), Inflight_j - c.int_val(C));
        expr over2 = zmax(c.int_val(0), Inflight_j - c.int_val(C2));
        sol.add(Pen_j == c.int_val(PEN_LO) * over1 + c.int_val(PEN_HI) * over2);
      } else
        sol.add(Pen_j == c.int_val(0));
      tl.Pen.push_back(Pen_j);

      // Service end: E[j] = St[j] + B + Pen[j]
      expr E_j = c.int_const(("E_" + tag + "_" + std::to_string(j)).c_str());
      sol.add(E_j == St_j + c.int_val(B) + Pen_j);
      tl.E.push_back(E_j);

      // MSHR release: Rel[j] = (Sq[j] ∧ ¬Live[j]) ? R : E[j]
      expr Rel_j = c.int_const(("Rel_" + tag + "_" + std::to_string(j)).c_str());
      sol.add(Rel_j == z3::ite(Sq[j] && !Live_j, R, E_j));
      tl.Rel.push_back(Rel_j);
    }

  // Pin R = E[BR] + RESOLVE_DELAY
  expr r_sel = c.int_val(0);
  for (int i = 0; i < N; ++i)
    r_sel = z3::ite(BR == i, tl.E[i], r_sel);
  sol.add(R == r_sel + c.int_val(RESOLVE_DELAY));

  // Axiom 4: Timeline T = max E[j] over correct-path requests
  expr tmax = c.int_val(0);
  for (int j = 0; j < N; ++j)
    tmax = zmax(tmax, z3::ite(Sq[j], c.int_val(0), tl.E[j]));
  expr T = c.int_const(("T_" + tag).c_str());
  sol.add(T == tmax);
  tl.T = T;
  return tl;
}

int
main(int argc, char** argv)
{
  using namespace cfg;

  // Parse optional solver timeout (default 60s, CLI arg in seconds)
  unsigned timeout_ms = 60000u;
  if (argc > 1)
    try
      {
        size_t pos = 0;
        double secs = std::stod(argv[1], &pos);
        if (pos != std::string(argv[1]).size() || secs < 0.0)
          throw std::invalid_argument("");
        timeout_ms = static_cast<unsigned>(secs * 1000.0);
      }
    catch (const std::exception&)
      {
        std::cerr << "usage: " << argv[0] << " [timeout_seconds]\n"
                     "  timeout_seconds: solver timeout in seconds "
                     "(default 60; 0 = no timeout)\n";
        return 1;
      }

  context c;
  solver sol(c);
  sol.set("timeout", timeout_ms);

  /* Solver tuning (model-preserving — affects search strategy only, never the
   * set of satisfying models). smt.arith.solver=2 selects Z3's Simplex-based
   * arithmetic core instead of the default LRA core (=6). Measured faster on
   * this model (e.g. SPEC=1/N=8 proves its maximum in ~45s vs ~68s on the
   * default) because the model is dominated by difference-logic-style
   * definitional equalities (St/E/Aeff chains) that Simplex bound propagation
   * handles well.
   *
   * Do NOT set this to 1 (Bellman-Ford). Value 1 is the *difference-logic-only*
   * engine: it is faster on the diff-logic subset, but this model also contains
   * genuine non-difference-logic constraints — the Σ ite(...) counting sums
   * (shadow_len cap, the LSQ per-stream count, and inflight). Bellman-Ford
   * cannot represent those; at small N it merely warns ("smt.diff_logic:
   * non-diff logic expression ..."), but at the default N=12 the sums grow and
   * it hard-aborts with "Overflow encountered when expanding vector". Simplex
   * (=2) is complete for this model and is what earlier notes meant by "the
   * Simplex core." */
  sol.set("arith.solver", (unsigned)2);

  // Synthesized workload: arrivals A, stream ids K, read/write RW
  std::vector<expr> A, K, RW;
  A.reserve(N); K.reserve(N); RW.reserve(N);
  for (int i = 0; i < N; ++i)
    {
      A.push_back(c.int_const(("A_" + std::to_string(i)).c_str()));
      K.push_back(c.int_const(("K_" + std::to_string(i)).c_str()));
      RW.push_back(c.int_const(("RW_" + std::to_string(i)).c_str()));
      sol.add(A[i] >= 0 && A[i] <= HORIZON);
      sol.add(K[i] >= 0 && K[i] <  S);
      sol.add(RW[i] >= 0 && RW[i] <= 1);
    }
  // Program order: arrivals non-decreasing
  for (int i = 0; i + 1 < N; ++i)
    sol.add(A[i] <= A[i + 1]);

  // Stream-label symmetry break (first-occurrence canonical form)
  if (S > 1) {
    sol.add(K[0] == 0);
    expr running_max = K[0];
    for (int i = 1; i < N; ++i) {
      sol.add(K[i] <= running_max + 1);
      running_max = zmax(running_max, K[i]);
    }
  }

  // Read/write-label symmetry break (pin first request as read)
  sol.add(RW[0] == 0);

  // Wrong-path speculation: mispredicted branch BR and shadow Sq[] (shared workload)
  expr BR = c.int_const("BR");
  std::vector<expr> Sq;
  Sq.reserve(N);
  for (int i = 0; i < N; ++i)
    Sq.push_back(c.bool_const(("Sq_" + std::to_string(i)).c_str()));

  if (SPEC) {
    sol.add(BR >= 0 && BR < N);
    for (int i = 0; i < N; ++i)
      sol.add(z3::implies(Sq[i], BR < i));
    // Contiguous shadow: {BR+1,...,SE}
    for (int i = 2; i < N; ++i)
      sol.add(z3::implies(Sq[i] && (i - 1 > BR), Sq[i - 1]));
    // Cap shadow length
    expr shadow_len = c.int_val(0);
    for (int i = 0; i < N; ++i)
      shadow_len = shadow_len + b2i(c, Sq[i]);
    sol.add(shadow_len <= c.int_val(MAX_SHADOW));
  } else {
    sol.add(BR == 0);
    for (int i = 0; i < N; ++i)
      sol.add(!Sq[i]);
  }

  // Dependency matrix Dep[i][j] (upper-triangular, ROB-bounded)
  std::vector<std::vector<expr>> Dep(N, std::vector<expr>(N, c.bool_val(false)));
  for (int i = 0; i < N; ++i)
    for (int j = i + 1; j < N && j - i <= ROB_SIZE; ++j)
      Dep[i][j] = c.bool_const(("Dep_" + std::to_string(i) + "_" + std::to_string(j)).c_str());

  // Dependencies must be same-stream
  for (int i = 0; i < N; ++i)
    for (int j = i + 1; j < N && j - i <= ROB_SIZE; ++j)
      sol.add(z3::implies(Dep[i][j], K[i] == K[j]));

  // LSQ capacity per stream
  for (int j = 0; j < N; ++j)
    {
      expr indep_in_stream = c.int_val(0);
      int lo = (j - ROB_SIZE > 0) ? (j - ROB_SIZE) : 0;
      for (int i = lo; i < j; ++i)
        indep_in_stream = indep_in_stream + b2i(c, (K[i] == K[j]) && !Dep[i][j]);
      sol.add(indep_in_stream <= MAX_STREAM_MLP);
    }

  // Instantiate both machines
  Timeline high = build_machine(c, sol, "High", W_HIGH, Dep, A, RW, BR, Sq);
  Timeline low  = build_machine(c, sol, "Low",  W_LOW,  Dep, A, RW, BR, Sq);

  // Discovery: does T_HighMLP > T_LowMLP exist?
  expr Delta = c.int_const("Delta");
  sol.add(Delta == high.T - low.T);
  sol.add(Delta > 0);

  std::cout << "=== MLP dogma discovery engine ===\n"
            << "N=" << N << "  streams(S)=" << S << "  B=" << B
            << "  ROB=" << ROB_SIZE << "  MAX_STREAM_MLP=" << MAX_STREAM_MLP << "\n"
            << "G=" << G << "  TT=" << TT
            << "  C=" << C << "  C2=" << C2
            << "  PEN_LO=" << PEN_LO << "  PEN_HI=" << PEN_HI << "\n"
            << "contention=" << (CONTENTION ? "on" : "off (Pen=0)") << "\n"
            << "SPEC=" << SPEC
            << (SPEC ? "  MAX_SHADOW=" : "  (speculation off -> reduces to base model)")
            << (SPEC ? std::to_string(MAX_SHADOW) : std::string())
            << (SPEC ? "  RESOLVE_DELAY=" + std::to_string(RESOLVE_DELAY) : std::string()) << "\n"
            << "W_HighMLP=" << W_HIGH << "  W_LowMLP=" << W_LOW << "\n"
            << "solver timeout=" << timeout_ms << " ms"
            << (timeout_ms == 0 ? " (none)" : "") << "\n"
            << "Query: exists workload with  T_HighMLP > T_LowMLP ?\n\n";

  switch (sol.check()) {
  case z3::unsat:
    std::cout << "UNSAT: no counterexample. Within these bounds, more MLP "
                 "is never worse -- the dogma holds.\n";
    return 0;
  case z3::unknown:
    std::cout << "UNKNOWN: solver gave up (timeout?). reason: "
              << sol.reason_unknown() << "\n";
    return 2;
  case z3::sat:
    break;
  }

  // Maximize Delta via incremental tightening
  z3::model m = sol.get_model();
  auto delta_of = [&](const z3::model& mm)
    {
      return mm.eval(Delta, true).get_numeral_int64();
    };
  int64_t best = delta_of(m);
  std::cout << "SAT at delta = " << best << " cycles; maximizing...\n";

  for (;;) {
    // Monotone tightening: each probe asserts a strictly higher floor
    // (Delta >= best+1) than the last, so every earlier bound is implied by the
    // current one and nothing ever needs retracting. We therefore assert the new
    // floor directly on the solver rather than via push/pop. This RETAINS the
    // lemmas Z3 learns on each probe instead of discarding them at every pop --
    // a large win for the final, hardest UNSAT probe (the maximality proof),
    // which would otherwise re-derive everything from a cold solver state.
    sol.add(Delta >= c.int_val((int)best + 1));
    z3::check_result r = sol.check();
    if (r == z3::sat) {
      m = sol.get_model();
      best = delta_of(m);
      std::cout << "  found larger delta = " << best << " cycles\n";
    } else {
      if (r == z3::unknown)
        std::cout << "  stopped (timeout); reporting best found.\n";
      else
        std::cout << "  proved maximum: delta = " << best << " cycles.\n";
      break;
    }
  }
  std::cout << "\n";

  // Extract and print counterexample workload
  auto iv = [&](const expr& e)
    {
      return m.eval(e, true).get_numeral_int64();
    };

  std::cout << "Worst-case workload where High-MLP is SLOWER "
               "(maximum deviation):\n\n";

  std::cout << "Stream K[i], arrival A[i], type RW[i] (0=read,1=write):\n  i :";
  for (int i = 0; i < N; ++i)
    std::cout << std::setw(4) << i;
  std::cout << "\n  K :";
  for (int i = 0; i < N; ++i)
    std::cout << std::setw(4) << iv(K[i]);
  std::cout << "\n  A :";
  for (int i = 0; i < N; ++i)
    std::cout << std::setw(4) << iv(A[i]);
  std::cout << "\n  RW:";
  for (int i = 0; i < N; ++i)
    std::cout << std::setw(4) << iv(RW[i]);
  if (SPEC) {
    std::cout << "\n  Sq:";
    for (int i = 0; i < N; ++i)
      std::cout << std::setw(4) << (m.eval(Sq[i], true).is_true() ? 1 : 0);
    std::cout << "      (BR = " << iv(BR) << ", shadow = wrong-path requests)";
  }
  std::cout << "\n\n";

  std::cout << "Dependency matrix Dep[i][j]  (1 => j consumes i's result):\n     j:";
  for (int j = 0; j < N; ++j)
    std::cout << std::setw(3) << j;
  std::cout << "\n";
  for (int i = 0; i < N; ++i) {
    std::cout << "  i=" << i << " :";
    for (int j = 0; j < N; ++j) {
      bool d = m.eval(Dep[i][j], true).is_true();
      std::cout << std::setw(3) << (d ? 1 : 0);
    }
    std::cout << "\n";
  }
  std::cout << "\n";

  auto dump = [&](const char* name, const Timeline& tl) {
    std::cout << name << " timeline:\n";
    std::cout << "   Aeff :";
    for (auto& e : tl.Aeff)
      std::cout << std::setw(5) << iv(e);
    std::cout << "\n";
    std::cout << "   A'   :";
    for (auto& e : tl.Aprime)
      std::cout << std::setw(5) << iv(e);
    std::cout << "\n";
    if (SPEC) {
      std::cout << "   cf   :";
      for (auto& e : tl.Cf)
        std::cout << std::setw(5) << iv(e);
      std::cout << "\n";
    }
    std::cout << "   St   :";
    for (auto& e : tl.St)
      std::cout << std::setw(5) << iv(e);
    std::cout << "\n";
    if (SPEC) {
      std::cout << "   live :";
      for (auto& e : tl.Live)
        std::cout << std::setw(5) << (m.eval(e, true).is_true() ? 1 : 0);
      std::cout << "\n";
    }
    std::cout << "   nfly :";
    for (auto& e : tl.Inflight)
      std::cout << std::setw(5) << iv(e);
    std::cout << "\n";
    std::cout << "   pen  :";
    for (auto& e : tl.Pen)
      std::cout << std::setw(5) << iv(e);
    std::cout << "\n";
    std::cout << "   E    :";
    for (auto& e : tl.E)
      std::cout << std::setw(5) << iv(e);
    std::cout << "\n";
    if (SPEC) {
      // Wrong-path issue depth (squashed requests that reached bus)
      int depth = 0;
      for (int j = 0; j < N; ++j)
        if (m.eval(Sq[j], true).is_true() && m.eval(tl.Live[j], true).is_true())
          ++depth;
      std::cout << "   R    = " << iv(tl.R)
                << "   wrong-path issue depth = " << depth << "\n";
    }
    std::cout << "   T    = " << iv(tl.T) << "\n";
  };
  dump("System_HighMLP", high);
  dump("System_LowMLP ", low);

  std::cout << "\nConclusion: T_HighMLP = " << iv(high.T)
            << "  >  T_LowMLP = " << iv(low.T)
            << "   (delta = " << (iv(high.T) - iv(low.T)) << " cycles)\n"
            << "More memory-level parallelism made this workload SLOWER.\n";
  return 0;
}
