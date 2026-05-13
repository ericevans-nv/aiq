#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""AI-Q skill-eval coordinator.

This script validates Agent Skill eval specs, generates Harbor-style datasets,
and can optionally run Harbor when the target environment is available.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = Path(os.environ.get("AIQ_SKILL_EVAL_OUTPUT_DIR", "/tmp/aiq-skill-eval/datasets"))
REQUIRED_SPEC_KEYS = ("skills", "resources", "env", "expects")


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, text=True, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _git_changed_files(base: str) -> list[str]:
    result = _run(["git", "diff", "--name-only", f"{base}...HEAD"])
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _changed_skill_names(base: str | None) -> set[str]:
    if not base:
        return set()
    changed: set[str] = set()
    for path in _git_changed_files(base):
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == ".agents" and parts[1] == "skills":
            changed.add(parts[2])
    return changed


def _discover_specs(*, all_specs: bool, base: str | None) -> list[Path]:
    changed = _changed_skill_names(base)
    specs = sorted((REPO_ROOT / ".agents" / "skills").glob("*/eval/*.json"))
    if all_specs or not changed:
        return specs
    return [spec for spec in specs if spec.parts[-3] in changed]


def _validate_spec(spec_path: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text())
    missing = [key for key in REQUIRED_SPEC_KEYS if key not in spec]
    if missing:
        raise ValueError(f"{spec_path} missing required keys: {', '.join(missing)}")
    if not isinstance(spec["skills"], list) or not spec["skills"]:
        raise ValueError(f"{spec_path} must define non-empty skills list")
    platforms = spec.get("resources", {}).get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ValueError(f"{spec_path} must define resources.platforms")
    if not isinstance(spec["expects"], list) or not spec["expects"]:
        raise ValueError(f"{spec_path} must define non-empty expects list")
    for idx, expect in enumerate(spec["expects"], start=1):
        if "query" not in expect or "checks" not in expect:
            raise ValueError(f"{spec_path} expects[{idx}] must define query and checks")
    return spec


def _adapter_for(skill: str) -> Path:
    return REPO_ROOT / ".github" / "skill-eval" / "adapters" / skill / "generate.py"


def _task_roots(dataset_root: Path) -> list[Path]:
    roots: list[Path] = []
    for task_toml in dataset_root.rglob("task.toml"):
        parent = task_toml.parent
        if parent.name.startswith("step-"):
            roots.append(parent.parent)
        else:
            roots.append(parent)
    return sorted(set(roots))


def _run_harbor(task_root: Path) -> int:
    agent = os.environ.get("AIQ_SKILL_EVAL_AGENT", "claude-code")
    model = os.environ.get("AIQ_SKILL_EVAL_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    max_retries = os.environ.get("AIQ_SKILL_EVAL_MAX_RETRIES", "1")
    if agent != "oracle" and not model:
        print(
            "BLOCKED: AIQ_SKILL_EVAL_MODEL or ANTHROPIC_MODEL is required for Harbor execution",
            file=sys.stderr,
        )
        return 2

    cmd = [
        "uvx",
        "harbor",
        "run",
        "-p",
        str(task_root),
        "-a",
        agent,
        "--max-retries",
        max_retries,
        "-n",
        "1",
        "--yes",
        "-o",
        os.environ.get("AIQ_SKILL_EVAL_RESULTS_DIR", "/tmp/aiq-skill-eval/results"),
    ]
    if model:
        cmd.extend(["--model", model])
    if agent == "claude-code":
        cmd.extend(["--ae", "CLAUDE_CODE_DISABLE_THINKING=1"])
    if agent == "codex":
        for key in ("CODEX_FORCE_AUTH_JSON", "CODEX_AUTH_JSON_PATH"):
            value = os.environ.get(key)
            if value:
                cmd.extend(["--ae", f"{key}={value}"])
    result = _run(cmd)
    print(result.stdout)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Generate every checked-in spec")
    parser.add_argument("--base", default=os.environ.get("PR_BASE") or os.environ.get("GITHUB_BASE_REF"))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--run-harbor", action="store_true", help="Run Harbor after generating datasets")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    specs = _discover_specs(all_specs=args.all, base=args.base)
    if not specs:
        print("BLOCKED: no AI-Q skill eval specs found")
        return 0

    generated_roots: list[Path] = []
    for spec_path in specs:
        spec = _validate_spec(spec_path)
        skill = spec["skills"][0]
        skill_dir = REPO_ROOT / ".agents" / "skills" / skill
        adapter = _adapter_for(skill)
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill directory not found: {skill_dir}")
        if not adapter.exists():
            raise FileNotFoundError(f"adapter not found: {adapter}")

        result = _run(
            [
                sys.executable,
                str(adapter),
                "--output-dir",
                str(output_dir / skill),
                "--skill-dir",
                str(skill_dir),
                "--spec",
                str(spec_path),
                "--repo-root",
                str(REPO_ROOT),
            ]
        )
        print(result.stdout)
        if result.returncode != 0:
            return result.returncode
        generated_roots.extend(_task_roots(output_dir / skill / spec_path.stem))

    summary = {
        "specs": [str(path.relative_to(REPO_ROOT)) for path in specs],
        "datasets": [str(path) for path in sorted(set(generated_roots))],
    }
    print(json.dumps(summary, indent=2))

    if args.run_harbor:
        for task_root in sorted(set(generated_roots)):
            rc = _run_harbor(task_root)
            if rc != 0:
                return rc

    print(f"DONE: generated {len(set(generated_roots))} AI-Q skill-eval dataset(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
