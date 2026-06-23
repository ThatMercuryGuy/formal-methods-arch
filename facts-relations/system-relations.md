# System-Latent Relations (Coupled Epsilons)

These are relations whose epsilons cannot be solved independently — they share latent variables or form transitive chains where tightening one bound constrains another.

---

## 1. The Miss Cost Chain (Size → Hit Rate → Stalls → IPC)

A transitive chain where each link has its own epsilon, but the end-to-end bound requires solving them jointly.

**Link 1 (R1):** Larger cache improves hit rate.

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{HitRate}[C_a] \geq \text{HitRate}[C_b] + \varepsilon_1$$

**Link 2 (R5):** Higher hit rate reduces stalls.

$$\text{HitRate}[t_a] \geq \text{HitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_5$$

**Link 3 (derived):** Fewer stalls improve IPC.

$$\text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_5 \implies \text{IPC}[t_a] \geq \text{IPC}[t_b] - \varepsilon_{\text{ipc}}$$

**System constraint (end-to-end):** Bigger cache → better IPC.

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{IPC}[t_a] \geq \text{IPC}[t_b] - f(\varepsilon_1, \varepsilon_5, \varepsilon_{\text{ipc}})$$

The combined slack $f(\varepsilon_1, \varepsilon_5, \varepsilon_{\text{ipc}})$ depends on all three jointly. Tightening $\varepsilon_1$ (bigger cache helps hit rate more) allows a tighter end-to-end bound even if $\varepsilon_5$ is loose. Memory-level parallelism determines how $\varepsilon_5$ and $\varepsilon_{\text{ipc}}$ interact — a core that can hide latency has loose $\varepsilon_5$ (hit rate doesn't predict stalls well) but that same hiding means $\varepsilon_{\text{ipc}}$ is also loose (stalls don't predict IPC well either).

---

## 2. The Coherence Stall Chain (Invalidations → Coherence Misses → Hit Rate → Stalls → IPC)

The multicore analog of the Miss Cost Chain. In a shared-memory system, coherence protocol traffic introduces a category of misses invisible to single-core analysis. This chain traces how inter-core communication degrades IPC through the cache miss path.

**Link 1 (R13):** More invalidations cause more coherence misses.

$$\text{Invalidations}[C_a] \geq \text{Invalidations}[C_b] \implies \text{CoherenceMisses}[C_a] \geq \text{CoherenceMisses}[C_b] - \varepsilon_{13}$$

**Link 2 (R12):** Coherence misses contribute to total miss count.

$$\text{MissCount}[C] \geq \text{CompulsoryMisses}[C] + \text{CapacityMisses}[C] + \text{ConflictMisses}[C] + \text{CoherenceMisses}[C] + \varepsilon_{12}$$

**Link 3 (F1):** Total misses determine hit rate.

$$\text{MissRate}[C] + \text{HitRate}[C] = 1$$

**Link 4 (R5):** Lower hit rate causes more stalls.

$$\text{HitRate}[t_a] \geq \text{HitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_5$$

**System constraint (end-to-end):** More invalidations → worse IPC.

$$\text{Invalidations}[C_a] \geq \text{Invalidations}[C_b] \implies \text{IPC}[t_a] \leq \text{IPC}[t_b] + g(\varepsilon_{13}, \varepsilon_{12}, \varepsilon_5)$$

The combined slack $g(\varepsilon_{13}, \varepsilon_{12}, \varepsilon_5)$ cannot be decomposed into independent per-link contributions. $\varepsilon_{13}$ is loose when invalidations hit lines that were about to be evicted anyway — the coherence miss overlaps with what would have been a capacity miss, so the re-fetch is not an additional cost. $\varepsilon_5$ is loose when out-of-order execution hides miss latency via memory-level parallelism. These interact in a non-trivial way: bursty invalidations (tight $\varepsilon_{13}$ within each burst) create bursty stall patterns that the reorder buffer can absorb (loosening $\varepsilon_5$). Conversely, a steady drip of invalidations (loose $\varepsilon_{13}$ per individual event) produces a sustained throughput drag with predictable stall patterns (tightening $\varepsilon_5$). Tightening one epsilon can loosen another — they cannot be solved independently.

---

## Entity Legend

| Symbol | Name | Description |
|--------|------|-------------|
| $C$    | Cache | A cache instance |
| $C_a$  | Cache A | A cache instance at a given level (e.g., L2, LLC) in the hierarchy |
| $C_b$  | Cache B | A second cache instance at the same level as $C_a$ |
| $t_a$  | Interval A | A large program execution interval |
| $t_b$  | Interval B | A second large program execution interval |