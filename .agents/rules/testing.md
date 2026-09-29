# Testing Directives & Quality Standards

> **Verify observable behavior and interface contracts, not implementation internals. Maintain fast feedback loops with small deterministic inputs while isolating heavy runs. Enforce invariant verification on new logic, isolate failures with pinpoint precision, and prioritize economic correctness over vanity metrics.**

## 1. Development Paradigm: Invariant-Driven Development (IDD)
- **Contracts Over Premature Tests:** Prioritize strict static typing, explicit schema models, and domain invariants over dogmatic test-first rituals. Define the interface and invariants before writing code; avoid premature test code that freezes internal APIs.
- **Pragmatic Invariant Verification (Zero Ceremony):**
  - *Direct Execution (Bug fixes, localized changes, data probes, scripts):* Implement directly with targeted invariant guard tests and verify via the project toolchain. Formal specs are not required for localized work.
  - *Contract Design (Complex interfaces, multi-component pipelines):* Formulate explicit interface contracts and invariant scenarios (`docs/specs/*_spec.md`) before implementation.
  - *On-Demand Utilities:* Skills (`probe`, `spec`, `implement`, `check`) are independent tools, not a mandatory sequential conveyor belt.
- **Observable Behavior & Invariants:** Verify return contracts, state mutations, conservation laws, and error conditions rather than private implementation details or mock call-counts.
- **In-Memory & Minimal Inputs:** Use the smallest deterministic synthetic data sufficient to test target logic near-instantaneously; never load multi-year disk datasets in unit tests.
- **Functional Correctness Over Stochastic Outcomes:** Unit and integration tests verify contract correctness, edge cases, and schema transformations—never long-horizon statistical convergence or business outcomes.

## 2. State Invariants & Boundary Testing
- **Conservation & State Invariants:** Verify structural conservation laws: balance/resource reconciliations, allocation parity, deterministic outputs for identical inputs, and idempotent deduplication of operations.
- **Temporal & Numerical Boundaries:** Stress-test boundary conditions rather than happy paths alone: zeros, NaNs, empty collections, missing keys, boundary timestamps, timezone transitions, and floating-point tolerances (explicit epsilon bounds, relative/absolute tolerances like `rtol`/`atol`).
- **Causal Invariance:** Explicitly test that calculations do not access future time steps, uninitialized state, or unreleased data.

## 3. Execution, Latency Budgets & Failure Triage
- **Latency Budgets:** Fast unit tests must execute near-instantaneously using in-memory fixtures. Heavy end-to-end simulations, full model retraining, or multi-year workloads must be marked with explicit boundary tags and isolated from default test runs.
- **Pinpoint Failure Isolation:** During localized TDD and debugging, execute focused test targets via the project runner and inspect direct tracebacks for rapid feedback. For final task validation, run the designated spec suite and verify directly coupled integration tests.
- **Process-Isolated Temp Workspaces:** Never wipe shared temp directories globally. Each test runner invocation operates in its own partitioned, process-isolated temporary workspace directory to prevent race conditions and ghost crashes between concurrent agents and developer terminal commands.
- **Hermetic Environment Invariant:** Unit tests must be 100% hermetic and isolated from live shell credentials, ambient secret tokens, and production environment variables. Tests requiring specific configuration must declare explicit, isolated test fixtures rather than reading ambient host state.
- **Tooling Artifact Awareness:** If tests pass alone but fail strictly under instrumentation (coverage or profiling), diagnose tracer overhead, timeout expiration, or multiprocessing/fork interference before assuming a domain code regression.
- **Pragmatic Fixtures & Mocking:** Mock external boundaries (network APIs, clock/system time, filesystem I/O). Never mock internal domain calculations or transform tests into meaningless mock-chains.

## 4. Diff-Coverage & Quality Philosophy
- **Diff-Coverage & Domain Focus:** Focus test coverage on newly-added production logic and critical boundary scenarios. Prune dead defensive branches rather than writing vacuous tests, and use coverage exclusion annotations sparingly for non-testable scaffolding.
- **Test Integrity:** Never weaken assertions, delete valid tests, or skip failing checks to satisfy CI. Diagnose and fix the root cause.