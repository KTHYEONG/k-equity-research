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

4. **Behavior Preservation Guarantee**:
   - Any refactoring proposal MUST strictly preserve existing public API contracts and external behavior. All existing tests must continue to pass without behavioral regression.

5. **Two-Track Handoff**:
   - **Track A (Direct / Low-Risk)**: Structural cleanups ready for `/spec` blueprinting or direct implementation.
   - **Track B (Spike / High-Risk)**: Complex architectural trade-offs or performance optimizations requiring exploratory validation via `/probe`.

## Chat Output Format

Keep chat response clear, intuitive for humans, and token-efficient. Retain the emoji and Korean label headers while writing descriptions in natural Korean:

### 🔎 [REFACTOR] <Target File / Module / Symbol>
> 🚦 **판정**: [🛡️ 현행 유지 (KEEP AS-IS) | 💡 개선 제안 (PROPOSAL)]

> 💡 **한눈에 보기**: <현재 코드의 마찰점과 개선 효과를 3초 만에 이해할 수 있는 일상적 비유나 쉬운 요약 1~2줄>

#### 1. 현행 유지 사유
- <현재 구조가 충분히 적절하거나, 변경 위험 대비 실익이 적어 그대로 두어야 하는 이유 1-2줄>

*(이하 2, 3번 항목은 판정이 💡 개선 제안일 때만 포함)*

#### 2. 핵심 마찰점
- 📍 **위치**: `<file:line 또는 symbol>`
- ⚠️ **문제**: <인지 부하, 결합도, 테스트 용이성 측면의 실제 문제 1줄>
- 💥 **영향**: <방치 시 유지보수 비용 및 버그 위험 1줄>

#### 3. 개선 제안
*(택일: Track A 또는 Track B)*

- **Track A: 구조 개선 (Direct)**
  - 🛠️ **개선 방향**: <단순화/인라인/격리 등 핵심 변경 방향 1줄>
  - 🔒 **보존 불변식**: <100% 동일하게 유지되어야 하는 공개 API 및 동작>
  - 🧪 **검증 방안**: <기존 테스트 통과 및 회귀 방지 검증 명령어>
  - 👉 **다음 단계**: `/spec` 또는 승인 후 구현

- **Track B: 가설 검증 (Spike / Probe)**
  - 🔬 **가설 및 목표**: <검증하고자 하는 설계 가설 또는 성능 목표 1줄>
  - ⚠️ **잠재 위험**: <예상되는 부작용, 트레이드오프 또는 구조적 위험>
  - 🧪 **실험 방안**: <scratch/ 스크립트 또는 벤치마크 계획>
  - 👉 **다음 단계**: `/probe` 연계 탐색
