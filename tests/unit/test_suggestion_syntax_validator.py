"""Unit tests for CA-20: TreeSitterSuggestionValidator and Publisher Suggestion Guard."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from codesight.agents.schemas import CodeSuggestion, VerifiedFinding
from codesight.ast_engine.suggestion_validator import (
    SuggestionValidationResult,
    TreeSitterSuggestionValidator,
    is_suggestion_syntax_safe,
)
from codesight.core.config import Settings
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs.github_auth import GitHubAuthProvider
from codesight.vcs.github_publisher import (
    GitHubReviewPublisher,
    ReviewCommentDraft,
    partition_review_comments,
)


def _parse_single_file_diff(file_path: str, patch: str, *, status: str = "modified"):
    parsed = SmartDiffParser().parse_patch_entries(
        [PatchFileEntry(filename=file_path, patch=patch, status=status)]
    )
    pf = parsed.get_file(file_path)
    assert pf is not None
    return parsed, pf


class TestTreeSitterSuggestionValidator:
    def test_python_valid_single_and_multiline_suggestions_pass(self) -> None:
        source = (
            "import sqlite3\n"
            "\n"
            "def find_user(conn: sqlite3.Connection, email: str) -> object:\n"
            "    query = f\"SELECT id FROM users WHERE email = '{email}'\"\n"
            "    return conn.execute(query).fetchone()\n"
        )
        patch = (
            "@@ -3,3 +3,3 @@ def find_user(conn: sqlite3.Connection, email: str) -> object:\n"
            " def find_user(conn: sqlite3.Connection, email: str) -> object:\n"
            "+    query = f\"SELECT id FROM users WHERE email = '{email}'\"\n"
            "+    return conn.execute(query).fetchone()\n"
        )
        _, pf = _parse_single_file_diff("src/repo.py", patch)
        validator = TreeSitterSuggestionValidator()

        single_sugg = CodeSuggestion(
            start_line=4,
            end_line=4,
            replacement_code='    query = "SELECT id FROM users WHERE email = ?"',
            explanation="Use parameterized placeholders.",
        )
        res_single = validator.validate_suggestion(
            "src/repo.py",
            single_sugg,
            after_source=source,
            parsed_file=pf,
        )
        assert isinstance(res_single, SuggestionValidationResult)
        assert res_single.is_safe is True
        assert res_single.reason is None
        assert res_single.suggestion == single_sugg
        assert res_single.syntax_error_count == 0

        multi_sugg = CodeSuggestion(
            start_line=4,
            end_line=5,
            replacement_code=(
                '    query = "SELECT id FROM users WHERE email = ?"\n'
                "    return conn.execute(query, (email,)).fetchone()"
            ),
            explanation="Bind email as a SQL parameter tuple.",
        )
        res_multi = validator.validate_suggestion(
            "src/repo.py",
            multi_sugg,
            after_source=source,
            parsed_file=pf,
        )
        assert res_multi.is_safe is True
        assert res_multi.suggestion == multi_sugg

    @pytest.mark.parametrize(
        "bad_code",
        [
            '  query = "SELECT 1"',  # broken Python 2-space indent inside 4-space block
            "        query = 'too_indented'\n    return None",  # IndentationError
            '    return conn.execute("SELECT 1", (email,)',  # unclosed parenthesis (MISSING)
            "    def broken(: pass",  # syntax ERROR node
        ],
    )
    def test_python_broken_indent_or_syntax_is_detected_and_stripped_keeping_explanation(
        self,
        bad_code: str,
    ) -> None:
        source = (
            "def find_user(conn, email):\n"
            "    query = f\"SELECT id FROM users WHERE email = '{email}'\"\n"
            "    return conn.execute(query).fetchone()\n"
        )
        patch = (
            "@@ -1,3 +1,3 @@\n"
            " def find_user(conn, email):\n"
            "+    query = f\"SELECT id FROM users WHERE email = '{email}'\"\n"
            "+    return conn.execute(query).fetchone()\n"
        )
        _, pf = _parse_single_file_diff("src/repo.py", patch)
        validator = TreeSitterSuggestionValidator()

        bad_sugg = CodeSuggestion(
            start_line=2,
            end_line=2,
            replacement_code=bad_code,
            explanation="Always bind SQL parameters instead of interpolating strings.",
        )
        res = validator.validate_suggestion(
            "src/repo.py",
            bad_sugg,
            after_source=source,
            parsed_file=pf,
        )
        assert res.is_safe is False
        assert res.suggestion is None
        assert res.reason == "syntax_error"
        assert res.syntax_error_count >= 1
        assert (
            res.preserved_explanation
            == "Always bind SQL parameters instead of interpolating strings."
        )

    @pytest.mark.parametrize(
        ("file_path", "source", "patch", "valid_code", "broken_code"),
        [
            (
                "src/service.ts",
                ("export function greet(name: string): string {\n  return `hello ${name}`;\n}\n"),
                (
                    "@@ -1,3 +1,3 @@\n"
                    " export function greet(name: string): string {\n"
                    "+  return `hello ${name}`;\n"
                    " }\n"
                ),
                "  return `hi ${name.trim()}`;",
                "  if (name) {\n    return `hi ${name}`;",  # missing closing '}'
            ),
            (
                "src/Panel.tsx",
                (
                    "export function Panel({ title }: { title: string }) {\n"
                    "  return <section><h1>{title}</h1></section>;\n"
                    "}\n"
                ),
                (
                    "@@ -1,3 +1,3 @@\n"
                    " export function Panel({ title }: { title: string }) {\n"
                    "+  return <section><h1>{title}</h1></section>;\n"
                    " }\n"
                ),
                "  return <section aria-label={title}><h1>{title}</h1></section>;",
                "  return <section><h1>{title}</section>;",  # mismatched JSX closing tag
            ),
        ],
    )
    def test_typescript_and_tsx_valid_vs_missing_brace_or_jsx_error(
        self,
        file_path: str,
        source: str,
        patch: str,
        valid_code: str,
        broken_code: str,
    ) -> None:
        _, pf = _parse_single_file_diff(file_path, patch)
        validator = TreeSitterSuggestionValidator()

        ok = validator.validate_suggestion(
            file_path,
            CodeSuggestion(start_line=2, end_line=2, replacement_code=valid_code, explanation="OK"),
            after_source=source,
            parsed_file=pf,
        )
        assert ok.is_safe is True
        assert ok.suggestion is not None

        bad = validator.validate_suggestion(
            file_path,
            CodeSuggestion(
                start_line=2,
                end_line=2,
                replacement_code=broken_code,
                explanation="Keep JSX tags and braces balanced.",
            ),
            after_source=source,
            parsed_file=pf,
        )
        assert bad.is_safe is False
        assert bad.suggestion is None
        assert bad.reason == "syntax_error"
        assert bad.syntax_error_count >= 1
        assert bad.preserved_explanation == "Keep JSX tags and braces balanced."

    def test_missing_after_source_on_ast_file_fails_closed(self) -> None:
        patch = "@@ -1,2 +1,2 @@\n def f():\n+    return 1\n"
        _, pf = _parse_single_file_diff("src/app.py", patch)
        validator = TreeSitterSuggestionValidator()

        res = validator.validate_suggestion(
            "src/app.py",
            CodeSuggestion(
                start_line=2, end_line=2, replacement_code="    return 2", explanation="Ex"
            ),
            after_source=None,
            parsed_file=pf,
        )
        assert res.is_safe is False
        assert res.suggestion is None
        assert res.reason == "missing_after_source"
        assert res.preserved_explanation == "Ex"

    def test_preexisting_syntax_error_in_after_source_still_fails_has_error_check(self) -> None:
        broken_full = "def broken(:\n    x = 1\n"
        patch = "@@ -1,2 +1,2 @@\n def broken(:\n+    x = 1\n"
        _, pf = _parse_single_file_diff("src/broken.py", patch)
        validator = TreeSitterSuggestionValidator()

        res = validator.validate_suggestion(
            "src/broken.py",
            CodeSuggestion(
                start_line=2, end_line=2, replacement_code="    x = 2", explanation="Fix"
            ),
            after_source=broken_full,
            parsed_file=pf,
        )
        assert res.is_safe is False
        assert res.suggestion is None
        assert res.reason == "syntax_error"

    def test_non_ast_extensions_validate_hunk_coords_and_allow_valid_suggestions(self) -> None:
        patch = "@@ -1,2 +1,2 @@\n key: old\n+key: new\n"
        _, pf = _parse_single_file_diff("config/settings.yaml", patch)
        validator = TreeSitterSuggestionValidator()

        ok = validator.validate_suggestion(
            "config/settings.yaml",
            CodeSuggestion(
                start_line=2, end_line=2, replacement_code="key: safe", explanation="Safe"
            ),
            after_source=None,
            parsed_file=pf,
        )
        assert ok.is_safe is True
        assert ok.suggestion is not None

        stub_patch = (
            "@@ -1,1 +1,2 @@\n"
            " export declare function a(): void;\n"
            "+export declare function b(): void;\n"
        )
        _, pf_dts = _parse_single_file_diff("src/types.d.ts", stub_patch)
        ok_dts = validator.validate_suggestion(
            "src/types.d.ts",
            CodeSuggestion(
                start_line=2,
                end_line=2,
                replacement_code="export declare function b(x: string): void;",
            ),
            after_source="export declare function a(): void;\nexport declare function b(): void;\n",
            parsed_file=pf_dts,
        )
        assert ok_dts.is_safe is True

    def test_out_of_hunk_cross_hunk_eof_and_crlf_guards(self) -> None:
        source = "def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2"  # CRLF + no trailing LF
        patch = (
            "@@ -1,2 +1,2 @@\n def a():\n+    return 1\n@@ -4,2 +4,2 @@\n def b():\n+    return 2\n"
        )
        _, pf = _parse_single_file_diff("src/crlf.py", patch)
        validator = TreeSitterSuggestionValidator()

        # Line 3 is outside any hunk
        out_res = validator.validate_suggestion(
            "src/crlf.py",
            CodeSuggestion(start_line=3, end_line=3, replacement_code=""),
            after_source=source,
            parsed_file=pf,
        )
        assert out_res.is_safe is False
        assert out_res.reason == "outside_diff_hunk"

        # Lines 2..5 span hunk 1 and hunk 2
        cross_res = validator.validate_suggestion(
            "src/crlf.py",
            CodeSuggestion(start_line=2, end_line=5, replacement_code="    return 9"),
            after_source=source,
            parsed_file=pf,
        )
        assert cross_res.is_safe is False
        assert cross_res.reason == "cross_hunk_range"

        # Line 5 at EOF without trailing newline splices cleanly
        eof_res = validator.validate_suggestion(
            "src/crlf.py",
            CodeSuggestion(start_line=5, end_line=5, replacement_code="    return 42"),
            after_source=source.encode("utf-8"),
            parsed_file=pf,
        )
        assert eof_res.is_safe is True
        assert is_suggestion_syntax_safe(source, 5, 5, "    return 42", "src/crlf.py") is True

        # Out of file line bounds
        bounds_res = validator.validate_suggestion(
            "src/crlf.py",
            CodeSuggestion(start_line=2, end_line=2, replacement_code="    return 10"),
            after_source="def a():\n",
            right_line_hunks={2: 1},
        )
        assert bounds_res.is_safe is False
        assert bounds_res.reason == "line_out_of_bounds"

        # Empty replacement code rejected
        empty_res = validator.validate_suggestion(
            "src/crlf.py",
            CodeSuggestion(start_line=2, end_line=2, replacement_code="   \n  "),
            after_source=source,
            parsed_file=pf,
        )
        assert empty_res.is_safe is False
        assert empty_res.reason == "empty_suggestion"

    def test_sanitize_markdown_comment_and_publisher_defense_in_depth(self) -> None:
        source = "def compute(x: int) -> int:\n    clamped = x + 1\n    return clamped\n"
        patch = (
            "@@ -1,3 +1,3 @@\n"
            " def compute(x: int) -> int:\n"
            "+    clamped = x + 1\n"
            "+    return clamped\n"
        )
        parsed_diff, pf = _parse_single_file_diff("src/math.py", patch)
        validator = TreeSitterSuggestionValidator()

        bad_md = (
            "**[WARNING] Avoid overflow**\n\n"
            "Clamp input before adding.\n\n"
            "```suggestion\n"
            "  clamped = min(x, 100) + 1\n"
            "```"
        )
        cleaned, stripped, reason = validator.sanitize_markdown_comment(
            "src/math.py",
            bad_md,
            start_line=None,
            line=2,
            after_source=source,
            parsed_file=pf,
        )
        assert stripped is True
        assert reason == "syntax_error"
        assert "```suggestion" not in cleaned
        assert "Clamp input before adding." in cleaned

        valid_md = (
            "**[WARNING] Avoid overflow**\n\n"
            "Clamp input before adding.\n\n"
            "```suggestion\n"
            "    clamped = min(x, 100) + 1\n"
            "```"
        )
        kept, stripped_ok, reason_ok = validator.sanitize_markdown_comment(
            "src/math.py",
            valid_md,
            start_line=None,
            line=2,
            after_source=source,
            parsed_file=pf,
        )
        assert stripped_ok is False
        assert reason_ok is None
        assert "```suggestion" in kept

        # Defense-in-depth inside partition_review_comments when after_sources_by_file is passed
        prepared = partition_review_comments(
            parsed_diff,
            [
                ReviewCommentDraft(
                    path="src/math.py",
                    line=2,
                    body=bad_md,
                    severity="WARNING",
                )
            ],
            after_sources_by_file={"src/math.py": source},
        )
        assert len(prepared.inline_comments) == 1
        assert "```suggestion" not in prepared.inline_comments[0]["body"]
        assert "Clamp input before adding." in prepared.inline_comments[0]["body"]

        # Unclosed, multi-block, and line=None Markdown sanitization branches
        unclosed = "Explain.\n\n```suggestion\n    clamped = 1"
        c1, s1, r1 = validator.sanitize_markdown_comment(
            "src/math.py", unclosed, start_line=None, line=2, after_source=source, parsed_file=pf
        )
        assert s1 is True and r1 == "unclosed_suggestion_fence" and "Explain." in c1

        multi_blk = "```suggestion\n    a = 1\n```\n\n```suggestion\n    b = 2\n```"
        c2, s2, r2 = validator.sanitize_markdown_comment(
            "src/math.py", multi_blk, start_line=None, line=2, after_source=source, parsed_file=pf
        )
        assert s2 is True and r2 == "multiple_suggestion_blocks" and "omitted" in c2

        no_line = "Keep text.\n\n~~~suggestion\n    clamped = 1\n~~~"
        c3, s3, r3 = validator.sanitize_markdown_comment(
            "src/math.py", no_line, start_line=None, line=None, after_source=source, parsed_file=pf
        )
        assert s3 is True and r3 == "missing_line_coordinate" and "Keep text." in c3

    def test_verified_finding_syncs_github_coords_to_validated_suggestion(self) -> None:
        finding = VerifiedFinding(
            candidate_id="b000-sec-000",
            role="security_auditor",
            batch_index=0,
            file_path="src/math.py",
            start_line=1,
            end_line=3,
            severity="WARNING",
            category="logic",
            title="Fix range",
            evidence="Line 2 returns wrong value.",
            confidence=0.9,
            rationale="Confirmed on L2.",
            suggestion=CodeSuggestion(
                start_line=2,
                end_line=2,
                replacement_code="    return 0",
                explanation="Return zero safely.",
            ),
        )
        draft = finding.to_review_comment_draft()
        assert draft.start_line is None
        assert draft.line == 2
        assert "```suggestion\n    return 0\n```" in draft.body
        assert "Return zero safely." in draft.body

        # Multi-line valid suggestion + 4-backtick fence when code contains triple backticks
        multi_finding = finding.model_copy(
            update={
                "suggestion": CodeSuggestion(
                    start_line=1,
                    end_line=3,
                    replacement_code='def compute() -> str:\n    return "```ok```"',
                    explanation="Use ```suggestion fence safely.",
                )
            }
        )
        multi_draft = multi_finding.to_review_comment_draft()
        assert multi_draft.start_line == 1
        assert multi_draft.line == 3
        assert (
            '````suggestion\ndef compute() -> str:\n    return "```ok```"\n````' in multi_draft.body
        )

        # Stripped suggestion keeps explanation and neutralizes suggestion fences in prose
        stripped_finding = finding.model_copy(
            update={
                "title": "Fix range\n```suggestion\nbad()\n```",
                "evidence": "Line 2 returns wrong value.\n~~~suggestion\nbad2()\n~~~",
                "rationale": "Confirmed on L2.\n```suggestion\nbad3()\n```",
                "suggestion": None,
                "suggestion_explanation": "Return zero safely.\n```suggestion\nbad4()\n```",
                "suggestion_stripped_reason": "syntax_error",
            }
        )
        stripped_draft = stripped_finding.to_review_comment_draft()
        assert stripped_draft.start_line == 1
        assert stripped_draft.line == 3
        assert "```suggestion" not in stripped_draft.body.lower()
        assert "~~~suggestion" not in stripped_draft.body.lower()
        assert "Return zero safely." in stripped_draft.body

    def test_commonmark_fence_bypass_and_docstring_indented_backticks(self) -> None:
        source = "def f(x: int) -> int:\n    return x + 1\n"
        patch = "@@ -1,2 +1,2 @@\n def f(x: int) -> int:\n+    return x + 1\n"
        _, pf = _parse_single_file_diff("src/f.py", patch)
        validator = TreeSitterSuggestionValidator()

        # 1. Inline triple backtick span before a broken suggestion must NOT swallow it
        inline_prefix = (
            "```foo``` is deprecated here.\n\n```suggestion\n  def broken(:\n```\nExplain."
        )
        cleaned1, stripped1, reason1 = validator.sanitize_markdown_comment(
            "src/f.py", inline_prefix, start_line=None, line=2, after_source=source, parsed_file=pf
        )
        assert stripped1 is True
        assert reason1 == "syntax_error"
        assert "```suggestion" not in cleaned1
        assert "Explain." in cleaned1

        # 2. 4-space indented code line before a broken suggestion must NOT open a fence
        indented_prefix = "    ```python\n\n```suggestion\n  def broken(:\n```"
        cleaned2, stripped2, reason2 = validator.sanitize_markdown_comment(
            "src/f.py",
            indented_prefix,
            start_line=None,
            line=2,
            after_source=source,
            parsed_file=pf,
        )
        assert stripped2 is True
        assert reason2 == "syntax_error"
        assert "```suggestion" not in cleaned2

        # 3. 4-space indented triple backticks inside valid suggestion must NOT close early
        valid_docstring_sugg = (
            "Add docstring.\n\n"
            "````suggestion\n"
            '    """Example:\n'
            "    ```\n"
            '    """\n'
            "    return x + 1\n"
            "````"
        )
        kept, stripped3, reason3 = validator.sanitize_markdown_comment(
            "src/f.py",
            valid_docstring_sugg,
            start_line=None,
            line=2,
            after_source=source,
            parsed_file=pf,
        )
        assert stripped3 is False
        assert reason3 is None
        assert "````suggestion" in kept

    def test_unicode_line_separators_utf8_bom_mts_cts_and_non_ast_bounds(self) -> None:
        # \u2028 in TS must NOT shift line numbers
        ts_source = 'const sep = "\u2028";\nexport function f(): number {\n  return 1;\n}\n'
        assert is_suggestion_syntax_safe(ts_source, 3, 3, "  return 2;", "src/a.ts") is True

        # \x0c (form feed) in Python must NOT shift line numbers
        py_ff_source = "x = 1\n\x0c\ndef g():\n    return x\n"
        assert is_suggestion_syntax_safe(py_ff_source, 4, 4, "    return x + 1", "src/a.py") is True

        # UTF-8 BOM Python file must succeed with valid suggestion
        py_bom = b"\xef\xbb\xbfdef greet() -> int:\n    return 1\n"
        assert is_suggestion_syntax_safe(py_bom, 2, 2, "    return 2", "src/bom.py") is True

        # .mts and .cts AST files
        mts_src = "export const run = (): number => {\n  return 1;\n};\n"
        cts_src = "const run = (): number => {\n  return 1;\n};\nmodule.exports = { run };\n"
        assert is_suggestion_syntax_safe(mts_src, 2, 2, "  return 2;", "src/mod.mts") is True
        assert is_suggestion_syntax_safe(mts_src, 2, 2, "  if (x) {", "src/mod.mts") is False
        assert is_suggestion_syntax_safe(cts_src, 2, 2, "  return 2;", "src/mod.cts") is True
        assert is_suggestion_syntax_safe(cts_src, 2, 2, "  if (x) {", "src/mod.cts") is False

        # Non-AST extension rejects empty/out-of-bounds consistently in is_suggestion_syntax_safe
        yaml_src = "key: old\n"
        assert is_suggestion_syntax_safe(yaml_src, 1, 1, "", "config/app.yaml") is False
        assert is_suggestion_syntax_safe(yaml_src, 99, 99, "key: new", "config/app.yaml") is False
        assert is_suggestion_syntax_safe(yaml_src, 1, 1, "key: new", "config/app.yaml") is True

    @pytest.mark.asyncio
    async def test_multiline_partition_and_publish_review_defense_in_depth(self) -> None:
        source = "def calc(a: int) -> int:\n    x = a + 1\n    return x\n"
        patch = "@@ -1,3 +1,3 @@\n def calc(a: int) -> int:\n+    x = a + 1\n+    return x\n"
        parsed_diff, _ = _parse_single_file_diff("src/calc.py", patch)

        bad_multiline = (
            "Fix calculation.\n\n```suggestion\n  x = a + 2\n    return x\n```"  # mixed indent
        )
        prepared = partition_review_comments(
            parsed_diff,
            [
                ReviewCommentDraft(
                    path="src/calc.py",
                    start_line=2,
                    line=3,
                    body=bad_multiline,
                    severity="WARNING",
                )
            ],
            after_sources_by_file={"src/calc.py": source},
        )
        assert len(prepared.inline_comments) == 1
        assert prepared.inline_comments[0]["start_line"] == 2
        assert prepared.inline_comments[0]["line"] == 3
        assert "```suggestion" not in prepared.inline_comments[0]["body"]
        assert "Fix calculation." in prepared.inline_comments[0]["body"]

        captured_payloads: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            captured_payloads.append(payload)
            return httpx.Response(
                200,
                json={
                    "id": 88,
                    "html_url": "https://github.com/acme/app/pull/1#pullrequestreview-88",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            auth = GitHubAuthProvider(
                Settings(_env_file=None, github_token=SecretStr("ghp_test")),  # type: ignore[call-arg]
                http_client=client,
            )
            publisher = GitHubReviewPublisher(auth, http_client=client)
            rec = await publisher.publish_review(
                "acme/app",
                1,
                head_sha="abc1234",
                parsed_diff=parsed_diff,
                drafts=[
                    ReviewCommentDraft(
                        path="src/calc.py",
                        start_line=2,
                        line=3,
                        body=bad_multiline,
                        severity="WARNING",
                    )
                ],
                after_sources_by_file={"src/calc.py": source},
            )
        assert rec.inline_count == 1
        assert len(captured_payloads) == 1
        sent_comment = captured_payloads[0]["comments"][0]
        assert "```suggestion" not in sent_comment["body"]
        assert "Fix calculation." in sent_comment["body"]
