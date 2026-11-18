"""22 edge-case unit tests for SmartDiffParser and DiffHunkMapper (CA-9)."""

from __future__ import annotations

from codesight.config_reader.repo_config import RepoConfig
from codesight.diff.file_filter import FileFilter
from codesight.diff.hunk_mapper import DiffHunkMapper
from codesight.diff.models import FilterReason, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from tests.conftest import load_guard_validator


def test_add_function_at_top_of_file_shifts_right_coordinates() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/handler.py b/src/handler.py",
            "--- a/src/handler.py",
            "+++ b/src/handler.py",
            "@@ -1,3 +1,8 @@",
            "+def guard_request(token: str) -> None:",
            "+    if not token:",
            "+        raise ValueError('missing token')",
            "+",
            "+",
            " def handle() -> int:",
            "     return 1",
            " ",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/handler.py")
    assert f is not None
    assert f.valid_commentable_lines == {1, 2, 3, 4, 5, 6, 7, 8}
    assert f.right_line_to_hunk == {i: 1 for i in range(1, 9)}
    lines = f.hunks[0].lines
    assert [(ln.kind, ln.left_line, ln.right_line) for ln in lines] == [
        ("add", None, 1),
        ("add", None, 2),
        ("add", None, 3),
        ("add", None, 4),
        ("add", None, 5),
        ("context", 1, 6),
        ("context", 2, 7),
        ("context", 3, 8),
    ]
    assert lines[0].llm_text == "L001 | + def guard_request(token: str) -> None:"
    assert lines[5].llm_text == "L006 |   def handle() -> int:"


def test_add_function_at_bottom_of_file_tracks_trailing_lines() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/footer.py b/src/footer.py",
            "--- a/src/footer.py",
            "+++ b/src/footer.py",
            "@@ -48,3 +48,9 @@ def existing_tail():",
            "     cleanup()",
            "     return True",
            " ",
            "+",
            "+def append_audit_event(event_id: str) -> str:",
            "+    return f'audit:{event_id}'",
            "+",
            "+",
            "+__all__ = ['existing_tail', 'append_audit_event']",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/footer.py")
    assert f is not None
    assert f.valid_commentable_lines == {48, 49, 50, 51, 52, 53, 54, 55, 56}
    assert f.hunks[0].lines[0].left_line == 48
    assert f.hunks[0].lines[-1].kind == "add"
    assert f.hunks[0].lines[-1].right_line == 56
    assert f.hunks[0].lines[-1].llm_text == (
        "L056 | + __all__ = ['existing_tail', 'append_audit_event']"
    )


def test_add_function_into_previously_empty_existing_file() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/empty_mod.py b/src/empty_mod.py",
            "index e69de29..a1b2c3d 100644",
            "--- a/src/empty_mod.py",
            "+++ b/src/empty_mod.py",
            "@@ -0,0 +1,4 @@",
            "+from __future__ import annotations",
            "+",
            "+def bootstrap() -> str:",
            "+    return 'ready'",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/empty_mod.py")
    assert f is not None
    assert f.change_type == "modified"
    assert f.old_path == "src/empty_mod.py"
    assert f.new_path == "src/empty_mod.py"
    assert f.hunks[0].old_count == 0
    assert f.hunks[0].new_count == 4
    assert f.valid_commentable_lines == {1, 2, 3, 4}


def test_interleaved_deletions_and_consecutive_multiline_edits() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/interleaved.py b/src/interleaved.py",
            "--- a/src/interleaved.py",
            "+++ b/src/interleaved.py",
            "@@ -10,5 +10,6 @@ def compute(items):",
            "     total = 0",
            "-    for x in items:",
            "-        total += x",
            "+    valid = [x for x in items if x > 0]",
            "+    for x in valid:",
            "+        total += x * 2",
            "     log_total(total)",
            "-    return total",
            "+    return max(total, 0)",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/interleaved.py")
    assert f is not None
    assert f.valid_commentable_lines == {10, 11, 12, 13, 14, 15}
    coords = [(ln.kind, ln.left_line, ln.right_line) for ln in f.hunks[0].lines]
    assert coords == [
        ("context", 10, 10),
        ("delete", 11, None),
        ("delete", 12, None),
        ("add", None, 11),
        ("add", None, 12),
        ("add", None, 13),
        ("context", 13, 14),
        ("delete", 14, None),
        ("add", None, 15),
    ]
    assert f.is_range_commentable(11, 13) is True
    assert f.is_range_commentable(14, 15) is True


def test_deleted_lines_and_pure_delete_blocks_rejected_by_commentability_guard() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/trimmed.py b/src/trimmed.py",
            "--- a/src/trimmed.py",
            "+++ b/src/trimmed.py",
            "@@ -5,4 +5,2 @@ def trim():",
            "     keep_first()",
            "-    legacy_step_a()",
            "-    legacy_step_b()",
            "     keep_last()",
            "diff --git a/src/obsolete.py b/src/obsolete.py",
            "deleted file mode 100644",
            "--- a/src/obsolete.py",
            "+++ /dev/null",
            "@@ -1,3 +0,0 @@",
            "-def old():",
            "-    return 0",
            "-",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)

    trimmed = parsed.get_file("src/trimmed.py")
    assert trimmed is not None
    assert trimmed.valid_commentable_lines == {5, 6}
    assert trimmed.is_line_commentable(5) is True
    assert trimmed.is_line_commentable(6) is True
    assert trimmed.is_line_commentable(7) is False
    assert trimmed.is_line_commentable(8) is False
    assert trimmed.is_range_commentable(5, 7) is False

    obsolete = parsed.get_file("src/obsolete.py")
    assert obsolete is not None
    assert obsolete.change_type == "deleted"
    assert obsolete.valid_commentable_lines == set()
    assert obsolete.is_line_commentable(1) is False


def test_renamed_file_with_code_edits_maps_hunks_to_new_path() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/legacy_auth.py b/src/auth_v2.py",
            "similarity index 78%",
            "rename from src/legacy_auth.py",
            "rename to src/auth_v2.py",
            "index 1111111..2222222 100644",
            "--- a/src/legacy_auth.py",
            "+++ b/src/auth_v2.py",
            "@@ -2,2 +2,3 @@ def verify_signature(sig):",
            "     if not sig:",
            "+        log_missing_sig()",
            "         return False",
            "@@ -20,2 +21,2 @@ def issue_token():",
            "-    ttl = 300",
            "+    ttl = 600",
            "     return ttl",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/auth_v2.py")
    assert f is not None
    assert parsed.get_file("src/legacy_auth.py") is None
    assert f.change_type == "renamed"
    assert f.old_path == "src/legacy_auth.py"
    assert f.new_path == "src/auth_v2.py"
    assert f.valid_commentable_lines == {2, 3, 4, 21, 22}
    assert f.right_line_to_hunk == {2: 1, 3: 1, 4: 1, 21: 2, 22: 2}


def test_renamed_c_quoted_unicode_and_space_path_with_code_edits() -> None:
    diff_text = "\n".join(
        [
            'diff --git "a/src/c\\305\\251 dir/t\\341\\273\\207p.py" '
            '"b/src/m\\341\\273\\233i dir/t\\341\\273\\207p.py"',
            "similarity index 80%",
            'rename from "src/c\\305\\251 dir/t\\341\\273\\207p.py"',
            'rename to "src/m\\341\\273\\233i dir/t\\341\\273\\207p.py"',
            '--- "a/src/c\\305\\251 dir/t\\341\\273\\207p.py"',
            '+++ "b/src/m\\341\\273\\233i dir/t\\341\\273\\207p.py"',
            "@@ -4,2 +4,3 @@",
            " title = 'ok'",
            "+status = 'active'",
            " return title",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/mới dir/tệp.py")
    assert f is not None
    assert f.change_type == "renamed"
    assert f.old_path == "src/cũ dir/tệp.py"
    assert f.new_path == "src/mới dir/tệp.py"
    assert f.valid_commentable_lines == {4, 5, 6}


def test_copied_file_with_code_edits_maps_to_new_path() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/base_client.py b/src/async_client.py",
            "similarity index 82%",
            "copy from src/base_client.py",
            "copy to src/async_client.py",
            "--- a/src/base_client.py",
            "+++ b/src/async_client.py",
            "@@ -1,3 +1,4 @@",
            " import httpx",
            "+import asyncio",
            " ",
            " def ping():",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/async_client.py")
    assert f is not None
    assert f.change_type == "copied"
    assert f.old_path == "src/base_client.py"
    assert f.new_path == "src/async_client.py"
    assert f.valid_commentable_lines == {1, 2, 3, 4}


def test_multiple_far_apart_hunks_four_digit_padding_and_cross_hunk_rejection() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/monolith.py b/src/monolith.py",
            "--- a/src/monolith.py",
            "+++ b/src/monolith.py",
            "@@ -5,2 +5,3 @@ def head_fn():",
            "     x = 1",
            "+    y = 2",
            "     return x",
            "@@ -450,2 +451,2 @@ def mid_fn():",
            "-    mode = 'slow'",
            "+    mode = 'fast'",
            "     return mode",
            "@@ -1820,2 +1821,3 @@ def tail_fn():",
            "     emit()",
            "+    flush()",
            "     close()",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/monolith.py")
    assert f is not None
    assert len(f.hunks) == 3
    assert f.valid_commentable_lines == {5, 6, 7, 451, 452, 1821, 1822, 1823}
    assert f.right_line_to_hunk == {
        5: 1,
        6: 1,
        7: 1,
        451: 2,
        452: 2,
        1821: 3,
        1822: 3,
        1823: 3,
    }
    assert f.is_range_commentable(6, 451) is False
    assert f.is_range_commentable(451, 1821) is False
    assert f.is_range_commentable(1821, 1823) is True
    assert "L0005 |       x = 1" in f.annotated_diff
    assert "L0451 | +     mode = 'fast'" in f.annotated_diff
    assert "L1822 | +     flush()" in f.annotated_diff


def test_adjacent_hunks_with_one_line_untouched_gap_reject_gap_and_span() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/tight.py b/src/tight.py",
            "--- a/src/tight.py",
            "+++ b/src/tight.py",
            "@@ -13,3 +13,3 @@",
            " a = 1",
            "-b = 2",
            "+b = 20",
            " c = 3",
            "@@ -17,3 +17,3 @@",
            " e = 5",
            "-f = 6",
            "+f = 60",
            " g = 7",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/tight.py")
    assert f is not None
    assert 16 not in f.valid_commentable_lines
    assert f.is_line_commentable(16) is False
    assert f.is_range_commentable(15, 17) is False
    assert f.is_range_commentable(13, 15) is True
    assert f.is_range_commentable(17, 19) is True


def test_no_newline_at_eof_on_old_side_only() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/eof_old.py b/src/eof_old.py",
            "--- a/src/eof_old.py",
            "+++ b/src/eof_old.py",
            "@@ -10,2 +10,3 @@",
            " keep()",
            "-last()",
            "\\ No newline at end of file",
            "+last()",
            "+extra()",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/eof_old.py")
    assert f is not None
    assert f.valid_commentable_lines == {10, 11, 12}
    lines = f.hunks[0].lines
    assert [(ln.kind, ln.left_line, ln.right_line) for ln in lines] == [
        ("context", 10, 10),
        ("delete", 11, None),
        ("no_newline", None, None),
        ("add", None, 11),
        ("add", None, 12),
    ]


def test_no_newline_at_eof_on_new_side_only() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/eof_new.py b/src/eof_new.py",
            "--- a/src/eof_new.py",
            "+++ b/src/eof_new.py",
            "@@ -8,2 +8,2 @@",
            " first()",
            "-second()",
            "+second_no_nl()",
            "\\ No newline at end of file",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/eof_new.py")
    assert f is not None
    assert f.valid_commentable_lines == {8, 9}
    assert 10 not in f.valid_commentable_lines
    assert f.hunks[0].lines[3].kind == "no_newline"
    assert f.hunks[0].lines[3].right_line is None


def test_no_newline_at_eof_on_both_sides_after_prior_hunk() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/eof_both.py b/src/eof_both.py",
            "--- a/src/eof_both.py",
            "+++ b/src/eof_both.py",
            "@@ -2,2 +2,3 @@",
            " header = 1",
            "+mid = 2",
            " footer = 3",
            "@@ -20 +21 @@",
            "-END = False",
            "\\ No newline at end of file",
            "+END = True",
            "\\ No newline at end of file",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/eof_both.py")
    assert f is not None
    assert f.valid_commentable_lines == {2, 3, 4, 21}
    assert f.right_line_to_hunk == {2: 1, 3: 1, 4: 1, 21: 2}
    assert f.is_line_commentable(22) is False


def test_omitted_hunk_counts_old_only_new_only_and_both() -> None:
    hunk_lines = [
        "@@ -1 +1 @@",
        "-a = 1",
        "+a = 2",
        "@@ -5 +5,3 @@",
        "-b = 1",
        "+b = 2",
        "+c = 3",
        "+d = 4",
        "@@ -10,3 +12 @@",
        "-x = 1",
        "-y = 2",
        "-z = 3",
        "+xyz = 6",
    ]
    mapped = DiffHunkMapper().map_hunks(hunk_lines, file_path="src/counts.py")
    assert [(h.old_count, h.new_count) for h in mapped.hunks] == [
        (1, 1),
        (1, 3),
        (3, 1),
    ]
    assert mapped.valid_commentable_lines == {1, 5, 6, 7, 12}
    assert mapped.right_line_to_hunk == {1: 1, 5: 2, 6: 2, 7: 2, 12: 3}


def test_pure_deletion_hunk_followed_by_shifted_modification_hunk() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/prune.py b/src/prune.py",
            "--- a/src/prune.py",
            "+++ b/src/prune.py",
            "@@ -30,4 +29,0 @@ def dead_block():",
            "-    a = 1",
            "-    b = 2",
            "-    c = 3",
            "-    return a + b + c",
            "@@ -60,2 +56,3 @@ def live_block():",
            "     step_one()",
            "+    step_one_half()",
            "     step_two()",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/prune.py")
    assert f is not None
    assert len(f.hunks) == 2
    assert f.valid_commentable_lines == {56, 57, 58}
    assert f.right_line_to_hunk == {56: 2, 57: 2, 58: 2}
    assert all(ln.right_line is None for ln in f.hunks[0].lines)


def test_zero_context_pure_insertion_hunk() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/u0.py b/src/u0.py",
            "--- a/src/u0.py",
            "+++ b/src/u0.py",
            "@@ -10,0 +11,3 @@",
            "+def injected_one():",
            "+    pass",
            "+",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/u0.py")
    assert f is not None
    assert f.hunks[0].old_count == 0
    assert f.hunks[0].new_count == 3
    assert f.valid_commentable_lines == {11, 12, 13}
    assert f.is_line_commentable(10) is False


def test_stripped_empty_context_lines_inside_hunk_advance_both_cursors() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/blank_ctx.py b/src/blank_ctx.py",
            "--- a/src/blank_ctx.py",
            "+++ b/src/blank_ctx.py",
            "@@ -1,3 +1,4 @@",
            " x = 1",
            "",
            "+y = 2",
            " z = 3",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/blank_ctx.py")
    assert f is not None
    assert [(ln.kind, ln.left_line, ln.right_line) for ln in f.hunks[0].lines] == [
        ("context", 1, 1),
        ("context", 2, 2),
        ("add", None, 3),
        ("context", 3, 4),
    ]
    assert f.valid_commentable_lines == {1, 2, 3, 4}


def test_mode_only_change_produces_zero_hunks_and_empty_commentable_set() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/scripts/run.sh b/scripts/run.sh",
            "old mode 100644",
            "new mode 100755",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("scripts/run.sh")
    assert f is not None
    assert f.change_type == "modified"
    assert f.hunks == ()
    assert f.valid_commentable_lines == set()
    assert f.right_line_to_hunk == {}


def test_crlf_diff_with_no_newline_marker_preserves_coordinates() -> None:
    crlf_diff = (
        "diff --git a/src/win_eof.py b/src/win_eof.py\r\n"
        "--- a/src/win_eof.py\r\n"
        "+++ b/src/win_eof.py\r\n"
        "@@ -4,2 +4,3 @@\r\n"
        " line_4()\r\n"
        "-line_5()\r\n"
        "\\ No newline at end of file\r\n"
        "+line_5_new()\r\n"
        "+line_6_new()\r\n"
        "\\ No newline at end of file\r\n"
    )
    parsed = SmartDiffParser().parse_diff(crlf_diff)
    f = parsed.get_file("src/win_eof.py")
    assert f is not None
    assert f.valid_commentable_lines == {4, 5, 6}
    assert all("\r" not in ln.content for ln in f.hunks[0].lines)


def test_code_lines_starting_with_diff_headers_inside_hunk_body() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/tricky.py b/src/tricky.py",
            "--- a/src/tricky.py",
            "+++ b/src/tricky.py",
            "@@ -1,3 +1,5 @@",
            " start = 1",
            "---b",
            "+++c",
            "+@@ -1 +1 @@",
            "+diff --git a/fake.py b/fake.py",
            " end = 2",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    assert len(parsed.files) == 1
    f = parsed.get_file("src/tricky.py")
    assert f is not None
    assert f.valid_commentable_lines == {1, 2, 3, 4, 5}
    assert [ln.kind for ln in f.hunks[0].lines] == [
        "context",
        "delete",
        "add",
        "add",
        "add",
        "context",
    ]
    assert f.hunks[0].lines[2].content == "++c"
    assert f.hunks[0].lines[3].content == "@@ -1 +1 @@"
    assert f.hunks[0].lines[4].content == "diff --git a/fake.py b/fake.py"


def test_parse_patch_entries_parity_with_parse_diff_across_statuses_and_noise_filters() -> None:
    cfg = RepoConfig(ignore_paths=("docs/api/**",))
    parser = SmartDiffParser(file_filter=FileFilter.from_repo_config(cfg))

    raw_diff = "\n".join(
        [
            "diff --git a/src/added.py b/src/added.py",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/src/added.py",
            "@@ -0,0 +1,2 @@",
            "+def a():",
            "+    return 1",
            "diff --git a/src/old_mod.py b/src/new_mod.py",
            "similarity index 90%",
            "rename from src/old_mod.py",
            "rename to src/new_mod.py",
            "--- a/src/old_mod.py",
            "+++ b/src/new_mod.py",
            "@@ -3,2 +3,2 @@",
            "-v = 1",
            "+v = 2",
            " return v",
            "diff --git a/src/gone.py b/src/gone.py",
            "deleted file mode 100644",
            "--- a/src/gone.py",
            "+++ /dev/null",
            "@@ -1,2 +0,0 @@",
            "-old = 1",
            "-old2 = 2",
            "diff --git a/poetry.lock b/poetry.lock",
            "--- a/poetry.lock",
            "+++ b/poetry.lock",
            "@@ -1 +1 @@",
            "-a",
            "+b",
            "diff --git a/app/migrations/0002.py b/app/migrations/0002.py",
            "--- a/app/migrations/0002.py",
            "+++ b/app/migrations/0002.py",
            "@@ -1 +1 @@",
            "-op1",
            "+op2",
            "diff --git a/docs/api/openapi.yaml b/docs/api/openapi.yaml",
            "--- a/docs/api/openapi.yaml",
            "+++ b/docs/api/openapi.yaml",
            "@@ -1 +1 @@",
            "-v1",
            "+v2",
        ]
    )

    entries = [
        PatchFileEntry(
            filename="src/added.py",
            status="added",
            patch="@@ -0,0 +1,2 @@\n+def a():\n+    return 1\n",
        ),
        PatchFileEntry(
            filename="src/new_mod.py",
            previous_filename="src/old_mod.py",
            status="renamed",
            patch="@@ -3,2 +3,2 @@\n-v = 1\n+v = 2\n return v\n",
        ),
        PatchFileEntry(
            filename="src/gone.py",
            status="removed",
            patch="@@ -1,2 +0,0 @@\n-old = 1\n-old2 = 2\n",
        ),
        PatchFileEntry(
            filename="poetry.lock",
            status="modified",
            patch="@@ -1 +1 @@\n-a\n+b\n",
        ),
        PatchFileEntry(
            filename="app/migrations/0002.py",
            status="modified",
            patch="@@ -1 +1 @@\n-op1\n+op2\n",
        ),
        PatchFileEntry(
            filename="docs/api/openapi.yaml",
            status="modified",
            patch="@@ -1 +1 @@\n-v1\n+v2\n",
        ),
    ]

    from_raw = parser.parse_diff(raw_diff)
    from_entries = parser.parse_patch_entries(entries)

    assert [f.file_path for f in from_raw.files] == [f.file_path for f in from_entries.files]
    assert {f.file_path: f.valid_commentable_lines for f in from_raw.files} == {
        f.file_path: f.valid_commentable_lines for f in from_entries.files
    }
    assert {f.file_path: f.right_line_to_hunk for f in from_raw.files} == {
        f.file_path: f.right_line_to_hunk for f in from_entries.files
    }
    assert {r.file_path: r.reason for r in from_entries.skipped_files} == {
        "poetry.lock": FilterReason.LOCKFILE,
        "app/migrations/0002.py": FilterReason.MIGRATION,
        "docs/api/openapi.yaml": FilterReason.CUSTOM_IGNORE,
    }


def test_five_digit_line_numbers_and_guard_script_oracle_agreement() -> None:
    guard = load_guard_validator()
    diff_text = "\n".join(
        [
            "diff --git a/src/huge.py b/src/huge.py",
            "--- a/src/huge.py",
            "+++ b/src/huge.py",
            "@@ -12000,2 +12000,4 @@ def far_down():",
            "     keep_top()",
            "+    inserted_a()",
            "+    inserted_b()",
            "     keep_bottom()",
            "@@ -12050 +12052 @@",
            "-    old_end()",
            "\\ No newline at end of file",
            "+    new_end()",
            "\\ No newline at end of file",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/huge.py")
    assert f is not None
    assert f.valid_commentable_lines == {12000, 12001, 12002, 12003, 12052}
    assert "L12000 |       keep_top()" in f.annotated_diff
    assert "L12052 | +     new_end()" in f.annotated_diff

    oracle_map = guard.map_right_hunk_lines(diff_text)
    assert f.right_line_to_hunk == oracle_map["src/huge.py"]
    payload = [
        {"path": "src/huge.py", "line": 12052, "side": "RIGHT", "body": "ok single"},
        {
            "path": "src/huge.py",
            "start_line": 12001,
            "start_side": "RIGHT",
            "line": 12002,
            "side": "RIGHT",
            "body": "ok multi",
        },
    ]
    assert guard.validate_review_comments(oracle_map, payload) == []
