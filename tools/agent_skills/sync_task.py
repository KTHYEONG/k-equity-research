#!/usr/bin/env python3
"""Sync: Task Metadata + Code Map + Cleanup Protocol."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
from datetime import datetime


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _path_exists(path: str) -> bool:
    return os.path.exists(path)


def _resolve_test_path(source_file: str) -> str | None:
    if not source_file.startswith("src/") or source_file.endswith("__init__.py"):
        return None
    parts = source_file.split("/")
    module_name = parts[-1]
    test_name = f"test_{module_name}"
    # Root-level tests (repo convention: tests/test_<module>.py)
    if os.path.exists(f"tests/{test_name}"):
        return f"tests/{test_name}"
    for category in ["unit", "integration", "e2e"]:
        sub = "/".join(parts[1:-1])
        test_dir = f"tests/{category}/{sub}" if sub else f"tests/{category}"
        tp = f"{test_dir}/{test_name}"
        if os.path.exists(tp):
            return tp
    return None


_MEMORY_FIELD_CHAR_CAP = 300


def _cap_field(text: str) -> str:
    """Hard backstop on why/what/impact to keep future context lookups token-efficient."""
    text = text.strip()
    if len(text) <= _MEMORY_FIELD_CHAR_CAP:
        return text
    return text[:_MEMORY_FIELD_CHAR_CAP].rstrip() + "..."


def _update_decisions_json(
    task: str,
    title: str,
    why: str,
    what: str,
    impact: str,
    domain: str,
    caveat: str = "",
    failed_hypothesis: str | None = None,
    failure_reason: str | None = None,
) -> str:
    # Tolerate absent active task_index.json; do not recreate archived records under docs/
    # Archived decisions remain in legacy/docs/decisions
    if os.path.exists("legacy/docs/decisions/task_index.json") and not os.path.exists("docs/decisions"):
        # Active decisions are archived; skip recreation
        date_str = datetime.now().strftime("%Y-%m-%d")
        adr_date = datetime.now().strftime("%Y%m%d")
        return f"ADR_{adr_date}_{task.replace('TASK_', '')}"
    date_str = datetime.now().strftime("%Y-%m-%d")
    adr_date = datetime.now().strftime("%Y%m%d")
    adr_id = f"ADR_{adr_date}_{task.replace('TASK_', '')}"

    tasks_json_path = "docs/decisions/task_index.json"

    # 1. Update task_index.json
    index_data: dict[str, list[dict[str, str]]] = {"tasks": []}
    if _path_exists(tasks_json_path):
        with contextlib.suppress(Exception):
            parsed = json.loads(_read_file(tasks_json_path))
            if isinstance(parsed, dict) and "tasks" in parsed and isinstance(parsed["tasks"], list):
                index_data = parsed

    new_task_entry = {
        "task_id": task,
        "date": date_str,
        "adr_id": adr_id,
        "domain": domain,
        "title": title,
        "why": _cap_field(why),
        "resolution": _cap_field(what),
        "impact": _cap_field(impact),
    }
    if caveat:
        new_task_entry["caveat"] = _cap_field(caveat)

    # Prepend new task entry
    index_data["tasks"] = [new_task_entry] + [t for t in index_data.get("tasks", []) if t.get("task_id") != task]
    # Keep max 100 entries in task_index.json
    index_data["tasks"] = index_data["tasks"][:100]
    _write_file(tasks_json_path, json.dumps(index_data, indent=2, ensure_ascii=False) + "\n")

    return adr_id


def _update_index(source_file: str, test_file: str | None, doc_file: str | None) -> None:
    # Tolerate absent active code_map.json; do not recreate archived records under docs/
    if os.path.exists("legacy/docs/code_map.json") and not os.path.exists("docs/code_map.json"):
        return
    code_map_path = "docs/code_map.json"
    data: dict[str, dict[str, str | list[str]]] = {}

    if _path_exists(code_map_path):
        try:
            data = json.loads(_read_file(code_map_path))
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, ValueError):
            shutil.copyfile(code_map_path, f"{code_map_path}.bak")
            data = {}

    if source_file not in data:
        data[source_file] = {}

    entry = data[source_file]
    if doc_file is not None:
        entry["architecture"] = doc_file
    if test_file is not None:
        cur = entry.get("testing")
        if cur is None:
            entry["testing"] = test_file
        elif isinstance(cur, str) and cur != test_file:
            entry["testing"] = [cur, test_file]
        elif isinstance(cur, list) and test_file not in cur:
            cur.append(test_file)

    _write_file(code_map_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


EXCLUDED_DIRS = frozenset({".git", ".venv", ".mypy_cache", ".ruff_cache", "__pycache__", "node_modules"})


def _wipe_temp_artifacts() -> int:
    count = 0
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for f in files:
            if f.endswith((".tmp", ".bak")):
                fpath = os.path.join(root, f)
                try:
                    os.remove(fpath)
                    count += 1
                except OSError:
                    pass
    return count


def _clean_scratch_dir(purge_all: bool = True, task_id: str | None = None) -> int:
    scratch_dir = "scratch"
    if not os.path.exists(scratch_dir):
        return 0
    count = 0
    if purge_all:
        for entry in os.listdir(scratch_dir):
            if entry == ".gitignore":
                continue
            epath = os.path.join(scratch_dir, entry)
            try:
                if os.path.isdir(epath):
                    for _, _, files in os.walk(epath):
                        count += len(files)
                    shutil.rmtree(epath, ignore_errors=True)
                    count += 1
                else:
                    os.remove(epath)
                    count += 1
            except OSError:
                pass
        return count

    target_pattern = task_id.lower().replace("task_", "").strip() if task_id else None
    if not target_pattern:
        return 0
    regex = re.compile(rf"(?:^|[^a-zA-Z0-9]){re.escape(target_pattern)}(?:[^a-zA-Z0-9]|$)")
    for root, dirs, files in os.walk(scratch_dir, topdown=False):
        for f in files:
            if f == ".gitignore":
                continue
            if regex.search(f.lower()):
                fpath = os.path.join(root, f)
                try:
                    os.remove(fpath)
                    count += 1
                except OSError:
                    pass
        for d in dirs:
            dpath = os.path.join(root, d)
            with contextlib.suppress(OSError):
                if not os.listdir(dpath):
                    os.rmdir(dpath)
    return count


def _clean_tmp_dir() -> int:
    tmp_dir = "tmp"
    if not os.path.exists(tmp_dir):
        return 0
    count = 0
    for entry in os.listdir(tmp_dir):
        if entry == ".gitignore":
            continue
        epath = os.path.join(tmp_dir, entry)
        try:
            if os.path.isdir(epath):
                for _, _, files in os.walk(epath):
                    count += len(files)
                shutil.rmtree(epath, ignore_errors=True)
                count += 1
            else:
                os.remove(epath)
                count += 1
        except OSError:
            pass
    return count


def _clean_logs_dir() -> int:
    logs_dir = "logs"
    if not os.path.exists(logs_dir):
        return 0
    count = 0
    for root, dirs, files in os.walk(logs_dir, topdown=False):
        for f in files:
            if f == ".gitignore":
                continue
            fpath = os.path.join(root, f)
            try:
                os.remove(fpath)
                count += 1
            except OSError:
                pass
        for d in dirs:
            dpath = os.path.join(root, d)
            with contextlib.suppress(OSError):
                os.rmdir(dpath)
    return count


def _clean_specs(
    remove_specs: list[str] | None = None,
    keep_specs: list[str] | None = None,
    task_id: str | None = None,
    purge_all: bool = True,
) -> int:
    specs_dir = "docs/specs"
    if not _path_exists(specs_dir):
        return 0

    count = 0
    keep_set = set(keep_specs or [])

    # 1. Targeted removal when explicit spec files are provided
    if remove_specs:
        target_prefixes: set[str] = set()
        for item in remove_specs:
            base = (
                item.replace(".md", "")
                .replace("_contract.json", "")
                .replace("contract.json", "")
                .replace("docs/specs/", "")
                .strip()
            )
            if base:
                target_prefixes.add(base.lower())
        regexes = [
            re.compile(rf"(?:^|[^a-zA-Z0-9]){re.escape(p)}(?:[^a-zA-Z0-9]|$)")
            for p in target_prefixes
        ]
        for fname in os.listdir(specs_dir):
            if fname in keep_set:
                continue
            if fname.endswith((".md", "_contract.json", "contract.json")):
                fname_lower = fname.lower()
                fname_base = fname_lower.replace(".md", "").replace("_contract.json", "").replace("contract.json", "")
                if any(r.search(fname_lower) or r.search(fname_base) for r in regexes):
                    fpath = os.path.join(specs_dir, fname)
                    try:
                        os.remove(fpath)
                        count += 1
                    except OSError:
                        pass
        return count

    # 2. Default: purge all temporary specs except keep_set
    if purge_all:
        for fname in os.listdir(specs_dir):
            if fname in keep_set:
                continue
            if fname.endswith((".md", "_contract.json", "contract.json")):
                fpath = os.path.join(specs_dir, fname)
                try:
                    os.remove(fpath)
                    count += 1
                except OSError:
                    pass
        return count

    # 3. Fallback targeted pattern matching with task_id
    if task_id:
        norm = task_id.lower().replace("task_", "").strip()
        if norm:
            regex = re.compile(rf"(?:^|[^a-zA-Z0-9]){re.escape(norm)}(?:[^a-zA-Z0-9]|$)")
            for fname in os.listdir(specs_dir):
                if fname in keep_set:
                    continue
                if fname.endswith((".md", "_contract.json", "contract.json")):
                    fname_lower = fname.lower()
                    fname_base = fname_lower.replace(".md", "").replace("_contract.json", "").replace("contract.json", "")
                    if regex.search(fname_lower) or regex.search(fname_base):
                        fpath = os.path.join(specs_dir, fname)
                        try:
                            os.remove(fpath)
                            count += 1
                        except OSError:
                            pass
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync: Task Registry + Code Map + Anti-Pattern Registry + Cleanup.")
    parser.add_argument("--task", required=True, help="Task ID (e.g. TASK_L0_MTF_FUSION)")
    parser.add_argument("--title", required=True, help="Decision title")
    parser.add_argument("--why", required=True, help="Context/Why")
    parser.add_argument("--what", required=True, help="Resolution/What")
    parser.add_argument("--impact", required=True, help="Impact")
    parser.add_argument("--caveat", default="", help="Crucial assumption or boundary caveat to remember")
    parser.add_argument("--source", default=None, help="Modified source file path (auto-detected if omitted)")
    parser.add_argument("--domain", default="general", help="Domain category (e.g. data, research, model, config)")
    parser.add_argument("--failed-hypothesis", default=None, help="Failed hypothesis if applicable")
    parser.add_argument("--failure-reason", default=None, help="Reason why hypothesis failed")
    parser.add_argument("--test", default=None, help="Test file path")
    parser.add_argument("--doc", default=None, help="Architecture doc path")
    parser.add_argument("--remove-specs", nargs="*", default=[], help="Specific spec files to remove")
    parser.add_argument("--keep-specs", nargs="*", default=[], help="Spec files to preserve during cleanup")
    parser.add_argument("--no-purge-specs", action="store_true", help="Skip spec cleanup")
    parser.add_argument("--no-purge-scratch", action="store_true", help="Skip scratch directory purge")
    parser.add_argument("--no-purge-tmp", action="store_true", help="Skip tmp directory purge")
    parser.add_argument("--purge-all-specs", action="store_true", help="Legacy flag (default behavior)")
    parser.add_argument("--purge-scratch", action="store_true", help="Legacy flag (default behavior)")
    parser.add_argument("--purge-tmp", action="store_true", help="Legacy flag (default behavior)")
    parser.add_argument("--wipe-logs", action="store_true", help="Purge logs directory")
    args = parser.parse_args()

    logs: list[str] = []
    errors: list[str] = []

    # Auto-detect source file if omitted
    source_file = args.source
    test_file = args.test
    if not source_file:
        if args.remove_specs:
            for sf_name in args.remove_specs:
                sf_path = sf_name if os.path.isabs(sf_name) or sf_name.startswith("docs/specs/") else os.path.join("docs/specs", sf_name)
                if not sf_path.endswith(".md"):
                    sf_path += ".md"
                if os.path.exists(sf_path):
                    with contextlib.suppress(Exception):
                        content = _read_file(sf_path)
                        m_tgt = re.search(r"(?m)^##\s+Target:\s*`?([^\n`]+)`?", content)
                        if m_tgt:
                            cand_src = m_tgt.group(1).strip()
                            if cand_src.startswith("src/") and os.path.exists(cand_src):
                                source_file = cand_src
                        m_tst = re.search(
                            r"(?m)^##\s+(?:Test\s+Suite|Invariant\s+Scenarios):\s*`?([^\n`]+)`?",
                            content,
                        )
                        if m_tst and not test_file:
                            cand_tst = m_tst.group(1).strip()
                            if cand_tst.startswith("tests/") and os.path.exists(cand_tst):
                                test_file = cand_tst
                if source_file:
                    break

        norm_task = args.task.lower().replace("task_", "").strip()
        if not source_file and os.path.isdir("docs/specs") and norm_task:
            task_regex = re.compile(rf"(?:^|[^a-zA-Z0-9]){re.escape(norm_task)}(?:[^a-zA-Z0-9]|$)")
            for sf_name in os.listdir("docs/specs"):
                if task_regex.search(sf_name.lower()) and sf_name.endswith(".md"):
                    with contextlib.suppress(Exception):
                        content = _read_file(os.path.join("docs/specs", sf_name))
                        m_tgt = re.search(r"(?m)^##\s+Target:\s*`?([^\n`]+)`?", content)
                        if m_tgt:
                            cand_src = m_tgt.group(1).strip()
                            if cand_src.startswith("src/") and os.path.exists(cand_src):
                                source_file = cand_src
                        m_tst = re.search(
                            r"(?m)^##\s+(?:Test\s+Suite|Invariant\s+Scenarios):\s*`?([^\n`]+)`?",
                            content,
                        )
                        if m_tst and not test_file:
                            cand_tst = m_tst.group(1).strip()
                            if cand_tst.startswith("tests/") and os.path.exists(cand_tst):
                                test_file = cand_tst
                    if source_file:
                        break

        if not source_file:
            with contextlib.suppress(Exception):
                import subprocess

                diff_res = subprocess.run(
                    ["git", "status", "--porcelain"],  # noqa: S603, S607
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                for line in diff_res.stdout.splitlines():
                    fp = line[3:].strip()
                    if fp.startswith("src/") and fp.endswith(".py") and os.path.exists(fp):
                        source_file = fp
                        break
    if not source_file:
        source_file = "src/main.py"

    # 1. Spec Cleanup
    try:
        cleaned = 0
        if not args.no_purge_specs:
            cleaned = _clean_specs(
                remove_specs=args.remove_specs,
                keep_specs=args.keep_specs,
                task_id=args.task,
                purge_all=True,
            )
        if cleaned > 0:
            logs.append(f"Cleaned {cleaned} spec files")
    except Exception as e:
        errors.append(f"Spec cleanup failed: {e}")

    # 2. Update Smart JSON Registries (task_index.json & anti_patterns.json)
    try:
        adr_id = _update_decisions_json(
            task=args.task,
            title=args.title,
            why=args.why,
            what=args.what,
            impact=args.impact,
            domain=args.domain,
            caveat=args.caveat,
            failed_hypothesis=args.failed_hypothesis,
            failure_reason=args.failure_reason,
        )
        logs.append(f"Task Registry updated ({adr_id})")
    except Exception as e:
        errors.append(f"JSON Registry update failed: {e}")
        adr_id = "N/A"

    # 3. Code Map Update for files
    test_file = test_file or _resolve_test_path(source_file)
    try:
        _update_index(source_file, test_file, args.doc)
        logs.append(f"Code map updated for {source_file}")
    except Exception as e:
        errors.append(f"Code map update failed: {e}")

    # Auto-regenerate code map
    with contextlib.suppress(Exception):
        from tools.agent_skills import gen_code_map
        gen_code_map.main()


    # 4. Scratch, Tmp, Temp & Logs Wipe
    try:
        wiped_temp = _wipe_temp_artifacts()
        scratch_wiped = 0 if args.no_purge_scratch else _clean_scratch_dir(purge_all=True, task_id=args.task)
        tmp_wiped = 0 if args.no_purge_tmp else _clean_tmp_dir()
        logs_wiped = _clean_logs_dir() if args.wipe_logs else 0
        total_cleaned = wiped_temp + scratch_wiped + tmp_wiped + logs_wiped
        if total_cleaned > 0:
            parts = []
            if wiped_temp > 0:
                parts.append(f"{wiped_temp} temp")
            if scratch_wiped > 0:
                parts.append(f"{scratch_wiped} scratch")
            if tmp_wiped > 0:
                parts.append(f"{tmp_wiped} tmp")
            if logs_wiped > 0:
                parts.append(f"{logs_wiped} logs")
            logs.append(f"Wiped {', '.join(parts)} files")
    except Exception as e:
        errors.append(f"Cleanup failed: {e}")

    # 5. Summary
    status = "OK" if not errors else "PARTIAL"
    summary = f"### 🏁 [SYNC:{status}] [{adr_id}] | {' | '.join(logs)}"
    print(summary)  # noqa: T201
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
