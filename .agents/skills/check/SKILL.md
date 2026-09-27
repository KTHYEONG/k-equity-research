---
name: check
description: Independently audit contract compliance, typing, regressions, coverage, and test validity.
---

# Check Protocol

Independent audit gate completing the development loop (`probe` -> `spec` -> `implement` -> `check`). Performs deterministic mechanical verification followed by semantic review.

## Directives

1. **Scope Identification**:
   - Inspect modified files using `git status --short`.
   - Identify active spec under `docs/specs/*_spec.md`.

2. **Tier 1: Deterministic Mechanical Gate**:
   - Execute the unified quality verification gate:
     ```bash
     uv run python tools/agent_skills/lean_check.py
     ```
     (Pass `--spec docs/specs/<feature>_spec.md` if scoped to an active specification).
   - Verifies 4 mechanical pillars:
     1) **Scaffolding Exclusion**: No temporary comments, unresolved markers, or test skeletons in production paths.
     2) **Static Analysis & Linting**: Syntax correctness, idiomatic formatting, and static rule adherence.
     3) **Type Integrity**: Complete type annotations and strict static type/compilation check.
     4) **Invariant Test Suite & Diff-Coverage**: All unit tests pass, and every modified production branch is verified.
   - If Tier 1 fails on minor deterministic issues, apply surgical remediation (Section 4) and re-verify once. If failures persist or reveal unresolvable domain bugs, stop immediately and report diagnostics without proceeding to Tier 2.

3. **Tier 2: Semantic Review**:
   Review the `git diff` across three engineering lenses:
   1) **Test Efficacy**: Assertions validate real state transformations and domain calculations rather than vacuous checks or over-mocked logic.
   2) **Contract & Invariants**: Pre/post conditions, domain invariants, edge conditions, and error branches are handled safely.
   3) **Clean Wiring & Code Health**: New logic connects to production entry points without ghost paths, dead defensive fallback blocks, or temporary scaffolding.

4. **Surgical Remediation**:
   - The auditor may apply pinpoint fixes for deterministic issues:
     1) Minor typing, import/module binding, or format inconsistencies.
     2) Test fixture adjustments or assertion strengthening.
     3) Removal of dead code or temporary comments.
   - Re-run verification to confirm the fix is green.

5. **Non-Destructive Audit Rule (CRITICAL)**:
   - **NEVER delete, rename, or purge spec files (`docs/specs/*_spec.md`).**
   - The audit gate is strictly non-destructive. Spec archival and cleanup is exclusively reserved for the downstream `/sync` phase.

## Output

Keep chat output compact and token-efficient. Retain English keys/badges while writing descriptions in natural Korean (한국어):
When all checks pass, output only the minimal summary card below without echoing internal checklists:

### 🛡️ [CHECK] <Audit Target>
> 🚦 **Verdict**: ✅ PASS

- **Tier 1 (Mechanical)**: <정적 검사 · 타입 검증 · 테스트 실행 · Diff 커버리지 결과 요약>
- **Tier 2 (Semantic)**: Test Efficacy · Invariants · Wiring 검증 완료
*(Optional, only when surgical fix was applied)*:
- 🔧 **Remediation**: <적용한 정밀 수정 1줄 요약>

*(On Failure)*:
### 🛡️ [CHECK] <Audit Target>
> 🚦 **Verdict**: ❌ FAIL

- 💥 **Reason**: [<Tier 1 | Tier 2>] <실패 원인 및 위반 불변식 1줄>
- 🎯 **Action**: <필요한 해결 조치 1줄>
