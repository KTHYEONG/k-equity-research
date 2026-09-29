---
name: commit
description: Execute fast, automated git commits with Korean subject & 1-line rationale.
---

# Commit Protocol

Fast automated git execution protocol enforcing atomic commits, bisect-safe change grouping, and a concise 1-line Korean rationale (`Why:`) without redundant code verification loops.

## Directives

1. **Task-Scoped Working-Tree Staging**:
   - Inspect `git status --short` and stage ONLY the files verified as part of the current task/spec scope (`git add <file1> <file2> ...`).
   - Do NOT perform blind blanket staging (`git add .` or `git add -A`) when unrelated working tree modifications exist.
   - Do not stage ephemeral artifacts: `scratch/`, `tmp/`, local build/test caches, compiled binaries/bytecode, or logs.
   - **Specs Are NOT Commit Targets**: Never stage temporary specs or contract files under `docs/specs/` (`*.md`, `*_contract.json`). Specs are transient development artifacts and must be excluded from all commits.

2. **Commit Type Determination**:
   - Determine `<type>` based on the primary nature of the core changes:
     - `feat:`: New features, capabilities, or pipeline enhancements (`src/` + `tests/`).
     - `fix:`: Bug fixes, defect repairs, or behavioral corrections (`src/` + `tests/`).
     - `refactor:`: Code refactoring, restructuring, or renaming without behavioral changes.
     - `chore:`: Tooling, configs, scripts, rules (`pyproject.toml`, `.agents/`, `tools/`, `.gitignore`).
     - `docs:`: Standalone documentation changes (`README.md`, guides, architecture docs) with NO production code changes.
   - **No Artificial `docs:` Splitting**: When a feature or bugfix task updates `task_index.json` and `code_map.json` during `/sync`, commit them together in the same atomic `feat:` or `fix:` commit. Do NOT downgrade or split the commit into an artificial `docs:` commit.

3. **Strict Korean Language Invariant (Subject & Body)**:
   - **All commit subjects and Why bodies MUST be written in natural Korean.**
   - **Subject Format**: `<type>: <한국어 1줄 요약 <= 50자>` (명확한 비즈니스/기능 목적 지향)
     - ✅ CORRECT: `feat: KIS 투자자 수급 데이터 파이프라인 연동`
     - ❌ FORBIDDEN: `feat: flow union, KIS industry, 2016 scope`
     - ✅ CORRECT: `fix: 주문 체결 슬리피지 계산 오차 보정`
     - ❌ FORBIDDEN: `fix: bundle screen parity reads missing fields as defaults`
     - ✅ CORRECT: `chore: 에이전트 지침 및 동기화 도구 정비`
     - ❌ FORBIDDEN: `chore: agent tooling and instruction sync`
     - ✅ CORRECT: `docs: 아키텍처 문서 및 파이프라인 가이드 갱신`
     - ❌ FORBIDDEN: `docs: record offsite legacy cleanup task index`
     - **NEVER write English descriptions after the type prefix `<type>:` under any circumstances.**
   - **Body Format**: `- **Why:** <해결한 구체적 문제나 비즈니스 목적을 명확한 한국어로 기술>`
     - Example: `- **Why:** 실시간 수급 불균형 지표 산출을 위한 외국인·기관 순매수 데이터 필요`
   - **Prohibited Metadata**: Do not add `Co-authored-by:`, AI model names, or session trailers. Keep commits clean with only subject and rationale.

## Output

Return ONLY the minimal summary card below:

### 📌 [COMMIT] COMPLETE

- **Commit**: `[<short_hash>]` <subject>
- **Summary**: <commit_count> commit(s) | <total_files_changed> file(s) changed
