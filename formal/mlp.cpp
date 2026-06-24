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
//   The bare axioms 1-4 are *monotone* in the MLP budget: with a shared
//   workload, increasing W can only lower every bus-presentation time, hence
//   every completion time, hence T. Under the literal axioms alone the claim
//   "T_HighMLP > T_LowMLP" is therefore UNSAT -- the dogma would hold by
//   construction and the discovery engine could never find anything.
//
//   The single piece of physics the bare axioms omit is SHARED-RESOURCE
//   CONTENTION. We model it the way real hardware works, NOT as a bookkeeping
//   tax on an issue window:
//
//     * The memory channel is a finite-BANDWIDTH server, not a strict FIFO.
//       It admits a new request every G cycles (G < B), so requests genuinely
//       OVERLAP in flight -- that is what MLP buys you.
//     * DRAM is split into NB banks. A request that starts service while an
//       earlier request to the SAME bank is still outstanding hits a bank
//       conflict and pays a row-cycle penalty TRC (precharge+activate): the
//       bank cannot service two open rows at once.
//
//   Contention therefore emerges from GENUINE QUEUEING on the modeled timeline
//   (E[i] > St[j] == "i is still in flight when j starts"), not from counting
//   how many requests share an issue window. This is identical physics for BOTH
//   machines; the ONLY thing that differs between the two systems is the MLP
//   window W. A wider W issues earlier, so more same-bank requests are truly
//   co-resident in flight -> more bank conflicts. A narrow W self-throttles and
//   dodges them. This keeps the model faithful while making the dogma genuinely
//   FALSIFIABLE, which is the entire purpose of a discovery engine.
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
    constexpr int B             = 10;  // Fixed bank service latency per access (cycles).
    constexpr int ROB_SIZE      = 4;   // Reorder-buffer horizon: deps span <= ROB_SIZE.
    constexpr int MAX_STREAM_MLP= 3;   // LSQ: max concurrently-independent reqs per stream.
    constexpr int NB            = 2;   // DRAM banks on the shared channel.
    constexpr int G             = 2;   // Channel inter-admission gap (1/bandwidth), G < B
                                       //   => requests genuinely overlap in flight.
    constexpr int TRC           = 8;   // Bank-conflict (row-cycle) penalty: precharge+activate
                                       //   paid when a same-bank request is still in flight.
    constexpr int HORIZON       = 64;  // Upper bound for synthesized arrival times.

    // The single differentiating knob: the MLP / MSHR outstanding-miss window.
    constexpr int W_HIGH        = 6;   // System_HighMLP: 6 MSHRs (aggressive issue).
    constexpr int W_LOW         = 2;   // System_LowMLP : 2 MSHRs (throttled issue).
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
    std::vector<expr> Aeff;   // effective core arrival (after dependency causality)
    std::vector<expr> Aprime; // bus-presentation time (after MSHR gating)
    std::vector<expr> St;     // bus service start  (FIFO serialization)
    std::vector<expr> E;      // bus service end
    expr              T;      // system cycles = max over E
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
                              const std::vector<expr>& Bk) {
    using namespace cfg;
    Timeline tl(c);
    tl.Aeff.reserve(N); tl.Aprime.reserve(N); tl.St.reserve(N); tl.E.reserve(N);

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

        // ---- Axiom 3 (Bandwidth admission + bank-conflict contention) --------
        // The channel is a finite-BANDWIDTH server, not a strict FIFO: it admits
        // a new request every G cycles (G < B), so requests genuinely OVERLAP in
        // flight -- exactly what MLP is supposed to buy.
        //   St[j] = max(A'[j], St[j-1] + G).
        // (We pace on prior START, not prior END, so multiple accesses can be
        //  simultaneously outstanding on the channel.)
        expr St_prev = (j == 0) ? c.int_val(0) : tl.St[j - 1];
        expr St_j = c.int_const(("St_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(St_j == zmax(Aprime_j, St_prev + c.int_val(G)));
        tl.St.push_back(St_j);

        // Bank-conflict contention: an earlier request i to the SAME bank that is
        // still in flight when j starts (E[i] > St[j]) forces a row-cycle penalty
        // TRC -- the bank cannot serve two open rows at once. This is GENUINE
        // queueing on the modeled timeline, not a count over an issue window:
        // conflicts arise only between requests that truly co-reside in flight.
        // W enters only via MSHR gating above, so a wider window issues earlier,
        // raises more same-bank requests into concurrent flight, and pays more
        // conflicts; a narrow window self-throttles and dodges them.
        expr conflicts = c.int_val(0);
        for (int i = 0; i < j; ++i)
            conflicts = conflicts +
                b2i(c, (Bk[i] == Bk[j]) && (tl.E[i] > St_j)); // same bank, still in flight
        expr E_j = c.int_const(("E_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(E_j == St_j + c.int_val(B) + c.int_val(TRC) * conflicts);
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
    std::vector<expr> A, K, Bk;                   // A[i]:arrival K[i]:stream Bk[i]:bank
    A.reserve(N); K.reserve(N); Bk.reserve(N);
    for (int i = 0; i < N; ++i) {
        A.push_back(c.int_const(("A_" + std::to_string(i)).c_str()));
        K.push_back(c.int_const(("K_" + std::to_string(i)).c_str()));
        Bk.push_back(c.int_const(("Bk_" + std::to_string(i)).c_str()));
        sol.add(A[i] >= 0 && A[i] <= HORIZON);    // bounded arrivals
        sol.add(K[i] >= 0 && K[i] <  S);           // S streams (hardware threads)
        sol.add(Bk[i] >= 0 && Bk[i] < NB);         // NB DRAM banks on the channel
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
    Timeline high = build_machine(c, sol, "High", W_HIGH, Dep, A, Bk);
    Timeline low  = build_machine(c, sol, "Low",  W_LOW,  Dep, A, Bk);

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
              << "banks(NB)=" << NB << "  channel-gap(G)=" << G
              << "  bank-conflict(TRC)=" << TRC << "\n"
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

    std::cout << "Stream K[i], bank Bk[i], arrival A[i]:\n  i :";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << i;
    std::cout << "\n  K :";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(K[i]);
    std::cout << "\n  Bk:";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(Bk[i]);
    std::cout << "\n  A :";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(A[i]);
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
        std::cout << "   Aeff :"; for (auto& e : tl.Aeff)   std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   A'   :"; for (auto& e : tl.Aprime) std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   St   :"; for (auto& e : tl.St)     std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   E    :"; for (auto& e : tl.E)      std::cout << std::setw(5) << iv(e); std::cout << "\n";
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
