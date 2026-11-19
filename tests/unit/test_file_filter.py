"""Unit tests for FileFilter exclusion rules, POSIX globs, binary detection, and FilterMetrics."""

from __future__ import annotations

import io
import json
import time
import warnings

import pytest

from codesight.config_reader.repo_config import RepoConfig
from codesight.core.logging import configure_logging
from codesight.diff.file_filter import (
    FileFilter,
    compile_posix_glob,
    estimate_tokens,
    is_binary_patch,
    normalize_diff_path,
)
from codesight.diff.models import FilterMetrics, FilterReason, SkippedFileRecord


@pytest.mark.parametrize(
    ("file_path", "expected_reason"),
    [
        # Lockfiles
        ("package-lock.json", FilterReason.LOCKFILE),
        ("apps/web/pnpm-lock.yaml", FilterReason.LOCKFILE),
        ("uv.lock", FilterReason.LOCKFILE),
        ("poetry.lock", FilterReason.LOCKFILE),
        ("yarn.lock", FilterReason.LOCKFILE),
        ("bun.lock", FilterReason.LOCKFILE),
        ("bun.lockb", FilterReason.LOCKFILE),
        ("Pipfile.lock", FilterReason.LOCKFILE),
        ("Cargo.lock", FilterReason.LOCKFILE),
        ("go.sum", FilterReason.LOCKFILE),
        ("composer.lock", FilterReason.LOCKFILE),
        ("Gemfile.lock", FilterReason.LOCKFILE),
        ("npm-shrinkwrap.json", FilterReason.LOCKFILE),
        # Assets (including uppercase extensions)
        ("assets/logo.svg", FilterReason.ASSET),
        ("assets/Logo.SVG", FilterReason.ASSET),
        ("public/banner.png", FilterReason.ASSET),
        ("public/HERO.PNG", FilterReason.ASSET),
        ("docs/spec.pdf", FilterReason.ASSET),
        ("fonts/inter.woff2", FilterReason.ASSET),
        ("releases/archive.zip", FilterReason.ASSET),
        # Minified & Source Maps (including uppercase extensions)
        ("static/app.min.js", FilterReason.MINIFIED),
        ("static/app.MIN.JS", FilterReason.MINIFIED),
        ("static/styles.min.css", FilterReason.MINIFIED),
        ("dist_old/vendor.bundle.js", FilterReason.MINIFIED),
        ("static/app.js.map", FilterReason.MINIFIED),
        # Build / Vendor / Virtualenv (root and nested monorepo paths)
        ("dist/index.js", FilterReason.BUILD_OR_VENDOR),
        ("dist/nested/chunk.js", FilterReason.BUILD_OR_VENDOR),
        ("packages/a/dist/x.js", FilterReason.BUILD_OR_VENDOR),
        ("build/output.js", FilterReason.BUILD_OR_VENDOR),
        ("apps/web/build/main.js", FilterReason.BUILD_OR_VENDOR),
        ("apps/web/.next/server/app.js", FilterReason.BUILD_OR_VENDOR),
        ("node_modules/pkg/index.js", FilterReason.BUILD_OR_VENDOR),
        ("a/b/node_modules/pkg/index.js", FilterReason.BUILD_OR_VENDOR),
        ("vendor/lib.py", FilterReason.BUILD_OR_VENDOR),
        (".venv/lib/site.py", FilterReason.BUILD_OR_VENDOR),
        ("__pycache__/mod.cpython-312.pyc", FilterReason.BUILD_OR_VENDOR),
        ("services/api/__pycache__/cache.tag", FilterReason.BUILD_OR_VENDOR),
        # Migrations
        ("migrations/0001_initial.py", FilterReason.MIGRATION),
        ("apps/users/migrations/0002_auto.py", FilterReason.MIGRATION),
        ("services/billing/migrations/versions/003.py", FilterReason.MIGRATION),
        # Generated & Snapshots
        ("protos/user_pb2.py", FilterReason.GENERATED),
        ("api/schema.generated.ts", FilterReason.GENERATED),
        ("tests/__snapshots__/Button.snap", FilterReason.GENERATED),
        ("root.snap", FilterReason.GENERATED),
    ],
)
def test_default_exclusions_skip_noise_files(
    file_path: str,
    expected_reason: FilterReason,
) -> None:
    flt = FileFilter()
    decision = flt.evaluate(file_path, "@@ -1 +1 @@\n+dummy\n")
    assert decision.should_skip is True
    assert decision.reason == expected_reason
    assert decision.matched_pattern is not None


@pytest.mark.parametrize(
    ("file_path", "patch_text", "is_binary", "expected_patch_is_binary"),
    [
        ("data/blob.custom", "@@ -1 +1 @@\n+text\n", True, False),
        (
            "notes/readme.txt",
            "Binary files a/notes/readme.txt and b/notes/readme.txt differ\n",
            False,
            True,
        ),
        (
            "assets/custom.bin",
            "GIT binary patch\nliteral 128\nHcmV?d00001\n",
            False,
            True,
        ),
        ("raw/payload.txt", "@@ -1 +1 @@\n+abc\x00def\n", False, True),
        (
            "notes/crlf.txt",
            "Binary files a/notes/crlf.txt and b/notes/crlf.txt differ\r\n",
            False,
            True,
        ),
    ],
)
def test_binary_patch_detection_skips_all_binary_forms(
    file_path: str,
    patch_text: str,
    is_binary: bool,
    expected_patch_is_binary: bool,
) -> None:
    flt = FileFilter()
    assert is_binary_patch(patch_text) is expected_patch_is_binary
    decision = flt.evaluate(file_path, patch_text, is_binary=is_binary)
    assert decision.should_skip is True
    assert decision.reason == FilterReason.BINARY


@pytest.mark.parametrize(
    "file_path",
    [
        "src/codesight/api/main.py",
        "frontend/src/components/RadarChart.tsx",
        "src/services/migration_service.py",
        "src/dist_utils.py",
        "tests/unit/test_auth.py",
        "alembic/env.py",
    ],
)
def test_real_source_files_have_zero_false_positives(file_path: str) -> None:
    flt = FileFilter()
    decision = flt.evaluate(file_path, "@@ -1,2 +1,2 @@\n def ok():\n+    return True\n")
    assert decision.should_skip is False
    assert decision.reason == FilterReason.KEPT
    assert decision.matched_pattern is None


def test_custom_repo_config_ignore_paths_and_posix_globs() -> None:
    cfg = RepoConfig(
        ignore_paths=(
            "docs/generated/**",
            "**/*.snap",
            "internal/legacy/*.py",
            "custom_dir/",
            "src/v?/handler_[ab].py",
            "checks/[!z]*.py",
        )
    )
    flt = FileFilter.from_repo_config(cfg)

    d1 = flt.evaluate("docs/generated/v1/api.py")
    assert d1.should_skip is True
    assert d1.reason == FilterReason.CUSTOM_IGNORE
    assert d1.matched_pattern == "docs/generated/**"

    d2 = flt.evaluate("docs/generated/index.md")
    assert d2.should_skip is True
    assert d2.reason == FilterReason.CUSTOM_IGNORE

    d3 = flt.evaluate("internal/legacy/old.py")
    assert d3.should_skip is True
    assert d3.reason == FilterReason.CUSTOM_IGNORE
    assert d3.matched_pattern == "internal/legacy/*.py"

    # Single-level * must not cross slashes
    d4 = flt.evaluate("internal/legacy/sub/keep.py")
    assert d4.should_skip is False
    assert d4.reason == FilterReason.KEPT

    # Trailing-slash directory glob
    d5 = flt.evaluate("custom_dir/sub/file.py")
    assert d5.should_skip is True
    assert d5.reason == FilterReason.CUSTOM_IGNORE
    assert d5.matched_pattern == "custom_dir/"

    # Windows slashes and leading ./ or /
    d6 = flt.evaluate(".\\docs\\generated\\v1\\api.py")
    assert d6.should_skip is True
    assert d6.reason == FilterReason.CUSTOM_IGNORE

    # ? and [...] classes
    assert flt.evaluate("src/v1/handler_a.py").should_skip is True
    assert flt.evaluate("src/v1/handler_c.py").should_skip is False
    assert flt.evaluate("checks/alpha.py").should_skip is True
    assert flt.evaluate("checks/zeta.py").should_skip is False

    # Empty path & normalize helper
    assert normalize_diff_path(" /./src\\pkg//mod.py ") == "src/pkg/mod.py"
    assert flt.evaluate("").should_skip is False
    assert FileFilter(ignore_paths=("**",)).evaluate("anything/at/all.py").should_skip is True
    assert FileFilter(ignore_paths=("**/**",)).evaluate("nested/file.py").should_skip is True

    # Middle ** and duplicate ** segments + bracket edge cases
    edge_flt = FileFilter(
        ignore_paths=(
            "a/**/b.py",
            "x/**/**/y.py",
            "br/[^x].py",
            "br/[]z].py",
            "br/unclosed[a.py",
        )
    )
    assert edge_flt.evaluate("a/b.py").should_skip is True
    assert edge_flt.evaluate("a/mid/deep/b.py").should_skip is True
    assert edge_flt.evaluate("x/m/y.py").should_skip is True
    assert edge_flt.evaluate("br/^.py").should_skip is True
    assert edge_flt.evaluate("br/x.py").should_skip is True
    assert edge_flt.evaluate("br/a.py").should_skip is False
    assert edge_flt.evaluate("br/].py").should_skip is True
    assert edge_flt.evaluate("br/unclosed[a.py").should_skip is True

    # Case-insensitive range validation preserves raw ASCII range [Z-a] (90..97)
    ci_range = compile_posix_glob("src/[Z-a].py", ignore_case=True)
    assert ci_range.matches("src/_.py") is True
    assert ci_range.matches("src/z.py") is True
    assert ci_range.matches("src/A.py") is True
    assert ci_range.matches("src/b.py") is False
    with pytest.raises(ValueError):
        compile_posix_glob("src/[z-a].py", ignore_case=True)


@pytest.mark.parametrize("bad_pattern", ["", "   ", "/", "./", "bad\x00pat", "[z-a].py"])
def test_invalid_custom_glob_patterns_raise_value_error(bad_pattern: str) -> None:
    with pytest.raises(ValueError):
        FileFilter(ignore_paths=(bad_pattern,))


def test_from_repo_config_skips_corrupt_glob_without_leaking_pattern() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", service_name="test-file-filter", stream=stream)
    bypassed_cfg = RepoConfig.model_construct(
        ignore_paths=("[z-a]_SECRET.py", "docs/generated/**"),
    )
    flt = FileFilter.from_repo_config(bypassed_cfg)
    assert flt.evaluate("docs/generated/v1/client.py").should_skip is True
    assert flt.evaluate("src/main.py").should_skip is False

    raw_logs = stream.getvalue()
    assert "[z-a]_SECRET.py" not in raw_logs
    logs = [json.loads(line) for line in raw_logs.splitlines() if line.strip()]
    assert any(
        entry["event"] == "file_filter_invalid_ignore_pattern_skipped"
        and entry["pattern_index"] == 0
        for entry in logs
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        posix_flt = FileFilter(ignore_paths=("src/[[:alpha:]]*.py",))
        assert posix_flt.evaluate("src/a]test.py").should_skip is True
        assert posix_flt.evaluate("src/normal.py").should_skip is False
    assert caught == []


def test_pathological_redos_globs_run_in_linear_time() -> None:
    deep_glob = "**/" + "/".join(["*a*"] * 7) + "/zz"
    star_glob = "*a*a*a*a*a*a*a*a*b"
    flt = FileFilter(ignore_paths=(deep_glob, star_glob))

    deep_path = "/".join(["aaaaaaaa"] * 30)
    long_name = "a" * 36 + ".py"

    start = time.perf_counter()
    assert flt.evaluate(deep_path).should_skip is False
    assert flt.evaluate(long_name).should_skip is False
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"Glob evaluation took {elapsed:.3f}s (ReDoS regression)"


def test_estimate_tokens_and_filter_metrics_accuracy() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens(b"") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    # "á" in UTF-8 is 2 bytes; "báo" is 4 UTF-8 bytes -> 1 token
    assert estimate_tokens("báo") == 1
    assert estimate_tokens("báo cáo") == 3  # 9 UTF-8 bytes -> ceil(9/4) = 3

    kept_patch = "@@ -1,2 +1,3 @@\n def f():\n+    return 1\n"
    skipped_patch = "\n".join(f'+  "pkg-{i}": "1.0.{i}",' for i in range(40)) + "\n"

    flt = FileFilter()
    empty_metrics = flt.compute_metrics(kept_patches=[], skipped_records=[])
    assert empty_metrics == FilterMetrics()
    assert empty_metrics.token_reduction_ratio == 0.0

    skipped_decision = flt.evaluate("package-lock.json", skipped_patch)
    assert skipped_decision.should_skip is True
    skipped_record = SkippedFileRecord(
        file_path="package-lock.json",
        reason=skipped_decision.reason,
        matched_pattern=skipped_decision.matched_pattern,
        raw_lines=len(skipped_patch.splitlines()),
        raw_bytes=len(skipped_patch.encode("utf-8")),
        estimated_tokens=estimate_tokens(skipped_patch),
    )

    metrics = flt.compute_metrics(
        kept_patches=[kept_patch],
        skipped_records=[skipped_record],
    )
    assert metrics.total_files == 2
    assert metrics.kept_files == 1
    assert metrics.skipped_files == 1
    assert metrics.skipped_by_reason == {"lockfile": 1}
    assert metrics.raw_lines == metrics.kept_lines + metrics.skipped_lines
    assert metrics.raw_bytes == metrics.kept_bytes + metrics.skipped_bytes
    assert (
        metrics.raw_estimated_tokens
        == metrics.kept_estimated_tokens + metrics.saved_estimated_tokens
    )
    expected_pct = round(
        (metrics.saved_estimated_tokens / metrics.raw_estimated_tokens) * 100.0,
        2,
    )
    assert metrics.tokens_saved_pct == expected_pct
    assert metrics.tokens_saved_pct > 80.0
    assert 0.8 < metrics.token_reduction_ratio <= 1.0


def test_char_class_ignore_case_handles_unicode_multi_char_upper() -> None:
    from codesight.diff.file_filter import compile_posix_glob

    matcher = compile_posix_glob("src/[a-z].py", ignore_case=True)
    assert matcher.matches("src/A.py") is True
    assert matcher.matches("src/ß.py") is False
