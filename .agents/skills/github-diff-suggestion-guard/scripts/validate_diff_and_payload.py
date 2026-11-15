"""Reference validator for GitHub Unified Diff coordinate mapping and Review API payloads.

Prevents HTTP 422 errors (`start_line == line`, out-of-hunk lines, cross-hunk ranges,
`\\ No newline at end of file` off-by-one bugs).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def map_right_hunk_lines(diff_text: str) -> dict[str, dict[int, int]]:
    """Return `{file_path: {right_line_number: hunk_index}}` for valid RIGHT-side lines."""
    file_map: dict[str, dict[int, int]] = {}
    current_file: str | None = None
    hunk_idx = 0
    right_line = 0
    in_hunk = False

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            in_hunk = False
            current_file = None
            continue
        if raw_line.startswith("+++ "):
            target = raw_line[4:].strip()
            if target == "/dev/null":
                current_file = None
            else:
                current_file = target[2:] if target.startswith("b/") else target
                file_map.setdefault(current_file, {})
            in_hunk = False
            continue
        if raw_line.startswith("--- "):
            continue

        m = HUNK_HEADER_RE.match(raw_line)
        if m:
            if current_file is None:
                in_hunk = False
                continue
            hunk_idx += 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            right_line = new_start
            in_hunk = new_count > 0
            continue

        if not in_hunk or current_file is None:
            continue

        if raw_line.startswith("\\"):
            # `\ No newline at end of file` — never increments line numbers!
            continue
        if raw_line.startswith("+"):
            file_map[current_file][right_line] = hunk_idx
            right_line += 1
        elif raw_line.startswith("-"):
            # Deleted line exists only on LEFT
            continue
        elif raw_line.startswith(" "):
            file_map[current_file][right_line] = hunk_idx
            right_line += 1

    return file_map


def validate_review_comments(
    diff_map: dict[str, dict[int, int]],
    comments: list[dict[str, Any]],
) -> list[str]:
    """Validate GitHub Pull Request Review API `comments` items against mapped hunks."""
    errors: list[str] = []
    for i, c in enumerate(comments):
        path = c.get("path")
        line = c.get("line")
        side = c.get("side", "RIGHT")
        start_line = c.get("start_line")
        start_side = c.get("start_side")

        if side != "RIGHT":
            errors.append(f"Comment #{i} ({path}): `side` must be 'RIGHT', got {side!r}.")
        if not isinstance(path, str) or path not in diff_map:
            errors.append(f"Comment #{i}: path {path!r} has no RIGHT-side diff hunks (downgrade to Summary).")
            continue
        if not isinstance(line, int):
            errors.append(f"Comment #{i} ({path}): `line` must be int, got {line!r}.")
            continue

        right_lines = diff_map[path]
        if line not in right_lines:
            errors.append(f"Comment #{i} ({path}:{line}): line {line} is outside diff hunks (HTTP 422).")
            continue

        if start_line is not None:
            if not isinstance(start_line, int):
                errors.append(f"Comment #{i} ({path}): `start_line` must be int.")
                continue
            if start_line >= line:
                errors.append(
                    f"Comment #{i} ({path}:{start_line}-{line}): `start_line` ({start_line}) >= `line` ({line}) "
                    "triggers GitHub HTTP 422! Omit `start_line`/`start_side` for single-line comments."
                )
                continue
            if start_side != "RIGHT":
                errors.append(f"Comment #{i} ({path}): `start_side` must be 'RIGHT' when `start_line` is set.")
            if start_line not in right_lines:
                errors.append(f"Comment #{i} ({path}:{start_line}-{line}): `start_line` {start_line} outside hunks.")
                continue
            if right_lines[start_line] != right_lines[line]:
                errors.append(
                    f"Comment #{i} ({path}:{start_line}-{line}): crosses hunk boundary "
                    f"(hunk {right_lines[start_line]} vs {right_lines[line]}) -> HTTP 422!"
                )
    return errors


def run_self_test() -> int:
    sample_diff = "\n".join(
        [
            "diff --git a/src/auth.py b/src/auth.py",
            "index 1111111..2222222 100644",
            "--- a/src/auth.py",
            "+++ b/src/auth.py",
            "@@ -10 +10 @@",
            "-old_flag = False",
            "\\ No newline at end of file",
            "+old_flag = True",
            "@@ -20,3 +20,4 @@ def login(user):",
            "     if user is None:",
            "-        return False",
            "+        raise ValueError('missing')",
            "+    audit(user)",
            "     return True",
            "diff --git a/src/removed.py b/src/removed.py",
            "deleted file mode 100644",
            "--- a/src/removed.py",
            "+++ /dev/null",
            "@@ -1,3 +0,0 @@",
            "-x = 1",
        ]
    )
    mapped = map_right_hunk_lines(sample_diff)
    assert "src/removed.py" not in mapped, "Deleted file must not have RIGHT lines"
    assert mapped["src/auth.py"] == {10: 1, 20: 2, 21: 2, 22: 2, 23: 2}, f"Unexpected mapping: {mapped}"

    good_comments = [
        {"path": "src/auth.py", "line": 10, "side": "RIGHT", "body": "Single line ok"},
        {
            "path": "src/auth.py",
            "start_line": 21,
            "start_side": "RIGHT",
            "line": 22,
            "side": "RIGHT",
            "body": "```suggestion\n        return False\n```",
        },
    ]
    assert validate_review_comments(mapped, good_comments) == []

    bad_comments = [
        {"path": "src/auth.py", "start_line": 10, "start_side": "RIGHT", "line": 10, "side": "RIGHT", "body": "x"},
        {"path": "src/auth.py", "start_line": 10, "start_side": "RIGHT", "line": 21, "side": "RIGHT", "body": "x"},
        {"path": "src/auth.py", "line": 99, "side": "RIGHT", "body": "x"},
    ]
    errs = validate_review_comments(mapped, bad_comments)
    assert len(errs) == 3, f"Expected 3 validation errors, got {errs}"
    print("[PASS] github-diff-suggestion-guard self-test passed (5/5 edge cases verified).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate GitHub review payload against unified diff.")
    parser.add_argument("--self-test", action="store_true", help="Run built-in edge-case assertions")
    parser.add_argument("--diff", type=Path, help="Path to unified diff file")
    parser.add_argument("--payload", type=Path, help="Path to JSON file containing `comments` list or review dict")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    if not args.diff or not args.payload:
        parser.error("Provide both --diff and --payload, or run with --self-test.")

    diff_map = map_right_hunk_lines(args.diff.read_text(encoding="utf-8"))
    raw_payload = json.loads(args.payload.read_text(encoding="utf-8"))
    comments = raw_payload["comments"] if isinstance(raw_payload, dict) else raw_payload
    errors = validate_review_comments(diff_map, comments)
    if errors:
        print("[FAIL] GitHub HTTP 422 payload violations detected:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"[PASS] All {len(comments)} inline review comments map cleanly to RIGHT diff hunks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
