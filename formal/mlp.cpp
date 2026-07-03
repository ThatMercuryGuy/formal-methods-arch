/*
 * Bounded Model Checking engine for the "more MLP is always better" dogma.
 * Unrolls N memory requests on two machines (System_HighMLP, System_LowMLP) that
 * share the same synthesized workload but differ only in MSHR window W.
 * Seeks workloads where T_HighMLP > T_LowMLP by modeling pipelined finite bandwidth,
 * R/W turnaround, and wrong-path speculation waste.
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
  constexpr int N       = 12;   // Unroll depth (number of memory requests).
  constexpr int B       = 10;   // Memory access latency per request (cycles).
  constexpr int HORIZON = 64;   // Upper bound for synthesized arrival times.

  // ---- Pipelined finite-bandwidth channel (shared physics, both machines) --
  constexpr int G       = 2;    // Channel inter-admission gap (1/bandwidth), G < B.
  constexpr int TT      = 4;    // Read/write bus turnaround bubble (direction switch).

  constexpr int W_HIGH  = 6;    // System_HighMLP (aggressive).
  constexpr int W_LOW   = 2;    // System_LowMLP (throttled).

  // Wrong-path speculation (Strategy B): mispredicted branch BR -> shadow Sq[] of
  // wrong-path reqs, shared by both machines; per-machine issue depth emerges from
  // St[j] < R only. T counts correct-path completions. This is the sole anti-MLP
  // mechanism and is always on.
  constexpr int MAX_SHADOW    = 4;   // Shadow-length cap (logged if binds).
  constexpr int RESOLVE_DELAY = 0;   // R = E[BR] + this; 0 = resolve at condition-load.

  static_assert(G < B, "channel must pipeline: admission gap G must be < B");
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
  std::vector<expr> Aprime;   // channel presentation (MSHR gating)
  std::vector<expr> Cf;       // channel-free time (skips non-live shadow)
  std::vector<expr> St;       // service start (admission + turnaround)
  std::vector<expr> Live;     // reaches bus? (bool)
  std::vector<expr> E;        // service end
  std::vector<expr> Rel;      // MSHR release time (E, or R if squashed pre-issue)
  expr              R;        // branch-resolve cycle
  expr              T;        // system cycles (max correct-path E)
  explicit Timeline(context& c) : R(c.int_val(0)), T(c.int_val(0)) {}
};

// Build per-machine timeline (W-parameterized on the shared workload).
static Timeline
build_machine(context& c, solver& sol,
              const std::string& tag, int W,
              const std::vector<expr>& A,
              const std::vector<expr>& RW,
              const expr& BR,
              const std::vector<expr>& Sq)
{
  using namespace cfg;
  Timeline tl(c);

  // Declare a named per-request const, assert it equals rhs, store it, return it.
  // Naming each quantity keeps the encoding definitional (Simplex-friendly).
  auto def = [&](std::vector<expr>& v, const char* base, int j, const expr& rhs)
    {
      expr e = c.int_const((base + ("_" + tag + "_" + std::to_string(j))).c_str());
      sol.add(e == rhs);
      v.push_back(e);
      return e;
    };
  auto defb = [&](std::vector<expr>& v, const char* base, int j, const expr& rhs)
    {
      expr e = c.bool_const((base + ("_" + tag + "_" + std::to_string(j))).c_str());
      sol.add(e == rhs);
      v.push_back(e);
      return e;
    };

  expr R = c.int_const(("R_" + tag).c_str());  // branch-resolve cycle (pinned below)
  tl.R = R;

  for (int j = 0; j < N; ++j)
    {
      // MSHR gating: a request presents at its arrival A[j], but no earlier than the
      // release of the request W slots back (the window is full until that frees).
      expr Aprime = def(tl.Aprime, "Aprime", j,
                        j - W >= 0 ? zmax(A[j], tl.Rel[j - W]) : A[j]);

      // Pipelined admission + turnaround. Cf skips non-live shadow (killed in the
      // issue queue, so it never occupies the bus).
      expr cf_rhs = Aprime;
      if (j > 0)
        {
          expr gap = c.int_val(G)
                     + z3::ite(RW[j] != RW[j - 1], c.int_val(TT), c.int_val(0));
          cf_rhs = z3::ite(tl.Live[j - 1], tl.St[j - 1] + gap, tl.Cf[j - 1]);
        }
      expr Cf = def(tl.Cf, "Cf", j, cf_rhs);
      expr St = def(tl.St, "St", j, j == 0 ? Aprime : zmax(Aprime, Cf));

      // Bus liveness: reaches the bus unless a shadow request that starts after resolve.
      expr Live = defb(tl.Live, "Live", j, !Sq[j] || (St < R));
      expr E = def(tl.E, "E", j, St + c.int_val(B));

      // MSHR release: a squashed request that never issued frees its slot at resolve;
      // otherwise it holds to completion (an in-flight miss cannot be un-sent).
      def(tl.Rel, "Rel", j, z3::ite(Sq[j] && !Live, R, E));
    }

  // R = E[BR] + RESOLVE_DELAY
  expr r_sel = c.int_val(0);
  for (int i = 0; i < N; ++i)
    r_sel = z3::ite(BR == i, tl.E[i], r_sel);
  sol.add(R == r_sel + c.int_val(RESOLVE_DELAY));

  // T = max completion over correct-path (non-squashed) requests.
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

  // Simplex arithmetic core (2), not the default LRA (6): faster on this model's
  // definitional-equality chains. NOT 1 (Bellman-Ford): incomplete here — the
  // Σ ite(...) shadow-length sum isn't difference logic. See CLAUDE.md "Solver
  // tuning" for measurements. Model-preserving.
  sol.set("arith.solver", (unsigned)2);

  // Synthesized workload: arrivals A, read/write RW.
  std::vector<expr> A, RW;
  A.reserve(N); RW.reserve(N);
  for (int i = 0; i < N; ++i)
    {
      A.push_back(c.int_const(("A_" + std::to_string(i)).c_str()));
      RW.push_back(c.int_const(("RW_" + std::to_string(i)).c_str()));
      sol.add(A[i] >= 0 && A[i] <= HORIZON);
      sol.add(RW[i] >= 0 && RW[i] <= 1);
    }
  // Program order: arrivals non-decreasing.
  for (int i = 0; i + 1 < N; ++i)
    sol.add(A[i] <= A[i + 1]);

  // Read/write-label symmetry break (pin first request as a read, WLOG).
  sol.add(RW[0] == 0);

  // Wrong-path speculation: mispredicted branch BR and shadow Sq[] (shared workload).
  expr BR = c.int_const("BR");
  std::vector<expr> Sq;
  Sq.reserve(N);
  for (int i = 0; i < N; ++i)
    Sq.push_back(c.bool_const(("Sq_" + std::to_string(i)).c_str()));

  sol.add(BR >= 0 && BR < N);
  for (int i = 0; i < N; ++i)
    sol.add(z3::implies(Sq[i], BR < i));
  // Contiguous shadow: {BR+1,...,SE}.
  for (int i = 2; i < N; ++i)
    sol.add(z3::implies(Sq[i] && (i - 1 > BR), Sq[i - 1]));
  // Cap shadow length.
  expr shadow_len = c.int_val(0);
  for (int i = 0; i < N; ++i)
    shadow_len = shadow_len + b2i(c, Sq[i]);
  sol.add(shadow_len <= c.int_val(MAX_SHADOW));

  // Instantiate both machines.
  Timeline high = build_machine(c, sol, "High", W_HIGH, A, RW, BR, Sq);
  Timeline low  = build_machine(c, sol, "Low",  W_LOW,  A, RW, BR, Sq);

  // Discovery: does T_HighMLP > T_LowMLP exist?
  expr Delta = c.int_const("Delta");
  sol.add(Delta == high.T - low.T);
  sol.add(Delta > 0);

  std::cout << "=== MLP dogma discovery engine ===\n"
            << "N=" << N << "  B=" << B << "\n"
            << "G=" << G << "  TT=" << TT << "\n"
            << "MAX_SHADOW=" << MAX_SHADOW
            << "  RESOLVE_DELAY=" << RESOLVE_DELAY << "\n"
            << "W_HighMLP=" << W_HIGH << "  W_LowMLP=" << W_LOW << "\n"
            << "solver timeout=" << timeout_ms << " ms"
            << (timeout_ms == 0 ? " (none)" : "") << "\n"
            << "Query: exists workload with  T_HighMLP > T_LowMLP ?\n\n";

  switch (sol.check())
    {
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

  // Maximize Delta via incremental tightening.
  z3::model m = sol.get_model();
  auto delta_of = [&](const z3::model& mm)
    {
      return mm.eval(Delta, true).get_numeral_int64();
    };
  int64_t best = delta_of(m);
  std::cout << "SAT at delta = " << best << " cycles; maximizing...\n";

  for (;;)
    {
      // Each probe raises the floor monotonically, so earlier bounds are implied
      // and we assert directly (no push/pop) — this keeps Z3's learned lemmas for
      // the final maximality proof instead of discarding them.
      sol.add(Delta >= c.int_val((int)best + 1));
      z3::check_result r = sol.check();
      if (r == z3::sat)
        {
          m = sol.get_model();
          best = delta_of(m);
          std::cout << "  found larger delta = " << best << " cycles\n";
        }
      else
        {
          if (r == z3::unknown)
            std::cout << "  stopped (timeout); reporting best found.\n";
          else
            std::cout << "  proved maximum: delta = " << best << " cycles.\n";
          break;
        }
    }
  std::cout << "\n";

  // Extract and print the counterexample workload.
  auto iv  = [&](const expr& e) { return m.eval(e, true).get_numeral_int64(); };
  auto bit = [&](const expr& e) { return m.eval(e, true).is_true() ? 1 : 0; };

  // Print a labeled row of one value per request; cell(i) supplies each cell.
  auto row = [&](const char* label, int w, auto&& cell)
    {
      std::cout << label;
      for (int i = 0; i < N; ++i)
        std::cout << std::setw(w) << cell(i);
      std::cout << "\n";
    };
  // A row taken from a per-request expr vector (as_bit prints truth values).
  auto erow = [&](const char* label, const std::vector<expr>& v, bool as_bit = false)
    {
      row(label, 5, [&](int i) { return as_bit ? bit(v[i]) : iv(v[i]); });
    };

  std::cout << "Worst-case workload where High-MLP is SLOWER "
               "(maximum deviation):\n\n";

  std::cout << "Arrival A[i], type RW[i] (0=read,1=write):\n";
  row("  i :", 4, [](int i)  { return i; });
  row("  A :", 4, [&](int i) { return iv(A[i]); });
  row("  RW:", 4, [&](int i) { return iv(RW[i]); });
  row("  Sq:", 4, [&](int i) { return bit(Sq[i]); });
  std::cout << "      (BR = " << iv(BR) << ", shadow = wrong-path requests)\n";
  std::cout << "\n";

  auto dump = [&](const char* name, const Timeline& tl)
    {
      std::cout << name << " timeline:\n";
      erow("   A'   :", tl.Aprime);
      erow("   cf   :", tl.Cf);
      erow("   St   :", tl.St);
      erow("   live :", tl.Live, true);
      erow("   E    :", tl.E);
      // Wrong-path issue depth (squashed requests that reached the bus).
      int depth = 0;
      for (int j = 0; j < N; ++j)
        if (bit(Sq[j]) && bit(tl.Live[j]))
          ++depth;
      std::cout << "   R    = " << iv(tl.R)
                << "   wrong-path issue depth = " << depth << "\n";
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
