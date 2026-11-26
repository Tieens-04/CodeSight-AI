"""Unit tests for CodeSight-AI .ai-workflow/scripts/verify.py (v4.0)."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve().parent / "verify.py"


def _load_verify_module() -> Any:
    spec = importlib.util.spec_from_file_location("codesight_ai_workflow_verify", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify = _load_verify_module()


class VerifyWorkflowCliTests(unittest.TestCase):
    def test_validate_branch_name_enforces_codesight_ca_convention(self) -> None:
        self.assertEqual(
            verify.validate_branch_name("feat/ca-18-security-auditor-node"),
            (True, None),
        )
        self.assertEqual(
            verify.validate_branch_name("feat/ca-18-security-auditor-node", ticket="CA-18"),
            (True, None),
        )
        # Mismatched ticket number must fail
        ok_mismatch, reason_mismatch = verify.validate_branch_name(
            "feat/ca-18-security-auditor-node",
            ticket="CA-19",
        )
        self.assertFalse(ok_mismatch)
        self.assertIn("CA-19", reason_mismatch or "")

        for invalid in [
            "main",
            "master",
            "codex/ca-18-fix",
            "feature/ca-18-fix",
            "bugfix/ca-18-fix",
            "chore/ca-18-fix",
            "feat/CA-18-uppercase",
            "feat/cwb-18-wrong-project",
            "",
            "HEAD",
        ]:
            ok, reason = verify.validate_branch_name(invalid)
            self.assertFalse(ok, f"Expected branch '{invalid}' to be rejected")
            self.assertTrue(reason)

    def test_parse_git_status_and_classify_scopes_handle_tracked_and_untracked_files(self) -> None:
        porcelain = "\n".join(
            [
                " M .gitignore",
                " M src/codesight/agents/graph.py",
                "A  alembic/versions/20261124_01_add_index.py",
                "D  src/codesight/diff/legacy.py",
                "R  src/codesight/rag/old.py -> src/codesight/rag/new.py",
                "?? tests/unit/test_security_auditor.py",
                "?? docs/specs/ca-18.md",
                "?? .ai-workflow/tasks/CA-18/state.md",
                "?? .ai-workflow/BOARD.md",
            ]
        )
        parsed = verify.parse_git_status_porcelain(porcelain)
        self.assertEqual(
            parsed.modified,
            [
                ".gitignore",
                "src/codesight/agents/graph.py",
                "alembic/versions/20261124_01_add_index.py",
                "src/codesight/rag/new.py",
            ],
        )
        self.assertEqual(parsed.deleted, ["src/codesight/diff/legacy.py"])
        self.assertEqual(
            parsed.untracked,
            [
                "tests/unit/test_security_auditor.py",
                "docs/specs/ca-18.md",
            ],
        )

        scopes = verify.classify_scopes(parsed.live_files, parsed.deleted)
        self.assertTrue(scopes.touches_api_worker_db)
        self.assertTrue(scopes.touches_alembic)
        self.assertTrue(scopes.touches_ast_rag)
        self.assertTrue(scopes.touches_diff_agents)
        self.assertTrue(scopes.touches_docs)
        self.assertEqual(
            scopes.python_files,
            [
                "src/codesight/agents/graph.py",
                "alembic/versions/20261124_01_add_index.py",
                "src/codesight/rag/new.py",
                "tests/unit/test_security_auditor.py",
            ],
        )

    def test_scan_cleanliness_catches_env_keys_debuggers_bare_print_skip_and_secrets(self) -> None:
        dirty = verify.scan_cleanliness(
            live_files=[
                ".env.local",
                "certs/app.pem",
                "debug.log",
                "src/codesight/agents/graph.py",
                "tests/unit/test_graph.py",
            ],
            added_lines_by_file={
                "src/codesight/agents/graph.py": [
                    (40, '    print("debug state", state)'),
                    (41, "    breakpoint()"),
                    (42, '    token = "ghp_1234567890abcdefghijklmnopqrstuvwxyzABCD"'),
                ],
                "tests/unit/test_graph.py": [
                    (12, '@pytest.mark.skip("temporarily disabled")'),
                ],
            },
        )
        self.assertFalse(dirty.ok)
        joined = "\n".join(dirty.issues)
        self.assertIn(".env.local", joined)
        self.assertIn("certs/app.pem", joined)
        self.assertIn("debug.log", joined)
        self.assertIn("print(", joined)
        self.assertIn("breakpoint()", joined)
        self.assertIn("secret", joined)
        self.assertIn("@pytest.mark.skip", joined)

        # Rich console.print in CLI, @pytest.mark.skipif in tests, and .env.example must PASS
        clean = verify.scan_cleanliness(
            live_files=[
                ".env.example",
                "src/codesight/cli/commands/review.py",
                "tests/integration/test_smoke.py",
            ],
            added_lines_by_file={
                ".env.example": [
                    (5, "GEMINI_API_KEY=your-key-here"),
                ],
                "src/codesight/cli/commands/review.py": [
                    (88, '    console.print("[green]Done[/green]")'),
                ],
                "tests/integration/test_smoke.py": [
                    (10, '@pytest.mark.skipif(not HAS_KEY, reason="opt-in live smoke")'),
                    (15, '    settings = Settings(github_token=SecretStr("ghp_test_token"))'),
                ],
            },
        )
        self.assertTrue(clean.ok, f"Unexpected cleanliness issues: {clean.issues}")
        self.assertEqual(clean.issues, [])

    def test_parse_state_markdown_and_infer_ticket_state_support_v4_v3_and_merged_main(
        self,
    ) -> None:
        self.assertEqual(
            verify.CANONICAL_STATUSES,
            ("Todo", "Working", "Blocked", "Ready for Review", "Done"),
        )
        parsed = verify.parse_state_markdown(
            "\n".join(
                [
                    "# Handoff State: CA-18",
                    "- **Status**: Working",
                    "- **Step**: 02-implement",
                    "- **Branch**: `feat/ca-18-security-auditor`",
                    "- **Objective**: Implement Security & NFR-3 Auditor node",
                    "- **Strikes**: 1",
                    "- **Next Gate**: Run 03-review.md",
                ]
            )
        )
        self.assertEqual(parsed.status, "Working")
        self.assertEqual(parsed.step, "02-implement")
        self.assertEqual(parsed.branch, "feat/ca-18-security-auditor")
        self.assertEqual(parsed.strikes, 1)

        # Legacy v3 folder with plan.md only -> Working
        legacy_plan = verify.infer_ticket_state(
            ticket_id="CA-18",
            files=["jira.md", "plan.md"],
            state_content=None,
            plan_content=(
                "# Implementation Plan: CA-18 — Security Auditor\n- **Branch**: `feat/ca-18-sec`\n"
            ),
            merged_tickets={"CA-17"},
            current_branch="feat/ca-18-sec",
        )
        self.assertEqual(legacy_plan.status, "Working")
        self.assertIn("02-implement", legacy_plan.next_gate)

        # Unmerged ticket at Step 6 precommit.md -> Ready for Review (awaiting Step 7 close)
        step6_waiting = verify.infer_ticket_state(
            ticket_id="CA-18",
            files=["jira.md", "plan.md", "final-review.md", "precommit.md"],
            state_content=None,
            precommit_content="**`SAFE TO COMMIT`**\n",
            merged_tickets=set(),
            current_branch="feat/ca-18-sec",
        )
        self.assertEqual(step6_waiting.status, "Ready for Review")
        self.assertIn("close", step6_waiting.next_gate.lower())

        # Ticket with Jira Closure Record in precommit.md OR merged in main -> Done
        closed_jira = verify.infer_ticket_state(
            ticket_id="CA-18",
            files=["jira.md", "plan.md", "final-review.md", "precommit.md"],
            state_content=None,
            precommit_content="**`SAFE TO COMMIT`**\n\n## 8. Jira Closure Record\n- Done (41)\n",
            merged_tickets=set(),
            current_branch="feat/ca-18-sec",
        )
        self.assertEqual(closed_jira.status, "Done")

        merged_legacy = verify.infer_ticket_state(
            ticket_id="CA-17",
            files=["jira.md", "plan.md", "review-01.md", "final-review.md", "precommit.md"],
            state_content=None,
            merged_tickets={"CA-17"},
            current_branch="main",
        )
        self.assertEqual(merged_legacy.status, "Done")

    def test_build_board_from_disk_prioritizes_blocked_and_separates_done_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ca-board-test-") as tmp:
            root = Path(tmp)
            tasks_dir = root / ".ai-workflow" / "tasks"
            (tasks_dir / "CA-17").mkdir(parents=True)
            (tasks_dir / "CA-18").mkdir(parents=True)
            (tasks_dir / "CA-19").mkdir(parents=True)

            (tasks_dir / "CA-17" / "plan.md").write_text(
                "# Implementation Plan: CA-17 — LLMFactory & LangGraph\n",
                encoding="utf-8",
            )
            (tasks_dir / "CA-17" / "precommit.md").write_text(
                "**`SAFE TO COMMIT`**\n",
                encoding="utf-8",
            )

            (tasks_dir / "CA-18" / "jira.md").write_text("# CA-18\n", encoding="utf-8")
            (tasks_dir / "CA-18" / "plan.md").write_text(
                "# Implementation Plan: CA-18 — Security Auditor\n",
                encoding="utf-8",
            )

            (tasks_dir / "CA-19" / "state.md").write_text(
                "\n".join(
                    [
                        "# Handoff State: CA-19",
                        "- **Status**: Blocked",
                        "- **Step**: 04-fix",
                        "- **Branch**: `feat/ca-19-critic`",
                        "- **Objective**: Critic synthesis node",
                        "- **Strikes**: 2",
                        "- **Next Gate**: Human decision on schema strictness",
                    ]
                ),
                encoding="utf-8",
            )

            board = verify.build_board_from_disk(
                repo_root=root,
                merged_tickets={"CA-17"},
                current_branch="feat/ca-18-security-auditor",
                write_to_disk=True,
            )
            self.assertEqual(len(board.active), 2)
            self.assertEqual(board.active[0].ticket_id, "CA-19")  # Blocked surfaces first
            self.assertEqual(board.active[1].ticket_id, "CA-18")
            self.assertEqual(len(board.done), 1)
            self.assertEqual(board.done[0].ticket_id, "CA-17")

            board_path = root / ".ai-workflow" / "BOARD.md"
            self.assertTrue(board_path.exists())
            written = board_path.read_text(encoding="utf-8")
            self.assertIn("CA-19", written)
            self.assertIn("Blocked", written)
            self.assertEqual(written, verify.render_board_markdown(board))

    def test_summarize_pytest_output_ignores_trailing_atexit_stderr_noise(self) -> None:
        noisy = "\n".join(
            [
                "TOTAL      3100    210    980    110    90%",
                "================= 508 passed, 2 skipped in 189.61s =================",
                "--- Logging error ---",
                "ValueError: I/O operation on closed file.",
            ]
        )
        self.assertEqual(
            verify.summarize_pytest_output(noisy),
            "508 passed, 2 skipped in 189.61s · cov 90%",
        )


if __name__ == "__main__":
    unittest.main()
