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
static inline expr zmax(const expr& a, const expr& b)
{
  return z3::ite(a >= b, a, b);
}

static inline expr zmin(const expr& a, const expr& b)
{
  return z3::ite(a <= b, a, b);
}

static inline expr bool_to_int(context& c, const expr& b)
{
  return z3::ite(b, c.int_val(1), c.int_val(0));
}

struct Timeline
{
  std::vector<expr> chan_free;      // channel-free time (skips non-live shadow)
  std::vector<expr> service_start;  // service start (admission)
  std::vector<expr> live;           // reaches bus? (bool)
  std::vector<expr> service_end;    // service end
  expr              resolve;        // branch-resolve cycle
  expr              completion;     // system cycles (max correct-path service_end)
  explicit Timeline(context& c) : resolve(c.int_val(0)), completion(c.int_val(0)) {}
};

// ---------------------------------------------------------------------------
// Definitional naming. Each modeled quantity is a fresh const asserted equal to
// its defining expression; this keeps the encoding definitional (Simplex-friendly).
// `name_*` returns a bare named const; `def_*` also stores it in a Timeline vector.
// ---------------------------------------------------------------------------
struct Namer
{
  context&    c;
  solver&     sol;
  std::string tag;

  expr name_int(const std::string& nm, const expr& rhs)
    { expr e = c.int_const(nm.c_str());  sol.add(e == rhs); return e; }
  expr name_bool(const std::string& nm, const expr& rhs)
    { expr e = c.bool_const(nm.c_str()); sol.add(e == rhs); return e; }
  std::string qual(const char* base, int j) const
    { return base + ("_" + tag + "_" + std::to_string(j)); }
  expr def_int(std::vector<expr>& v, const char* base, int j, const expr& rhs)
    { expr e = name_int(qual(base, j), rhs);  v.push_back(e); return e; }
  expr def_bool(std::vector<expr>& v, const char* base, int j, const expr& rhs)
    { expr e = name_bool(qual(base, j), rhs); v.push_back(e); return e; }
};

// ---------------------------------------------------------------------------
// Per-request timeline stages (one discrete piece of the machine each). They are
// invoked in program order by build_machine; the assertion order they produce is
// the model.
// ---------------------------------------------------------------------------

// MSHR occupancy state: `window` interchangeable slot registers, each holding the
// cycle its entry next becomes free (initialized free at 0). Because latency varies,
// completions reorder, so the freeing slot is NOT a fixed index -- gating is by
// occupancy: a request presents once the EARLIEST slot frees. This reduces exactly
// to the fixed-index gate when latency is constant (releases in issue order).
static std::vector<expr> init_mshr_slots(context& c, int window)
{
  std::vector<expr> slot;
  slot.reserve(window);
  for (int s = 0; s < window; ++s)
    slot.push_back(c.int_val(0));
  return slot;
}

struct Gate
{
  expr min_free;   // earliest cycle any of the `window` MSHR slots frees
  expr present;    // when request j can present to the channel
};

// Occupancy gating: request j presents once the earliest MSHR slot frees.
static Gate occupancy_gate(Namer& nm, const std::vector<expr>& slot,
                           const expr& arrival_j, int window, int j)
{
  expr min_free = slot[0];
  for (int s = 1; s < window; ++s)
    min_free = zmin(min_free, slot[s]);
  min_free = nm.name_int(nm.qual("minfree", j), min_free);
  expr present = nm.name_int(nm.qual("present", j), zmax(arrival_j, min_free));
  return { min_free, present };
}

// Pipelined admission. chan_free skips non-live shadow (killed in the issue queue,
// so it never occupies the bus). Returns the request's service_start.
static expr admit(Namer& nm, Timeline& tl, const expr& present, int j)
{
  expr chan_free_rhs = present;
  if (j > 0)
    chan_free_rhs = z3::ite(tl.live[j - 1],
                            tl.service_start[j - 1] + nm.c.int_val(cfg::ADMISSION_GAP),
                            tl.chan_free[j - 1]);
  expr chan_free = nm.def_int(tl.chan_free, "chan_free", j, chan_free_rhs);
  return nm.def_int(tl.service_start, "service_start", j,
                    j == 0 ? present : zmax(present, chan_free));
}

// Bus liveness (reaches the bus unless a shadow request that starts after resolve)
// and service completion.
static void add_service(Namer& nm, Timeline& tl, const expr& squashed_j,
                        const expr& resolve, const expr& service_start,
                        const expr& latency_j, int j)
{
  nm.def_bool(tl.live, "live", j, !squashed_j || (service_start < resolve));
  nm.def_int(tl.service_end, "service_end", j, service_start + latency_j);
}

// MSHR release: a squashed request that never issued frees its slot at resolve;
// otherwise it holds to completion (an in-flight miss cannot be un-sent).
static expr mshr_release(Namer& nm, const expr& squashed_j, const expr& live,
                         const expr& resolve, const expr& service_end, int j)
{
  return nm.name_int(nm.qual("mshr_release", j),
                     z3::ite(squashed_j && !live, resolve, service_end));
}

// Hand j's entry back into the earliest-free slot (lowest index achieving the min,
// so ties replace exactly one) -- a canonical, deterministic assignment over the
// interchangeable slots.
static void writeback_slot(Namer& nm, std::vector<expr>& slot, const expr& min_free,
                           const expr& release, int window, int j)
{
  expr done = nm.c.bool_val(false);
  for (int s = 0; s < window; ++s)
    {
      expr is_target = (!done) && (slot[s] == min_free);
      slot[s] = nm.name_int("slot_" + nm.tag + "_" + std::to_string(j) + "_"
                              + std::to_string(s),
                            z3::ite(is_target, release, slot[s]));
      done = done || is_target;
    }
}

// resolve = service_end[branch] + RESOLVE_DELAY.
static void pin_resolve(Namer& nm, const Timeline& tl, const expr& resolve,
                        const expr& branch)
{
  expr resolve_sel = nm.c.int_val(0);
  for (int i = 0; i < cfg::UNROLL_DEPTH; ++i)
    resolve_sel = z3::ite(branch == i, tl.service_end[i], resolve_sel);
  nm.sol.add(resolve == resolve_sel + nm.c.int_val(cfg::RESOLVE_DELAY));
}

// completion = max service_end over correct-path (non-squashed) requests.
static expr compute_completion(Namer& nm, const Timeline& tl,
                               const std::vector<expr>& squashed)
{
  expr cmax = nm.c.int_val(0);
  for (int j = 0; j < cfg::UNROLL_DEPTH; ++j)
    cmax = zmax(cmax, z3::ite(squashed[j], nm.c.int_val(0), tl.service_end[j]));
  expr completion = nm.c.int_const(("completion_" + nm.tag).c_str());
  nm.sol.add(completion == cmax);
  return completion;
}

// Build per-machine timeline (window-parameterized on the shared workload).
static Timeline build_machine(context& c, solver& sol,
                              const std::string& tag, int window,
                              const std::vector<expr>& arrival,
                              const std::vector<expr>& latency,
                              const expr& branch,
                              const std::vector<expr>& squashed)
{
  Namer nm{c, sol, tag};
  Timeline tl(c);

  expr resolve = c.int_const(("resolve_" + tag).c_str());  // branch-resolve cycle (pinned below)
  tl.resolve = resolve;

  std::vector<expr> slot = init_mshr_slots(c, window);

  for (int j = 0; j < cfg::UNROLL_DEPTH; ++j)
    {
      Gate g = occupancy_gate(nm, slot, arrival[j], window, j);
      expr service_start = admit(nm, tl, g.present, j);
      add_service(nm, tl, squashed[j], resolve, service_start, latency[j], j);
      expr release = mshr_release(nm, squashed[j], tl.live[j],
                                  resolve, tl.service_end[j], j);
      writeback_slot(nm, slot, g.min_free, release, window, j);
    }

  pin_resolve(nm, tl, resolve, branch);
  tl.completion = compute_completion(nm, tl, squashed);
  return tl;
}

// ---------------------------------------------------------------------------
// Workload synthesis and the shared speculation constraints.
// ---------------------------------------------------------------------------

// Synthesized workload: arrivals `arrival` and per-request memory latency `latency`
// (shared by both machines; completion order still diverges through `window` alone).
static void synthesize_workload(context& c, solver& sol,
                                std::vector<expr>& arrival,
                                std::vector<expr>& latency)
{
  using namespace cfg;
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
}

// Wrong-path speculation: mispredicted branch `branch` and shadow `squashed[]`
// (shared workload). Returns the branch index expr; fills `squashed`.
static expr add_speculation(context& c, solver& sol, std::vector<expr>& squashed)
{
  using namespace cfg;
  expr branch = c.int_const("branch");
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
  return branch;
}

// ---------------------------------------------------------------------------
// CLI + reporting.
// ---------------------------------------------------------------------------

// Parse optional solver timeout (default 60s, CLI arg in seconds). Returns false
// after printing usage on a malformed argument.
static bool parse_timeout(int argc, char** argv, unsigned& timeout_ms)
{
  timeout_ms = 60000u;
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
        return false;
      }
  return true;
}

static void print_banner(unsigned timeout_ms)
{
  using namespace cfg;
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
}

// Maximize Delta via incremental tightening. Each probe raises the floor
// monotonically, so earlier bounds are implied and we assert directly (no push/pop)
// -- this keeps Z3's learned lemmas for the final maximality proof instead of
// discarding them. Updates `m` and `best` to the largest witness proven.
static void maximize_delta(solver& sol, const expr& Delta, z3::model& m, int64_t& best)
{
  auto delta_of = [&](const z3::model& mm)
    { return mm.eval(Delta, true).get_numeral_int64(); };
  best = delta_of(m);
  std::cout << "SAT at Delta = " << best << " cycles; maximizing...\n";

  for (;;)
    {
      sol.add(Delta >= Delta.ctx().int_val((int)best + 1));
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
}

static int64_t model_int(z3::model& m, const expr& e)
{
  return m.eval(e, true).get_numeral_int64();
}

static int model_bit(z3::model& m, const expr& e)
{
  return m.eval(e, true).is_true() ? 1 : 0;
}

// Print a labeled row of one value per request; cell(i) supplies each cell.
// Label is left-justified to a fixed width so all columns line up.
template <class Cell>
static void print_row(const char* label, Cell&& cell)
{
  std::cout << "  " << std::left << std::setw(13) << label << std::right;
  for (int i = 0; i < cfg::UNROLL_DEPTH; ++i)
    std::cout << std::setw(4) << cell(i);
  std::cout << "\n";
}

// Shared workload (identical for both machines by construction).
static void print_workload(z3::model& m, const expr& branch,
                           const std::vector<expr>& arrival,
                           const std::vector<expr>& squashed)
{
  int br = (int)model_int(m, branch);
  std::cout << "Worst-case workload where more MLP is SLOWER "
               "(maximum deviation):\n\n";
  print_row("request", [](int i)  { return i; });
  print_row("arrival", [&](int i) { return model_int(m, arrival[i]); });
  print_row("branch-path", [&](int i)
      { return i == br ? "BR" : model_bit(m, squashed[i]) ? "wp" : "."; });
  std::cout << "     (BR=mispredicted branch, wp=wrong-path shadow)\n\n";
}

// The only per-machine difference: wrong-path issue depth + outcome.
static void print_machine_summary(z3::model& m, const std::vector<expr>& squashed,
                                  const Timeline& high, const Timeline& low)
{
  auto depth = [&](const Timeline& tl)
    {
      int d = 0;
      for (int j = 0; j < cfg::UNROLL_DEPTH; ++j)
        if (model_bit(m, squashed[j]) && model_bit(m, tl.live[j]))
          ++d;
      return d;
    };
  std::cout << "  machine        MSHR-window   wrong-path-on-bus   resolve-cycle   completion-cycle\n";
  auto line = [&](const char* name, int window, const Timeline& tl)
    {
      std::cout << "  " << std::left << std::setw(15) << name << std::right
                << std::setw(11) << window
                << std::setw(20) << depth(tl)
                << std::setw(16) << model_int(m, tl.resolve)
                << std::setw(19) << model_int(m, tl.completion) << "\n";
    };
  line("HighMLP", cfg::WINDOW_HIGH, high);
  line("LowMLP",  cfg::WINDOW_LOW,  low);
}

static void print_schedule_dump(z3::model& m, const char* name,
                                const std::vector<expr>& latency, const Timeline& tl)
{
  std::cout << "\n  [" << name << "] per-request schedule\n";
  print_row("sstart", [&](int i) { return model_int(m, tl.service_start[i]); });
  print_row("send",   [&](int i) { return model_int(m, tl.service_end[i]); });
  print_row("latency",[&](int i) { return model_int(m, latency[i]); });
  print_row("live",   [&](int i) { return model_bit(m, tl.live[i]); });
}

static void print_delta(z3::model& m, const Timeline& high, const Timeline& low)
{
  std::cout << "\n  Delta = completion(HighMLP) - completion(LowMLP) = "
            << (model_int(m, high.completion) - model_int(m, low.completion)) << " cycles.\n"
            << "  More MLP made it "
               "SLOWER.\n";
}

int main(int argc, char** argv)
{
  using namespace cfg;

  unsigned timeout_ms;
  if (!parse_timeout(argc, argv, timeout_ms))
    return 1;

  context c;
  solver sol(c);
  sol.set("timeout", timeout_ms);

  /* Simplex arithmetic core (2), not the default LRA (6): faster on this model's
     definitional-equality chains. */
  sol.set("arith.solver", (unsigned)2);

  std::vector<expr> arrival, latency;
  synthesize_workload(c, sol, arrival, latency);

  std::vector<expr> squashed;
  expr branch = add_speculation(c, sol, squashed);

  // Instantiate both machines.
  Timeline high = build_machine(c, sol, "High", WINDOW_HIGH,
                                arrival, latency, branch, squashed);
  Timeline low  = build_machine(c, sol, "Low",  WINDOW_LOW,
                                arrival, latency, branch, squashed);

  // Discovery: does completion_HighMLP > completion_LowMLP exist?
  expr Delta = c.int_const("Delta");
  sol.add(Delta == high.completion - low.completion);
  sol.add(Delta > 0);

  print_banner(timeout_ms);

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

  z3::model m = sol.get_model();
  int64_t best = 0;
  maximize_delta(sol, Delta, m, best);

  print_workload(m, branch, arrival, squashed);
  print_machine_summary(m, squashed, high, low);
  print_schedule_dump(m, "HighMLP", latency, high);
  print_schedule_dump(m, "LowMLP",  latency, low);
  print_delta(m, high, low);
  return 0;
}
