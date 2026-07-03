/*
 * Bounded Model Checking engine for the "more MLP is always better" dogma.
 * Unrolls UNROLL_DEPTH memory requests on two machines (System_HighMLP,
 * System_LowMLP) that share the same synthesized workload but differ only in
 * MSHR window (WINDOW_HIGH vs WINDOW_LOW). Seeks workloads where
 * completion_HighMLP > completion_LowMLP by modeling pipelined finite bandwidth,
 * variable memory latency (reordered completion), and wrong-path speculation waste.
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
  constexpr int UNROLL_DEPTH    = 12;   // Number of memory requests unrolled.
  constexpr int ARRIVAL_HORIZON = 64;   // Upper bound for synthesized arrival times.

  // ---- Variable memory latency (shared workload; reorders completion) -------
  // Per-request latency is solver-chosen in [LAT_MIN, LAT_MAX] and shared by both
  // machines: a row-buffer hit returns fast, a row-miss (precharge+activate+CAS)
  // slow. A later request drawing a short latency can complete before an earlier
  // one that drew a long latency -- this IS the memory controller reordering
  // completions, so MSHR gating is occupancy-based (order of releases is not the
  // order of issue).
  constexpr int LAT_MIN = 6;    // row-buffer hit (fast return).
  constexpr int LAT_MAX = 18;   // row-miss: precharge+activate+CAS (slow return).

  // ---- Pipelined finite-bandwidth channel (shared physics, both machines) --
  constexpr int ADMISSION_GAP      = 2;  // Channel inter-admission gap (1/bandwidth), < LAT_MIN.

  constexpr int WINDOW_HIGH = 4;    // System_HighMLP MSHR window (aggressive).
  constexpr int WINDOW_LOW  = 2;    // System_LowMLP MSHR window (throttled).

  // Wrong-path speculation (Strategy B): mispredicted branch `branch` -> shadow
  // `squashed[]` of wrong-path reqs, shared by both machines; per-machine issue
  // depth emerges from service_start[j] < resolve only. completion counts
  // correct-path completions. This is the sole anti-MLP mechanism and is always on.
  constexpr int MAX_SHADOW    = 8;   // Shadow-length cap (logged if binds).
  constexpr int RESOLVE_DELAY = 3;   // resolve = service_end[branch] + this; models compare+redirect latency.

  static_assert(ADMISSION_GAP < LAT_MIN,
                "channel must pipeline: ADMISSION_GAP must be < LAT_MIN");
  static_assert(LAT_MIN <= LAT_MAX, "latency range must be non-empty");
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
zmin(const expr& a, const expr& b)
{
  return z3::ite(a <= b, a, b);
}

static inline expr
bool_to_int(context& c, const expr& b)
{
  return z3::ite(b, c.int_val(1), c.int_val(0));
}

struct Timeline
{
  std::vector<expr> present;        // channel presentation (MSHR gating)
  std::vector<expr> chan_free;      // channel-free time (skips non-live shadow)
  std::vector<expr> service_start;  // service start (admission)
  std::vector<expr> live;           // reaches bus? (bool)
  std::vector<expr> service_end;    // service end
  std::vector<expr> mshr_release;   // MSHR release time (service_end, or resolve if squashed pre-issue)
  expr              resolve;        // branch-resolve cycle
  expr              completion;     // system cycles (max correct-path service_end)
  explicit Timeline(context& c) : resolve(c.int_val(0)), completion(c.int_val(0)) {}
};

// Build per-machine timeline (window-parameterized on the shared workload).
static Timeline
build_machine(context& c, solver& sol,
              const std::string& tag, int window,
              const std::vector<expr>& arrival,
              const std::vector<expr>& latency,
              const expr& branch,
              const std::vector<expr>& squashed)
{
  using namespace cfg;
  Timeline tl(c);

  // Declare a named per-request const, assert it equals rhs, store it, return it.
  // Naming each quantity keeps the encoding definitional (Simplex-friendly).
  auto def_int = [&](std::vector<expr>& v, const char* base, int j, const expr& rhs)
    {
      expr e = c.int_const((base + ("_" + tag + "_" + std::to_string(j))).c_str());
      sol.add(e == rhs);
      v.push_back(e);
      return e;
    };
  auto def_bool = [&](std::vector<expr>& v, const char* base, int j, const expr& rhs)
    {
      expr e = c.bool_const((base + ("_" + tag + "_" + std::to_string(j))).c_str());
      sol.add(e == rhs);
      v.push_back(e);
      return e;
    };
  // Name a scalar (not stored in a Timeline vector): const == rhs, definitional.
  auto name_int = [&](const std::string& nm, const expr& rhs)
    {
      expr e = c.int_const(nm.c_str());
      sol.add(e == rhs);
      return e;
    };

  expr resolve = c.int_const(("resolve_" + tag).c_str());  // branch-resolve cycle (pinned below)
  tl.resolve = resolve;

  // MSHR occupancy state: `window` interchangeable slot registers, each holding the
  // cycle its entry next becomes free (initialized free at 0). Because latency varies,
  // completions reorder, so the freeing slot is NOT a fixed index -- gating is by
  // occupancy: a request presents once the EARLIEST slot frees. This reduces exactly
  // to the fixed-index gate when latency is constant (releases in issue order).
  std::vector<expr> slot;
  slot.reserve(window);
  for (int s = 0; s < window; ++s)
    slot.push_back(c.int_val(0));

  for (int j = 0; j < UNROLL_DEPTH; ++j)
    {
      // Occupancy gating: earliest cycle any of the `window` MSHR slots frees.
      expr min_free = slot[0];
      for (int s = 1; s < window; ++s)
        min_free = zmin(min_free, slot[s]);
      min_free = name_int("minfree_" + tag + "_" + std::to_string(j), min_free);
      expr present = def_int(tl.present, "present", j, zmax(arrival[j], min_free));

      // Pipelined admission. chan_free skips non-live shadow (killed in the issue
      // queue, so it never occupies the bus).
      expr chan_free_rhs = present;
      if (j > 0)
        chan_free_rhs = z3::ite(tl.live[j - 1],
                                tl.service_start[j - 1] + c.int_val(ADMISSION_GAP),
                                tl.chan_free[j - 1]);
      expr chan_free = def_int(tl.chan_free, "chan_free", j, chan_free_rhs);
      expr service_start = def_int(tl.service_start, "service_start", j,
                                   j == 0 ? present : zmax(present, chan_free));

      // Bus liveness: reaches the bus unless a shadow request that starts after resolve.
      expr live = def_bool(tl.live, "live", j, !squashed[j] || (service_start < resolve));
      expr service_end = def_int(tl.service_end, "service_end", j,
                                 service_start + latency[j]);

      // MSHR release: a squashed request that never issued frees its slot at resolve;
      // otherwise it holds to completion (an in-flight miss cannot be un-sent).
      expr mshr_release = def_int(tl.mshr_release, "mshr_release", j,
              z3::ite(squashed[j] && !live, resolve, service_end));

      // Hand j's entry back into the earliest-free slot (lowest index achieving the
      // min, so ties replace exactly one) -- a canonical, deterministic assignment
      // over the interchangeable slots.
      expr done = c.bool_val(false);
      for (int s = 0; s < window; ++s)
        {
          expr is_target = (!done) && (slot[s] == min_free);
          slot[s] = name_int("slot_" + tag + "_" + std::to_string(j) + "_"
                               + std::to_string(s),
                             z3::ite(is_target, mshr_release, slot[s]));
          done = done || is_target;
        }
    }

  // resolve = service_end[branch] + RESOLVE_DELAY
  expr resolve_sel = c.int_val(0);
  for (int i = 0; i < UNROLL_DEPTH; ++i)
    resolve_sel = z3::ite(branch == i, tl.service_end[i], resolve_sel);
  sol.add(resolve == resolve_sel + c.int_val(RESOLVE_DELAY));

  // completion = max service_end over correct-path (non-squashed) requests.
  expr cmax = c.int_val(0);
  for (int j = 0; j < UNROLL_DEPTH; ++j)
    cmax = zmax(cmax, z3::ite(squashed[j], c.int_val(0), tl.service_end[j]));
  expr completion = c.int_const(("completion_" + tag).c_str());
  sol.add(completion == cmax);
  tl.completion = completion;
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

  /* Simplex arithmetic core (2), not the default LRA (6): faster on this model's
     definitional-equality chains. */
  sol.set("arith.solver", (unsigned)2);

  // Synthesized workload: arrivals `arrival` and per-request memory latency `latency`
  // (shared by both machines; completion order still diverges through `window` alone).
  std::vector<expr> arrival, latency;
  arrival.reserve(UNROLL_DEPTH); latency.reserve(UNROLL_DEPTH);
  for (int i = 0; i < UNROLL_DEPTH; ++i)
    {
      arrival.push_back(c.int_const(("arrival_" + std::to_string(i)).c_str()));
      latency.push_back(c.int_const(("latency_" + std::to_string(i)).c_str()));
      sol.add(arrival[i] >= 0 && arrival[i] <= ARRIVAL_HORIZON);
      sol.add(latency[i] >= LAT_MIN && latency[i] <= LAT_MAX);
    }
  // Program order: arrivals non-decreasing.
  for (int i = 0; i + 1 < UNROLL_DEPTH; ++i)
    sol.add(arrival[i] <= arrival[i + 1]);

  // Wrong-path speculation: mispredicted branch `branch` and shadow `squashed[]`
  // (shared workload).
  expr branch = c.int_const("branch");
  std::vector<expr> squashed;
  squashed.reserve(UNROLL_DEPTH);
  for (int i = 0; i < UNROLL_DEPTH; ++i)
    squashed.push_back(c.bool_const(("squashed_" + std::to_string(i)).c_str()));

  sol.add(branch >= 0 && branch < UNROLL_DEPTH);
  for (int i = 0; i < UNROLL_DEPTH; ++i)
    sol.add(z3::implies(squashed[i], branch < i));
  // Contiguous shadow: {branch+1,...,shadow-end}.
  for (int i = 2; i < UNROLL_DEPTH; ++i)
    sol.add(z3::implies(squashed[i] && (i - 1 > branch), squashed[i - 1]));
  // Cap shadow length.
  expr shadow_len = c.int_val(0);
  for (int i = 0; i < UNROLL_DEPTH; ++i)
    shadow_len = shadow_len + bool_to_int(c, squashed[i]);
  sol.add(shadow_len <= c.int_val(MAX_SHADOW));

  // Instantiate both machines.
  Timeline high = build_machine(c, sol, "High", WINDOW_HIGH,
                                arrival, latency, branch, squashed);
  Timeline low  = build_machine(c, sol, "Low",  WINDOW_LOW,
                                arrival, latency, branch, squashed);

  // Discovery: does completion_HighMLP > completion_LowMLP exist?
  expr Delta = c.int_const("Delta");
  sol.add(Delta == high.completion - low.completion);
  sol.add(Delta > 0);

  std::cout << "=== MLP dogma discovery engine ===\n"
            << "UNROLL_DEPTH=" << UNROLL_DEPTH
            << "  LATENCY=[" << LAT_MIN << "," << LAT_MAX << "]\n"
            << "ADMISSION_GAP=" << ADMISSION_GAP << "\n"
            << "MAX_SHADOW=" << MAX_SHADOW
            << "  RESOLVE_DELAY=" << RESOLVE_DELAY << "\n"
            << "WINDOW_HIGH=" << WINDOW_HIGH << "  WINDOW_LOW=" << WINDOW_LOW << "\n"
            << "solver timeout=" << timeout_ms << " ms"
            << (timeout_ms == 0 ? " (none)" : "") << "\n"
            << "Query: exists workload with  completion_HighMLP > completion_LowMLP ?\n\n";

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
  std::cout << "SAT at Delta = " << best << " cycles; maximizing...\n";

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
          std::cout << "  found larger Delta = " << best << " cycles\n";
        }
      else
        {
          if (r == z3::unknown)
            std::cout << "  stopped (timeout); reporting best found.\n";
          else
            std::cout << "  proved maximum: Delta = " << best << " cycles.\n";
          break;
        }
    }
  std::cout << "\n";

  // Extract and print the counterexample workload.
  auto iv  = [&](const expr& e) { return m.eval(e, true).get_numeral_int64(); };
  auto bit = [&](const expr& e) { return m.eval(e, true).is_true() ? 1 : 0; };

  // Print a labeled row of one value per request; cell(i) supplies each cell.
  // Label is left-justified to a fixed width so all columns line up.
  auto row = [&](const char* label, auto&& cell)
    {
      std::cout << "  " << std::left << std::setw(13) << label << std::right;
      for (int i = 0; i < UNROLL_DEPTH; ++i)
        std::cout << std::setw(4) << cell(i);
      std::cout << "\n";
    };

  int br = (int)iv(branch);

  std::cout << "Worst-case workload where more MLP is SLOWER "
               "(maximum deviation):\n\n";

  // --- Shared workload (identical for both machines by construction) ---------
  row("request", [](int i)  { return i; });
  row("arrival", [&](int i) { return iv(arrival[i]); });
  row("branch-path", [&](int i)
      { return i == br ? "BR" : bit(squashed[i]) ? "wp" : "."; });
  std::cout << "     (BR=mispredicted branch, wp=wrong-path shadow)\n\n";

  // --- The only per-machine difference: wrong-path issue depth + outcome -----
  auto depth = [&](const Timeline& tl)
    {
      int d = 0;
      for (int j = 0; j < UNROLL_DEPTH; ++j)
        if (bit(squashed[j]) && bit(tl.live[j]))
          ++d;
      return d;
    };
  std::cout << "  machine        MSHR-window   wrong-path-on-bus   resolve-cycle   completion-cycle\n";
  auto line = [&](const char* name, int window, const Timeline& tl)
    {
      std::cout << "  " << std::left << std::setw(15) << name << std::right
                << std::setw(11) << window
                << std::setw(20) << depth(tl)
                << std::setw(16) << iv(tl.resolve)
                << std::setw(19) << iv(tl.completion) << "\n";
    };
  line("HighMLP", WINDOW_HIGH, high);
  line("LowMLP",  WINDOW_LOW,  low);

  std::cout << "\n  Delta = completion(HighMLP) - completion(LowMLP) = "
            << (iv(high.completion) - iv(low.completion)) << " cycles.\n"
            << "  Same workload, but the wide window issues deeper down the "
               "wrong path\n  before the branch resolves -- more MLP made it "
               "SLOWER.\n";
  return 0;
}
