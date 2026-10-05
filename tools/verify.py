#!/usr/bin/env python3
"""Fast, token-efficient local verification gate: Lint, Type, Tests & 100% Diff-Coverage."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from typing import Any

JsonDiag = dict[str, Any]

if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())


def _emit_json(
    status: str,
    phase: str,
    diagnostics: list[JsonDiag],
    coverage: int | None = None,
) -> str:
    return json.dumps(
        {
            "status": status,
            "phase": phase,
            "exit_code": 0 if status == "PASS" else 1,
            "coverage": coverage,
            "diagnostics": diagnostics,
        }
    )


def _exit_with_diags(phase: str, header: str, diags: list[JsonDiag], exit_code: int = 1) -> None:
    print(header)
    for d in diags:
        err = d.get("error", "")
        if err:
            print(f"FAIL | {err}")
    print(_emit_json("FAIL", phase, diags), file=sys.stderr)
    sys.exit(exit_code)


def run_cmd(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    # Strip unnecessary 'uv run' prefix when already running inside virtualenv
    if len(cmd) >= 3 and cmd[0] == "uv" and cmd[1] == "run" and os.environ.get("VIRTUAL_ENV"):
        cmd = cmd[2:]
    env = os.environ.copy()
    env["COVERAGE_NO_CTRACE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, shell=False, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"Error: timed out after {timeout}s.",
        )


def _available_memory_gb() -> float:
    """Return available system RAM in gigabytes using Linux procfs or sysconf."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError):
        pass
    try:
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")) / (1024**3)
    except (ValueError, OSError, AttributeError):
        return 8.0


# ---------------------------------------------------------------------------
# Scaffolding Leak Guard: Block temporary spec/recipe text from production code
# ---------------------------------------------------------------------------

_SCAFFOLDING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\[(?:STEP-BY-STEP\s+)?RECIPE", re.IGNORECASE),
        "Recipe directive leaked into code/docstring",
    ),
    (
        re.compile(r"\[ALGORITHM\s+RECIPE\]", re.IGNORECASE),
        "Algorithm recipe placeholder leaked into code",
    ),
    (
        re.compile(r"^\s*(?:#|/{2})?\s*Step\s+\d+\.\s+[A-Z]", re.MULTILINE),
        "Spec Step-by-step numbering leaked into code/comment",
    ),
    (
        re.compile(r"\b(?:TODO|FIXME)\b\s*:", re.IGNORECASE),
        "TODO/FIXME placeholder found in modified code",
    ),
)


def _check_scaffolding_leaks(py_files: list[str]) -> list[JsonDiag]:
    """Verify that no temporary spec recipes or placeholders remain in production code."""
    diags: list[JsonDiag] = []
    src_files = [f for f in py_files if (f.startswith("src/") or "/src/" in f) and os.path.isfile(f)]

    for fpath in src_files:
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue

        for idx, line in enumerate(lines, start=1):
            for pat, desc in _SCAFFOLDING_PATTERNS:
                if pat.search(line):
                    diags.append(
                        {
                            "file": fpath,
                            "line": idx,
                            "error": f"Scaffolding Leak: {desc} -> '{line.strip()}'",
                            "fix_hint": "Remove temporary spec/recipe directives and write clean production code/docstring",
                        }
                    )
                    break
    return diags


# ---------------------------------------------------------------------------
# Direct Test Matching (Predictable, zero-cascade convention mapping)
# ---------------------------------------------------------------------------


def _find_test_files(py_files: list[str]) -> tuple[list[str], list[str]]:
    """Find direct unit tests corresponding to modified source files.

    Returns ``(mapped, unmapped)``: files explicitly passed through are kept,
    ``src/<pkg>/<mod>.py`` resolves to ``tests/unit/<pkg>/test_<mod>.py`` and
    then to ``tests/unit/<pkg>/<mod>/`` when that package directory exists.
    Source modules without a mapped test are reported as ``unmapped``.
    """
    test_files = [f for f in py_files if f.startswith("tests/") or "test_" in f]
    source_files = [f for f in py_files if f.startswith("src/") and not f.endswith("__init__.py")]
    unmapped: list[str] = []

    # 1. Direct path convention: src/path/module.py -> tests/unit/path/test_module.py
    #    then tests/unit/path/module/ when that nested package exists.
    for sf in source_files:
        rel = sf[4:]  # strip 'src/'
        parts = rel.split("/")
        mod_name = parts[-1]
        test_name = f"test_{mod_name}"
        sub_path = "/".join(parts[:-1])

        candidates = [
            f"tests/unit/{sub_path}/{test_name}" if sub_path else f"tests/unit/{test_name}",
            f"tests/unit/{test_name}",
        ]
        nested_dir = f"tests/unit/{sub_path}/{mod_name[:-3]}" if sub_path and mod_name.endswith(".py") else ""
        found = False
        for cand in candidates:
            if cand in test_files:
                found = True
                break
            if os.path.isfile(cand):
                test_files.append(cand)
                found = True
                break
        if not found and nested_dir and os.path.isdir(nested_dir):
            nested_tests = sorted(
                f"tests/unit/{sub_path}/{mod_name[:-3]}/{p}"
                for p in os.listdir(nested_dir)
                if p.startswith("test_") and p.endswith(".py")
            )
            if nested_tests:
                test_files.extend(t for t in nested_tests if t not in test_files)
                found = True
        if not found:
            unmapped.append(sf)


    return sorted(dict.fromkeys(test_files)), sorted(dict.fromkeys(unmapped))


# ---------------------------------------------------------------------------
# Diff Coverage Gate
# ---------------------------------------------------------------------------


def _git_diff_added_lines(file: str) -> set[int] | None:
    """1-indexed line numbers this working-tree diff adds to `file`."""
    status_res = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain", "--", file],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if status_res.stdout.strip().startswith("??"):
        return None
    diff_res = subprocess.run(  # noqa: S603
        ["git", "diff", "--unified=0", "HEAD", "--", file],
        capture_output=True,
        text=True,
        timeout=10,
    )
    added: set[int] = set()
    cur_line = 0
    for line in diff_res.stdout.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            if m:
                cur_line = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.add(cur_line)
            cur_line += 1
        elif not line.startswith("-"):
            cur_line += 1
    return added


def _check_diff_coverage(
    src_files: list[str], cov_json_path: str, unmapped: list[str] | None = None
) -> tuple[list[JsonDiag], int | None]:
    """Verify that every line added to touched src/ files is executed by tests.

    Modules without a mapped test are reported as ``unmapped`` diagnostics by
    the caller instead: the mapped-test run cannot be expected to cover them.
    """
    if not os.path.exists(cov_json_path):
        return [], None
    try:
        with open(cov_json_path, encoding="utf-8") as f:
            cov_data = json.load(f)
    except Exception:
        return [], None

    skipped = set(unmapped or [])
    files_data = cov_data.get("files", {})
    diags: list[JsonDiag] = []
    total_added = 0
    total_covered = 0
    for sf in src_files:
        if sf in skipped:
            continue
        entry = files_data.get(sf) or files_data.get(sf.replace("/", os.sep))
        if not entry:
            continue
        missing = set(entry.get("missing_lines", []))
        executed = set(entry.get("executed_lines", []))
        added = _git_diff_added_lines(sf)
        if added is None:
            added = executed | missing
        added &= executed | missing
        if not added:
            continue
        total_added += len(added)
        total_covered += len(added - missing)
        uncovered_new = sorted(added & missing)
        if uncovered_new:
            shown = uncovered_new[:10]
            diags.append(
                {
                    "file": sf,
                    "line": shown[0],
                    "error": f"{len(uncovered_new)} newly-added line(s) not executed by tests: {shown}",
                    "fix_hint": "Add or update tests exercising these lines",
                }
            )
    pct = round(100 * total_covered / total_added) if total_added else None
    return diags, pct


# ---------------------------------------------------------------------------
# Main CLI Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast, token-efficient local verification gate.")
    parser.add_argument("--files", nargs="*", default=[], help="Explicit files to check")
    parser.add_argument("--fast", action="store_true", help="Run static checks only (scaffolding, ruff, mypy)")
    parser.add_argument("--skip-lint", action="store_true", help="Skip Ruff linting")
    parser.add_argument("--skip-mypy", action="store_true", help="Skip Mypy static check")
    parser.add_argument("--no-cov", action="store_true", help="Disable diff-coverage gate")
    parser.add_argument("--no-xdist", action="store_true", help="Force serial pytest execution (-n 0)")
    parser.add_argument("--timeout", type=int, default=None, help="Pytest timeout in seconds")
    parser.add_argument(
        "--run-slow",
        action="store_true",
        help="Run tests marked slow (real-data suite) instead of excluding them",
    )
    parser.add_argument(
        "--pre-impl",
        action="store_true",
        help="Validate spec blueprint paths and wiring anchors before implementation",
    )
    args = parser.parse_args()


    # 1. File discovery from git if not explicitly passed
    if not args.files:
        try:
            diff_res = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            git_files = [
                line[3:].strip()
                for line in diff_res.stdout.splitlines()
                if "D" not in line[:2]
                and line[3:].strip().endswith(".py")
                and not line[3:].strip().startswith("tools/")
                and not line[3:].strip().endswith("conftest.py")
                and os.path.exists(line[3:].strip())
            ]
            args.files = git_files
        except Exception:
            args.files = []

    py_files = [f for f in args.files if f.endswith(".py")]
    if not py_files:
        print("ALLCHECKS:PASS | No modified .py files detected")
        sys.exit(0)

    # 2. Scaffolding Leak Guard
    scaffolding_diags = _check_scaffolding_leaks(py_files)
    if scaffolding_diags:
        _exit_with_diags(
            "scaffolding-guard",
            f"FAIL | Scaffolding Leak: {len(scaffolding_diags)} temporary spec/recipe artifact(s) found in code",
            scaffolding_diags,
        )

    # 3. Parallel Static Checks (Ruff, Mypy)
    def check_ruff() -> tuple[str, int, list[JsonDiag], str]:
        if args.skip_lint or not py_files:
            return "ruff", 0, [], ""
        res = run_cmd(["uv", "run", "ruff", "check", *py_files, "--quiet"])
        if res.returncode != 0:
            out = "\n".join((res.stdout or res.stderr).strip().splitlines()[:10])
            return "ruff", 1, [{"file": py_files[0], "line": 0, "error": out, "fix_hint": "Fix ruff lint errors"}], "FAIL | Ruff Lint Failed"
        return "ruff", 0, [], ""

    def check_mypy() -> tuple[str, int, list[JsonDiag], str]:
        if args.skip_mypy or not py_files:
            return "mypy", 0, [], ""
        target_mypy = [f for f in py_files if f.startswith("src/")] or py_files
        res = run_cmd(["uv", "run", "mypy", *target_mypy, "--ignore-missing-imports"])
        if res.returncode != 0:
            out = "\n".join((res.stdout or res.stderr).strip().splitlines()[:10])
            return "mypy", 1, [{"file": target_mypy[0], "line": 0, "error": out, "fix_hint": "Fix mypy type errors"}], "FAIL | Mypy Type Check Failed"
        return "mypy", 0, [], ""

    # 3. Sequential Static Checks (Ruff Fail-Fast, then Mypy)
    for check_fn in (check_ruff, check_mypy):
        phase, code, diags, msg = check_fn()
        if code != 0:
            _exit_with_diags(phase, msg, diags)

    if args.fast:
        print("PASS | Fast Check Passed (Scaffolding, Ruff, Mypy verified)")
        print(_emit_json("PASS", "fast-check", [], None), file=sys.stderr)
        return

    # 4. Direct Test Discovery
    test_files, unmapped = _find_test_files(py_files)
    if not test_files:
        if unmapped:
            diags = [
                {"file": m, "line": 0, "error": f"unmapped: no test covers {m}", "fix_hint": "Add tests/unit coverage for this module"}
                for m in unmapped
            ]
            print(_emit_json("PASS", "all", diags, None), file=sys.stderr)
            print(f"PASS | Lint & Type check passed ({len(unmapped)} unmapped module(s) reported)")
            return
        print("PASS | Lint & Type check passed (no tests to run)")
        print(_emit_json("PASS", "all", [], None), file=sys.stderr)
        return

    # 5. Smart Pytest Execution (Resource Safety Guard)
    # 5. Smart Pytest Execution (Resource Safety Guard: Serial Execution Default)
    # 다중 프로젝트 및 로컬 동시성 환경 안정성을 위해 기본값은 항상 단일 프로세스(-n 0)로 고정.
    # CI 등에서 명시적으로 LEAN_CHECK_WORKERS 환경변수가 2 이상으로 지정된 경우에만 제한적 병렬 허용.
    env_workers = os.environ.get("LEAN_CHECK_WORKERS")
    avail_mem_gb = _available_memory_gb()

    if (
        args.no_xdist
        or not env_workers
        or not env_workers.isdigit()
        or int(env_workers) <= 1
        or avail_mem_gb < 2.0
    ):
        xdist_args = ["-p", "no:cacheprovider", "-n", "0"]
    else:
        target_workers = int(env_workers)
        worker_count = min(target_workers, os.cpu_count() or 2, len(test_files))
        xdist_args = ["-p", "no:cacheprovider", "-n", str(worker_count)]

    src_files = [f for f in py_files if f.startswith("src/")]
    cov_json_path = "scratch/verify_coverage.json"
    cov_args: list[str] = []

    if src_files and not args.no_cov:
        os.makedirs("scratch", exist_ok=True)
        with contextlib.suppress(OSError):
            os.remove(cov_json_path)
        pkgs = {f.split("/")[1] for f in src_files if len(f.split("/")) >= 2}
        cov_pkgs = [f"--cov=src/{p}" for p in sorted(pkgs)] if pkgs else ["--cov=src"]
        cov_args = [*cov_pkgs, f"--cov-report=json:{cov_json_path}"]

    pytest_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "not slow" if not args.run_slow else "slow",
        *test_files,
        *xdist_args,
        *cov_args,
        "-q",
        "--tb=line",
    ]
    pytest_timeout = args.timeout or max(60, min(240, 20 * len(test_files)))
    pt_res = run_cmd(pytest_cmd, timeout=pytest_timeout)

    if pt_res.returncode == 124:
        _exit_with_diags(
            "pytest-timeout",
            f"FAIL | Pytest Timed Out ({pytest_timeout}s)",
            [{
                "file": "",
                "line": 0,
                "error": f"pytest timed out after {pytest_timeout}s across {len(test_files)} file(s).",
                "fix_hint": "Use --files to scope checks, investigate slow tests, or pass --timeout with a larger value.",
            }],
        )

    if pt_res.returncode == 0:
        cov_diags, cov_pct = _check_diff_coverage(src_files, cov_json_path, unmapped) if cov_args else ([], None)
        if cov_diags:
            _exit_with_diags(
                "coverage",
                f"FAIL | Diff Coverage: {len(cov_diags)} file(s) with untested new lines",
                cov_diags,
            )
        unmapped_diags = [
            {"file": m, "line": 0, "error": f"unmapped: no test covers {m}", "fix_hint": "Add tests/unit coverage for this module"}
            for m in unmapped
        ]
        cov_suffix = f", Diff-Coverage {cov_pct}%" if cov_pct is not None else ""
        unmapped_suffix = f", {len(unmapped_diags)} unmapped" if unmapped_diags else ""
        print(f"PASS | All checks passed (Scaffolding-Clean, Lint, Type, Tests{cov_suffix}{unmapped_suffix})")
        print(_emit_json("PASS", "all", unmapped_diags, cov_pct), file=sys.stderr)
    else:
        last_err = [
            line
            for line in (pt_res.stdout or "").splitlines()
            if any(x in line for x in ("FAIL", "Error", "AssertionError"))
        ]
        cause = last_err[-1] if last_err else (pt_res.stderr or "Check pytest output.").strip()
        cause_sliced = "\n".join(cause.splitlines()[:10])
        _exit_with_diags(
            "pytest",
            f"FAIL | Pytest Failed: {cause_sliced}",
            [{"file": "", "line": 0, "error": cause_sliced, "fix_hint": "Fix failing pytest assertions"}],
        )


if __name__ == "__main__":
    main()
