#!/usr/bin/env python3
"""Regenerate ``docs/code_map.json`` from the R9 mirror convention.

Every ``src/<pkg>/<mod>.py`` maps to its primary test at
``tests/unit/<pkg>/test_<mod>.py``, plus any tests in the nested package
``tests/unit/<pkg>/<mod>/``. Only active ``src`` files are mapped.
"""
from __future__ import annotations

import json
import os
import pathlib


def _repository_test_files() -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk("tests"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        found.extend(
            os.path.join(root, filename)
            for filename in sorted(files)
            if filename.startswith("test_") and filename.endswith(".py")
        )
    return sorted(found)


def _matching_tests(source_file: str, test_files: list[str]) -> list[str]:
    """Return tests covering ``source_file`` via mirror and nested-package paths."""
    parts = source_file.split("/")
    module_name = parts[-1]
    test_name = f"test_{module_name}"
    sub_path = "/".join(parts[1:-1])
    mirror = f"tests/unit/{sub_path}/{test_name}" if sub_path else f"tests/unit/{test_name}"
    matched = [mirror] if mirror in test_files else []
    if sub_path and module_name.endswith(".py"):
        nested = f"tests/unit/{sub_path}/{module_name[:-3]}/"
        matched.extend(tp for tp in test_files if tp.startswith(nested))
    return matched


def main() -> None:
    py_files: list[str] = []
    for root, dirs, files in os.walk("src"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        py_files.extend(
            os.path.join(root, filename)
            for filename in sorted(files)
            if filename.endswith(".py")
        )
    py_files = sorted(py_files)
    test_files = _repository_test_files()

    code_map: dict[str, object] = {}
    for source_file in py_files:
        if source_file.endswith("__init__.py"):
            continue
        matched = _matching_tests(source_file, test_files)
        entry: dict[str, object] = {}
        if matched:
            entry["testing"] = matched[0] if len(matched) == 1 else matched
        code_map[source_file] = entry

    docs_path = pathlib.Path("docs/code_map.json")
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(docs_path, "w", encoding="utf-8") as handle:
        json.dump(code_map, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"regenerated docs/code_map.json with {len(code_map)} canonical sources")


if __name__ == "__main__":
    main()
