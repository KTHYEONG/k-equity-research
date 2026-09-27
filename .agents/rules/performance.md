# Performance & Optimization Directives (Physical Invariants)

> **Performance is an architectural invariant, not an afterthought. Never fake speed by silently truncating date ranges, universe constituents, or numerical precision. Correctness strictly precedes speed. Maximize hardware throughput within four physical boundaries.**

## 1. Bounded Resource Scaling & Memory Discipline
- **Bounded Resource Scaling:** Memory consumption must remain safely bounded within available host physical RAM. Select data structures and processing patterns whose memory footprint scales predictably with workload size; prevent unconstrained accumulation of intermediate state.
- **Eliminate Unnecessary Duplication:** Eliminate redundant in-memory data copies, intermediate full-collection materializations, and unneeded conversions. Prefer views, zero-copy operations, or memory-efficient formats when handling substantial datasets.
- **Deterministic Resource Reclamation:** Heavy memory buffers, file descriptors, network connections, and worker pools must be deterministically released or closed when exiting their operational scope.

## 2. Algorithmic Locality & Hot Path Efficiency
- **Hot Path Algorithmic Efficiency:** In computationally intensive loops or hot iteration paths, eliminate nested linear scans and repetitive collection filtering ($O(N^2)$ pitfalls). Use pre-indexed structures, hashing, grouping, or cursor positioning where lookup frequency is high.
- **I/O Locality & Pushdown:** Prohibit repetitive text parsing or deserialization in hot computational paths. Leverage efficient binary or columnar storage with column pruning and predicate pushdown for substantial data I/O.

## 3. Separation of Invariant Features and Dynamic State
- **One-Pass Invariant Materialization:** Where compute scale warrants and working memory budgets permit, compute state-independent features and indicators once upstream across the historical timeline. For massive datasets exceeding comfortable in-memory working sets, process in bounded chronological chunks.
- **Minimal Hot Paths:** Confine inner sequential iteration strictly to state-dependent transitions. Never recompute static or historical invariants inside simulation loops or across optimization folds.

## 4. Hardware Saturation & Observable Throughput
- **Balanced Parallelism:** When verified compute bottlenecks exist on parallelizable workloads, saturate available CPU cores while ensuring compute granularity heavily amortizes inter-process communication (IPC) overhead. Avoid unnecessary multiprocessing setup for lightweight operations.
- **Continuous Progress Telemetry:** Long-running workloads must emit deterministic progress heartbeats. A silent pipeline is indistinguishable from a deadlocked system.