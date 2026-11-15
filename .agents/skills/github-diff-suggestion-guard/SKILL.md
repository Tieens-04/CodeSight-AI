---
name: github-diff-suggestion-guard
description: Use when parsing unified git diffs, mapping GitHub PR line numbers, validating inline suggestion blocks with tree-sitter, or constructing GitHub Pull Request Review API payloads.
---

# GitHub Diff Coordinate Mapping & Syntax-Safe Suggestion Guard

## Overview

Prevents the two highest-risk failures in autonomous PR review: GitHub REST API `HTTP 422 Unprocessable Entity` from invalid diff coordinates and broken commits from syntactically invalid ```` ```suggestion ```` blocks.

## Unified Diff Hunk Mapping Contract

Parse hunk headers with `^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`:
- Omitted count (`@@ -10 +10 @@`) equals **`1`**, never `0`. Explicit `,0` (`@@ -1,5 +0,0 @@`) equals **`0`**.
- Track `hunk_id` per `@@` block. A multi-line comment cannot span across two hunks.

| Diff Line Prefix | `left_line` | `right_line` | Valid on `RIGHT`? |
| :--- | :--- | :--- | :--- |
| `' '` (context) | `+1` | `+1` | Yes (record `(hunk_id, right_line)`) |
| `'+'` (not `'+++'`) | unchanged | `+1` | Yes (record `(hunk_id, right_line)`) |
| `'-'` (not `'---'`) | `+1` | unchanged | No (`LEFT` only) |
| `'\\'` (`\ No newline...`) | **unchanged** | **unchanged** | No (marker only — never increment!) |

## GitHub Review Comment Payload Shape (`Zero HTTP 422`)

Every inline comment sent to `POST /repos/{owner}/{repo}/pulls/{number}/reviews` (`commit_id = head_sha`) MUST match one of three branches:

1. **Single-line (`start_line is None` or `start_line == line`)** — `line` is in `valid_right_lines`:
   - Send `{"path": path, "line": line, "side": "RIGHT", "body": body}`
   - **OMIT `start_line` and `start_side`**. Sending `start_line == line` triggers GitHub `HTTP 422 (start_line must precede line)`.
2. **Multi-line (`start_line < line`)** — `start_line` and `line` share the **exact same `hunk_id`** on `RIGHT`:
   - Send `{"path": path, "start_line": start_line, "start_side": "RIGHT", "line": line, "side": "RIGHT", "body": body}`
3. **Out-of-hunk or cross-hunk**:
   - Remove from `comments[]` and append to PR Summary `body` under `### Findings Outside Diff Hunks`.

## In-Memory `tree-sitter >= 0.22` Suggestion Validation

Never parse a ```` ```suggestion ```` block in isolation (indented statements or `return` outside a function fail standalone parsing). Always splice into the full file buffer in RAM first:

```python
from tree_sitter import Language, Parser
import tree_sitter_python as tspython
import tree_sitter_typescript as tsts

PARSERS = {
    ".py": Parser(Language(tspython.language())),
    ".ts": Parser(Language(tsts.language_typescript())),
    ".tsx": Parser(Language(tsts.language_tsx())),
}

def is_suggestion_syntax_safe(full_source: str, start_line: int, end_line: int, suggestion: str, ext: str) -> bool:
    parser = PARSERS.get(ext)
    if parser is None:
        return True
    lines = full_source.splitlines(keepends=True)
    replacement = [ln + "\n" for ln in suggestion.splitlines()]
    patched = "".join(lines[: start_line - 1] + replacement + lines[end_line:])
    tree = parser.parse(patched.encode("utf-8"))
    # has_error catches both ERROR nodes and MISSING tokens
    return not tree.root_node.has_error
```

If `is_suggestion_syntax_safe` returns `False`, strip the ```` ```suggestion ... ``` ```` fence and keep the diagnostic explanation text.

## Reference Validator Tool

Run built-in edge-case tests or validate a review payload JSON against a unified diff:

```powershell
python .agents/skills/github-diff-suggestion-guard/scripts/validate_diff_and_payload.py --self-test
```

## Common Mistakes

| Mistake | Fix |
| :--- | :--- |
| Using legacy `Language.build_library()` or `parser.set_language()` | `tree-sitter >= 0.22` passes `Language(capsule)` directly to `Parser(lang)`. |
| Checking only `node.type == "ERROR"` | Check `tree.root_node.has_error` so `MISSING` closing delimiters are caught too. |
| Counting `\ No newline at end of file` as a diff line | Shifts every subsequent line number by `+1`, causing `HTTP 422` or misaligned fixes. |
