#!/usr/bin/env python3
"""Deterministic verification, scope/hygiene, and board CLI for CodeSight-AI (.ai-workflow v4.0).

Subcommands:
  - diff   : Fast (<0.2s) branch, gitignore, Docker port coexistence, tracked + untracked `??`
             scope classification, whitespace (`git diff --check`), and static hygiene/secret scan.
  - check  : Runs `diff` + Ruff format/lint + Mypy strict + Pytest (>=80% branch cov) + optional
             live integration tests, domain mechanical validators, `preclose_audit.py`, and
             Playwright `--headed` — summarized in one compact Markdown table.
  - board  : Syncs `.ai-workflow/BOARD.md` from `.ai-workflow/tasks/CA-*` + `main` git history
             with 0 LLM tokens.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CANONICAL_STATUSES: tuple[str, ...] = (
    "Todo",
    "Working",
    "Blocked",
    "Ready for Review",
    "Done",
)

STATUS_PRIORITY: dict[str, int] = {
    "Blocked": 0,
    "Ready for Review": 1,
    "Working": 2,
    "Todo": 3,
    "Done": 4,
}

RESERVED_HOST_PORTS: dict[str, str] = {
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

TEXT_SCAN_EXT: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".html",
        ".jinja",
        ".jinja2",
        ".json",
        ".toml",
        ".yml",
        ".yaml",
        ".sql",
        ".ini",
        ".cfg",
        ".env",
    }
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Private key header",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("GitHub PAT/App token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Google AI Studio key", re.compile(r"\bAIzaSy[A-Za-z0-9_-]{33}\b")),
    ("OpenAI/API sk- token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
)

BRANCH_RE = re.compile(r"^feat/ca-\d+-[a-z0-9][a-z0-9-]*$")
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
DEBUGGER_RE = re.compile(r"\b(?:breakpoint|pdb\.set_trace|ipdb\.set_trace)\s*\(")
BARE_PRINT_RE = re.compile(r"(?<![.\w])print\s*\(")
UNCONDITIONAL_SKIP_RE = re.compile(r"@pytest\.mark\.skip\b(?!if)")


@dataclass(frozen=True)
class ParsedGitStatus:
    modified: list[str]
    deleted: list[str]
    untracked: list[str]
    live_files: list[str]


@dataclass(frozen=True)
class ScopeSummary:
    touches_api_worker_db: bool
    touches_alembic: bool
    touches_ast_rag: bool
    touches_diff_agents: bool
    touches_scoring_radar_evals: bool
    touches_docs: bool
    python_files: list[str]


@dataclass(frozen=True)
class CleanlinessResult:
    ok: bool
    issues: list[str]


@dataclass(frozen=True)
class ParsedState:
    status: str | None
    step: str | None
    branch: str | None
    objective: str | None
    strikes: int
    next_gate: str | None


@dataclass(frozen=True)
class TicketBoardRow:
    ticket_id: str
    status: str
    step: str
    branch: str
    objective: str
    strikes: int
    next_gate: str


@dataclass(frozen=True)
class BoardSnapshot:
    active: list[TicketBoardRow]
    done: list[TicketBoardRow]
    current_branch: str


@dataclass(frozen=True)
class WorkingTreeContext:
    branch: str
    branch_ok: bool
    branch_reason: str | None
    parsed_status: ParsedGitStatus
    scopes: ScopeSummary
    whitespace_issues: list[str]
    docker_issues: list[str]
    cleanliness: CleanlinessResult
    workflow_ignored: bool


@dataclass(frozen=True)
class GateRecord:
    name: str
    command: str
    status: str
    detail: str


def normalize_slashes(file_path: str) -> str:
    return file_path.replace("\\", "/")


def unquote_git_path(raw: str) -> str:
    trimmed = raw.strip()
    if len(trimmed) >= 2 and trimmed.startswith('"') and trimmed.endswith('"'):
        return trimmed[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return trimmed


def validate_branch_name(branch: str, ticket: str | None = None) -> tuple[bool, str | None]:
    name = (branch or "").strip()
    if not name or name == "HEAD":
        return False, "Detached or empty HEAD; expected feat/ca-<number>-<short-slug>"
    if name in {"main", "master"}:
        return False, f'On default branch "{name}"; create feat/ca-<number>-<slug> from main first'
    for bad_prefix in ("codex/", "feature/", "bugfix/", "chore/", "docs/"):
        if name.startswith(bad_prefix):
            return (
                False,
                f'Forbidden prefix on "{name}"; CodeSight-AI requires feat/ca-<number>-<slug>',
            )
    if not BRANCH_RE.match(name):
        return False, f'Branch "{name}" must match ^feat/ca-<number>-<lowercase-slug>$'
    if ticket:
        expected_prefix = f"feat/{ticket.strip().lower()}-"
        if not name.startswith(expected_prefix):
            return (
                False,
                f'Branch "{name}" does not match ticket "{ticket.upper()}" '
                f'(expected "{expected_prefix}*")',
            )
    return True, None


def _is_ignored_workflow_path(file_path: str) -> bool:
    return file_path == ".ai-workflow/BOARD.md" or file_path.startswith(".ai-workflow/")


def parse_git_status_porcelain(porcelain_output: str) -> ParsedGitStatus:
    modified: list[str] = []
    deleted: list[str] = []
    untracked: list[str] = []

    for raw_line in (porcelain_output or "").splitlines():
        if len(raw_line) < 4:
            continue
        status = raw_line[:2]
        target = raw_line[3:]

        if status == "??":
            file_path = normalize_slashes(unquote_git_path(target))
            if not _is_ignored_workflow_path(file_path):
                untracked.append(file_path)
            continue

        if " -> " in target:
            target = target.split(" -> ")[-1]
        file_path = normalize_slashes(unquote_git_path(target))
        if _is_ignored_workflow_path(file_path):
            continue

        if "D" in status:
            deleted.append(file_path)
        else:
            modified.append(file_path)

    return ParsedGitStatus(
        modified=modified,
        deleted=deleted,
        untracked=untracked,
        live_files=[*modified, *untracked],
    )


def classify_scopes(live_files: list[str], deleted_files: list[str] | None = None) -> ScopeSummary:
    all_files = [normalize_slashes(f) for f in [*live_files, *(deleted_files or [])]]

    touches_alembic = any(f.startswith("alembic/") for f in all_files)
    touches_api_worker_db = touches_alembic or any(
        f.startswith(
            (
                "src/codesight/api/",
                "src/codesight/worker/",
                "src/codesight/db/",
                "src/codesight/vcs/",
                "src/codesight/config_reader/",
            )
        )
        for f in all_files
    )
    touches_ast_rag = any(
        f.startswith(("src/codesight/ast_engine/", "src/codesight/rag/"))
        or f.endswith("audit_qdrant_zerocode.py")
        for f in all_files
    )
    touches_diff_agents = any(
        f.startswith(("src/codesight/diff/", "src/codesight/agents/"))
        or f == "src/codesight/vcs/github_publisher.py"
        or f.endswith("validate_diff_and_payload.py")
        for f in all_files
    )
    touches_scoring_radar_evals = any(
        f.startswith(("src/codesight/scoring/", "evals/")) or "/radar" in f for f in all_files
    )
    touches_docs = any(f.startswith("docs/") or f == "README.md" for f in all_files)
    python_files = [normalize_slashes(f) for f in live_files if f.lower().endswith(".py")]

    return ScopeSummary(
        touches_api_worker_db=touches_api_worker_db,
        touches_alembic=touches_alembic,
        touches_ast_rag=touches_ast_rag,
        touches_diff_agents=touches_diff_agents,
        touches_scoring_radar_evals=touches_scoring_radar_evals,
        touches_docs=touches_docs,
        python_files=python_files,
    )


def _is_test_file(file_path: str) -> bool:
    norm = normalize_slashes(file_path)
    base = norm.rsplit("/", 1)[-1]
    return (
        norm.startswith("tests/")
        or "/tests/" in norm
        or base.startswith("test_")
        or base.endswith("_test.py")
    )


def _is_runtime_python_source(file_path: str) -> bool:
    norm = normalize_slashes(file_path)
    return norm.startswith("src/codesight/") and norm.endswith(".py") and not _is_test_file(norm)


def _is_example_env_file(base_name: str) -> bool:
    lower = base_name.lower()
    return lower.endswith((".example", ".sample", ".template"))


def scan_cleanliness(
    *,
    live_files: list[str],
    added_lines_by_file: dict[str, list[tuple[int, str]]],
) -> CleanlinessResult:
    issues: list[str] = []

    for raw_file in live_files:
        file_path = normalize_slashes(raw_file)
        base = file_path.rsplit("/", 1)[-1]
        lower_base = base.lower()
        if (lower_base == ".env" or lower_base.startswith(".env.")) and not _is_example_env_file(
            lower_base
        ):
            issues.append(f"{file_path}: real .env* file must never be tracked or left unignored")
        if lower_base.endswith((".pem", ".key")):
            issues.append(f"{file_path}: credential/private-key file (*.pem/*.key) in working tree")
        if lower_base.endswith(".log"):
            issues.append(f"{file_path}: unexpected *.log artifact in working tree")
        if lower_base == ".coverage" or lower_base.startswith(".coverage."):
            issues.append(f"{file_path}: coverage artifact exposed in working tree")

    for raw_file, lines in added_lines_by_file.items():
        file_path = normalize_slashes(raw_file)
        base = file_path.rsplit("/", 1)[-1]
        is_test = _is_test_file(file_path)
        is_runtime_py = _is_runtime_python_source(file_path)
        is_example = _is_example_env_file(base)

        for line_no, text in lines:
            stripped = text.lstrip()
            is_comment = stripped.startswith("#")

            if not is_comment and not is_test:
                dbg_match = DEBUGGER_RE.search(text)
                if dbg_match:
                    issues.append(
                        f"{file_path}:{line_no}: leftover debugger statement ({dbg_match.group(0)})"
                    )

            if not is_comment and is_runtime_py and BARE_PRINT_RE.search(text):
                issues.append(
                    f"{file_path}:{line_no}: added bare print( in runtime source "
                    "(use structlog or Rich console.print)"
                )

            if not is_comment and is_test and UNCONDITIONAL_SKIP_RE.search(text):
                issues.append(
                    f"{file_path}:{line_no}: unconditional @pytest.mark.skip in test file "
                    "(use @pytest.mark.skipif with explicit credential/env guard)"
                )

            if not is_test and not is_example:
                for secret_name, secret_re in SECRET_PATTERNS:
                    if secret_re.search(text):
                        issues.append(
                            f"{file_path}:{line_no}: potential secret detected ({secret_name})"
                        )

    return CleanlinessResult(ok=len(issues) == 0, issues=issues)


def parse_unified_zero_added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    added: dict[str, list[tuple[int, str]]] = {}
    current_file: str | None = None
    current_new_line = 0

    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            current_file = normalize_slashes(unquote_git_path(line[6:]))
            continue
        if line.startswith("+++ /dev/null"):
            current_file = None
            continue
        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            current_new_line = int(hunk_match.group(1))
            continue
        if not current_file or _is_ignored_workflow_path(current_file):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current_file, []).append((current_new_line, line[1:]))
            current_new_line += 1

    return added


def parse_state_markdown(markdown: str | None) -> ParsedState:
    if not markdown:
        return ParsedState(
            status=None,
            step=None,
            branch=None,
            objective=None,
            strikes=0,
            next_gate=None,
        )

    def _extract(label: str) -> str | None:
        pattern = re.compile(
            rf"^\s*[-*]\s*\*\*{re.escape(label)}\*\*:\s*(.+)$",
            re.IGNORECASE | re.MULTILINE,
        )
        match = pattern.search(markdown)
        if not match:
            return None
        return match.group(1).strip().strip("`")

    raw_status = _extract("Status")
    canonical_status: str | None = None
    if raw_status:
        for candidate in CANONICAL_STATUSES:
            if candidate.lower() == raw_status.lower():
                canonical_status = candidate
                break

    raw_strikes = _extract("Strikes")
    strikes = int(raw_strikes) if raw_strikes and raw_strikes.isdigit() else 0

    return ParsedState(
        status=canonical_status,
        step=_extract("Step"),
        branch=_extract("Branch"),
        objective=_extract("Objective"),
        strikes=strikes,
        next_gate=_extract("Next Gate"),
    )


def infer_ticket_state(
    *,
    ticket_id: str,
    files: list[str],
    state_content: str | None,
    final_review_content: str = "",
    review_content: str = "",
    plan_content: str = "",
    precommit_content: str = "",
    merged_tickets: set[str] | None = None,
    active_branch_tickets: set[str] | None = None,
    current_branch: str = "",
) -> TicketBoardRow:
    file_set = set(files)
    parsed = parse_state_markdown(state_content)
    upper_id = ticket_id.upper()
    merged = merged_tickets or set()

    title = parsed.objective or ""
    if not title and plan_content:
        heading = re.search(
            r"^#\s+(?:Implementation Plan:\s*)?[A-Z]+-\d+\s*[\u2014\u2013:-]\s*(.+)$",
            plan_content,
            re.MULTILINE,
        )
        if heading:
            title = heading.group(1).strip()

    branch_from_plan = parsed.branch or ""
    if not branch_from_plan and plan_content:
        branch_match = re.search(
            r"^\s*[-*]\s*\*\*Branch\*\*:\s*`([^`]+)`",
            plan_content,
            re.MULTILINE,
        )
        if branch_match:
            branch_from_plan = branch_match.group(1).strip()

    if upper_id in merged:
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Done",
            step="07-close-jira (merged)",
            branch=branch_from_plan or "main",
            objective=title or "Merged to main",
            strikes=parsed.strikes,
            next_gate="None (merged)",
        )

    if parsed.status:
        inferred_branch = branch_from_plan or (
            current_branch if upper_id.lower() in current_branch.lower() else "-"
        )
        return TicketBoardRow(
            ticket_id=upper_id,
            status=parsed.status,
            step=parsed.step or "unknown",
            branch=inferred_branch,
            objective=title or "-",
            strikes=parsed.strikes,
            next_gate=parsed.next_gate or "-",
        )

    if (
        active_branch_tickets is not None
        and upper_id not in active_branch_tickets
        and upper_id.lower() not in current_branch.lower()
        and "review-01.md" in file_set
    ):
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Done",
            step="07-close-jira (archived)",
            branch=branch_from_plan or "main",
            objective=title or "Completed (legacy)",
            strikes=0,
            next_gate="None (archived)",
        )

    if "precommit.md" in file_set:
        if "Jira Closure Record" in precommit_content:
            return TicketBoardRow(
                ticket_id=upper_id,
                status="Done",
                step="07-close-jira",
                branch=branch_from_plan or "-",
                objective=title or "Closed on Jira (41 Done)",
                strikes=0,
                next_gate="None (Jira Done 41)",
            )
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Ready for Review",
            step="06-precommit -> 07-close-jira",
            branch=branch_from_plan or "-",
            objective=title or "Ready to commit",
            strikes=0,
            next_gate=f"Human git commit & /ai-workflow close {upper_id}",
        )

    if "final-review.md" in file_set:
        changes_requested = "CHANGES REQUESTED" in final_review_content
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Working" if changes_requested else "Ready for Review",
            step="04-fix (re-opened)" if changes_requested else "05-final-review -> 06-precommit",
            branch=branch_from_plan or "-",
            objective=title or "-",
            strikes=1 if changes_requested else 0,
            next_gate="Run 04-fix.md" if changes_requested else "Run 06-precommit.md",
        )

    if "fix-report.md" in file_set:
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Ready for Review",
            step="04-fix -> 05-final-review",
            branch=branch_from_plan or "-",
            objective=title or "-",
            strikes=0,
            next_gate="Run 05-final-review.md",
        )

    if "review-01.md" in file_set:
        approved = bool(re.search(r"\bAPPROVED\b", review_content)) and (
            "CHANGES REQUESTED" not in review_content
        )
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Ready for Review" if approved else "Working",
            step="03-review -> 05-final-review" if approved else "03-review -> 04-fix",
            branch=branch_from_plan or "-",
            objective=title or "-",
            strikes=0,
            next_gate="Run 05-final-review.md" if approved else "Run 04-fix.md",
        )

    if "implementation-report.md" in file_set:
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Ready for Review",
            step="02-implement -> 03-review",
            branch=branch_from_plan or "-",
            objective=title or "-",
            strikes=0,
            next_gate="Run 03-review.md (New Chat + Cross-Model)",
        )

    if "plan.md" in file_set:
        return TicketBoardRow(
            ticket_id=upper_id,
            status="Working",
            step="01-plan -> 02-implement",
            branch=branch_from_plan or "-",
            objective=title or "-",
            strikes=0,
            next_gate="Gate 1: Approve plan & run 02-implement.md",
        )

    return TicketBoardRow(
        ticket_id=upper_id,
        status="Todo",
        step="01-plan",
        branch="-",
        objective=title or "Initialized",
        strikes=0,
        next_gate="Run 01-plan.md",
    )


def _ticket_sort_key(row: TicketBoardRow) -> tuple[int, int]:
    priority = STATUS_PRIORITY.get(row.status, 99)
    num_match = re.search(r"(\d+)$", row.ticket_id)
    number = int(num_match.group(1)) if num_match else 0
    return (priority, -number)


def _escape_cell(text: str) -> str:
    return (text or "-").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def render_board_markdown(board: BoardSnapshot) -> str:
    lines: list[str] = [
        "# CodeSight-AI — AI Workflow Board",
        "",
        "> Auto-synced by `python .ai-workflow/scripts/verify.py board`. "
        "Statuses: `Todo` | `Working` | `Blocked` | `Ready for Review` | `Done`.",
        f"> Current Git branch: `{board.current_branch or 'unknown'}` · "
        f"Active tickets: **{len(board.active)}** · Completed archive: **{len(board.done)}**",
        "",
        "## 1. Active Queue (`Blocked` / `Ready for Review` / `Working` / `Todo`)",
        "",
    ]

    if not board.active:
        lines.extend(["_No active tickets in flight. Start one with `/ai-workflow CA-XX`._", ""])
    else:
        lines.extend(
            [
                "| Ticket | Status | Step | Branch | Strikes | Objective | Next Gate |",
                "| :--- | :--- | :--- | :--- | ---: | :--- | :--- |",
            ]
        )
        for row in board.active:
            lines.append(
                f"| **{_escape_cell(row.ticket_id)}** | `{_escape_cell(row.status)}` | "
                f"{_escape_cell(row.step)} | `{_escape_cell(row.branch)}` | {row.strikes} | "
                f"{_escape_cell(row.objective)} | {_escape_cell(row.next_gate)} |"
            )
        lines.append("")

    lines.extend([f"## 2. Completed Tickets (`Done` — {len(board.done)} total)", ""])
    if not board.done:
        lines.extend(["_None yet._", ""])
    else:
        recent_done = board.done[:15]
        lines.extend(["| Ticket | Step | Objective |", "| :--- | :--- | :--- |"])
        for row in recent_done:
            lines.append(
                f"| {_escape_cell(row.ticket_id)} | {_escape_cell(row.step)} | "
                f"{_escape_cell(row.objective)} |"
            )
        if len(board.done) > len(recent_done):
            older = len(board.done) - len(recent_done)
            lines.extend(["", f"_Plus {older} older completed tickets in `.ai-workflow/tasks/`._"])
        lines.append("")

    return "\n".join(lines)


def _read_optional_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_board_from_disk(
    *,
    repo_root: Path,
    merged_tickets: set[str],
    active_branch_tickets: set[str] | None = None,
    current_branch: str = "",
    write_to_disk: bool = True,
) -> BoardSnapshot:
    tasks_root = repo_root / ".ai-workflow" / "tasks"
    rows: list[TicketBoardRow] = []

    if tasks_root.is_dir():
        for entry in tasks_root.iterdir():
            if not entry.is_dir() or not re.match(r"^CA-\d+$", entry.name, re.IGNORECASE):
                continue
            files = [child.name for child in entry.iterdir() if child.is_file()]
            state_raw = _read_optional_utf8(entry / "state.md")
            rows.append(
                infer_ticket_state(
                    ticket_id=entry.name.upper(),
                    files=files,
                    state_content=state_raw or None,
                    final_review_content=_read_optional_utf8(entry / "final-review.md"),
                    review_content=_read_optional_utf8(entry / "review-01.md"),
                    plan_content=_read_optional_utf8(entry / "plan.md"),
                    precommit_content=_read_optional_utf8(entry / "precommit.md"),
                    merged_tickets=merged_tickets,
                    active_branch_tickets=active_branch_tickets,
                    current_branch=current_branch,
                )
            )

    rows.sort(key=_ticket_sort_key)
    snapshot = BoardSnapshot(
        active=[r for r in rows if r.status != "Done"],
        done=[r for r in rows if r.status == "Done"],
        current_branch=current_branch,
    )

    if write_to_disk:
        board_path = repo_root / ".ai-workflow" / "BOARD.md"
        board_path.parent.mkdir(parents=True, exist_ok=True)
        board_path.write_text(render_board_markdown(snapshot), encoding="utf-8")

    return snapshot


def _run_proc(cmd: list[str], cwd: Path) -> tuple[int, str, float]:
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.perf_counter() - started
    parts = [p.rstrip("\r\n") for p in (proc.stdout, proc.stderr) if p and p.strip()]
    return proc.returncode, "\n".join(parts), elapsed


def _resolve_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")
    return str(fallback) if fallback.exists() else None


def check_docker_coexistence(repo_root: Path) -> list[str]:
    if not shutil.which("docker"):
        return []
    rc, out, _ = _run_proc(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"], repo_root)
    if rc != 0:
        return []
    issues: list[str] = []
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
                issues.append(
                    f"Docker port collision: '{name}' binds reserved port {host_port} ({owner})"
                )
    return issues


def collect_git_branch_state(repo_root: Path) -> tuple[set[str], set[str]]:
    merged: set[str] = set()
    active_branches: set[str] = set()

    rc_log, log_out, _ = _run_proc(["git", "log", "main", "--oneline"], repo_root)
    if rc_log == 0:
        for match in re.finditer(r"\b(CA-\d+)\b", log_out, re.IGNORECASE):
            merged.add(match.group(1).upper())

    rc_sha, main_sha_raw, _ = _run_proc(["git", "rev-parse", "main"], repo_root)
    main_sha = main_sha_raw.strip() if rc_sha == 0 else ""

    rc_all, all_refs, _ = _run_proc(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        repo_root,
    )
    rc_merged, merged_refs, _ = _run_proc(
        [
            "git",
            "for-each-ref",
            "--merged=main",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/",
        ],
        repo_root,
    )

    merged_ref_names: set[str] = set()
    if rc_merged == 0:
        for line in merged_refs.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] != main_sha:
                merged_ref_names.add(parts[0])

    if rc_all == 0:
        for line in all_refs.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            ref_name = parts[0]
            ticket_match = re.search(r"\b(ca-\d+)\b", ref_name, re.IGNORECASE)
            if not ticket_match:
                continue
            ticket_id = ticket_match.group(1).upper()
            if ref_name in merged_ref_names:
                merged.add(ticket_id)
            else:
                active_branches.add(ticket_id)

    return merged, active_branches


def collect_working_tree_context(repo_root: Path, ticket: str | None = None) -> WorkingTreeContext:
    _, branch_out, _ = _run_proc(["git", "branch", "--show-current"], repo_root)
    branch = branch_out.strip()
    branch_ok, branch_reason = validate_branch_name(branch, ticket=ticket)

    _, status_out, _ = _run_proc(["git", "status", "--porcelain=v1", "-uall"], repo_root)
    parsed_status = parse_git_status_porcelain(status_out)
    scopes = classify_scopes(parsed_status.live_files, parsed_status.deleted)

    rc_check, diff_check_out, _ = _run_proc(["git", "diff", "HEAD", "--check"], repo_root)
    whitespace_issues = (
        [] if rc_check == 0 else [ln for ln in diff_check_out.splitlines() if ln.strip()][:10]
    )

    _, unified_out, _ = _run_proc(
        ["git", "diff", "HEAD", "--unified=0", "--no-color"],
        repo_root,
    )
    added_lines = parse_unified_zero_added_lines(unified_out)

    for untracked_file in parsed_status.untracked:
        suffix = Path(untracked_file).suffix.lower()
        base = Path(untracked_file).name.lower()
        if suffix not in TEXT_SCAN_EXT and not base.startswith(".env"):
            continue
        try:
            content = (repo_root / untracked_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        added_lines[untracked_file] = [(idx + 1, ln) for idx, ln in enumerate(content.splitlines())]

    cleanliness = scan_cleanliness(
        live_files=parsed_status.live_files,
        added_lines_by_file=added_lines,
    )
    docker_issues = check_docker_coexistence(repo_root)

    rc_ig_tasks, _, _ = _run_proc(
        ["git", "check-ignore", "-q", "--no-index", ".ai-workflow/tasks/CA-0/state.md"],
        repo_root,
    )
    rc_ig_board, _, _ = _run_proc(
        ["git", "check-ignore", "-q", "--no-index", ".ai-workflow/BOARD.md"],
        repo_root,
    )
    workflow_ignored = rc_ig_tasks == 0 and rc_ig_board == 0

    return WorkingTreeContext(
        branch=branch,
        branch_ok=branch_ok,
        branch_reason=branch_reason,
        parsed_status=parsed_status,
        scopes=scopes,
        whitespace_issues=whitespace_issues,
        docker_issues=docker_issues,
        cleanliness=cleanliness,
        workflow_ignored=workflow_ignored,
    )


def print_diff_report(ctx: WorkingTreeContext) -> None:
    active_scopes = (
        ", ".join(
            label
            for flag, label in (
                (ctx.scopes.touches_api_worker_db, "api_worker_db"),
                (ctx.scopes.touches_alembic, "alembic"),
                (ctx.scopes.touches_ast_rag, "ast_rag"),
                (ctx.scopes.touches_diff_agents, "diff_agents"),
                (ctx.scopes.touches_scoring_radar_evals, "scoring_radar_evals"),
                (ctx.scopes.touches_docs, "docs"),
            )
            if flag
        )
        or "none (clean working tree)"
    )

    branch_label = "VALID" if ctx.branch_ok else f"INVALID ({ctx.branch_reason})"
    ig_label = "YES" if ctx.workflow_ignored else "NO (WARNING!)"
    docker_label = "PASS" if not ctx.docker_issues else f"FAIL ({len(ctx.docker_issues)} collision)"

    print("## AI-Workflow Working-Tree Scope & Hygiene")
    print(f"- **Branch**: `{ctx.branch or 'DETACHED'}` — {branch_label}")
    print(f"- **tasks/* & BOARD.md gitignored**: {ig_label}")
    print(f"- **Docker Port Coexistence**: `{docker_label}`")
    print(f"- **Touched Scopes**: `{active_scopes}`")
    print(
        f"- **Files**: {len(ctx.parsed_status.modified)} modified/added · "
        f"{len(ctx.parsed_status.deleted)} deleted · "
        f"**{len(ctx.parsed_status.untracked)} untracked (`??`)**"
    )

    if ctx.parsed_status.modified:
        formatted = ", ".join(f"`{f}`" for f in ctx.parsed_status.modified)
        print(f"  - Tracked changed: {formatted}")
    if ctx.parsed_status.deleted:
        formatted = ", ".join(f"`{f}`" for f in ctx.parsed_status.deleted)
        print(f"  - Deleted: {formatted}")
    if ctx.parsed_status.untracked:
        formatted = ", ".join(f"`{f}`" for f in ctx.parsed_status.untracked)
        print(f"  - Untracked (`??`): {formatted}")

    all_issues = [
        *(f"git diff --check: {w}" for w in ctx.whitespace_issues),
        *ctx.docker_issues,
        *ctx.cleanliness.issues,
    ]
    if not all_issues:
        print("- **Static Hygiene & Secret Scan**: `PASS` (0 issues)")
    else:
        print(f"- **Static Hygiene & Secret Scan**: `FAIL` ({len(all_issues)} issue(s))")
        for issue in all_issues:
            print(f"  - {issue}")


def _tail_lines(text: str, count: int = 35) -> str:
    non_empty = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(non_empty) <= count:
        return "\n".join(non_empty)
    omitted = len(non_empty) - count
    return f"... ({omitted} earlier lines omitted)\n" + "\n".join(non_empty[-count:])


def summarize_pytest_output(output: str) -> str:
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "ok"
    summary_line = ""
    cov_pct = ""
    for ln in reversed(lines):
        if not summary_line and re.search(r"\b\d+\s+(?:passed|failed|error|errors)\b", ln):
            summary_line = ln.strip("= ").strip()
        if not cov_pct:
            total_match = re.match(r"^TOTAL\s+\d+\s+\d+(?:\s+\d+\s+\d+)?\s+(\d+(?:\.\d+)?%)$", ln)
            if total_match:
                cov_pct = total_match.group(1)
        if summary_line and cov_pct:
            break
    if summary_line and cov_pct:
        return f"{summary_line} · cov {cov_pct}"
    return summary_line or lines[-1]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CodeSight-AI .ai-workflow v4.0 verification & board CLI."
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        default="check",
        choices=("check", "diff", "scope", "board"),
        help="Subcommand to execute (default: check)",
    )
    parser.add_argument("--ticket", default=None, help="Jira key, e.g. CA-18")
    parser.add_argument(
        "--full-format",
        action="store_true",
        help="Run `uv run ruff format --check .` across entire repository",
    )
    parser.add_argument(
        "--allow-main",
        action="store_true",
        help="Allow running diff/check while checked out on `main`",
    )
    parser.add_argument(
        "--skip-integration",
        action="store_true",
        help="Explicitly skip `-m integration` requirement during fast local iteration",
    )
    parser.add_argument(
        "--integration",
        nargs="*",
        default=None,
        help="Run live Docker integration suite (`all` or specific test file paths)",
    )
    parser.add_argument(
        "--preclose",
        action="store_true",
        help="Also run `preclose_audit.py --ticket <CA-XX>` mechanical gate",
    )
    parser.add_argument(
        "--e2e",
        nargs="*",
        default=None,
        help="Run Playwright tests in mandatory `--headed` mode for specified specs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]

    if args.subcommand == "board":
        _, branch_out, _ = _run_proc(["git", "branch", "--show-current"], repo_root)
        current_branch = branch_out.strip()
        merged_tickets, active_branches = collect_git_branch_state(repo_root)
        board = build_board_from_disk(
            repo_root=repo_root,
            merged_tickets=merged_tickets,
            active_branch_tickets=active_branches,
            current_branch=current_branch,
            write_to_disk=True,
        )
        print(
            f"[ai-workflow board] Synced .ai-workflow/BOARD.md — "
            f"Active: {len(board.active)}, Done: {len(board.done)}."
        )
        for row in board.active:
            print(
                f"  - {row.ticket_id} [{row.status}] ({row.step}) "
                f"branch={row.branch} -> {row.next_gate}"
            )
        return 0

    ctx = collect_working_tree_context(repo_root, ticket=args.ticket)
    print_diff_report(ctx)

    branch_ok = args.allow_main or ctx.branch_ok
    hygiene_ok = (
        ctx.workflow_ignored
        and not ctx.whitespace_issues
        and not ctx.docker_issues
        and ctx.cleanliness.ok
    )

    if args.subcommand in {"diff", "scope"}:
        return 0 if (branch_ok and hygiene_ok) else 1

    uv_bin = _resolve_uv()
    if not uv_bin:
        print("\n[verify.py] ERROR: `uv` executable not found on PATH.", file=sys.stderr)
        return 1

    gates: list[GateRecord] = []
    failures: list[tuple[str, str]] = []

    hygiene_issue_count = (
        len(ctx.cleanliness.issues) + len(ctx.whitespace_issues) + len(ctx.docker_issues)
    )
    gates.append(
        GateRecord(
            name="Branch, Docker & Hygiene",
            command="verify.py diff",
            status="PASS" if (branch_ok and hygiene_ok) else "FAIL",
            detail=(
                f"{len(ctx.parsed_status.live_files)} live changed file(s), 0 hygiene/port issues"
                if (branch_ok and hygiene_ok)
                else (ctx.branch_reason or f"{hygiene_issue_count} issue(s)")
            ),
        )
    )

    # 1. Ruff Format Check
    if args.full_format:
        rc, out, sec = _run_proc([uv_bin, "run", "ruff", "format", "--check", "."], repo_root)
        gates.append(
            GateRecord(
                name="Ruff Format (Full)",
                command="uv run ruff format --check .",
                status="PASS" if rc == 0 else "FAIL",
                detail=f"{sec:.1f}s ({out.splitlines()[-1] if out else 'ok'})",
            )
        )
        if rc != 0:
            failures.append(("Ruff Format (Full)", out))
    elif ctx.scopes.python_files:
        rc, out, sec = _run_proc(
            [uv_bin, "run", "ruff", "format", "--check", *ctx.scopes.python_files],
            repo_root,
        )
        gates.append(
            GateRecord(
                name="Ruff Format (Scoped)",
                command=f"uv run ruff format --check ({len(ctx.scopes.python_files)} py file(s))",
                status="PASS" if rc == 0 else "FAIL",
                detail=f"{sec:.1f}s",
            )
        )
        if rc != 0:
            failures.append(("Ruff Format (Scoped)", out))
    else:
        gates.append(
            GateRecord(
                name="Ruff Format",
                command="uv run ruff format --check",
                status="N/A",
                detail="0 changed .py files",
            )
        )

    # 2. Ruff Lint
    rc_lint, out_lint, sec_lint = _run_proc([uv_bin, "run", "ruff", "check", "."], repo_root)
    gates.append(
        GateRecord(
            name="Ruff Lint",
            command="uv run ruff check .",
            status="PASS" if rc_lint == 0 else "FAIL",
            detail=f"{sec_lint:.1f}s ({out_lint.splitlines()[-1] if out_lint else 'ok'})",
        )
    )
    if rc_lint != 0:
        failures.append(("Ruff Lint", out_lint))

    # 3. Mypy Strict
    rc_mypy, out_mypy, sec_mypy = _run_proc([uv_bin, "run", "mypy", "src"], repo_root)
    gates.append(
        GateRecord(
            name="Mypy Strict",
            command="uv run mypy src",
            status="PASS" if rc_mypy == 0 else "FAIL",
            detail=f"{sec_mypy:.1f}s ({out_mypy.splitlines()[-1] if out_mypy else 'ok'})",
        )
    )
    if rc_mypy != 0:
        failures.append(("Mypy Strict", out_mypy))

    # 4. Pytest + Branch Coverage >= 80%
    pytest_cmd = [uv_bin, "run", "pytest", "--cov=src", "--cov-fail-under=80"]
    rc_cov, out_cov, sec_cov = _run_proc(pytest_cmd, repo_root)
    cov_tail = summarize_pytest_output(out_cov)
    gates.append(
        GateRecord(
            name="Pytest + Coverage (>=80%)",
            command="uv run pytest --cov=src --cov-fail-under=80",
            status="PASS" if rc_cov == 0 else "FAIL",
            detail=f"{sec_cov:.1f}s ({cov_tail})",
        )
    )
    if rc_cov != 0:
        failures.append(("Pytest + Coverage (>=80%)", out_cov))

    # 5. Live Integration Tests (`-m integration`)
    needs_integration = (
        ctx.scopes.touches_api_worker_db or ctx.scopes.touches_alembic or ctx.scopes.touches_ast_rag
    )
    if args.integration is not None:
        targets = (
            []
            if (len(args.integration) == 0 or args.integration == ["all"])
            else list(args.integration)
        )
        int_cmd = [uv_bin, "run", "pytest", "-m", "integration", *targets]
        rc_int, out_int, sec_int = _run_proc(int_cmd, repo_root)
        gates.append(
            GateRecord(
                name="Live Integration Tests",
                command=" ".join(["uv", "run", "pytest", "-m", "integration", *targets]),
                status="PASS" if rc_int == 0 else "FAIL",
                detail=f"{sec_int:.1f}s ({summarize_pytest_output(out_int)})",
            )
        )
        if rc_int != 0:
            failures.append(("Live Integration Tests", out_int))
    elif needs_integration:
        gates.append(
            GateRecord(
                name="Live Integration Tests",
                command="pass --integration all or --integration <files>",
                status="SKIPPED" if args.skip_integration else "REQUIRED",
                detail=(
                    "Explicitly skipped via --skip-integration"
                    if args.skip_integration
                    else "DB/Redis/Qdrant/Worker scope changed — pass --integration"
                ),
            )
        )
    else:
        gates.append(
            GateRecord(
                name="Live Integration Tests",
                command="N/A",
                status="N/A",
                detail="Infra/DB/Qdrant scopes untouched",
            )
        )

    # 6. Domain Mechanical Validators
    if ctx.scopes.touches_ast_rag:
        auditor = repo_root / ".agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py"
        if auditor.exists():
            rc_zc, out_zc, sec_zc = _run_proc(
                [sys.executable, str(auditor), "--self-test"],
                repo_root,
            )
            gates.append(
                GateRecord(
                    name="Zero-Code Qdrant Guard",
                    command="audit_qdrant_zerocode.py --self-test",
                    status="PASS" if rc_zc == 0 else "FAIL",
                    detail=f"{sec_zc:.1f}s",
                )
            )
            if rc_zc != 0:
                failures.append(("Zero-Code Qdrant Guard", out_zc))

    if ctx.scopes.touches_diff_agents:
        diff_guard = (
            repo_root
            / ".agents/skills/github-diff-suggestion-guard/scripts/validate_diff_and_payload.py"
        )
        if diff_guard.exists():
            rc_dg, out_dg, sec_dg = _run_proc(
                [sys.executable, str(diff_guard), "--self-test"],
                repo_root,
            )
            gates.append(
                GateRecord(
                    name="Diff & HTTP-422 Guard",
                    command="validate_diff_and_payload.py --self-test",
                    status="PASS" if rc_dg == 0 else "FAIL",
                    detail=f"{sec_dg:.1f}s",
                )
            )
            if rc_dg != 0:
                failures.append(("Diff & HTTP-422 Guard", out_dg))

    # 7. Pre-Close Audit (when --preclose passed)
    if args.preclose:
        preclose_script = (
            repo_root / ".agents/skills/codesight-jira-workflow/scripts/preclose_audit.py"
        )
        preclose_cmd = [sys.executable, str(preclose_script)]
        if args.ticket:
            preclose_cmd.extend(["--ticket", args.ticket])
        rc_pc, out_pc, sec_pc = _run_proc(preclose_cmd, repo_root)
        gates.append(
            GateRecord(
                name="Pre-Close Audit",
                command="preclose_audit.py" + (f" --ticket {args.ticket}" if args.ticket else ""),
                status="PASS" if rc_pc == 0 else "FAIL",
                detail=f"{sec_pc:.1f}s",
            )
        )
        if rc_pc != 0:
            failures.append(("Pre-Close Audit", out_pc))

    # 8. Headed Playwright E2E (always --headed per user rule!)
    if args.e2e is not None:
        e2e_targets = [] if args.e2e == ["all"] else list(args.e2e)
        e2e_cmd = [uv_bin, "run", "pytest", "--headed", *e2e_targets]
        rc_e2e, out_e2e, sec_e2e = _run_proc(e2e_cmd, repo_root)
        gates.append(
            GateRecord(
                name="Playwright E2E (Headed)",
                command=" ".join(["uv", "run", "pytest", "--headed", *e2e_targets]),
                status="PASS" if rc_e2e == 0 else "FAIL",
                detail=f"{sec_e2e:.1f}s",
            )
        )
        if rc_e2e != 0:
            failures.append(("Playwright E2E (Headed)", out_e2e))

    print("\n## Quality Gates Summary")
    print("| Gate | Command | Status | Detail |")
    print("| :--- | :--- | :--- | :--- |")
    for gate in gates:
        print(
            f"| {_escape_cell(gate.name)} | `{_escape_cell(gate.command)}` | "
            f"**{gate.status}** | {_escape_cell(gate.detail)} |"
        )

    for fail_name, fail_out in failures:
        print(f"\n### Failure Tail — {fail_name}\n```text\n{_tail_lines(fail_out)}\n```")

    all_passed = (
        branch_ok
        and hygiene_ok
        and not failures
        and not any(g.status in {"FAIL", "REQUIRED"} for g in gates)
    )
    verdict = "ALL GATES GREEN" if all_passed else "CHECK FAILED / INCOMPLETE"
    print(f"\n**VERDICT**: `{verdict}`")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
