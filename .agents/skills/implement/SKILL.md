---
name: implement
description: Precision implementation protocol delivering production logic, invariant guard tests, and wiring in a single verified pass.
---

# Implement Protocol

Atomic code implementation protocol delivering verified, production-ready changes in a single pass. Supports both **spec-driven execution** (formal blueprints) and **requirement-driven implementation** (direct task requests).

## Directives

1. **Dual-Mode Input Flexibility**:
   - **Spec-Driven (`/implement docs/specs/<spec>.md`)**: Treat the blueprint as the authoritative contract. Adhere strictly to declared signatures, invariants, and wiring points without speculative invention.
   - **Requirement-Driven (`/implement <task / targets>`)**: When no spec exists, derive contracts and tests directly from user requirements and existing codebase patterns.

2. **Atomic One-Pass Delivery**:
   - Deliver complete production logic together with corresponding invariant guard tests and caller wiring in one cohesive pass.
   - Wire all new symbols to their production callers and entry points without leaving dead or unreachable code.
   - Zero incomplete stubs, empty placeholder blocks, or unhandled `TODO`/`pass` statements.

3. **Single-Gate Verification**:
   - Run the project's native verification toolchain declared in `AGENTS.md` under `Project Toolchain` (or project runner).
   - Ensure all tests pass with zero static analysis or type regressions.
   - If verification reports failures, isolate and fix only the flagged points.

4. **Production Hygiene**:
   - Production code and docstrings must contain only finalized implementations. Never paste temporary spec directives, step numbers, or scratch commentary into production files.
   - Focus tests on meaningful domain transformations and boundary conditions; prune dead defensive branches rather than writing vacuous tests.

## Chat Output Format

Keep output concise and token-efficient. Retain English badges/keys while writing descriptions in natural Korean (한국어):

### 🔨 [IMPLEMENT] <Task Title>
> 📄 **Scope**: [<spec_file.md> or <target files modified>]  
> 🚦 **Status**: ✅ COMPLETE (<N> file(s) modified)

- 🧪 **Verification**: <정적 검사 · 타입 검증 · 테스트 실행 결과 요약>

*(On Failure / Escalation)*:
### 🔨 [IMPLEMENT] <Task Title>
> 📄 **Scope**: [<spec_file.md> or <target files modified>]  
> 🚦 **Status**: ❌ FAIL (or ❌ ESCALATED)

- 💥 **Failure Point**: <실패한 테스트 또는 에러 1줄>
- 🎯 **Root Cause & Action**: <원인 및 필요한 조치 1-2줄>
