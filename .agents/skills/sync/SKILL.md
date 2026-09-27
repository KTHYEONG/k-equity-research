---
name: sync
description: Documentation Synchronization, Compact Decision Logging, and Full Cleanup.
---

# Sync Protocol

Post-development protocol for task finalization, compact decision recording, and complete temporary artifact cleanup.

## Directives

1. **Auto-Inference & Task Sync**:
   - Do NOT ask the user to provide command arguments.
   - Automatically extract `title`, `why`, `what`, `impact`, `caveat`, `source`, and `test` from the active spec (`docs/specs/*_spec.md`) or recent audit context.
   - Run task sync:
     ```bash
     uv run python tools/agent_skills/sync_task.py --task TASK_ID --title "<Title>" --why "<Why>" --what "<What>" --impact "<Impact>" --caveat "<Caveat>" --domain <domain> --source <source_file> --test <test_file>
     ```
   - Automatically updates single ledger: `docs/decisions/task_index.json` and `docs/code_map.json`.
   - Keep each field strictly to 1 concise sentence to preserve token efficiency for future sessions.

2. **Complete Temporary Artifact Cleanup**:
   - `sync_task.py` executes full post-task cleanup by default:
     - Purges completed temporary specs (`docs/specs/*.md`, `*_contract.json`).
     - Clears all temporary scratch probe scripts and caches under `scratch/` (preserving `.gitignore`).
     - Clears all test coverage and runner artifacts under `tmp/` (preserving `.gitignore`).
     - Wipes dangling `.tmp` and `.bak` files across the workspace.
   - If specific specs must be preserved, pass `--keep-specs <path...>`. If only specific specs should be removed, pass `--remove-specs <path...>`.
   - Prepares clean decision records ready for the downstream atomic commit phase without leaving temporary clutter.

## Output

Keep chat output ultra-compact (1-2 lines). Retain English keys/badges while confirming status:

### 🧹 [SYNC] <Task Title>
- **Registry Updated**: `task_index.json` (<ADR_ID>) / `code_map.json`
- **Cleanup Completed**: 임시 스펙(`docs/specs/`), `scratch/`, `tmp/` 산출물 정리 완료 (커밋 대기)
