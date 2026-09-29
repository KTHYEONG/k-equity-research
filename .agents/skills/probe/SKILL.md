---
name: probe
description: High-reasoning diagnostic protocol to analyze root causes, test hypotheses empirically, and establish invariants before specification.
---

# Probe Protocol

High-reasoning diagnostic protocol dedicated to **causal discovery, empirical validation, and failure-mode analysis** before specification.

## Cognitive Focus

Your cognitive budget is focused 100% on **Problem Space**:
*What is the fundamental causality behind this state? Under what realistic boundary conditions does it fail? What deterministic invariants must be established?*

## Directives

1. **Diagnostic Rigor & Efficient Pathing**:
   - For straightforward logical bugs or direct code-path flaws, isolate the root cause via static inspection without creating unnecessary scratch files.
   - For numerical discrepancies, subtle data pipeline distortions, or multi-step anomalies, do NOT guess. Write and run a lightweight script under `scratch/` to measure real values and reproduce failures deterministically.

2. **Concept Blueprinting (No Production Sprawl)**:
   - Do NOT generate full production patches or extensive files.
   - You may include minimal 1-3 line conceptual snippets if they demonstrate the resolution mechanism clearer than prose. Leave full implementation contracts for `/spec`.

3. **Invariants & Boundary Conditions**:
   - Formulate unambiguous Fail-Closed invariants (what state transitions must be strictly forbidden).
   - Evaluate trade-offs between competing architectural fixes.

4. **Handoff**:
   - Conclude by handing off proven invariants and root causes to `/spec` (or directly to implementation for minor fixes).

## Chat Output Format

Keep chat response clear, intuitive for humans, and token-efficient. Retain the emoji and Korean label headers while writing descriptions in natural Korean:

### 🔬 [PROBE] <Topic Title>

> 💡 **한눈에 보기**: <Intuitive real-world analogy or plain-language summary in Korean that anyone can grasp in 3 seconds>

- 🔍 **현상**: <User-facing anomaly or core issue in natural Korean>
- ⚙️ **원인**: <Data/numerical/causal root cause in 1-2 lines in natural Korean>
- 📊 **실측 증거**: <Empirically measured numbers, error magnitude, or reproduced state (optional)>
- 🛠️ **해결 전략**: <How to resolve, core mechanism, and optional 1-3 line concept snippet in natural Korean>
- 🔒 **핵심 불변식**: <1-2 non-negotiable safety invariants in natural Korean>
- ⚠️ **주의사항**: <Boundary conditions, performance limits, or caveats in 1 line in natural Korean>
