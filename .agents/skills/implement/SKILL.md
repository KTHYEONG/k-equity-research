---
name: implement
description: Implement an approved spec mechanically with focused invariant guards and integration verification.
---

# Implement Protocol

Fast-execution protocol for mechanical code implementation based strictly on frozen specs (`_spec.md`).

## Execution Principles

Execute the approved specification into production code and passing tests:
1. Implement clean production logic satisfying the spec's invariants and documentation across all targets.
2. Implement targeted invariant guard tests satisfying the spec's Invariant Scenarios.
3. Wire the caller at the specified anchor point(s).
4. Verify in one pass with project verification tooling.

## Directives

1. **Scaffolding Exclusion**:
   - Production code must contain only finalized code and docstrings.
   - Do not paste or leave temporary spec directives, step numbers, or placeholder comments in code or docstrings.

2. **Fidelity with Pragmatic Grounding**:
   - Treat the spec as the authoritative blueprint. Do not invent unrequested parameters, speculative abstraction layers, or dead defensive branches.
   - Do not leave incomplete stubs, empty placeholder blocks, or unhandled unimplemented exceptions.
   - **System Truth Discrepancy Escalation**: If the spec conflicts with real codebase invariants, external type signatures, or existing contracts, do not force an incompatible implementation. Document the concrete discrepancy and escalate/adjust the invariant rather than guessing.

3. **Clean Test Naming Rule**:
   - Do NOT hardcode temporary spec/ticket IDs into test identifiers.
   - Use idiomatic, descriptive names reflecting the target symbol and verified invariant behavior.
   - If scenario traceability is desired, add it optionally to the test description/docstring, not the identifier.

4. **Streamlined Implementation Pipeline (One-Pass Gate)**:
   - **Phase 1 (Production Logic & Tests)**:
     - Implement clean production logic for all targets specified in the blueprint.
     - Implement corresponding invariant guard tests in designated test files.
     - Batch related target and test implementations without fracturing into unnecessary intermediate turns.
     - Do NOT run redundant Red-check runs or intermediate ad-hoc checks before code is written.
   - **Phase 2 (Anchor Wiring)**:
     - Wire invocations into caller files at designated `- Anchor: <anchor>` points once target symbols are in place.
   - **Phase 3 (Single-Gate Verification)**:
     - Run the unified verification gate for the target feature and spec scope:
       ```bash
       uv run python tools/agent_skills/lean_check.py --spec <spec_file>
       ```
     - `lean_check.py` executes static analysis, type checking, test suites, and scaffolding guards in parallel.
   - **Phase 4 (Targeted Failure Isolation - Only on Failure)**:
     - If verification reports failures, isolate and fix only the flagged points:
       - Test failure: run only the failing test target using the project runner to debug and repair.
       - Lint/Type failure: fix the exact line reported in the diagnostic.
       - Re-run verification to confirm resolution.

5. **Diff Coverage Resolution (Pruning Over Bloat)**:
   - If diff coverage reports untested lines:
     1. Evaluate if it is speculative defensive code (unrequested dead branches): **Prune and delete the bloat**.
     2. If required domain logic lacks coverage, add the missing boundary scenario.
     3. For non-testable infrastructure branches, use explicit coverage exclusion annotations appropriately.

## Output

Keep chat output compact and token-efficient. Retain English keys/badges while writing descriptions in natural Korean (한국어):
- **On success**: Output only the minimal completion card below without redundant code dumps or conversational filler.
- **On failure or discrepancy**: Clearly report the issue (Problem → Root Cause → Fix).

### 🔨 [IMPLEMENT] <Task Title>
> 📄 **Spec**: [`<spec_filename>.md`](docs/specs/<spec_filename>.md)  
> 🚦 **Status**: ✅ COMPLETE (<Count> file(s) modified)

- 🧪 **Verification**: <정적 검사 · 타입 검증 · 테스트 실행 · Diff 커버리지 결과 요약>

*(On Failure / Escalation)*:
### 🔨 [IMPLEMENT] <Task Title>
> 📄 **Spec**: [`<spec_filename>.md`](docs/specs/<spec_filename>.md)  
> 🚦 **Status**: ❌ ESCALATED (or ❌ FAIL)

- 💥 **Failure Point**: [<Tests | Lint | Types | Diff Coverage | Anchor Wiring | Invariant Conflict>] `<실패한 테스트명 또는 핵심 에러 1줄>`
- 🎯 **Root Cause & Action**: `<불일치 원인 또는 해결을 위해 필요한 조치 1-2줄>`
