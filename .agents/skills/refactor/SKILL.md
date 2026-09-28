---
name: refactor
description: Audit code for high-ROI refactoring opportunities, prioritizing conservative retention over speculative changes. Strictly Read-Only.
---

# Refactor Assessment Protocol

Strictly read-only advisory protocol to audit code for real maintenance friction and produce actionable refactoring proposals without modifying any source code.

## Core Directives

1. **Strictly Read-Only (Non-Destructive)**:
   - Do NOT edit, delete, or create source code files.
   - Your sole responsibility is assessment and proposal generation.

2. **Conservative Retention Bias (Keep As-Is First)**:
   - The default verdict is ALWAYS "Keep As-Is".
   - Do NOT propose changes for cosmetic styling, textbook purism, or hypothetical future requirements.
   - Propose refactoring ONLY when the cost of inaction (high bug risk, severe cognitive drag, fragile cascading changes) clearly outweighs the risk of regression.

3. **Evaluation Lenses (Assess Only 3 Core Factors)**:
   - **Cognitive Load**: Is the mental model needlessly complicated by deep nesting, hidden state mutations, or opaque control flow?
   - **Change Locality**: Does a single business rule change force synchronized edits across unrelated files or modules?
   - **Testability**: Is core logic tightly coupled to external systems or side-effects, making isolated testing difficult?

4. **Environment Adaptation**:
   - Detect and respect the project's native tooling (e.g. existing test runners, linters, type-checkers).
   - Validation steps must leverage the project's existing verification harness without introducing new dependencies.

5. **Two-Track Output Routing**:
   - **Track A (Clear / Low-Risk)**: Blueprint proposal ready for implementation or spec freezing.
   - **Track B (Complex / High-Risk)**: Exploratory spike/probe proposal to test architectural trade-offs or benchmark performance before altering contracts.

## Output Format

Keep chat response compact and token-efficient. Retain English keys/badges while writing descriptions, findings, and rationales in natural Korean (한국어):

### 🔎 [REFACTOR-AUDIT] <Target File / Module / Symbol>
> 🚦 **Verdict**: [🛡️ KEEP AS-IS | 💡 PROPOSAL]

#### 1. 현황 및 유지 사유 (Keep As-Is Defense)
- <현재 구조가 충분히 적절하거나, 굳이 건드리지 말아야 하는 이유 1-2줄>

*(Include Sections 2 & 3 only if Verdict is 💡 PROPOSAL)*

#### 2. 핵심 마찰점 (Friction Points)
- **위치**: `<file:line 또는 symbol>`
- **문제**: <인지 부하, 결합도, 테스트 용이성 측면의 실제 문제 1줄>
- **영향**: <방치 시 유지보수 비용 및 회귀 위험 1줄>

#### 3. 실행 제안 (Actionable Proposal)
*(택일: Track A 또는 Track B)*

- **Track A: Blueprint (명확한 구조 개선)**
  - 🛠️ **개선 방향**: <단순화/인라인/격리 등 핵심 변경 방향 1줄>
  - 🔒 **동결 불변식**: <반드시 보존되어야 하는 공개 API 및 외부 동작>
  - 🧪 **검증 방안**: <프로젝트 내 테스트/린트 검증 명령어>
  - 👉 **다음 단계**: `/spec` 또는 승인 후 구현 연계

- **Track B: Spike / Probe (아키텍처 가설 검증)**
  - 🔬 **가설 및 목표**: <검증하고자 하는 설계 가설 또는 성능 목표 1줄>
  - ⚠️ **경계 위험**: <예상되는 부작용, 트레이드오프 또는 파괴적 영향>
  - 🧪 **실험 방안**: <스크래치 스크립트 또는 벤치마크 검증 계획>
  - 👉 **다음 단계**: `/probe` 연계 탐색 진행
