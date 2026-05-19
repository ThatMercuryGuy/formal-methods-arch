# System-Latent Relations (Coupled Epsilons)

These are relations whose epsilons cannot be solved independently — they share latent variables or form transitive chains where tightening one bound constrains another.

---

## 1. The Miss Cost Chain (Size → Hit Rate → Stalls → IPC)

A transitive chain where each link has its own epsilon, but the end-to-end bound requires solving them jointly.

**Link 1 (R3):** Larger cache improves hit rate.

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{HitRate}[C_a] \geq \text{HitRate}[C_b] + \varepsilon_3$$

**Link 2 (R1):** Higher hit rate reduces stalls.

$$\text{HitRate}[t_a] \geq \text{HitRate}[t_b] \implies \text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_1$$

**Link 3 (derived):** Fewer stalls improve IPC.

$$\text{Stalls}[t_a] \leq \text{Stalls}[t_b] + \varepsilon_1 \implies \text{IPC}[t_a] \geq \text{IPC}[t_b] - \varepsilon_{\text{ipc}}$$

**System constraint (end-to-end):** Bigger cache → better IPC.

$$\text{Size}[C_a] \geq \text{Size}[C_b] \implies \text{IPC}[t_a] \geq \text{IPC}[t_b] - f(\varepsilon_3, \varepsilon_1, \varepsilon_{\text{ipc}})$$

The combined slack $f(\varepsilon_3, \varepsilon_1, \varepsilon_{\text{ipc}})$ depends on all three jointly. Tightening $\varepsilon_3$ (bigger cache helps hit rate more) allows a tighter end-to-end bound even if $\varepsilon_1$ is loose. Memory-level parallelism determines how $\varepsilon_1$ and $\varepsilon_{\text{ipc}}$ interact — a core that can hide latency has loose $\varepsilon_1$ (hit rate doesn't predict stalls well) but that same hiding means $\varepsilon_{\text{ipc}}$ is also loose (stalls don't predict IPC well either).

---