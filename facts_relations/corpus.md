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

**F4.** Full Associativity Eliminates Conflict Misses

When every block in the cache can map to any set (i.e., there's only one set), conflicts are impossible by definition.

$$\text{Assoc}[C] = \frac{\text{Size}[C]}{B} \implies \text{ConflictMisses}[C] = 0$$

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

---

## Policy Comparison

**R9.** OPT Upper-Bounds All Policies

Belady's optimal algorithm, which has perfect future knowledge, achieves the best possible hit rate for any demand-fetch policy on the same trace.

$$\text{Size}[C_{\text{opt}}] = \text{Size}[C_{\text{any}}] \;\wedge\; \text{Assoc}[C_{\text{opt}}] = \text{Assoc}[C_{\text{any}}]$$
$$\implies \text{HitRate}[C_{\text{opt}}] \geq \text{HitRate}[C_{\text{any}}] - \varepsilon_9$$

Domain: demand-fetch only, same geometry and workload.

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

**R18.** Temporal Locality Decay Increases Misses

If locality degrades over time (reuse distances grow), later intervals in the execution experience higher miss rates than earlier ones.

$$\text{ReuseDistance}[t_{\text{later}}] \geq \text{ReuseDistance}[t_{\text{earlier}}] \implies \text{MissRate}[t_{\text{later}}] \geq \text{MissRate}[t_{\text{earlier}}] - \varepsilon_{18}$$

Domain: same cache, same policy, sequential intervals.

---

## Associativity Effects

**R22.** Conflict Misses Decrease With Associativity

More ways per set means fewer blocks compete for the same slots, directly reducing conflict misses regardless of which eviction policy is used.

$$\text{Assoc}[C_{\text{hi}}] \geq \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{Size}[C_{\text{hi}}] = \text{Size}[C_{\text{lo}}]$$
$$\implies \text{ConflictMisses}[C_{\text{hi}}] \leq \text{ConflictMisses}[C_{\text{lo}}] + \varepsilon_{22}$$

Domain: any policy (same for both), same workload.

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

---

## Hit Rate Decomposition

**R26.** Hit Rate Between Load and Store

Overall hit rate is a weighted average of load and store hit rates, so it falls between the two.

$$\text{LoadHitRate}[C] \geq \text{StoreHitRate}[C] \implies \text{HitRate}[C] \geq \text{StoreHitRate}[C]$$

Domain: any policy, any workload with both loads and stores.

**R27.** Demand Hit Rate ≥ Overall Hit Rate

Prefetch-initiated fills that go unused dilute overall hit rate without hurting demand hits, so demand hit rate is at least as high.

$$\text{DemandHitRate}[C] \geq \text{HitRate}[C] - \varepsilon_{27}$$

Domain: any cache with prefetching enabled.

**R28.** Prefetch Coverage Reduces Demand Misses

Higher prefetch coverage (fraction of would-be demand misses that prefetches eliminate) directly improves demand hit rate.

$$\text{PrefetchCoverage}[C_a] \geq \text{PrefetchCoverage}[C_b] \;\wedge\; \text{same geometry}$$
$$\implies \text{DemandHitRate}[C_a] \geq \text{DemandHitRate}[C_b] - \varepsilon_{28}$$

Domain: same geometry, same workload, same replacement policy.

**R29.** Low Prefetch Accuracy Hurts Demand Hit Rate

When most prefetched blocks are never used (accuracy ≤ 25%), the cache fills with useless data, reducing demand hit rate compared to no prefetching.

$$\text{PrefetchAccuracy}[C] \leq 0.25 \;\wedge\; \text{same geometry}$$
$$\implies \text{DemandHitRate}[C_{\text{prefetch}}] \leq \text{DemandHitRate}[C_{\text{no-prefetch}}] + \varepsilon_{29}$$

Domain: same geometry, same workload, same replacement policy.

**R30.** Stores Hit Less Than Loads

Write-allocate caches see lower store hit rates because first-write misses are common for newly allocated data.

$$\text{StoreHitRate}[C] \leq \text{LoadHitRate}[C] + \varepsilon_{30}$$

Domain: write-allocate cache, any policy.

---

## Page-Level Memory Footprint

Memory footprint here means **distinct pages touched** — a coarser granularity than cache blocks. This matters because a workload can touch many pages but reuse few lines per page (TLB pressure without proportional cache pressure), or touch few pages but stride across many lines within them (high block-level WSS from few pages).

**R31.** More Pages Touched → Higher Miss Rate

Touching more pages scatters accesses across a wider address range, reducing per-set reuse density and increasing miss rate.

$$\text{Footprint}[W_a] \geq \text{Footprint}[W_b] \;\wedge\; \text{same geometry} \;\wedge\; \text{same total accesses}$$
$$\implies \text{MissRate}[C_{W_a}] \geq \text{MissRate}[C_{W_b}] - \varepsilon_{31}$$

Domain: any policy $P$, same geometry, same total access count.

**R32.** Good Spatial Locality Bounds Compulsory Misses

When the block-level working set size approaches page_footprint × blocks_per_page, the workload uses most lines within each page it touches. Each new page contributes at least one compulsory miss.

$$\text{WSS}[W] \geq \text{Footprint}[W] \cdot \frac{\text{PageSize}}{B} - \varepsilon_{32} \implies \text{CompulsoryMisses}[C] \geq \text{Footprint}[W]$$

Domain: any policy, any geometry.

**R33.** Page Footprint Growth Increases Evictions

As a program touches new pages over time, it brings in previously-unseen blocks that compete for capacity, increasing eviction pressure.

$$\text{Footprint}[t_{\text{later}}] > \text{Footprint}[t_{\text{earlier}}] \implies \text{Evictions}[t_{\text{later}}] \geq \text{Evictions}[t_{\text{earlier}}] - \varepsilon_{33}$$

Domain: any policy, same cache, sequential intervals.

---

## Coherence Effects

**R35.** 4C Miss Decomposition

Total misses decompose into compulsory + capacity + conflict + coherence (the fourth C for multicore).

$$\text{MissCount}[C] \geq \text{CompulsoryMisses}[C] + \text{CapacityMisses}[C] + \text{ConflictMisses}[C] + \text{CoherenceMisses}[C] - \varepsilon_{35}$$

Domain: multicore with coherence protocol.

**R36.** More Invalidations → More Coherence Misses

Invalidations are the direct mechanism by which coherence causes misses; more invalidations imply more coherence misses.

$$\text{Invalidations}[C_a] \geq \text{Invalidations}[C_b] \;\wedge\; \text{same geometry}$$
$$\implies \text{CoherenceMisses}[C_a] \geq \text{CoherenceMisses}[C_b] - \varepsilon_{36}$$

Domain: same geometry, multicore.

**R37.** More Dirty Lines + More Evictions → More Writebacks

Caches with more store hits (more modified resident lines) produce more writebacks when those lines are evicted.

$$\text{StoreHitRate}[C_a] \geq \text{StoreHitRate}[C_b] \;\wedge\; \text{Evictions}[C_a] \geq \text{Evictions}[C_b] \;\wedge\; \text{same size}$$
$$\implies \text{Writebacks}[C_a] \geq \text{Writebacks}[C_b] - \varepsilon_{37}$$

Domain: write-back cache, same geometry.
