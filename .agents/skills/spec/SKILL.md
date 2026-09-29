---
name: spec
description: Produce a machine-readable, zero-invention implementation blueprint (Markdown Spec) from probed architecture rationale.
---

# Spec Protocol

Produce an unambiguous implementation blueprint (`docs/specs/<feature>_spec.md`) optimized for mechanical downstream execution.

## Allocation

The design rationale, user requirements, and technical boundaries may come from user discussion, codebase inspection, or an exploratory probe.
Your output in `/spec` focuses strictly on:
1. **Target Interfaces & Invariants**: Exact signatures, finalized production docstrings (explaining Why and constraints), and bulleted core invariants. Do not include temporary pseudo-code, code skeletons, or recipes in docstrings or invariants (prevents model anchoring and scaffolding leaks).
2. **Wiring Points**: Caller anchor and invocation snippet.
3. **Invariant Verification Scenarios**: Concrete scenarios (Given / When / Invariant) validating contracts without freezing internal test syntax prematurely.

## Directives

1. **Context Alignment**:
   - Infer feature context, invariants, and decisions from the user request, recent discussion, or preceding investigation.
   - Inspect target files and immediate callers for exact imports and AST anchors without scanning unrelated paths.

2. **Sizing & Modularity (Single-Bolt Rule)**:
   - Keep each specification focused on a single coherent architectural unit or feature layer that can be implemented and verified in one pass.
   - **Mandatory Split**: Split into sequential sub-specs (ordered by dependency: `<feature>_<sub_layer>_spec.md`) when targets span distinct architectural layers or independent workflows that should not be coupled into a single implementation turn.

3. **Blueprint Structure (`docs/specs/<feature>_spec.md`)**:
   Organize targets with **component-centric co-location** (group Target, Wiring, and Invariant Scenarios together per component to prevent cross-referencing context loss). For multi-component specs, repeat this 3-part sequence for each component unit:

   ### A. Target Blueprint (`## Target: <relative_path>`)
   - Function/class/interface signature with finalized production interface documentation (domain context, inputs, returns, thrown errors).
   - Core Invariants: bulleted preconditions, calculation sequence, negative constraints ("Do NOT..."), edge case handling, and postconditions.
   - **No Code Skeletons**: Specify strict types and invariants only; do not provide implementation code snippets or algorithms to avoid model overfitting.

   ### B. Wiring Blueprint (`## Wiring: <caller_file>`)
   - Anchor point (`- Anchor: <symbol_or_line>`) and invocation snippet.

   ### C. Invariant Scenarios (`## Invariant Scenarios: <target_test_file>`)
   - Bulleted test scenarios with descriptive domain titles:
     - `- **<Behavior/Invariant Name>** Given ...; When ...; Invariant: ...`
     - Keep titles clear so they translate directly into clean test function names (`test_<target>_<behavior>`).

## Chat Output Format

Keep chat output ultra-compact and token-efficient. Focus strictly on mechanical implementation execution targets and execution sequence:

### 📐 [SPEC] <Feature Name>

- 🎯 **요약**: <1-line Korean summary of core capability> (<N> target file(s) · <N> scenario(s))

*(단일 스펙 실행)*:
```bash
/implement docs/specs/<feature>_spec.md
```

*(다중 분할 스펙 시 순차 실행 순서)*:
```bash
# 1. Base layer / Data models
/implement docs/specs/<feature>_part1_spec.md
# 2. Business logic / Domain services
/implement docs/specs/<feature>_part2_spec.md
# 3. Upstream wiring & Interfaces
/implement docs/specs/<feature>_part3_spec.md
```
