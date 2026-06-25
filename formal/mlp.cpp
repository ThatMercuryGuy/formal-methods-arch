// mlp.cpp
// =============================================================================
// Bounded Model Checking (BMC) engine for the "more MLP is always better" dogma.
//
// We unroll a sequence of N memory requests and evaluate it on TWO parallel
// mathematical state machines that share the SAME synthesized workload but differ
// only in their Memory-Level-Parallelism budget (the MSHR / outstanding-miss
// window W):
//
//     System_HighMLP : large W  -> issues aggressively, many requests in flight
//     System_LowMLP  : small W  -> throttles issue, few requests in flight
//
// Z3 autonomously synthesizes a *pure Data Dependency Graph* (the boolean matrix
// Dep[i][j], stream ids K[i], and program-order arrivals A[i]) such that the
// High-MLP machine takes STRICTLY MORE total cycles than the Low-MLP machine.
// No heuristics, no hand-built schedule: the solver discovers the adversarial
// workload on its own.
//
// ---------------------------------------------------------------------------
// MODELING NOTE (important):
//
//   A purely serial, work-conserving channel is *monotone* in the MLP budget:
//   a larger window lets requests present no later, so completion times can
//   only fall. Under such a model "T_HighMLP > T_LowMLP" is UNSAT and the
//   discovery engine finds nothing. The realistic effects that BREAK that
//   monotonicity -- and that we model here in Axiom 3 -- are the genuine
//   physics of a shared, pipelined memory channel:
//
//     (a) PIPELINED FINITE BANDWIDTH. The channel admits a new request every G
//         cycles (G < B), so requests OVERLAP in flight -- this is the latency
//         hiding that makes MLP beneficial in the first place. A wider window
//         packs requests tighter against the G bound and finishes earlier:
//         the BENEFIT side of MLP.
//
//     (b) CONVEX QUEUEING DELAY, measured in TIME not index. We count how many
//         earlier requests are still in service when j starts (inflight[j]),
//         and charge a convex cost: the first C overlaps are free (bank-level
//         parallelism), past C each costs PEN_LO, past C2 each costs PEN_HI
//         more. A wide window drives more requests concurrently in flight,
//         climbing the convex curve: the COST side of MLP. Because inflight is
//         derived from the schedule (St/E), not from a W-indexed window, the
//         cost is NOT monotone-by-construction -- the backfire must EMERGE.
//
//     (c) CROSS-THREAD INTERFERENCE. inflight[j] counts in-flight requests from
//         ALL streams on the shared channel, so an aggressive (wide-W) stream
//         floods the channel and delays another stream's critical request.
//
//     (d) READ/WRITE TURNAROUND. The bus pays a TT-cycle bubble whenever the
//         service direction switches (read<->write). A wide window interleaves
//         R/W tightly and eats the bubbles; a throttled window hides them in
//         gaps it already had.
//
//   The physics is IDENTICAL for both machines; the ONLY thing that differs is
//   the MLP window W. Whether wide is faster or slower is now genuinely
//   workload-dependent -- which is what makes the dogma honestly falsifiable.
//
// Build:
//   g++ -std=c++23 mlp.cpp -lz3 -o mlp -Ofast -march=native
// =============================================================================

#include <z3++.h>
#include <vector>
#include <string>
#include <iostream>
#include <iomanip>

using z3::expr;
using z3::context;
using z3::solver;

// ----------------------------------------------------------------------------
// Tunable parameters of the unrolled model.
// ----------------------------------------------------------------------------
namespace cfg {
    constexpr int N             = 8;   // Unroll depth (number of memory requests).
    constexpr int S             = 2;   // Hardware threads -> number of distinct streams.
    constexpr int B             = 10;  // Bank access latency per request (cycles).
    constexpr int ROB_SIZE      = 4;   // Reorder-buffer horizon: deps span <= ROB_SIZE.
    constexpr int MAX_STREAM_MLP= 3;   // LSQ: max concurrently-independent reqs per stream.
    constexpr int HORIZON       = 64;  // Upper bound for synthesized arrival times.

    // ---- Pipelined finite-bandwidth channel (shared physics, both machines) --
    constexpr int G             = 2;   // Channel inter-admission gap (1/bandwidth), G < B.
    constexpr int TT            = 4;   // Read/write bus turnaround bubble (direction switch).

    // ---- Convex queueing-delay curve (shared physics, both machines) ---------
    // The first C concurrently in-flight requests are FREE (bank-level
    // parallelism / bus pipelining doing their job). Past C each costs PEN_LO;
    // past the steeper knee C2 each ADDITIONALLY costs PEN_HI. This is a convex
    // cost with a service-rate knee, not a flat per-overlap tax.
    //
    // C is NOT hand-picked: a pipelined channel of service latency B admitting a
    // new request every G cycles sustains ~B/G requests in flight for free (the
    // bandwidth-delay product). Tying C := B/G makes the free-concurrency budget
    // a DERIVED property of the channel physics rather than an asserted constant.
    constexpr int C             = B / G; // Free concurrency = bandwidth-delay product.
    constexpr int C2            = C + 2;  // Steeper knee: just beyond the free regime.
    constexpr int PEN_LO        = 3;   // Per-overlap cost in the [C, C2) regime.
    constexpr int PEN_HI        = 5;   // Additional per-overlap cost beyond C2.

    // The single differentiating knob: the MLP / MSHR outstanding-miss window.
    constexpr int W_HIGH        = 6;   // System_HighMLP: 6 MSHRs (aggressive issue).
    constexpr int W_LOW         = 2;   // System_LowMLP : 2 MSHRs (throttled issue).

    static_assert(G < B, "channel must pipeline: admission gap G must be < B");
    static_assert(C < C2, "convex knee C2 must lie beyond the free regime C");
}

// ----------------------------------------------------------------------------
// Small helpers.
// ----------------------------------------------------------------------------
static inline expr zmax(const expr& a, const expr& b) {
    return z3::ite(a >= b, a, b);
}
static inline expr b2i(context& c, const expr& b) {
    return z3::ite(b, c.int_val(1), c.int_val(0));
}

// ----------------------------------------------------------------------------
// Result of evaluating one machine: the completion-time array E[] and the
// system timeline T = max(E).
// ----------------------------------------------------------------------------
struct Timeline {
    std::vector<expr> Aeff;     // effective core arrival (after dependency causality)
    std::vector<expr> Aprime;   // channel-presentation time (after MSHR gating)
    std::vector<expr> St;       // channel service start (pipelined admission + turnaround)
    std::vector<expr> Inflight; // # earlier requests still in service when j starts
    std::vector<expr> Pen;      // convex queueing penalty incurred while serving j
    std::vector<expr> E;        // channel service end
    expr              T;        // system cycles = max over E
    explicit Timeline(context& c) : T(c.int_val(0)) {}
};

// ----------------------------------------------------------------------------
// Build the full timeline of one machine, parameterized by its MLP window W.
// The synthesized workload (Dep, K, A) is shared across machines; everything
// computed here is named per-machine so the model is independently extractable.
// All defining equations are asserted as fresh-const == expr so they show up in
// the model and can be printed back verbatim.
// ----------------------------------------------------------------------------
static Timeline build_machine(context& c, solver& sol,
                              const std::string& tag, int W,
                              const std::vector<std::vector<expr>>& Dep,
                              const std::vector<expr>& A,
                              const std::vector<expr>& RW) {
    using namespace cfg;
    Timeline tl(c);
    tl.Aeff.reserve(N); tl.Aprime.reserve(N); tl.St.reserve(N);
    tl.Inflight.reserve(N); tl.Pen.reserve(N); tl.E.reserve(N);

    for (int j = 0; j < N; ++j) {
        // ---- Axiom 1 (Causality) ---------------------------------------------
        //   if Dep[i][j]:  Aeff[j] >= E[i] + 1   (must wait for producer to retire)
        //   else        :  Aeff[j] >= A[i]       (program order, enforced globally
        //                                         via the monotone A constraint)
        // We pin Aeff[j] to its minimal feasible value so the comparison between
        // machines is well-defined (no slack for the solver to game).
        expr aeff_rhs = A[j];
        for (int i = 0; i < j; ++i)
            aeff_rhs = z3::ite(Dep[i][j], zmax(aeff_rhs, tl.E[i] + 1), aeff_rhs);
        expr Aeff_j = c.int_const(("Aeff_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Aeff_j == aeff_rhs);
        tl.Aeff.push_back(Aeff_j);

        // ---- Axiom 2 (MSHR Gating) -------------------------------------------
        // A request cannot present to the bus until an outstanding-miss slot is
        // free. With a W-deep MSHR file, request j must wait for the request
        // W positions earlier to complete:  A'[j] = max(Aeff[j], E[j-W]).
        expr Aprime_j = c.int_const(("Aprime_" + tag + "_" + std::to_string(j)).c_str());
        if (j - W >= 0)
            sol.add(Aprime_j == zmax(Aeff_j, tl.E[j - W]));
        else
            sol.add(Aprime_j == Aeff_j);
        tl.Aprime.push_back(Aprime_j);

        // ---- Axiom 3 (pipelined channel + convex queueing + turnaround) ------
        // Channel start: the bus admits a new request every G cycles (pipelined
        // finite bandwidth, G < B, so requests OVERLAP in flight -- this is the
        // latency hiding MLP exists to exploit), plus a TT-cycle turnaround
        // bubble whenever the service direction switches read<->write, PLUS the
        // queueing penalty the previous request is still paying:
        //   St[j] = max( A'[j], St[j-1] + G + TT*switch[j] + Pen[j-1] )
        //
        // BACKPRESSURE (the closed loop): the convex contention penalty is not a
        // dead-end term on E -- it feeds FORWARD into the next admission. A
        // request that the channel is serving slowly (because it is one of many
        // in flight) holds the resource longer, so the next request admits later.
        // Because St is a forward chain, this penalty COMPOUNDS across every later
        // request: a wide window that floods the channel stalls its OWN future
        // issue, not just the contended request's completion. This is what makes
        // "more MLP is worse" physically possible rather than impossible by
        // construction. St stays non-decreasing in index (no reordering modeled).
        expr St_j = c.int_const(("St_" + tag + "_" + std::to_string(j)).c_str());
        if (j == 0) {
            sol.add(St_j == Aprime_j);
        } else {
            expr sw = b2i(c, RW[j] != RW[j - 1]);          // bus direction change
            expr gap = c.int_val(G) + c.int_val(TT) * sw + tl.Pen[j - 1];
            sol.add(St_j == zmax(Aprime_j, tl.St[j - 1] + gap));
        }
        tl.St.push_back(St_j);

        // Temporal in-flight count: how many EARLIER requests are still being
        // served when j starts on the channel. This is measured in TIME (via
        // St/E), not in an index window, and it spans ALL streams -- so it
        // captures cross-thread interference and depends on W only THROUGH the
        // schedule. That is what keeps the contention non-monotone-by-design.
        expr inflight = c.int_val(0);
        for (int i = 0; i < j; ++i)
            inflight = inflight + b2i(c, tl.E[i] > St_j);
        expr Inflight_j = c.int_const(("Inflight_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Inflight_j == inflight);
        tl.Inflight.push_back(Inflight_j);

        // Convex queueing delay: the first C overlaps are FREE (bank-level
        // parallelism / bus pipelining), past C each costs PEN_LO, and past the
        // steeper knee C2 each costs PEN_HI more.
        //   Pen[j] = PEN_LO*max(0, inflight-C) + PEN_HI*max(0, inflight-C2)
        // Named so it can both extend E[j] (slower completion) AND back-pressure
        // the next admission St[j+1] (the closed loop -- see Axiom 3 above).
        expr over1 = zmax(c.int_val(0), Inflight_j - c.int_val(C));
        expr over2 = zmax(c.int_val(0), Inflight_j - c.int_val(C2));
        expr Pen_j = c.int_const(("Pen_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Pen_j == c.int_val(PEN_LO) * over1 + c.int_val(PEN_HI) * over2);
        tl.Pen.push_back(Pen_j);

        //   E[j] = St[j] + B + Pen[j]
        expr E_j = c.int_const(("E_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(E_j == St_j + c.int_val(B) + Pen_j);
        tl.E.push_back(E_j);
    }

    // ---- Axiom 4 (Timeline) --------------------------------------------------
    // System cycles = maximum completion time over the whole window.
    expr tmax = tl.E[0];
    for (int j = 1; j < N; ++j) tmax = zmax(tmax, tl.E[j]);
    expr T = c.int_const(("T_" + tag).c_str());
    sol.add(T == tmax);
    tl.T = T;
    return tl;
}

int main(int argc, char** argv) {
    using namespace cfg;

    // Solver timeout (milliseconds) -- guard against pathological hangs and the
    // bound on each maximization probe. Overridable as the first CLI argument,
    // interpreted as SECONDS for convenience:  ./mlp 120  -> 120s timeout.
    unsigned timeout_ms = 60000u;       // 60s default.
    if (argc > 1) {
        try {
            size_t pos = 0;
            double secs = std::stod(argv[1], &pos);
            if (pos != std::string(argv[1]).size() || secs < 0.0)
                throw std::invalid_argument("");
            timeout_ms = static_cast<unsigned>(secs * 1000.0);
        } catch (const std::exception&) {
            std::cerr << "usage: " << argv[0] << " [timeout_seconds]\n"
                         "  timeout_seconds: solver timeout in seconds "
                         "(default 60; 0 = no timeout)\n";
            return 1;
        }
    }

    context c;
    solver sol(c);
    sol.set("timeout", timeout_ms); // guard against pathological hangs.

    // -------------------------------------------------------------------------
    // Synthesized symbolic workload (what Z3 gets to choose).
    // -------------------------------------------------------------------------
    std::vector<expr> A, K, RW;                   // A: arrival, K: stream id, RW: read/write
    A.reserve(N); K.reserve(N); RW.reserve(N);
    for (int i = 0; i < N; ++i) {
        A.push_back(c.int_const(("A_" + std::to_string(i)).c_str()));
        K.push_back(c.int_const(("K_" + std::to_string(i)).c_str()));
        RW.push_back(c.int_const(("RW_" + std::to_string(i)).c_str()));
        sol.add(A[i] >= 0 && A[i] <= HORIZON);    // bounded arrivals
        sol.add(K[i] >= 0 && K[i] <  S);           // S streams (hardware threads)
        sol.add(RW[i] >= 0 && RW[i] <= 1);         // 0 = read, 1 = write
    }
    // Program order: arrivals are non-decreasing. This realizes the "else
    // A[j] >= A[i]" leg of Axiom 1 for all non-dependent predecessors at once.
    for (int i = 0; i + 1 < N; ++i)
        sol.add(A[i] <= A[i + 1]);

    // Dependency matrix Dep[i][j]: "request j consumes a value produced by i".
    std::vector<std::vector<expr>> Dep(N, std::vector<expr>(N, c.bool_val(false)));
    for (int i = 0; i < N; ++i)
        for (int j = 0; j < N; ++j)
            Dep[i][j] = c.bool_const(("Dep_" + std::to_string(i) + "_" + std::to_string(j)).c_str());

    // -------------------------------------------------------------------------
    // Physical pipeline bounds on the dependency matrix.
    // -------------------------------------------------------------------------
    for (int i = 0; i < N; ++i) {
        for (int j = 0; j < N; ++j) {
            // Strictly upper triangular: a request may only depend on an earlier one.
            if (i >= j) { sol.add(!Dep[i][j]); continue; }
            // ROB horizon: dependencies cannot reach beyond the reorder window.
            if (j - i > ROB_SIZE) { sol.add(!Dep[i][j]); continue; }
            // Stream matching: a true dependency requires identical stream ids.
            sol.add(z3::implies(Dep[i][j], K[i] == K[j]));
        }
    }

    // LSQ capacity (MAX_STREAM_MLP): within the ROB window, a single stream may
    // have at most MAX_STREAM_MLP concurrently-INDEPENDENT (no true-dep) requests.
    for (int j = 0; j < N; ++j) {
        expr indep_in_stream = c.int_val(0);
        int lo = (j - ROB_SIZE > 0) ? (j - ROB_SIZE) : 0;
        for (int i = lo; i < j; ++i)
            indep_in_stream = indep_in_stream + b2i(c, (K[i] == K[j]) && !Dep[i][j]);
        sol.add(indep_in_stream <= MAX_STREAM_MLP);
    }

    // -------------------------------------------------------------------------
    // Instantiate both machines over the shared workload.
    // -------------------------------------------------------------------------
    Timeline high = build_machine(c, sol, "High", W_HIGH, Dep, A, RW);
    Timeline low  = build_machine(c, sol, "Low",  W_LOW,  Dep, A, RW);

    // -------------------------------------------------------------------------
    // The discovery query: does there exist a workload on which MORE MLP is WORSE?
    //   (standard solver, NOT z3::optimize)
    // -------------------------------------------------------------------------
    // Named deviation so we can both query it and, once SAT, maximize it.
    expr Delta = c.int_const("Delta");
    sol.add(Delta == high.T - low.T);
    sol.add(Delta > 0);   // i.e. T_HighMLP > T_LowMLP

    std::cout << "=== MLP dogma discovery engine ===\n"
              << "N=" << N << "  streams(S)=" << S << "  B=" << B
              << "  ROB=" << ROB_SIZE << "  MAX_STREAM_MLP=" << MAX_STREAM_MLP << "\n"
              << "G=" << G << "  TT=" << TT << "  C=" << C << "  C2=" << C2
              << "  PEN_LO=" << PEN_LO << "  PEN_HI=" << PEN_HI << "\n"
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

    // -------------------------------------------------------------------------
    // SAT: the dogma is falsifiable. Now find the WORST-CASE workload -- the one
    // that maximizes the deviation Delta = T_HighMLP - T_LowMLP -- WITHOUT using
    // z3::optimize. We tighten incrementally on the standard solver: keep the
    // best model found, assert Delta >= best+1, and re-solve. The first UNSAT
    // proves the previous Delta was the maximum achievable within these bounds.
    // -------------------------------------------------------------------------
    z3::model m = sol.get_model();
    auto delta_of = [&](const z3::model& mm) {
        return mm.eval(Delta, true).get_numeral_int64();
    };
    int64_t best = delta_of(m);
    std::cout << "SAT at delta = " << best << " cycles; maximizing...\n";

    for (;;) {
        sol.push();                         // checkpoint before the tighter bound
        sol.add(Delta >= c.int_val((int)best + 1));
        z3::check_result r = sol.check();
        if (r == z3::sat) {
            m = sol.get_model();            // strictly better witness
            best = delta_of(m);
            std::cout << "  found larger delta = " << best << " cycles\n";
            sol.pop();                      // drop the bound; model m is retained
            sol.add(Delta >= c.int_val((int)best)); // re-pin the floor permanently
        } else {
            sol.pop();                      // no larger delta exists (or timeout)
            if (r == z3::unknown)
                std::cout << "  stopped (solver gave up proving a larger delta: "
                          << sol.reason_unknown() << "); reporting best found.\n";
            else
                std::cout << "  proved maximum: no workload exceeds delta = "
                          << best << " cycles.\n";
            break;
        }
    }
    std::cout << "\n";

    // -------------------------------------------------------------------------
    // Extract and print the maximum-deviation counterexample workload.
    // -------------------------------------------------------------------------
    auto iv = [&](const expr& e) { return m.eval(e, true).get_numeral_int64(); };

    std::cout << "Worst-case workload where High-MLP is SLOWER "
                 "(maximum deviation):\n\n";

    std::cout << "Stream K[i], arrival A[i], type RW[i] (0=read,1=write):\n  i :";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << i;
    std::cout << "\n  K :";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(K[i]);
    std::cout << "\n  A :";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(A[i]);
    std::cout << "\n  RW:";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(RW[i]);
    std::cout << "\n\n";

    std::cout << "Dependency matrix Dep[i][j]  (1 => j consumes i's result):\n     j:";
    for (int j = 0; j < N; ++j) std::cout << std::setw(3) << j;
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
        std::cout << "   Aeff :"; for (auto& e : tl.Aeff)     std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   A'   :"; for (auto& e : tl.Aprime)   std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   St   :"; for (auto& e : tl.St)       std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   nfly :"; for (auto& e : tl.Inflight) std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   pen  :"; for (auto& e : tl.Pen)      std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   E    :"; for (auto& e : tl.E)        std::cout << std::setw(5) << iv(e); std::cout << "\n";
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
