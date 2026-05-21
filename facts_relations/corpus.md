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

---

## Cache Size & Associativity Properties

**R1.** Larger Cache → Higher Hit Rate

More capacity means more of the working set can stay resident, so hit rate should not decrease when the cache grows.

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{HitRate}[C_a] \geq \text{HitRate}[C_b] + \varepsilon_1$$

Domain: any policy $P$, same workload and associativity.

**R2.** Diminishing Returns of Associativity

Doubling the number of ways helps, but each successive doubling helps less — the hit rate improvement is concave in associativity.

$$\text{Size}[C_a] = \text{Size}[C_b] = \text{Size}[C_c], \quad 2\,\text{Assoc}[C_a] = \text{Assoc}[C_b], \quad 2\,\text{Assoc}[C_b] = \text{Assoc}[C_c]$$
$$\implies \bigl(\text{HitRate}[C_c] - \text{HitRate}[C_b]\bigr) \leq \bigl(\text{HitRate}[C_b] - \text{HitRate}[C_a]\bigr) + \varepsilon_2$$

Domain: any policy $P$, fixed size, single workload.

---

## Policy Comparison

**R3.** OPT Upper-Bounds All Policies

Belady's optimal algorithm, which has perfect future knowledge, achieves the best possible hit rate for any demand-fetch policy on the same trace.

$$\text{Size}[C_{\text{opt}}] = \text{Size}[C_{\text{any}}] \;\wedge\; \text{Assoc}[C_{\text{opt}}] = \text{Assoc}[C_{\text{any}}]$$
$$\implies \text{HitRate}[C_{\text{opt}}] \geq \text{HitRate}[C_{\text{any}}] - \varepsilon_3$$

Domain: demand-fetch only, same geometry and workload.

---

## Associativity Effects

**R4.** Conflict Misses Decrease With Associativity

More ways per set means fewer blocks compete for the same slots, directly reducing conflict misses regardless of which eviction policy is used.

$$\text{Assoc}[C_{\text{hi}}] \geq \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{Size}[C_{\text{hi}}] = \text{Size}[C_{\text{lo}}]$$
$$\implies \text{ConflictMisses}[C_{\text{hi}}] \leq \text{ConflictMisses}[C_{\text{lo}}] + \varepsilon_4$$

Domain: any policy (same for both), same workload.

---

## Temporal / Interval Relations

**R5.** Higher Hit Rate → Fewer Stalls

Within the same execution, intervals that achieve higher cache hit rates experience fewer pipeline stalls from memory latency.

$$\text{HitRate}[t_a] \geq \text{HitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_5$$

Domain: single-core, same geometry, intervals large enough for metric stability.

**R6.** Critical Hit Rate → Fewer Stalls (Tighter)

Critical hit rate (hits on loads that are on the execution critical path) is a tighter predictor of stalls than overall hit rate, because not all misses are equally costly.

$$\text{CriticalHitRate}[t_a] \geq \text{CriticalHitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_6$$

Hypothesis: $\varepsilon_6 < \varepsilon_5$.

---

## Hit Rate Decomposition

**R7.** Hit Rate Between Load and Store

Overall hit rate is a weighted average of load and store hit rates, so it falls between the two.

$$\text{LoadHitRate}[C] \geq \text{StoreHitRate}[C] \implies \text{HitRate}[C] \geq \text{StoreHitRate}[C]$$

Domain: any policy, any workload with both loads and stores.

**R9.** Prefetch Coverage Reduces Demand Misses

Higher prefetch coverage (fraction of would-be demand misses that prefetches eliminate) directly improves demand hit rate.

$$\text{PrefetchCoverage}[C_a] \geq \text{PrefetchCoverage}[C_b] \;\wedge\; \text{same geometry}$$
$$\implies \text{DemandHitRate}[C_a] \geq \text{DemandHitRate}[C_b] - \varepsilon_9$$

Domain: same geometry, same workload, same replacement policy.

**R10.** Low Prefetch Accuracy Hurts Demand Hit Rate

When most prefetched blocks are never used (accuracy ≤ 25%), the cache fills with useless data, reducing demand hit rate compared to no prefetching.

$$\text{PrefetchAccuracy}[C] \leq 0.25 \;\wedge\; \text{same geometry}$$
$$\implies \text{DemandHitRate}[C_{\text{prefetch}}] \leq \text{DemandHitRate}[C_{\text{no-prefetch}}] + \varepsilon_{10}$$

Domain: same geometry, same workload, same replacement policy.

**R11.** Stores Hit Less Than Loads

Write-allocate caches see lower store hit rates because first-write misses are common for newly allocated data.

$$\text{StoreHitRate}[C] \leq \text{LoadHitRate}[C] + \varepsilon_{11}$$

Domain: write-allocate cache, any policy.

---

## Coherence Effects

**R12.** 4C Miss Decomposition

Total misses decompose into compulsory + capacity + conflict + coherence (the fourth C for multicore).

$$\text{MissCount}[C] \geq \text{CompulsoryMisses}[C] + \text{CapacityMisses}[C] + \text{ConflictMisses}[C] + \text{CoherenceMisses}[C] + \varepsilon_{12}$$

Domain: multicore with coherence protocol.

**R13.** More Invalidations → More Coherence Misses

Invalidations are the direct mechanism by which coherence causes misses; more invalidations imply more coherence misses.

$$\text{Invalidations}[C_a] \geq \text{Invalidations}[C_b] \;\wedge\; \text{same geometry}$$
$$\implies \text{CoherenceMisses}[C_a] \geq \text{CoherenceMisses}[C_b] - \varepsilon_{13}$$

Domain: same geometry, multicore.

**R14.** More Dirty Lines + More Evictions → More Writebacks

Caches with more store hits (more modified resident lines) produce more writebacks when those lines are evicted.

$$\text{StoreHitRate}[C_a] \geq \text{StoreHitRate}[C_b] \;\wedge\; \text{Evictions}[C_a] \geq \text{Evictions}[C_b] \;\wedge\; \text{same size}$$
$$\implies \text{Writebacks}[C_a] \geq \text{Writebacks}[C_b] - \varepsilon_{14}$$

Domain: write-back cache, same geometry.
