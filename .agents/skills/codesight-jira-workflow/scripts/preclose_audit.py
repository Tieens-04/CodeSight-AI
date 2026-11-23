"""Pre-close mechanical gate for CodeSight-AI Jira tickets (CA-*).

Checks:
1. Git repository exists, active branch matches `feat/ca-<N>-<slug>`, no secret files untracked/staged.
2. Docker coexistence: ensures no `codesight-*` container binds reserved FlintFlow/DocuMind host ports.
3. Quality gates via `uv run` (`ruff`, `mypy`, `pytest --cov`) once `pyproject.toml` exists.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

RESERVED_HOST_PORTS = {
    "3001": "flintflow",
    "6379": "flintflow-redis",
    "8080": "flintflow",
    "3002": "documind",
    "5432": "documind-postgres",
    "6380": "documind-redis",
    "8081": "documind",
    "9000": "documind-minio",
    "9001": "documind-minio-console",
}
SECRET_FILE_PATTERNS = re.compile(r"(^|/)\.env($|\.(?!example$|sample$|template$))|\.pem$|\.key$", re.IGNORECASE)


def run_cmd(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode, out


def resolve_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")
    return str(fallback) if fallback.exists() else None


def check_git(repo_root: Path, ticket: str | None) -> list[str]:
    errors: list[str] = []
    rc, branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if rc != 0:
        errors.append("Git repository is not initialized or has no commits (`git rev-parse HEAD` failed).")
        return errors

    expected_prefix = f"feat/{ticket.lower()}-" if ticket else "feat/ca-"
    if not re.match(r"^feat/ca-\d+-[a-z0-9][a-z0-9-]*$", branch):
        errors.append(f"Active Git branch '{branch}' must match 'feat/ca-<number>-<short-slug>'.")
    elif ticket and not branch.startswith(expected_prefix):
        errors.append(f"Active Git branch '{branch}' does not match ticket '{ticket}' (expected '{expected_prefix}*').")

    rc, status_out = run_cmd(["git", "status", "--porcelain"], repo_root)
    if rc == 0 and status_out:
        for line in status_out.splitlines():
            path_part = line[3:].strip().strip('"')
            if SECRET_FILE_PATTERNS.search(path_part):
                errors.append(f"Secret/credential file exposed in git workspace: '{path_part}'. Add to .gitignore!")
    return errors


def check_docker_ports(repo_root: Path) -> list[str]:
    errors: list[str] = []
    if not shutil.which("docker"):
        return errors
    rc, out = run_cmd(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"], repo_root)
    if rc != 0:
        return errors
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        name = parts[0].strip()
        ports = parts[1] if len(parts) > 1 else ""
        if not name.lower().startswith("codesight"):
            continue
        for host_port, owner in RESERVED_HOST_PORTS.items():
            if f":{host_port}->" in ports:
                errors.append(f"Container '{name}' collides with reserved host port {host_port} ({owner})!")
    return errors


def check_python_gates(repo_root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    summaries: list[str] = []
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        errors.append("Missing `pyproject.toml` at repository root.")
        return errors, summaries

    uv_bin = resolve_uv()
    if not uv_bin:
        errors.append("`uv` executable not found on PATH or `~/.local/bin`.")
        return errors, summaries

    rc, out = run_cmd([uv_bin, "run", "ruff", "check", "."], repo_root)
    summaries.append(f"ruff: {'PASS' if rc == 0 else 'FAIL'} ({out.splitlines()[-1] if out else 'ok'})")
    if rc != 0:
        errors.append(f"`uv run ruff check .` failed:\n{out}")

    rc, out = run_cmd([uv_bin, "run", "ruff", "format", "--check", "."], repo_root)
    summaries.append(
        f"ruff-format: {'PASS' if rc == 0 else 'FAIL'} ({out.splitlines()[-1] if out else 'ok'})"
    )
    if rc != 0:
        errors.append(f"`uv run ruff format --check .` failed:\n{out}")

    target_dir = "src" if (repo_root / "src").exists() else "."
    rc, out = run_cmd([uv_bin, "run", "mypy", target_dir], repo_root)
    summaries.append(f"mypy: {'PASS' if rc == 0 else 'FAIL'} ({out.splitlines()[-1] if out else 'ok'})")
    if rc != 0:
        errors.append(f"`uv run mypy {target_dir}` failed:\n{out}")

    if (repo_root / "tests").exists():
        cov_args = [uv_bin, "run", "pytest"]
        if (repo_root / "src").exists():
            cov_args.extend(["--cov=src", "--cov-fail-under=80"])
        rc, out = run_cmd(cov_args, repo_root)
        summaries.append(f"pytest: {'PASS' if rc == 0 else 'FAIL'} ({out.splitlines()[-1] if out else 'ok'})")
        if rc != 0:
            errors.append(f"`{' '.join(cov_args)}` failed:\n{out}")
    else:
        errors.append("Missing `tests/` directory — unit tests are required before closing a ticket.")

    return errors, summaries


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CodeSight-AI pre-close Jira quality gate.")
    parser.add_argument("--ticket", default=None, help="Jira key, e.g., CA-5")
    parser.add_argument("--repo-root", default=".", help="Path to repository root")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    all_errors: list[str] = []
    all_errors.extend(check_git(repo_root, args.ticket))
    all_errors.extend(check_docker_ports(repo_root))
    py_errors, summaries = check_python_gates(repo_root)
    all_errors.extend(py_errors)

    for s in summaries:
        print(f"[GATE] {s}")

    if all_errors:
        print("\n[BLOCKED] Pre-close audit failed — keep Jira ticket in '31' (Verify / Test):")
        for idx, err in enumerate(all_errors, 1):
            print(f"  {idx}. {err}")
        return 1

    print("\n[PASS] All CodeSight-AI pre-close gates passed. Ready for `jira_add_comment` -> transition `41` (Done).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
