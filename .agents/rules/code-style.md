# Code Style & Engineering Standards

> **Respect existing architecture and conventions. Maintain strong typed contracts at public boundaries without breaking scope. Reconcile code with specifications objectively, and apply equivalent engineering rigor across all languages.**

## 1. Architectural Integrity & Modularity
- **Layout & Scope Hygiene:** Respect the repository's existing source/test layout and architectural layer boundaries. Maintain high cohesion and avoid out-of-scope sweeping refactorings.
- **Contract & Spec Reconciliation:** Treat validated executable code and contracts as authoritative for current implementation, but actively investigate discrepancies with specifications or documentation rather than assuming either side is automatically correct.

## 2. Typing, Contracts & Configuration
- **Strong Typing at Boundaries:** Enforce strong static typing for public interfaces, domain models, and core production logic. Never weaken existing type guarantees without explicit justification.
- **Explicit Configuration:** Keep runtime settings externalized and validated at application boundaries; never hardcode environment-dependent secrets or endpoints.
- **Toolchain Alignment:** Use the repository's declared environment and quality tooling (linters, type-checkers, and test runners); do not introduce competing toolchains unnecessarily.

## 3. Polyglot Architecture & Cross-Language Boundaries
- **Equivalence of Rigor:** Any introduced subsystem, module, or polyglot extension must meet equivalent first-class engineering baselines: static compilation or type checks, idiomatic linting, and rigorous automated testing.
- **FFI & Inter-Service Safety:** Explicitly define memory ownership, concurrency safety, and error semantics across language or process boundaries. Safely translate recoverable errors without crashing host or client runtimes.
- **Contract Continuity:** Maintain explicit, machine-verifiable typed interface boundaries across subsystem boundaries so end-to-end verification remains seamless across all integrated components.
