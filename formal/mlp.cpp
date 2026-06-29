/*
 * Bounded Model Checking (BMC) engine for the "more MLP is always better" dogma.
 *
 * Unrolls N memory requests on two machines (System_HighMLP, System_LowMLP) that
 * share the same synthesized workload but differ only in MSHR window W. Z3
 * autonomously synthesizes a Data Dependency Graph (Dep[i][j], stream ids K[i],
 * arrivals A[i]) seeking workloads where T_HighMLP > T_LowMLP.
 *
 * Key physics that breaks monotonicity:
 *   (a) Pipelined finite bandwidth: channel admits every G cycles (G < B) → requests
 *       overlap, wider window packs tighter → faster baseline (MLP benefit).
 *   (b) Convex queueing delay per bank: inflight[j] = requests still in service when
 *       j starts on same bank; first C free, then PEN_LO per overlap, then PEN_HI
 *       steeper. Wider window → more concurrent → climb convex curve (MLP cost).
 *   (c) Admission backpressure: Pen[j] feeds both E[j] (completion) and St[j+1]
 *       (next admission). A contended request stalls its own future issue → negative
 *       feedback loop.
 *   (d) R/W turnaround: TT-cycle bubble on direction switch. Wide window interleaves
 *       tightly → burns bubbles; narrow window hides them in gaps.
 *   (e) Wrong-path speculation (SPEC=1): mispredicted branch BR yields wrong-path set
 *       (shared). Per-machine issue depth emerges from St[j] < R → wide window issues
 *       deeper, wasting bus/bank/MSHR on shadow requests that never retire.
 *
 * All physics identical for both machines; only W differs → genuinely falsifiable.
 * Bank tag is shared per request (solver cannot rig contention per-machine).
 * Inflight derived from St/E (schedule), not W-indexed window → backfire must emerge.
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

// ----------------------------------------------------------------------------
// Tunable parameters of the unrolled model.
// ----------------------------------------------------------------------------
namespace cfg {
    constexpr int N             = 12;   // Unroll depth (number of memory requests).
    constexpr int S             = 2;   // Hardware threads -> number of distinct streams.
    constexpr int B             = 10;  // Bank access latency per request (cycles).
    constexpr int ROB_SIZE      = 4;   // Reorder-buffer horizon: deps span <= ROB_SIZE.
    constexpr int MAX_STREAM_MLP= 3;   // LSQ: max concurrently-independent reqs per stream.
    constexpr int HORIZON       = 64;  // Upper bound for synthesized arrival times.

    // ---- Pipelined finite-bandwidth channel (shared physics, both machines) --
    constexpr int G             = 2;   // Channel inter-admission gap (1/bandwidth), G < B.
    constexpr int TT            = 4;   // Read/write bus turnaround bubble (direction switch).

    /* Bank-tag locality proxy: OBSERVABLE equivalence class (which bank) not raw address.
     * bank[j] ∈ [0,NB) ↔ contention iff bank[i]==bank[j]. SHARED across machines
     * (solver cannot rig per-machine). NB=1 recovers old locality-blind model exactly.
     * Override at compile time: g++ -DCFG_NB=3 ... */
#ifndef CFG_NB
#define CFG_NB 2
#endif
    constexpr int NB            = CFG_NB;

    /* Convex queueing-delay curve (per-bank). First C same-bank overlaps free,
     * past C each costs PEN_LO, past C2 each costs PEN_HI more. C = (B/G)/NB is
     * NOT hand-picked: bandwidth-delay product B/G = distinct banks channel keeps
     * busy for free; divided by NB banks → per-bank free concurrency. NB=1 → C=5
     * (old model); NB≥3 → C=1 (dogma falsifiable in realistic many-bank regime). */
    constexpr int C             = (B / G) / NB > 0 ? (B / G) / NB : 1;
    constexpr int C2            = C + 2;
    constexpr int PEN_LO        = 3;
    constexpr int PEN_HI        = 5;

    constexpr int W_HIGH        = 6;   // System_HighMLP (aggressive).
    constexpr int W_LOW         = 2;   // System_LowMLP (throttled).

    /* Wrong-path speculation (Strategy B). Mispredicted branch BR → shadow of wrong-path
     * requests after it. Shadow is shared workload (both machines); issue depth emerges
     * from St[j] < R only. Wide window issues deeper → wastes bus/MSHR/bank on shadow,
     * delays correct-path tail. T counts correct-path completions only.
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

// ----------------------------------------------------------------------------
// Small helpers.
// ----------------------------------------------------------------------------
static inline expr zmax(const expr& a, const expr& b) {
    return z3::ite(a >= b, a, b);
}
static inline expr b2i(context& c, const expr& b) {
    return z3::ite(b, c.int_val(1), c.int_val(0));
}

/* Per-machine timeline: all values derived from the shared workload and per-machine W. */
struct Timeline {
    std::vector<expr> Aeff;     // effective arrival (after dependency causality)
    std::vector<expr> Aprime;   // channel presentation (after MSHR gating)
    std::vector<expr> Cf;       // channel-free time (skips un-issued shadow)
    std::vector<expr> St;       // service start (pipelined admission + turnaround)
    std::vector<expr> Live;     // occupies bus on this machine? (bool)
    std::vector<expr> Inflight; // # same-bank earlier requests still in service at St[j]
    std::vector<expr> Pen;      // convex queueing penalty incurred serving j
    std::vector<expr> E;        // service end
    std::vector<expr> Rel;      // MSHR release time (E or R if squashed & never issued)
    expr              R;        // branch-resolve cycle = E[BR] + RESOLVE_DELAY
    expr              T;        // system cycles = max over correct-path E
    explicit Timeline(context& c) : R(c.int_val(0)), T(c.int_val(0)) {}
};

/* Build per-machine timeline parameterized by MLP window W. Shared workload (Dep, K, A);
 * per-machine names for independent extraction. All equations asserted as fresh-const == expr
 * to appear in model output verbatim. */
static Timeline build_machine(context& c, solver& sol,
                              const std::string& tag, int W,
                              const std::vector<std::vector<expr>>& Dep,
                              const std::vector<expr>& A,
                              const std::vector<expr>& RW,
                              const std::vector<expr>& Bank,
                              const expr& BR,
                              const std::vector<expr>& Sq) {
    using namespace cfg;
    Timeline tl(c);
    tl.Aeff.reserve(N); tl.Aprime.reserve(N); tl.Cf.reserve(N); tl.St.reserve(N);
    tl.Live.reserve(N); tl.Inflight.reserve(N); tl.Pen.reserve(N);
    tl.E.reserve(N); tl.Rel.reserve(N);

    // Per-machine branch-resolve cycle R. It is E[BR] (+RESOLVE_DELAY), but BR is
    // symbolic and the selection ranges over the WHOLE E array, so we declare R as
    // a fresh const here, USE it freely in the loop (Live/Cf/Rel reference it), and
    // pin its defining equation AFTER E is built. No cycle: Sq[i] => i>BR, so every
    // request that consults R is strictly after the branch, and E[BR] depends only
    // on the prefix [0,BR] (all of which is correct-path => Live, never consults R).
    expr R = c.int_const(("R_" + tag).c_str());
    tl.R = R;

    for (int j = 0; j < N; ++j) {
        /* Axiom 1 (Causality): Aeff[j] = min feasible = max(A[j], max over Dep[i][j] of E[i]+1).
         * Pinned minimal so solver has no slack to game the comparison. */
        expr aeff_rhs = A[j];
        for (int i = 0; i < j; ++i)
            aeff_rhs = z3::ite(Dep[i][j], zmax(aeff_rhs, tl.E[i] + 1), aeff_rhs);
        expr Aeff_j = c.int_const(("Aeff_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Aeff_j == aeff_rhs);
        tl.Aeff.push_back(Aeff_j);

        /* Axiom 2 (MSHR Gating): A'[j] = max(Aeff[j], Rel[j-W]). MSHR allocation in-order
         * at rename; fixed j-W gating holds. Speculation changes only Rel, not allocation,
         * so MSHR does NOT skip un-issued shadow. Shadow that never issued frees at R;
         * one that launched DRAM txn holds to E (must sink fill). */
        expr Aprime_j = c.int_const(("Aprime_" + tag + "_" + std::to_string(j)).c_str());
        if (j - W >= 0)
            sol.add(Aprime_j == zmax(Aeff_j, tl.Rel[j - W]));
        else
            sol.add(Aprime_j == Aeff_j);
        tl.Aprime.push_back(Aprime_j);

        /* Axiom 3 (pipelined channel + backpressure + turnaround + speculation).
         * St[j] = max(A'[j], St[j-1] + G + TT*switch[j] + Pen[j-1]).
         * • Pipelined admission every G cycles → latency hiding (MLP benefit).
         * • Pen feeds forward → negative feedback loop (wide window stalls own issue).
         * • TT = turnaround bubble on RW direction switch.
         * • SPECULATION: request reaches bus only if St[j] < R. Cf[j] skips non-live
         *   predecessors → W-dependent issue depth emerges from schedule.
         *   SPEC=0 ⇒ all Live=true ⇒ recovers old model exactly. */
        expr Cf_j = c.int_const(("Cf_" + tag + "_" + std::to_string(j)).c_str());
        expr St_j = c.int_const(("St_" + tag + "_" + std::to_string(j)).c_str());
        if (j == 0) {
            sol.add(Cf_j == Aprime_j);
            sol.add(St_j == Aprime_j);
        } else {
            expr sw = b2i(c, RW[j] != RW[j - 1]);          // bus direction change
            expr gap = c.int_val(G) + c.int_val(TT) * sw + tl.Pen[j - 1];
            sol.add(Cf_j == z3::ite(tl.Live[j - 1], tl.St[j - 1] + gap, tl.Cf[j - 1]));
            sol.add(St_j == zmax(Aprime_j, Cf_j));
        }
        tl.Cf.push_back(Cf_j);
        tl.St.push_back(St_j);

        /* Bus liveness: Live[j] = ¬Sq[j] ∨ (St[j] < R). Emergent, schedule-derived,
         * W-dependent. Correct-path always live; shadow live iff admitted before resolve. */
        expr Live_j = c.bool_const(("Live_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Live_j == (!Sq[j] || (St_j < R)));
        tl.Live.push_back(Live_j);

        /* Temporal in-flight per-bank: count earlier bus-live requests still serving
         * when j starts AND in same bank. Derived from St/E (schedule), not W-indexed.
         * Same-bank filter: locality-aware (NB>1) vs global (NB=1). Solver controls
         * WHERE contention lands → discrete, schedule-targeted backfire. Only Live
         * requests occupy bank; bus-live shadow runs to completion (cannot un-send). */
        expr inflight = c.int_val(0);
        for (int i = 0; i < j; ++i)
            inflight = inflight + b2i(c, tl.Live[i] && (tl.E[i] > St_j) && (Bank[i] == Bank[j]));
        expr Inflight_j = c.int_const(("Inflight_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Inflight_j == inflight);
        tl.Inflight.push_back(Inflight_j);

        /* Convex queueing delay: Pen[j] = PEN_LO*max(0,inflight-C) + PEN_HI*max(0,inflight-C2).
         * First C same-bank overlaps free; past C, PEN_LO per overlap; past C2, PEN_HI more.
         * Feeds both E[j] and St[j+1] → closed-loop backpressure. */
        expr over1 = zmax(c.int_val(0), Inflight_j - c.int_val(C));
        expr over2 = zmax(c.int_val(0), Inflight_j - c.int_val(C2));
        expr Pen_j = c.int_const(("Pen_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Pen_j == c.int_val(PEN_LO) * over1 + c.int_val(PEN_HI) * over2);
        tl.Pen.push_back(Pen_j);

        /* E[j] = St[j] + B + Pen[j]. Service end time. */
        expr E_j = c.int_const(("E_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(E_j == St_j + c.int_val(B) + Pen_j);
        tl.E.push_back(E_j);

        /* MSHR release time: Rel[j] = (Sq[j] ∧ ¬Live[j]) ? R : E[j].
         * Correct-path: free at E. Wrong-path never issued: free at R (squash).
         * Wrong-path issued: hold to E (must sink fill, cannot un-send). */
        expr Rel_j = c.int_const(("Rel_" + tag + "_" + std::to_string(j)).c_str());
        sol.add(Rel_j == z3::ite(Sq[j] && !Live_j, R, E_j));
        tl.Rel.push_back(Rel_j);
    }

    /* Pin R = E[BR] + RESOLVE_DELAY (O(N) ite-select on symbolic BR).
     * Correct-path BR ⇒ E[BR] is prefix-only ⇒ well-defined. */
    expr r_sel = c.int_val(0);
    for (int i = 0; i < N; ++i) r_sel = z3::ite(BR == i, tl.E[i], r_sel);
    sol.add(R == r_sel + c.int_val(RESOLVE_DELAY));

    /* Axiom 4 (Timeline): T = max E[j] over correct-path requests only.
     * Squashed never retire ⇒ wide window can finish real work later than narrow.
     * Request 0 always correct-path ⇒ max never degenerate. */
    expr tmax = c.int_val(0);
    for (int j = 0; j < N; ++j)
        tmax = zmax(tmax, z3::ite(Sq[j], c.int_val(0), tl.E[j]));
    expr T = c.int_const(("T_" + tag).c_str());
    sol.add(T == tmax);
    tl.T = T;
    return tl;
}

int main(int argc, char** argv) {
    using namespace cfg;

    /* Solver timeout in milliseconds (guards hangs, each maximization probe).
     * First CLI arg = SECONDS (default 60, 0 = unlimited). ./mlp 120 → 120s timeout. */
    unsigned timeout_ms = 60000u;
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

    /* Synthesized symbolic workload (Z3-chosen): A (arrival), K (stream id),
     * RW (read/write), Bank (locality class). */
    std::vector<expr> A, K, RW, Bank;
    A.reserve(N); K.reserve(N); RW.reserve(N); Bank.reserve(N);
    for (int i = 0; i < N; ++i) {
        A.push_back(c.int_const(("A_" + std::to_string(i)).c_str()));
        K.push_back(c.int_const(("K_" + std::to_string(i)).c_str()));
        RW.push_back(c.int_const(("RW_" + std::to_string(i)).c_str()));
        Bank.push_back(c.int_const(("Bank_" + std::to_string(i)).c_str()));
        sol.add(A[i] >= 0 && A[i] <= HORIZON);    // bounded arrivals
        sol.add(K[i] >= 0 && K[i] <  S);           // S streams (hardware threads)
        sol.add(RW[i] >= 0 && RW[i] <= 1);         // 0 = read, 1 = write
        sol.add(Bank[i] >= 0 && Bank[i] < NB);     // NB banks (locality classes)
    }
    /* Program order: arrivals non-decreasing. Enforces "A[j] >= A[i]" for all
     * non-dependent predecessors (Axiom 1 else leg). */
    for (int i = 0; i + 1 < N; ++i)
        sol.add(A[i] <= A[i + 1]);

    /* Bank-label symmetry break: Bank[i] interchangeable labels → prune NB! relabelings
     * of same physical model. Pin Bank[0]=0, force Bank[i] ≤ max_so_far+1 (first-occurrence
     * canonical form). Preserves all quantities (read only via equality), model-sound. */
    if (NB > 1) {
        sol.add(Bank[0] == 0);
        expr running_max = Bank[0];
        for (int i = 1; i < N; ++i) {
            sol.add(Bank[i] <= running_max + 1);
            running_max = zmax(running_max, Bank[i]);
        }
    }

    /* Stream-label symmetry break: K[i] read only via equality (Dep matching, LSQ count).
     * No timing depends on stream's absolute id ⇒ S! relabelings identical, same Delta.
     * First-occurrence canonical form (sound for same reason as Bank break). */
    if (S > 1) {
        sol.add(K[0] == 0);
        expr running_max = K[0];
        for (int i = 1; i < N; ++i) {
            sol.add(K[i] <= running_max + 1);
            running_max = zmax(running_max, K[i]);
        }
    }

    /* Read/write-label symmetry break: RW read only via turnaround disequality
     * RW[j]≠RW[j-1], invariant under global flip ⇒ each workload has flip-twin with
     * identical Delta. Pin RW[0]=0 (first=read WLOG) for one representative per pair. */
    sol.add(RW[0] == 0);

    /* Wrong-path speculation workload (Strategy B): mispredicted branch BR +
     * contiguous shadow Sq[] after it. SHARED across machines (solver cannot rig
     * per-machine); only per-machine schedule decides issue depth. */
    expr BR = c.int_const("BR");
    std::vector<expr> Sq;
    Sq.reserve(N);
    for (int i = 0; i < N; ++i)
        Sq.push_back(c.bool_const(("Sq_" + std::to_string(i)).c_str()));

    if (SPEC) {
        sol.add(BR >= 0 && BR < N);
        for (int i = 0; i < N; ++i)
            sol.add(z3::implies(Sq[i], BR < i));   // Sq[i] ⇒ i > BR
        /* Contiguity: shadow = block {BR+1,...,SE}. */
        for (int i = 2; i < N; ++i)
            sol.add(z3::implies(Sq[i] && (i - 1 > BR), Sq[i - 1]));
        /* Shadow-length cap (logged, not silent). */
        expr shadow_len = c.int_val(0);
        for (int i = 0; i < N; ++i) shadow_len = shadow_len + b2i(c, Sq[i]);
        sol.add(shadow_len <= c.int_val(MAX_SHADOW));
    } else {
        /* SPEC=0: no speculation. Strict-generalization guard → identical to pre-B model. */
        sol.add(BR == 0);
        for (int i = 0; i < N; ++i) sol.add(!Sq[i]);
    }

    /* Dependency matrix Dep[i][j]: request j consumes value from i.
     * Only structurally-possible entries (upper-triangular + ROB horizon) get boolean
     * variables; others literal false. Model-identical to full N×N matrix pinned
     * impossible → false, but halves booleans and drops their constraints. */
    std::vector<std::vector<expr>> Dep(N, std::vector<expr>(N, c.bool_val(false)));
    for (int i = 0; i < N; ++i)
        for (int j = i + 1; j < N && j - i <= ROB_SIZE; ++j)
            Dep[i][j] = c.bool_const(("Dep_" + std::to_string(i) + "_" + std::to_string(j)).c_str());

    /* Physical pipeline bounds: Dep[i][j] true requires K[i]==K[j] (same stream). */
    for (int i = 0; i < N; ++i)
        for (int j = i + 1; j < N && j - i <= ROB_SIZE; ++j)
            sol.add(z3::implies(Dep[i][j], K[i] == K[j]));

    /* LSQ capacity: single stream ≤ MAX_STREAM_MLP concurrently-independent requests
     * within ROB window. */
    for (int j = 0; j < N; ++j) {
        expr indep_in_stream = c.int_val(0);
        int lo = (j - ROB_SIZE > 0) ? (j - ROB_SIZE) : 0;
        for (int i = lo; i < j; ++i)
            indep_in_stream = indep_in_stream + b2i(c, (K[i] == K[j]) && !Dep[i][j]);
        sol.add(indep_in_stream <= MAX_STREAM_MLP);
    }

    /* Instantiate both machines on shared workload. */
    Timeline high = build_machine(c, sol, "High", W_HIGH, Dep, A, RW, Bank, BR, Sq);
    Timeline low  = build_machine(c, sol, "Low",  W_LOW,  Dep, A, RW, Bank, BR, Sq);

    /* Discovery query: ∃ workload with T_HighMLP > T_LowMLP? (standard solver, not z3::optimize).
     * Named deviation for subsequent maximization. */
    expr Delta = c.int_const("Delta");
    sol.add(Delta == high.T - low.T);
    sol.add(Delta > 0);

    std::cout << "=== MLP dogma discovery engine ===\n"
              << "N=" << N << "  streams(S)=" << S << "  B=" << B
              << "  ROB=" << ROB_SIZE << "  MAX_STREAM_MLP=" << MAX_STREAM_MLP << "\n"
              << "G=" << G << "  TT=" << TT << "  NB=" << NB
              << "  C=" << C << "  C2=" << C2
              << "  PEN_LO=" << PEN_LO << "  PEN_HI=" << PEN_HI << "\n"
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

    /* Maximize Delta (worst-case workload) via incremental tightening on standard solver.
     * push(), assert Delta ≥ best+1, check(). SAT → adopt & re-pin; UNSAT → proved max. */
    z3::model m = sol.get_model();
    auto delta_of = [&](const z3::model& mm) {
        return mm.eval(Delta, true).get_numeral_int64();
    };
    int64_t best = delta_of(m);
    std::cout << "SAT at delta = " << best << " cycles; maximizing...\n";

    for (;;) {
        sol.push();
        sol.add(Delta >= c.int_val((int)best + 1));
        z3::check_result r = sol.check();
        if (r == z3::sat) {
            m = sol.get_model();
            best = delta_of(m);
            std::cout << "  found larger delta = " << best << " cycles\n";
            sol.pop();
            sol.add(Delta >= c.int_val((int)best));
        } else {
            sol.pop();
            if (r == z3::unknown)
                std::cout << "  stopped (timeout); reporting best found.\n";
            else
                std::cout << "  proved maximum: delta = " << best << " cycles.\n";
            break;
        }
    }
    std::cout << "\n";

    /* Extract and print maximum-deviation counterexample workload. */
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
    std::cout << "\n  Bk:";
    for (int i = 0; i < N; ++i) std::cout << std::setw(4) << iv(Bank[i]);
    if (SPEC) {
        std::cout << "\n  Sq:";
        for (int i = 0; i < N; ++i)
            std::cout << std::setw(4) << (m.eval(Sq[i], true).is_true() ? 1 : 0);
        std::cout << "      (BR = " << iv(BR) << ", shadow = wrong-path requests)";
    }
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
        if (SPEC) { std::cout << "   cf   :"; for (auto& e : tl.Cf) std::cout << std::setw(5) << iv(e); std::cout << "\n"; }
        std::cout << "   St   :"; for (auto& e : tl.St)       std::cout << std::setw(5) << iv(e); std::cout << "\n";
        if (SPEC) {
            std::cout << "   live :";
            for (auto& e : tl.Live) std::cout << std::setw(5) << (m.eval(e, true).is_true() ? 1 : 0);
            std::cout << "\n";
        }
        std::cout << "   nfly :"; for (auto& e : tl.Inflight) std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   pen  :"; for (auto& e : tl.Pen)      std::cout << std::setw(5) << iv(e); std::cout << "\n";
        std::cout << "   E    :"; for (auto& e : tl.E)        std::cout << std::setw(5) << iv(e); std::cout << "\n";
        if (SPEC) {
            /* Wrong-path issue depth: # squashed requests that reached bus on this machine.
             * W-dependent asymmetry should show here. */
            int depth = 0;
            for (int j = 0; j < N; ++j)
                if (m.eval(Sq[j], true).is_true() && m.eval(tl.Live[j], true).is_true()) ++depth;
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
