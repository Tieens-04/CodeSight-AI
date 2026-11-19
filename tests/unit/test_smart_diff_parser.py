"""Unit tests for DiffHunkMapper, SmartDiffParser, and GitHub RIGHT-line coordinate guard."""

from __future__ import annotations

import io
import json

from codesight.config_reader.repo_config import RepoConfig
from codesight.core.logging import configure_logging
from codesight.diff.file_filter import FileFilter
from codesight.diff.hunk_mapper import DiffHunkMapper
from codesight.diff.models import FilterReason, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser, unquote_git_path
from tests.conftest import load_guard_validator as _load_guard_validator


def test_multi_hunk_mapping_annotated_lines_and_commentability_guards() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/service.py b/src/service.py",
            "index 1111111..2222222 100644",
            "--- a/src/service.py",
            "+++ b/src/service.py",
            "@@ -10,3 +10,4 @@ def validate(token):",
            "     if not token:",
            "-        return False",
            "+        raise ValueError('empty')",
            "+    audit_token(token)",
            "     return True",
            "@@ -40,2 +41,3 @@ def run():",
            "     start()",
            "+    track()",
            "     stop()",
        ]
    )

    parser = SmartDiffParser()
    parsed = parser.parse_diff(diff_text)
    assert len(parsed.files) == 1
    f = parsed.get_file("src/service.py")
    assert f is not None
    assert parsed.get_file("src/missing.py") is None
    assert f.change_type == "modified"
    assert len(f.hunks) == 2

    # Hunk 1 RIGHT lines: 10 (context), 11 (+), 12 (+), 13 (context)
    # Hunk 2 RIGHT lines: 41 (context), 42 (+), 43 (context)
    assert f.valid_commentable_lines == {10, 11, 12, 13, 41, 42, 43}
    assert f.right_line_to_hunk == {
        10: 1,
        11: 1,
        12: 1,
        13: 1,
        41: 2,
        42: 2,
        43: 2,
    }

    # Commentability helpers (Zero HTTP 422)
    assert f.is_line_commentable(11) is True
    assert f.is_line_commentable(25) is False
    assert f.is_line_commentable(True) is False
    assert f.is_range_commentable(11, 12) is True
    assert f.is_range_commentable(11, 11) is False  # start_line == line -> 422
    assert f.is_range_commentable(12, 11) is False
    assert f.is_range_commentable(11, 42) is False  # cross-hunk -> 422
    assert f.is_range_commentable(True, 12) is False

    # LLM line prefixes: RIGHT lines get L0xx, LEFT-only deleted lines get blank label
    annotated_lines = f.hunks[0].annotated_text.splitlines()
    assert annotated_lines == [
        "@@ -10,3 +10,4 @@ def validate(token):",
        "L010 |       if not token:",
        "     | -         return False",
        "L011 | +         raise ValueError('empty')",
        "L012 | +     audit_token(token)",
        "L013 |       return True",
    ]
    assert "L042 | +     track()" in f.annotated_diff


def test_omitted_count_and_added_or_deleted_zero_count_hunks() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/single.py b/src/single.py",
            "--- a/src/single.py",
            "+++ b/src/single.py",
            "@@ -10 +10 @@",
            "-old_val = 1",
            "+new_val = 2",
            "diff --git a/src/new_file.py b/src/new_file.py",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/src/new_file.py",
            "@@ -0,0 +1,2 @@",
            "+def hello() -> str:",
            "+    return 'hi'",
            "diff --git a/src/removed.py b/src/removed.py",
            "deleted file mode 100644",
            "--- a/src/removed.py",
            "+++ /dev/null",
            "@@ -1,3 +0,0 @@",
            "-line1 = 1",
            "-line2 = 2",
            "-line3 = 3",
        ]
    )

    parsed = SmartDiffParser().parse_diff(diff_text)
    single = parsed.get_file("src/single.py")
    assert single is not None
    assert single.hunks[0].old_count == 1
    assert single.hunks[0].new_count == 1
    assert single.valid_commentable_lines == {10}

    added = parsed.get_file("src/new_file.py")
    assert added is not None
    assert added.change_type == "added"
    assert added.old_path is None
    assert added.new_path == "src/new_file.py"
    assert added.valid_commentable_lines == {1, 2}

    removed = parsed.get_file("src/removed.py")
    assert removed is not None
    assert removed.change_type == "deleted"
    assert removed.old_path == "src/removed.py"
    assert removed.new_path is None
    assert removed.valid_commentable_lines == set()
    assert removed.right_line_to_hunk == {}


def test_no_newline_marker_never_shifts_right_line_coordinates() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/eof.py b/src/eof.py",
            "--- a/src/eof.py",
            "+++ b/src/eof.py",
            "@@ -10 +10 @@",
            "-old_flag = False",
            "\\ No newline at end of file",
            "+old_flag = True",
            "\\ No newline at end of file",
            "@@ -20,2 +20,3 @@ def next_fn():",
            "     keep_a()",
            "+    added_b()",
            "     keep_c()",
            "\\ No newline at end of file",
        ]
    )

    parsed = SmartDiffParser().parse_diff(diff_text)
    f = parsed.get_file("src/eof.py")
    assert f is not None
    assert f.valid_commentable_lines == {10, 20, 21, 22}
    assert f.right_line_to_hunk == {10: 1, 20: 2, 21: 2, 22: 2}
    assert f.hunks[0].lines[1].kind == "no_newline"
    assert f.hunks[0].lines[1].left_line is None
    assert f.hunks[0].lines[1].right_line is None
    assert 23 not in f.valid_commentable_lines


def test_adversarial_triple_dash_plus_and_diff_text_inside_hunk_body() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/sql_helper.py b/src/sql_helper.py",
            "--- a/src/sql_helper.py",
            "+++ b/src/sql_helper.py",
            "@@ -5,3 +5,5 @@",
            " def query():",
            "--- legacy sql comment",
            "+++modern comment",
            "+    banner = 'diff --git a/fake b/fake'",
            "+    return banner",
            "     pass",
        ]
    )

    parsed = SmartDiffParser().parse_diff(diff_text)
    assert len(parsed.files) == 1
    f = parsed.get_file("src/sql_helper.py")
    assert f is not None
    assert f.valid_commentable_lines == {5, 6, 7, 8, 9}
    kinds = [line.kind for line in f.hunks[0].lines]
    assert kinds == ["context", "delete", "add", "add", "add", "context"]
    assert f.hunks[0].lines[1].content == "-- legacy sql comment"
    assert f.hunks[0].lines[2].content == "++modern comment"
    assert "L006 | + ++modern comment" in f.annotated_diff


def test_large_line_numbers_over_1000_pad_consistently() -> None:
    hunk_lines = [
        "@@ -999,3 +999,4 @@ def big_file():",
        "     before_1000()",
        "-    old_line()",
        "+    line_1000()",
        "+    line_1001()",
        "     after_1001()",
    ]
    mapper = DiffHunkMapper()
    mapped = mapper.map_hunks(
        hunk_lines,
        file_path="src/big.py",
        old_path="src/big.py",
        new_path="src/big.py",
        change_type="modified",
    )
    assert mapped.valid_commentable_lines == {999, 1000, 1001, 1002}
    assert mapped.hunks[0].annotated_text.splitlines() == [
        "@@ -999,3 +999,4 @@ def big_file():",
        "L0999 |       before_1000()",
        "      | -     old_line()",
        "L1000 | +     line_1000()",
        "L1001 | +     line_1001()",
        "L1002 |       after_1001()",
    ]


def test_renamed_files_and_c_quoted_paths_with_spaces_and_octal_utf8() -> None:
    assert unquote_git_path('"b/src/my module.py"') == "src/my module.py"
    assert unquote_git_path('"b/src/b\\303\\241o_c\\303\\241o.py"') == "src/báo_cáo.py"
    assert unquote_git_path("a/src/plain.py\t2026-11-17 00:00:00") == "src/plain.py"

    diff_text = "\n".join(
        [
            'diff --git "a/src/old dir/my file.py" "b/src/new dir/my file.py"',
            "similarity index 85%",
            'rename from "src/old dir/my file.py"',
            'rename to "src/new dir/my file.py"',
            '--- "a/src/old dir/my file.py"',
            '+++ "b/src/new dir/my file.py"',
            "@@ -1,2 +1,2 @@",
            " x = 1",
            "-y = 2",
            "+y = 3",
            "diff --git a/src/pure_old.py b/src/pure_new.py",
            "similarity index 100%",
            "rename from src/pure_old.py",
            "rename to src/pure_new.py",
            'diff --git "a/src/b\\303\\241o_c\\303\\241o.py" "b/src/b\\303\\241o_c\\303\\241o.py"',
            '--- "a/src/b\\303\\241o_c\\303\\241o.py"',
            '+++ "b/src/b\\303\\241o_c\\303\\241o.py"',
            "@@ -1 +1 @@",
            "-a = 1",
            "+a = 2",
        ]
    )

    parsed = SmartDiffParser().parse_diff(diff_text)
    renamed = parsed.get_file("src/new dir/my file.py")
    assert renamed is not None
    assert renamed.change_type == "renamed"
    assert renamed.old_path == "src/old dir/my file.py"
    assert renamed.new_path == "src/new dir/my file.py"
    assert renamed.valid_commentable_lines == {1, 2}

    pure = parsed.get_file("src/pure_new.py")
    assert pure is not None
    assert pure.change_type == "renamed"
    assert pure.hunks == ()
    assert pure.valid_commentable_lines == set()

    utf8_file = parsed.get_file("src/báo_cáo.py")
    assert utf8_file is not None
    assert utf8_file.valid_commentable_lines == {1}


def test_end_to_end_filter_and_parser_matches_github_guard_validator() -> None:
    guard = _load_guard_validator()
    cfg = RepoConfig(ignore_paths=("docs/generated/**",))
    parser = SmartDiffParser(file_filter=FileFilter.from_repo_config(cfg))

    lock_lines = "\n".join(f'+    "dep-{i}": "1.0.{i}",' for i in range(60))
    raw_diff = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1,2 +1,62 @@",
            " {",
            lock_lines,
            " }",
            "diff --git a/assets/icon.svg b/assets/icon.svg",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/assets/icon.svg",
            "@@ -0,0 +1,1 @@",
            "+<svg></svg>",
            "diff --git a/static/bundle.min.js b/static/bundle.min.js",
            "--- a/static/bundle.min.js",
            "+++ b/static/bundle.min.js",
            "@@ -1 +1 @@",
            "-console.log(1)",
            "+console.log(2)",
            "diff --git a/assets/font.bin b/assets/font.bin",
            "GIT binary patch",
            "literal 32",
            "HcmV?d00001",
            "",
            "diff --git a/docs/generated/sdk.py b/docs/generated/sdk.py",
            "--- a/docs/generated/sdk.py",
            "+++ b/docs/generated/sdk.py",
            "@@ -1 +1 @@",
            "-CLIENT = 1",
            "+CLIENT = 2",
            "diff --git a/src/removed.py b/src/removed.py",
            "deleted file mode 100644",
            "--- a/src/removed.py",
            "+++ /dev/null",
            "@@ -1,1 +0,0 @@",
            "-x = 1",
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
        ]
    )

    stream = io.StringIO()
    configure_logging(level="DEBUG", service_name="test-diff-parser", stream=stream)

    result = parser.parse_diff(raw_diff)
    assert result.metrics.total_files == 7
    assert result.metrics.skipped_files == 5
    assert result.metrics.kept_files == 2
    assert result.metrics.tokens_saved_pct > 50.0
    assert {r.file_path: r.reason for r in result.skipped_files} == {
        "package-lock.json": FilterReason.LOCKFILE,
        "assets/icon.svg": FilterReason.ASSET,
        "static/bundle.min.js": FilterReason.MINIFIED,
        "assets/font.bin": FilterReason.BINARY,
        "docs/generated/sdk.py": FilterReason.CUSTOM_IGNORE,
    }

    # Verify NFR-3: raw source code/diff lines never leaked into structured logs
    raw_logs = stream.getvalue()
    assert "raise ValueError" not in raw_logs
    logs = [json.loads(line) for line in raw_logs.splitlines() if line.strip()]
    assert any(entry["event"] == "diff_parsed" and entry["skipped_files"] == 5 for entry in logs)

    # Cross-check exact 1:1 coordinate parity against github-diff-suggestion-guard
    reference_map = guard.map_right_hunk_lines(raw_diff)
    auth_only_diff = (
        "diff --git a/src/auth.py b/src/auth.py"
        + raw_diff.split("diff --git a/src/auth.py b/src/auth.py", 1)[1]
    )
    auth_only_map = guard.map_right_hunk_lines(auth_only_diff)
    auth_file = result.get_file("src/auth.py")
    assert auth_file is not None
    assert auth_file.valid_commentable_lines == set(reference_map["src/auth.py"].keys())
    assert auth_file.right_line_to_hunk == auth_only_map["src/auth.py"]
    assert auth_file.is_range_commentable(10, 21) is False

    # Comments allowed by ParsedFileDiff must pass validate_review_comments with 0 errors
    candidate_comments = [
        {"path": "src/auth.py", "line": 10, "side": "RIGHT", "body": "ok single"},
        {
            "path": "src/auth.py",
            "start_line": 21,
            "start_side": "RIGHT",
            "line": 22,
            "side": "RIGHT",
            "body": "ok multi",
        },
    ]
    assert guard.validate_review_comments(reference_map, candidate_comments) == []
    cross_hunk_comment = [
        {
            "path": "src/auth.py",
            "start_line": 10,
            "start_side": "RIGHT",
            "line": 21,
            "side": "RIGHT",
            "body": "cross hunk",
        }
    ]
    assert len(guard.validate_review_comments(reference_map, cross_hunk_comment)) == 1


def test_parse_patch_entries_for_github_files_api() -> None:
    entries = [
        PatchFileEntry(
            filename="src/handler.py",
            status="modified",
            patch="@@ -1,2 +1,3 @@\n def ok():\n+    log()\n     return 1\n",
        ),
        PatchFileEntry(
            filename="uv.lock",
            status="modified",
            patch="@@ -1 +1 @@\n-a\n+b\n",
        ),
        PatchFileEntry(
            filename="assets/logo.png",
            status="added",
            patch=None,
            is_binary=True,
        ),
        PatchFileEntry(
            filename="src/renamed.py",
            previous_filename="src/legacy.py",
            status="renamed",
            patch=None,
        ),
        PatchFileEntry(
            filename="src/deleted.py",
            status="removed",
            patch="@@ -1,2 +0,0 @@\n-a = 1\n-b = 2\n",
        ),
    ]

    parsed = SmartDiffParser().parse_patch_entries(entries)
    assert parsed.metrics.total_files == 5
    assert parsed.metrics.skipped_files == 2
    assert parsed.metrics.kept_files == 3

    handler = parsed.get_file("src/handler.py")
    assert handler is not None
    assert handler.valid_commentable_lines == {1, 2, 3}

    renamed = parsed.get_file("src/renamed.py")
    assert renamed is not None
    assert renamed.change_type == "renamed"
    assert renamed.old_path == "src/legacy.py"
    assert renamed.hunks == ()

    deleted = parsed.get_file("src/deleted.py")
    assert deleted is not None
    assert deleted.change_type == "deleted"
    assert deleted.new_path is None
    assert deleted.valid_commentable_lines == set()

    copied_parsed = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/copy.py",
                previous_filename="src/orig.py",
                status="copied",
                patch="@@ -1 +1 @@\n-v = 1\n+v = 2\n\n",
            )
        ]
    )
    copied = copied_parsed.get_file("src/copy.py")
    assert copied is not None
    assert copied.change_type == "copied"
    assert copied.old_path == "src/orig.py"
    assert copied.valid_commentable_lines == {1}


def test_classic_unified_diff_copy_unicode_sep_and_trailing_blank_lines() -> None:
    # Empty diff returns empty result
    empty_res = SmartDiffParser().parse_diff("   \n\n")
    assert empty_res.files == ()
    assert empty_res.metrics.total_files == 0

    # Classic multi-file unified diff without `diff --git` headers, plus U+2028 inside code
    classic_diff = "\n".join(
        [
            "",
            "--- a/src/classic_one.py",
            "+++ b/src/classic_one.py",
            "@@ -1 +1 @@",
            "-sep = 'old'",
            "+sep = 'line\u2028break'",
            "--- a/src/classic_two.py",
            "+++ b/src/classic_two.py",
            "@@ -5,1 +5,2 @@",
            " keep = 1",
            "+extra = 2",
        ]
    )
    classic_parsed = SmartDiffParser().parse_diff(classic_diff)
    assert len(classic_parsed.files) == 2
    one = classic_parsed.get_file("src/classic_one.py")
    two = classic_parsed.get_file("src/classic_two.py")
    assert one is not None and one.valid_commentable_lines == {1}
    assert one.hunks[0].lines[1].content == "sep = 'line\u2028break'"
    assert two is not None and two.valid_commentable_lines == {5, 6}

    # Copy block, unquoted path with space containing ` b/`, and trailing blank separator line
    edge_git_diff = "\n".join(
        [
            "diff --git a/my b/mod.py b/my b/mod.py",
            "--- a/my b/mod.py",
            "+++ b/my b/mod.py",
            "@@ -1 +1 @@",
            "-a = 1",
            "+a = 2",
            "",
            'diff --git "a/src/orig.py" b/src/cloned.py',
            "copy from src/orig.py",
            "copy to src/cloned.py",
            "--- a/src/orig.py",
            "+++ b/src/cloned.py",
            "@@ -1 +1 @@",
            "-x = 1",
            "+x = 2",
        ]
    )
    edge_parsed = SmartDiffParser().parse_diff(edge_git_diff)
    space_file = edge_parsed.get_file("my b/mod.py")
    assert space_file is not None
    assert space_file.valid_commentable_lines == {1}

    cloned = edge_parsed.get_file("src/cloned.py")
    assert cloned is not None
    assert cloned.change_type == "copied"
    assert cloned.old_path == "src/orig.py"
    assert cloned.valid_commentable_lines == {1}

    # Direct DiffHunkMapper ignores preamble lines before first @@
    direct = DiffHunkMapper().map_hunks(
        ["preamble ignored", "@@ -1 +1 @@", "-a", "+b"],
        file_path="src/direct.py",
        old_path="src/direct.py",
        new_path="src/direct.py",
    )
    assert direct.valid_commentable_lines == {1}


def test_map_hunks_without_new_path_and_headerless_patch_preserve_commentable_lines() -> None:
    mapped = DiffHunkMapper().map_hunks(
        ["@@ -1,2 +1,2 @@", " a", "+b"],
        file_path="src/mod.py",
    )
    assert mapped.change_type == "modified"
    assert mapped.valid_commentable_lines == {1, 2}
    assert mapped.right_line_to_hunk == {1: 1, 2: 1}

    headerless = SmartDiffParser().parse_diff("@@ -1 +1 @@\n-a\n+b\n")
    assert len(headerless.files) == 1
    assert headerless.files[0].valid_commentable_lines == {1}


def test_parse_patch_entries_skips_patchless_binary_files_and_preserves_exact_paths() -> None:
    entries = [
        PatchFileEntry(
            filename="lib/native.wasm",
            status="modified",
            patch=None,
        ),
        PatchFileEntry(
            filename="notes/crlf.bin",
            status="modified",
            patch="Binary files a/notes/crlf.bin and b/notes/crlf.bin differ\r\n",
        ),
        PatchFileEntry(
            filename="package-lock.json",
            status="modified",
            patch=None,
        ),
        PatchFileEntry(
            filename="src/weird\\name.py",
            status="modified",
            patch="@@ -1 +1 @@\n-old = 1\n+new = 2\n",
        ),
    ]
    parsed = SmartDiffParser().parse_patch_entries(entries)
    assert parsed.metrics.total_files == 4
    assert parsed.metrics.skipped_files == 3
    assert parsed.metrics.kept_files == 1
    assert {r.file_path: r.reason for r in parsed.skipped_files} == {
        "lib/native.wasm": FilterReason.BINARY,
        "notes/crlf.bin": FilterReason.BINARY,
        "package-lock.json": FilterReason.LOCKFILE,
    }
    kept = parsed.get_file("src/weird\\name.py")
    assert kept is not None
    assert kept.file_path == "src/weird\\name.py"
    assert kept.valid_commentable_lines == {1}

    quoted_backslash_diff = "\n".join(
        [
            'diff --git "a/src/weird\\\\name.py" "b/src/weird\\\\name.py"',
            '--- "a/src/weird\\\\name.py"',
            '+++ "b/src/weird\\\\name.py"',
            "@@ -1 +1 @@",
            "-old = 1",
            "+new = 2",
        ]
    )
    git_parsed = SmartDiffParser().parse_diff(quoted_backslash_diff)
    git_kept = git_parsed.get_file("src/weird\\name.py")
    assert git_kept is not None
    assert git_kept.old_path == "src/weird\\name.py"
    assert git_kept.new_path == "src/weird\\name.py"


def test_rename_and_copy_inside_top_level_a_or_b_directories_keep_prefix() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/a/x.py b/a/y.py",
            "similarity index 100%",
            "rename from a/x.py",
            "rename to a/y.py",
            "diff --git a/b/u.py b/b/v.py",
            "similarity index 100%",
            "copy from b/u.py",
            "copy to b/v.py",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)
    renamed = parsed.get_file("a/y.py")
    assert renamed is not None
    assert renamed.change_type == "renamed"
    assert renamed.old_path == "a/x.py"
    assert renamed.new_path == "a/y.py"

    copied = parsed.get_file("b/v.py")
    assert copied is not None
    assert copied.change_type == "copied"
    assert copied.old_path == "b/u.py"
    assert copied.new_path == "b/v.py"


def test_classic_diff_binary_line_does_not_poison_adjacent_text_files() -> None:
    classic_diff = "\n".join(
        [
            "--- a/src/x.py",
            "+++ b/src/x.py",
            "@@ -1 +1 @@",
            "-a = 1",
            "+a = 2",
            "Binary files a/img.bin and b/img.bin differ",
            "--- a/src/y.py",
            "+++ b/src/y.py",
            "@@ -1 +1 @@",
            "-c = 3",
            "+c = 4",
        ]
    )
    parsed = SmartDiffParser().parse_diff(classic_diff)
    assert [f.file_path for f in parsed.files] == ["src/x.py", "src/y.py"]
    assert parsed.get_file("src/x.py") is not None
    assert parsed.get_file("src/y.py") is not None
    assert [(r.file_path, r.reason) for r in parsed.skipped_files] == [
        ("img.bin", FilterReason.BINARY)
    ]

    new_bin_git_diff = "\n".join(
        [
            "diff --git a/assets/raw.dat b/assets/raw.dat",
            "new file mode 100644",
            "Binary files /dev/null and b/assets/raw.dat differ",
        ]
    )
    bin_parsed = SmartDiffParser().parse_diff(new_bin_git_diff)
    assert bin_parsed.files == ()
    assert [(r.file_path, r.reason) for r in bin_parsed.skipped_files] == [
        ("assets/raw.dat", FilterReason.BINARY)
    ]

    diff_run_with_headers = "\n".join(
        [
            "diff -ruN a/img.bin b/img.bin",
            "Binary files a/img.bin and b/img.bin differ",
            "diff -ruN a/src/y.py b/src/y.py",
            "--- a/src/y.py",
            "+++ b/src/y.py",
            "@@ -1 +1 @@",
            "-c = 3",
            "+c = 4",
        ]
    )
    run_parsed = SmartDiffParser().parse_diff(diff_run_with_headers)
    assert [f.file_path for f in run_parsed.files] == ["src/y.py"]
    assert [(r.file_path, r.reason) for r in run_parsed.skipped_files] == [
        ("img.bin", FilterReason.BINARY)
    ]


def test_format_patch_trailer_crlf_deletion_only_hunk_and_at_in_section_header() -> None:
    format_patch = "\n".join(
        [
            "From 1111111111111111111111111111111111111111 Mon Sep 17 00:00:00 2001",
            "From: Dev <dev@example.com>",
            "Subject: [PATCH] Fix app",
            "",
            "--- note in commit message",
            "---",
            " src/app.py | 2 +-",
            " 2 files changed, 1 insertion(+), 1 deletion(-)",
            "",
            "diff --git a/src/app.py b/src/app.py",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -1,2 +1,2 @@ def f(): # @@ marker",
            "-a = 1",
            "+b = 2",
            " c = 3",
            "diff --git a/src/old_n.py b/src/new_n.py",
            "similarity index 100%",
            "rename from src/old_n.py",
            "rename to src/new_n.py",
            "-- ",
            "2.43.0",
        ]
    )
    fp_parsed = SmartDiffParser().parse_diff(format_patch)
    assert [f.file_path for f in fp_parsed.files] == ["src/app.py", "src/new_n.py"]
    assert fp_parsed.metrics.total_files == 2
    app_file = fp_parsed.get_file("src/app.py")
    assert app_file is not None
    assert app_file.valid_commentable_lines == {1, 2}
    assert app_file.hunks[0].section_header == "def f(): # @@ marker"
    assert len(app_file.hunks[0].lines) == 3
    assert "| - - " not in app_file.annotated_diff

    crlf_diff = (
        "diff --git a/src/crlf.py b/src/crlf.py\r\n"
        "--- a/src/crlf.py\r\n"
        "+++ b/src/crlf.py\r\n"
        "@@ -1,2 +1,2 @@\r\n"
        " line1\r\n"
        "-old2\r\n"
        "+new2\r\n"
    )
    crlf_parsed = SmartDiffParser().parse_diff(crlf_diff)
    crlf_file = crlf_parsed.get_file("src/crlf.py")
    assert crlf_file is not None
    assert crlf_file.valid_commentable_lines == {1, 2}

    del_hunk_diff = "\n".join(
        [
            "diff --git a/src/mixed.py b/src/mixed.py",
            "--- a/src/mixed.py",
            "+++ b/src/mixed.py",
            "@@ -5,2 +4,0 @@",
            "-dead_1()",
            "-dead_2()",
            "@@ -20,2 +18,3 @@",
            " keep_1()",
            "+added()",
            " keep_2()",
        ]
    )
    mixed_parsed = SmartDiffParser().parse_diff(del_hunk_diff)
    mixed_file = mixed_parsed.get_file("src/mixed.py")
    assert mixed_file is not None
    assert mixed_file.valid_commentable_lines == {18, 19, 20}
    assert mixed_file.right_line_to_hunk == {18: 2, 19: 2, 20: 2}


def test_zero_byte_added_and_deleted_files_clear_missing_side_path() -> None:
    diff_text = "\n".join(
        [
            "diff --git a/src/empty_del.py b/src/empty_del.py",
            "deleted file mode 100644",
            "index e69de29..0000000",
            "diff --git a/src/empty_add.py b/src/empty_add.py",
            "new file mode 100644",
            "index 0000000..e69de29",
        ]
    )
    parsed = SmartDiffParser().parse_diff(diff_text)

    empty_del = parsed.get_file("src/empty_del.py")
    assert empty_del is not None
    assert empty_del.change_type == "deleted"
    assert empty_del.old_path == "src/empty_del.py"
    assert empty_del.new_path is None
    assert empty_del.valid_commentable_lines == set()

    empty_add = parsed.get_file("src/empty_add.py")
    assert empty_add is not None
    assert empty_add.change_type == "added"
    assert empty_add.old_path is None
    assert empty_add.new_path == "src/empty_add.py"
    assert empty_add.valid_commentable_lines == set()


def test_patch_truncated_blob_sha_and_changed_right_lines_for_sprint2_ast() -> None:
    entries = [
        PatchFileEntry(
            filename="src/huge_module.py",
            status="modified",
            patch=None,
            changes=4500,
            blob_sha="aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        ),
        PatchFileEntry(
            filename="src/service.py",
            status="modified",
            blob_sha="9999888877776666555544443333222211110000",
            changes=2,
            patch=(
                "@@ -10,3 +10,4 @@ def run():\n"
                "     ctx_before()\n"
                "+    added_line()\n"
                "     ctx_after()\n"
                "@@ -30,2 +31,0 @@\n"
                "-    deleted_a()\n"
                "-    deleted_b()\n"
            ),
        ),
    ]
    parsed = SmartDiffParser().parse_patch_entries(entries)
    assert [(r.file_path, r.reason) for r in parsed.skipped_files] == [
        ("src/huge_module.py", FilterReason.PATCH_TRUNCATED)
    ]
    svc = parsed.get_file("src/service.py")
    assert svc is not None
    assert svc.blob_sha == "9999888877776666555544443333222211110000"
    assert svc.valid_commentable_lines == {10, 11, 12}
    # changed_right_lines excludes context lines 10 & 12, includes added 11 and pure-del anchor 31
    assert svc.changed_right_lines == {11, 31}
