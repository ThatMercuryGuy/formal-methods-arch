# Cache Replacement Facts & Relations

## Unconditional Facts

**F1.** Miss Rate / Hit Rate Complement

$$\text{MissRate}[C] + \text{HitRate}[C] = 1$$

**F2.** Hit Rate Bounded

$$\text{HitRate}[C] \leq 1$$

**F3.** Compulsory Misses Policy-Independent

$$\text{Size}[C_a] = \text{Size}[C_b] \;\wedge\; \text{Assoc}[C_a] = \text{Assoc}[C_b] \implies \text{CompulsoryMisses}[C_a] = \text{CompulsoryMisses}[C_b]$$

**F4.** Replacement Only Affects Capacity + Conflict

$$\text{Size}[C_a] = \text{Size}[C_b] \;\wedge\; \text{Assoc}[C_a] = \text{Assoc}[C_b]$$
$$\implies \text{MissCount}[C_a] - \text{MissCount}[C_b] = \bigl(\text{CapMisses}[C_a] + \text{ConflMisses}[C_a]\bigr) - \bigl(\text{CapMisses}[C_b] + \text{ConflMisses}[C_b]\bigr)$$

**F5.** Full Associativity Eliminates Conflict Misses

$$\text{Assoc}[C] = \frac{\text{Size}[C]}{B} \implies \text{ConflictMisses}[C] = 0$$

**F6.** Stack Policy Inclusion

$$\text{Size}[C_{\text{large}}] \geq \text{Size}[C_{\text{small}}] \implies \text{MissCount}[C_{\text{large}}] \leq \text{MissCount}[C_{\text{small}}]$$

Domain: LRU or any stack algorithm. Same workload, same associativity.

---

## LRU Stack Properties

**R3.** Larger Cache → Higher Hit Rate

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{HitRate}[C_a] \geq \text{HitRate}[C_b] + \varepsilon_3$$

Domain: LRU, same workload and associativity.

**R4.** Diminishing Returns of Associativity

$$\text{Size}[C_a] = \text{Size}[C_b] = \text{Size}[C_c], \quad 2\,\text{Assoc}[C_a] = \text{Assoc}[C_b], \quad 2\,\text{Assoc}[C_b] = \text{Assoc}[C_c]$$
$$\implies \bigl(\text{HitRate}[C_c] - \text{HitRate}[C_b]\bigr) \leq \bigl(\text{HitRate}[C_b] - \text{HitRate}[C_a]\bigr) + \varepsilon_{12}$$

Domain: LRU, fixed size, single workload.

**R6.** Working Set Fits → All Hits

$$\text{WSS}[W] \leq \frac{\text{Size}[C]}{B} \implies \text{HitRate}[C] \geq 1 - \varepsilon_6$$

Domain: LRU, fully-associative or high-assoc, after warmup.

**R7.** Miss Rate Monotone in Reuse Distance

$$\text{ReuseDistance}[W_a] \leq \text{ReuseDistance}[W_b] \implies \text{MissRate}[C_{W_a}] \leq \text{MissRate}[C_{W_b}] + \varepsilon_7$$

Domain: LRU, same cache geometry.

**R8.** LRU Set Decomposition

$$\text{StackDepth}[C] = \text{Assoc}[C] \implies \text{HitRate}[C_{\text{set-assoc}}] \geq \text{HitRate}[C_{\text{per-set avg}}] - \varepsilon_8$$

Domain: LRU, uniform set indexing.

---

## Policy Comparison

**R9.** OPT Upper-Bounds All Policies

$$\text{Size}[C_{\text{opt}}] = \text{Size}[C_{\text{any}}] \;\wedge\; \text{Assoc}[C_{\text{opt}}] = \text{Assoc}[C_{\text{any}}]$$
$$\implies \text{HitRate}[C_{\text{opt}}] \geq \text{HitRate}[C_{\text{any}}] - \varepsilon_9$$

Domain: demand-fetch only, same geometry and workload.

**R10.** LRU Beats FIFO Under Temporal Locality

$$\text{ReuseDistance}[W] \leq \frac{\text{Size}[C]}{2B} \;\wedge\; \text{Size}[C_{\text{lru}}] = \text{Size}[C_{\text{fifo}}] \;\wedge\; \text{Assoc}[C_{\text{lru}}] = \text{Assoc}[C_{\text{fifo}}]$$
$$\implies \text{HitRate}[C_{\text{lru}}] \geq \text{HitRate}[C_{\text{fifo}}] + \varepsilon_{10}$$

**R11.** Random Replacement Lower Bound

$$\text{Size}[C_{\text{lru}}] = \text{Size}[C_{\text{rand}}] \;\wedge\; \text{Assoc}[C_{\text{lru}}] = \text{Assoc}[C_{\text{rand}}]$$
$$\implies \text{HitRate}[C_{\text{rand}}] \geq \left(1 - \frac{1}{\text{Assoc}[C]}\right) \cdot \text{HitRate}[C_{\text{lru}}] - \varepsilon_{11}$$

**R12.** Adaptive Tracks Best Component

$$\text{HitRate}[C_{\text{best static}}] \geq \text{HitRate}[C_{\text{other static}}] \;\wedge\; \text{same geometry}$$
$$\implies \text{HitRate}[C_{\text{adaptive}}] \geq \text{HitRate}[C_{\text{best static}}] - \varepsilon_{\text{adapt}}$$

Domain: set-dueling adaptive policy (DIP, DRRIP).

**R13.** PLRU Approximates LRU

$$\text{Size}[C_{\text{lru}}] = \text{Size}[C_{\text{plru}}] \;\wedge\; \text{Assoc}[C_{\text{lru}}] = \text{Assoc}[C_{\text{plru}}]$$
$$\implies \text{HitRate}[C_{\text{plru}}] \geq \text{HitRate}[C_{\text{lru}}] - \varepsilon_{13}$$

**R14.** PLRU–LRU Gap Grows With Associativity

$$\text{Assoc}[C_{\text{hi}}] > \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{same size}$$
$$\implies \bigl(\text{HitRate}[C_{\text{lru,hi}}] - \text{HitRate}[C_{\text{plru,hi}}]\bigr) \geq \bigl(\text{HitRate}[C_{\text{lru,lo}}] - \text{HitRate}[C_{\text{plru,lo}}]\bigr) - \varepsilon_{14}$$

---

## Working Set / Capacity

**R15.** Capacity Cliff at Working Set Boundary

$$\text{WSS}[W_{\text{over}}] > \frac{\text{Size}[C]}{B} \;\wedge\; \text{WSS}[W_{\text{under}}] \leq \frac{\text{Size}[C]}{B} \;\wedge\; \text{same geometry}$$
$$\implies \text{MissRate}[C_{\text{over}}] \geq 2 \cdot \text{MissRate}[C_{\text{under}}] - \varepsilon_{15}$$

Domain: LRU-family, looping access patterns.

**R16.** Capacity Misses Dominate Beyond WSS

$$\text{WSS}[W] > \frac{\text{Size}[C]}{B} \implies \text{CapacityMisses}[C] \geq \frac{\text{MissCount}[C]}{2} - \varepsilon_{16}$$

Domain: fully-associative or high-associativity.

**R17.** Reuse Distance Predicts High Miss Rate

$$\text{ReuseDistance}[W] \cdot B \geq \text{Size}[C] \implies \text{MissRate}[C] \geq 0.5 - \varepsilon_{17}$$

Domain: LRU, fully-associative.

**R18.** Temporal Locality Decay Increases Misses

$$\text{ReuseDistance}[t_{\text{later}}] \geq \text{ReuseDistance}[t_{\text{earlier}}] \implies \text{MissRate}[t_{\text{later}}] \geq \text{MissRate}[t_{\text{earlier}}] - \varepsilon_{18}$$

Domain: same cache, same policy, sequential intervals.

---

## Thrashing & Pathological

**R19.** LRU Thrashing on Cyclic Pattern

$$\text{UniqueBlocks}[W] = \frac{\text{Size}[C]}{B} + 1 \implies \text{HitRate}[C] \leq \varepsilon_{19}$$

Domain: LRU, purely cyclic access pattern. $\varepsilon_{19} \approx 0$.

**R20.** BIP/Adaptive Mitigates Thrashing

$$\text{UniqueBlocks}[W] = \frac{\text{Size}[C]}{B} + 1 \implies \text{HitRate}[C] \geq \varepsilon_{20}$$

Domain: Adaptive policy (BIP, DIP). $\varepsilon_{20} \gg 0$.

**R21.** Random Avoids Deterministic Thrashing

$$\text{UniqueBlocks}[W] = \frac{\text{Size}[C]}{B} + 1 \implies \text{HitRate}[C] \geq \frac{1}{\text{Assoc}[C]} - \varepsilon_{21}$$

Domain: Random replacement, cyclic workload.

---

## Associativity Effects

**R22.** Conflict Misses Decrease With Associativity

$$\text{Assoc}[C_{\text{hi}}] \geq \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{Size}[C_{\text{hi}}] = \text{Size}[C_{\text{lo}}]$$
$$\implies \text{ConflictMisses}[C_{\text{hi}}] \leq \text{ConflictMisses}[C_{\text{lo}}] + \varepsilon_{22}$$

Domain: same policy, same workload.

**R23.** Scan-Resistant Policy Beats LRU on Mixed Workloads

$$\text{ReuseDistance}[W_{\text{scan}}] > \frac{\text{Size}[C]}{B} \;\wedge\; \text{WSS}[W_{\text{recur}}] < \frac{\text{Size}[C]}{B} \;\wedge\; \text{same geometry}$$
$$\implies \text{HitRate}[C_{\text{adaptive}}] \geq \text{HitRate}[C_{\text{lru}}] + \varepsilon_{23}$$

Domain: mixed scan + recurrent workload.

---

## Belady's Anomaly / Non-Stack

**R24.** Belady's Anomaly (FIFO) — `expected_violable`

$$\text{Size}[C_{\text{large}}] > \text{Size}[C_{\text{small}}] \;\wedge\; \text{Assoc equal}$$
$$\implies \text{MissRate}[C_{\text{large}}] \leq \text{MissRate}[C_{\text{small}}] + \varepsilon_{24}$$

Domain: FIFO. Negative $\varepsilon_{24}$ = anomaly manifesting.

**R25.** Non-Stack Non-Monotone Associativity — `expected_violable`

$$\text{Assoc}[C_{\text{hi}}] > \text{Assoc}[C_{\text{lo}}] \;\wedge\; \text{Size equal}$$
$$\implies \text{HitRate}[C_{\text{hi}}] \geq \text{HitRate}[C_{\text{lo}}] - \varepsilon_{25}$$

Domain: FIFO / non-stack. Negative $\varepsilon_{25}$ = violation.

---

## Temporal / Interval Relations

**R1.** Higher Hit Rate → Fewer Stalls

$$\text{HitRate}[t_a] \geq \text{HitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_1$$

Domain: single-core, same geometry, intervals large enough for metric stability.

**R2.** Critical Hit Rate → Fewer Stalls (Tighter)

$$\text{CriticalHitRate}[t_a] \geq \text{CriticalHitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_2$$

Hypothesis: $\varepsilon_2 < \varepsilon_1$.
