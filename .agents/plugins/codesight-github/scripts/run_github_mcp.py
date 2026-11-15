"""Stdio launcher for official GitHub MCP Server using `gh auth token` from OS keyring.

Never writes or logs GitHub tokens to disk.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    try:
        token = subprocess.check_output(
            ["gh", "auth", "token"],
            text=True,
            stderr=subprocess.PIPE,
            timeout=10,
        ).strip()
    except Exception as exc:
        sys.stderr.write(f"[codesight-github-mcp] Failed to read `gh auth token`: {exc}\n")
        return 1

    if not token:
        sys.stderr.write("[codesight-github-mcp] Empty token returned by `gh auth token`.\n")
        return 1

    env = os.environ.copy()
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token

    cmd = [
        "docker",
        "run",
        "-i",
        "--rm",
        "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server:latest",
        "stdio",
        "--toolsets=repos,pull_requests,issues,actions,code_security",
    ]
    completed = subprocess.run(
        cmd,
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
