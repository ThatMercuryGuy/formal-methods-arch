# Cache Replacement Facts & Relations

## Unconditional Facts

**F1.** Miss Rate / Hit Rate Complement

Every access either hits or misses; the two rates sum to one.

$$\text{MissRate}[C] + \text{HitRate}[C] = 1$$

**F2.** Hit Rate Bounded

Hit rate is a ratio of hits to total accesses, so it cannot exceed 1.

$$\text{HitRate}[C] \leq 1$$

**F3.** Compulsory Misses Policy-Independent

The first access to any block always misses regardless of which replacement policy is in use, so compulsory miss counts are identical across policies on the same workload and geometry.

$$\text{Size}[C_a] = \text{Size}[C_b] \;\wedge\; \text{Assoc}[C_a] = \text{Assoc}[C_b] \implies \text{CompulsoryMisses}[C_a] = \text{CompulsoryMisses}[C_b]$$

**F4.** Replacement Only Affects Capacity + Conflict

Since compulsory misses are fixed, the only way two policies can differ in total misses is through their capacity and conflict miss counts.

$$\text{Size}[C_a] = \text{Size}[C_b] \;\wedge\; \text{Assoc}[C_a] = \text{Assoc}[C_b]$$
$$\implies \text{MissCount}[C_a] - \text{MissCount}[C_b] = \bigl(\text{CapMisses}[C_a] + \text{ConflMisses}[C_a]\bigr) - \bigl(\text{CapMisses}[C_b] + \text{ConflMisses}[C_b]\bigr)$$

**F5.** Full Associativity Eliminates Conflict Misses

When every block in the cache can map to any set (i.e., there's only one set), conflicts are impossible by definition.

$$\text{Assoc}[C] = \frac{\text{Size}[C]}{B} \implies \text{ConflictMisses}[C] = 0$$

**F6.** Stack Policy Inclusion

Under a stack algorithm, the contents of a smaller cache are always a subset of a larger cache, so the larger cache can never have more misses.

$$\text{Size}[C_{\text{large}}] \geq \text{Size}[C_{\text{small}}] \implies \text{MissCount}[C_{\text{large}}] \leq \text{MissCount}[C_{\text{small}}]$$

Domain: LRU or any stack algorithm. Same workload, same associativity.

---

## Cache Size & Associativity Properties

**R3.** Larger Cache → Higher Hit Rate

More capacity means more of the working set can stay resident, so hit rate should not decrease when the cache grows.

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{HitRate}[C_a] \geq \text{HitRate}[C_b] + \varepsilon_3$$

Domain: any policy $P$, same workload and associativity.

**R4.** Diminishing Returns of Associativity

Doubling the number of ways helps, but each successive doubling helps less — the hit rate improvement is concave in associativity.

$$\text{Size}[C_a] = \text{Size}[C_b] = \text{Size}[C_c], \quad 2\,\text{Assoc}[C_a] = \text{Assoc}[C_b], \quad 2\,\text{Assoc}[C_b] = \text{Assoc}[C_c]$$
$$\implies \bigl(\text{HitRate}[C_c] - \text{HitRate}[C_b]\bigr) \leq \bigl(\text{HitRate}[C_b] - \text{HitRate}[C_a]\bigr) + \varepsilon_{12}$$

Domain: any policy $P$, fixed size, single workload.

**R6.** Working Set Fits → All Hits

If the entire working set fits in the cache, every block eventually becomes resident and stays there, yielding near-perfect hits after warmup.

$$\text{WSS}[W] \leq \frac{\text{Size}[C]}{B} \implies \text{HitRate}[C] \geq 1 - \varepsilon_6$$

Domain: any demand-fetch policy, fully-associative or high-assoc, after warmup.

**R7.** Miss Rate Monotone in Reuse Distance

A workload with shorter average reuse distances has more of its accesses falling within the cache's reach, so it misses less.

$$\text{ReuseDistance}[W_a] \leq \text{ReuseDistance}[W_b] \implies \text{MissRate}[C_{W_a}] \leq \text{MissRate}[C_{W_b}] + \varepsilon_7$$

Domain: any policy $P$, same cache geometry.

**R8.** LRU Set Decomposition

Under LRU, each set operates as an independent stack, so the overall hit rate is approximately the average of per-set hit rates.

$$\text{StackDepth}[C] = \text{Assoc}[C] \implies \text{HitRate}[C_{\text{set-assoc}}] \geq \text{HitRate}[C_{\text{per-set avg}}] - \varepsilon_8$$

Domain: LRU only, uniform set indexing.

---

## Policy Comparison

**R9.** OPT Upper-Bounds All Policies

Belady's optimal algorithm, which has perfect future knowledge, achieves the best possible hit rate for any demand-fetch policy on the same trace.

$$\text{Size}[C_{\text{opt}}] = \text{Size}[C_{\text{any}}] \;\wedge\; \text{Assoc}[C_{\text{opt}}] = \text{Assoc}[C_{\text{any}}]$$
$$\implies \text{HitRate}[C_{\text{opt}}] \geq \text{HitRate}[C_{\text{any}}] - \varepsilon_9$$

Domain: demand-fetch only, same geometry and workload.

**R10.** Recency-Aware Beats Recency-Blind Under Temporal Locality

When reuse distances are well within cache capacity, policies that track recency outperform those that don't.

$$\text{ReuseDistance}[W] \leq \frac{\text{Size}[C]}{2B} \;\wedge\; \text{same geometry}$$
$$\implies \text{HitRate}[C_{\text{recency-aware}}] \geq \text{HitRate}[C_{\text{recency-blind}}] + \varepsilon_{10}$$

Domain: any recency-aware policy (LRU, RRIP, Mockingjay) vs any recency-blind policy (FIFO, random).

**R11.** Random Replacement Lower Bound

Random eviction retains any given useful block with probability (ways-1)/ways per replacement event, establishing a probabilistic floor on hit rate relative to any reference policy.

$$\text{Size}[C_P] = \text{Size}[C_{\text{rand}}] \;\wedge\; \text{Assoc}[C_P] = \text{Assoc}[C_{\text{rand}}]$$
$$\implies \text{HitRate}[C_{\text{rand}}] \geq \left(1 - \frac{1}{\text{Assoc}[C]}\right) \cdot \text{HitRate}[C_P] - \varepsilon_{11}$$

Domain: any reference policy $P$, same geometry, same workload.

**R12.** Adaptive Tracks Best Component

A set-dueling adaptive policy dynamically identifies which of its constituent sub-policies is performing better and converges to within epsilon of that best choice.

$$\text{HitRate}[C_{\text{best static}}] \geq \text{HitRate}[C_{\text{other static}}] \;\wedge\; \text{same geometry}$$
$$\implies \text{HitRate}[C_{\text{adaptive}}] \geq \text{HitRate}[C_{\text{best static}}] - \varepsilon_{\text{adapt}}$$

Domain: set-dueling adaptive policy (DIP, DRRIP).

**R13.** Hardware Approximation Tracks Ideal

Any hardware-feasible approximation of an ideal policy stays within epsilon of it (e.g., PLRU/LRU, quantized-Mockingjay/full-precision, distilled/teacher).

$$\text{Size}[C_{\text{ideal}}] = \text{Size}[C_{\text{approx}}] \;\wedge\; \text{Assoc}[C_{\text{ideal}}] = \text{Assoc}[C_{\text{approx}}]$$
$$\implies \text{HitRate}[C_{\text{approx}}] \geq \text{HitRate}[C_{\text{ideal}}] - \varepsilon_{13}$$

Domain: same geometry, same workload, $P_{\text{approx}}$ is a hw-feasible version of $P_{\text{ideal}}$.

**R14.** Approximation Gap Grows With Decision Space

More candidates to rank (higher associativity) makes approximation harder, widening the gap between an ideal policy and its hardware-constrained version.

$$\text{Assoc}[C_{\text{hi}}] > \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{same size}$$
$$\implies \bigl(\text{HitRate}[C_{\text{ideal,hi}}] - \text{HitRate}[C_{\text{approx,hi}}]\bigr) \geq \bigl(\text{HitRate}[C_{\text{ideal,lo}}] - \text{HitRate}[C_{\text{approx,lo}}]\bigr) - \varepsilon_{14}$$

Domain: same total size, same workload, $P_{\text{approx}}$ is hw-feasible version of $P_{\text{ideal}}$.

---

## Working Set / Capacity

**R15.** Capacity Cliff at Working Set Boundary

When the working set crosses from fitting to not fitting in the cache, miss rate jumps sharply — not linearly but superlinearly, at least doubling.

$$\text{WSS}[W_{\text{over}}] > \frac{\text{Size}[C]}{B} \;\wedge\; \text{WSS}[W_{\text{under}}] \leq \frac{\text{Size}[C]}{B} \;\wedge\; \text{same geometry}$$
$$\implies \text{MissRate}[C_{\text{over}}] \geq 2 \cdot \text{MissRate}[C_{\text{under}}] - \varepsilon_{15}$$

Domain: any policy, looping or structured access patterns.

**R16.** Capacity Misses Dominate Beyond WSS

Once the working set exceeds cache capacity, the majority of misses are due to insufficient space rather than set conflicts or cold starts.

$$\text{WSS}[W] > \frac{\text{Size}[C]}{B} \implies \text{CapacityMisses}[C] \geq \frac{\text{MissCount}[C]}{2} - \varepsilon_{16}$$

Domain: fully-associative or high-associativity.

**R17.** Reuse Distance Predicts High Miss Rate

If the average number of distinct blocks accessed between two uses of the same block exceeds cache capacity, at least half of all accesses will miss.

$$\text{ReuseDistance}[W] \cdot B \geq \text{Size}[C] \implies \text{MissRate}[C] \geq 0.5 - \varepsilon_{17}$$

Domain: any policy, fully-associative.

**R18.** Temporal Locality Decay Increases Misses

If locality degrades over time (reuse distances grow), later intervals in the execution experience higher miss rates than earlier ones.

$$\text{ReuseDistance}[t_{\text{later}}] \geq \text{ReuseDistance}[t_{\text{earlier}}] \implies \text{MissRate}[t_{\text{later}}] \geq \text{MissRate}[t_{\text{earlier}}] - \varepsilon_{18}$$

Domain: same cache, same policy, sequential intervals.

---

## Thrashing & Pathological

**R19.** LRU Thrashing on Cyclic Pattern

A cyclic scan over N+1 distinct blocks through an N-block LRU cache always evicts the next-needed block, producing zero hits.

$$\text{UniqueBlocks}[W] = \frac{\text{Size}[C]}{B} + 1 \implies \text{HitRate}[C] \leq \varepsilon_{19}$$

Domain: LRU, purely cyclic access pattern. $\varepsilon_{19} \approx 0$.

**R20.** BIP/Adaptive Mitigates Thrashing

Under the same cyclic pattern that kills LRU, an adaptive insertion policy avoids the cascade eviction and maintains a significantly positive hit rate.

$$\text{UniqueBlocks}[W] = \frac{\text{Size}[C]}{B} + 1 \implies \text{HitRate}[C] \geq \varepsilon_{20}$$

Domain: Adaptive policy (BIP, DIP). $\varepsilon_{20} \gg 0$.

**R21.** Random Avoids Deterministic Thrashing

Random eviction can never be forced into zero hits because it always has a nonzero probability of keeping the right block, giving a floor proportional to 1/associativity.

$$\text{UniqueBlocks}[W] = \frac{\text{Size}[C]}{B} + 1 \implies \text{HitRate}[C] \geq \frac{1}{\text{Assoc}[C]} - \varepsilon_{21}$$

Domain: Random replacement, cyclic workload.

---

## Associativity Effects

**R22.** Conflict Misses Decrease With Associativity

More ways per set means fewer blocks compete for the same slots, directly reducing conflict misses regardless of which eviction policy is used.

$$\text{Assoc}[C_{\text{hi}}] \geq \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{Size}[C_{\text{hi}}] = \text{Size}[C_{\text{lo}}]$$
$$\implies \text{ConflictMisses}[C_{\text{hi}}] \leq \text{ConflictMisses}[C_{\text{lo}}] + \varepsilon_{22}$$

Domain: any policy (same for both), same workload.

**R23.** Scan-Aware Beats Scan-Naive on Mixed Workloads

When a workload mixes a streaming scan (large reuse distance) with a recurrent working set that fits in cache, policies that detect and deprioritize scan traffic protect the useful data that scan-naive policies would evict.

$$\text{ReuseDistance}[W_{\text{scan}}] > \frac{\text{Size}[C]}{B} \;\wedge\; \text{WSS}[W_{\text{recur}}] < \frac{\text{Size}[C]}{B} \;\wedge\; \text{same geometry}$$
$$\implies \text{HitRate}[C_{\text{scan-aware}}] \geq \text{HitRate}[C_{\text{scan-naive}}] + \varepsilon_{23}$$

Domain: any scan-aware policy (RRIP, ARC, LIRS, Mockingjay) vs any scan-naive policy (LRU, FIFO).

---

## Belady's Anomaly / Non-Stack

**R24.** Belady's Anomaly (FIFO) — `expected_violable`

FIFO can exhibit the counterintuitive behavior where a larger cache produces more misses than a smaller one on certain access patterns.

$$\text{Size}[C_{\text{large}}] > \text{Size}[C_{\text{small}}] \;\wedge\; \text{Assoc equal}$$
$$\implies \text{MissRate}[C_{\text{large}}] \leq \text{MissRate}[C_{\text{small}}] + \varepsilon_{24}$$

Domain: FIFO. Negative $\varepsilon_{24}$ = anomaly manifesting.

**R25.** Non-Stack Non-Monotone Associativity — `expected_violable`

For non-stack policies, increasing associativity at fixed total size does not guarantee improvement — the analog of Belady's anomaly in the associativity dimension.

$$\text{Assoc}[C_{\text{hi}}] > \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{Size equal}$$
$$\implies \text{HitRate}[C_{\text{hi}}] \geq \text{HitRate}[C_{\text{lo}}] - \varepsilon_{25}$$

Domain: FIFO / non-stack. Negative $\varepsilon_{25}$ = violation.

---

## Temporal / Interval Relations

**R1.** Higher Hit Rate → Fewer Stalls

Within the same execution, intervals that achieve higher cache hit rates experience fewer pipeline stalls from memory latency.

$$\text{HitRate}[t_a] \geq \text{HitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_1$$

Domain: single-core, same geometry, intervals large enough for metric stability.

**R2.** Critical Hit Rate → Fewer Stalls (Tighter)

Critical hit rate (hits on loads that are on the execution critical path) is a tighter predictor of stalls than overall hit rate, because not all misses are equally costly.

$$\text{CriticalHitRate}[t_a] \geq \text{CriticalHitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_2$$

Hypothesis: $\varepsilon_2 < \varepsilon_1$.
