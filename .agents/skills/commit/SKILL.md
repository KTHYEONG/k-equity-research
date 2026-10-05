---
name: commit
description: Execute fast, automated git commits with Korean subject & 1-line rationale.
---

# Commit Protocol

Fast automated git execution protocol enforcing atomic commits, bisect-safe change grouping, and a concise 1-line Korean rationale (`Why:`) without redundant code verification loops.

## Directives

1. **Task-Scoped Working-Tree Staging**:
   - Inspect `git status --short`. If there are no staged or modified files for the task, do not force an empty commit; report clean working tree and halt.
   - Stage ONLY the files verified as part of the current task scope (`git add <file1> <file2> ...`).
   - Do NOT perform blind blanket staging (`git add .` or `git add -A`) when unrelated working tree modifications exist.
   - Do not stage ephemeral artifacts: `scratch/`, `tmp/`, local build/test caches, compiled binaries/bytecode, or logs.
   - **Specs Are Gitignored**: `docs/specs/` is gitignored by contract. Specs serve as model/tool handoff blueprints and must never be forced into VCS staging.
   - **Immediate Post-Commit Cleanup (Specs & Scratch)**:
     - Upon successful commit, immediately delete the completed blueprint file (e.g., `rm docs/specs/<spec>.md`) tied to the committed scope.
     - Clean up temporary diagnostic/experiment scripts and artifacts created under `scratch/` for the committed task (or purge transient files in `scratch/`).
     - **Zero Confirmation**: Delete immediately without asking user confirmation or warning about untracked deletion—specs and scratch files are designed as ephemeral handoffs. Preserve other pending specifications.

2. **Commit Type Determination**:
   - Determine `<type>` based on the primary nature of the core changes:
     - `feat:`: New features, capabilities, or pipeline enhancements (`src/` + `tests/`).
     - `fix:`: Bug fixes, defect repairs, or behavioral corrections (`src/` + `tests/`).
     - `refactor:`: Code refactoring, restructuring, or renaming without behavioral changes.
     - `chore:`: Tooling, configs, scripts, rules (`pyproject.toml`, `.agents/`, `tools/`, `.gitignore`).
     - `docs:`: Standalone documentation changes (`README.md`, guides, architecture docs) with NO production code changes.

3. **Strict Korean Language Invariant (Subject & Body)**:
   - **All commit subjects and Why bodies MUST be written in natural Korean.**
   - **Subject Format**: `<type>: <한국어 1줄 요약 <= 50자>` (비즈니스/기능 목적 지향, 영어 설명 금지)
     - ✅ `feat: 실시간 데이터 수집 및 정합성 검증 파이프라인 연동`
     - ❌ `feat: implement data collection pipeline`
   - **Body Format**: `- **Why:** <해결한 구체적 문제나 비즈니스 목적을 명확한 한국어로 기술>`
     - Example: `- **Why:** 주문 체결 시 슬리피지 보정 및 상태 전이 신뢰성 확보`
   - **Prohibited Metadata**: Do not add `Co-authored-by:`, AI model names, or session trailers. Keep commits clean with only subject and rationale.
   - **Execution Safety**: Use safe quoting or heredoc syntax to prevent shell syntax errors when commit messages contain special characters.

4. **Verification**:
   - Verify the commit succeeded by checking `git log -1 --format="%h %s"`.

## Output

Return ONLY the minimal summary card below. Do NOT append conversational prose, confirmation questions, or file deletion warnings:

### 📌 [COMMIT] 완료

- 📌 **커밋**: `[<short_hash>]` <subject>
- 📊 **변경 내역**: <commit_count> commit(s) | <total_files_changed> file(s) changed
