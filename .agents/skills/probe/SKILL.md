---
name: probe
description: Explore hypotheses, conduct empirical scratch experiments, and establish high-reasoning design rationale before contracting.
---

# Probe Protocol

High-reasoning exploratory protocol to formulate hypotheses, uncover failure modes, and empirically validate solutions via scratch experiments before freezing contracts.

## Allocation

Your cognitive budget is focused on **causal root-cause discovery, architectural trade-offs, and empirical stress testing**.
Focus on: *What is the fundamental causality behind this state? What competing architectures resolve it? Under what realistic boundary conditions does each approach break?*

## Directives

1. **Focused Context**:
   - Inspect the target module and its immediate 1-depth callers or fixtures using targeted `grep` / `view_file`.
   - Challenge historical assumptions when current empirical evidence contradicts past decisions.

2. **Empirical Probing (When Warranted)**:
   - When verifying non-obvious mathematical behavior, data transformations, or reproducing complex failures, create a lightweight scratch probe in `scratch/` and execute it via the project's runner.
   - Measure real values, state transformations, or execution bottlenecks directly on actual or synthetic workloads.
   - For straightforward defect isolation or direct code-path inspection, proceed with static reasoning and direct codebase checks without creating unnecessary scratch files.

3. **Invariants & Performance Budget**:
   - Define strict Fail-Closed invariants and domain boundaries.
   - If touching backtest, training, or bulk I/O, draft a realistic performance budget (memory, data scale, chunking).

4. **Seamless Transition**:
   - Diagnosis, invariants, and architectural decisions established here flow directly through the conversation context into `spec`. Intermediate JSON files are not required.

## Chat Output Format

Keep chat response clear, intuitive for humans, and token-efficient. Retain English keys/badges while writing descriptions in natural Korean (한국어):

### 🔬 [PROBE] <Feature / Topic Title>

- 🔍 **Problem**: <결함 또는 요구사항 1-2줄 직관적 요약>
- ⚙️ **Root Cause**: <데이터 흐름 또는 시스템 제약상의 원인 1-2줄>
- 🛠️ **Resolution**: <선택한 기술적 접근법 및 핵심 근거 1-2줄>
- 🎯 **Impact**: <시스템 및 사용자 관점의 개선 효과 1줄>
- ⚠️ **Caveats**: <핵심 가정, 경계 조건 및 주의사항 1-2줄>

---
👉 Next Step: Run `spec` skill (e.g. `/spec <feature>`)
