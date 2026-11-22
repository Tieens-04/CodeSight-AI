"""Unit tests for PythonComplexityAnalyzer, delta CC, and test_companion_detector."""

from __future__ import annotations

import pytest

from codesight.ast_engine.complexity import (
    PythonComplexityAnalyzer,
    compute_complexity_delta,
    detect_missing_test_companion,
    test_companion_detector,
)
from codesight.diff.smart_diff_parser import SmartDiffParser


def test_python_complexity_analyzer_mccabe_rules_and_nested_isolation() -> None:
    source = """
from typing import Any

def flat_fn(a: int, b: int) -> int:
    return a + b

def branching_fn(items: list[int], flag: bool, code: int) -> int:
    total = 1 if flag else 0                    # +1 (conditional_expression)
    for item in items:                          # +1 (for_statement)
        if item > 10 and item < 100:            # +1 (if_statement) +1 (and)
            total += item
        elif item < 0 or item == -999:          # +1 (elif_clause) +1 (or)
            continue
        else:                                   # +0
            pass
    while total > 500:                          # +1 (while_statement)
        total -= 10
    try:
        evens = [x for x in items if x % 2 == 0]  # +1 (for_in_clause) +1 (if_clause)
    except ValueError:                          # +1 (except_clause)
        evens = []
    finally:                                    # +0
        pass
    try:
        evens = list(evens)
    except* TypeError:                          # +1 (except_clause / except*)
        evens = []
    match code:
        case 200:                               # +1 (case_clause)
            return len(evens)
        case _ if flag:                         # +1 (guarded wildcard case_clause)
            return -1
        case _:                                 # +0 (bare irrefutable wildcard)
            return 0

def outer_with_nested(x: int) -> int:
    def inner(y: int) -> int:
        if y > 0:
            return 1
        elif y < 0:
            return -1
        for i in range(3):
            y += i
        return y if y else 0
    return inner(x)

async def async_control_flow(stream: Any, ctx: Any) -> int:
    count = 0
    async for chunk in stream:                  # +1 (for_statement), depth 1
        async with ctx() as handle:             # depth 2
            if handle:                          # +1 (if_statement), depth 3
                count += len(chunk)
    return count
"""
    # Ensure fixture is valid Python syntax under CPython 3.12+
    compile(source, "<complexity-fixture>", "exec")
    analyzer = PythonComplexityAnalyzer()
    metrics = {m.qualified_name: m for m in analyzer.analyze_source(source)}

    assert metrics["flat_fn"].cyclomatic_complexity == 1
    assert metrics["flat_fn"].max_nesting_depth == 0
    assert metrics["flat_fn"].parameter_count == 2

    # Base 1 + 13 branches = 14
    assert metrics["branching_fn"].cyclomatic_complexity == 14
    assert metrics["branching_fn"].parameter_count == 3

    # Nested function MUST NOT inflate outer function CC
    assert metrics["outer_with_nested"].cyclomatic_complexity == 1
    assert metrics["outer_with_nested.inner"].cyclomatic_complexity == 5

    assert metrics["async_control_flow"].is_async is True
    assert metrics["async_control_flow"].cyclomatic_complexity == 3
    assert metrics["async_control_flow"].max_nesting_depth == 3


def test_max_nesting_depth_and_parameter_counting_rules() -> None:
    source = """
from typing import Any, overload

def flat_elif_chain(x: int) -> int:
    if x == 1:
        return 1
    elif x == 2:
        return 2
    elif x == 3:
        return 3
    else:
        return 4

def deeply_nested(items: list[Any], path: str) -> int:
    if items:                                   # depth 1
        for item in items:                      # depth 2
            with open(path) as handle:          # depth 3
                try:                            # depth 4
                    return len(handle.read()) + int(item)
                except ValueError:
                    return 0
    return -1

def standalone_with_self_name(self: int, *, flag: bool = False) -> int:
    return self if flag else 0

class Sample:
    @overload
    def process(self, x: int) -> int:
        ...

    def process(
        self,
        a: int,
        b: str = "x",
        /,
        c: float = 1.0,
        *args: Any,
        kw: bool = True,
        **kwargs: Any,
    ) -> int:
        return a

    @classmethod
    def from_cfg(cls, data: dict[str, Any]) -> "Sample":
        return cls()

    @staticmethod
    def static_helper(cls: str, value: int) -> int:
        return len(cls) + value
"""
    analyzer = PythonComplexityAnalyzer()
    metrics = {m.qualified_name: m for m in analyzer.analyze_source(source)}

    assert metrics["flat_elif_chain"].max_nesting_depth == 1
    assert metrics["deeply_nested"].max_nesting_depth == 4
    # Standalone function does not drop parameter named `self`; bare `*` separator excluded
    assert metrics["standalone_with_self_name"].parameter_count == 2
    # Instance method drops `self` and `/`, counts a, b, c, *args, kw, **kwargs = 6
    # Typing @overload ellipsis stub is skipped cleanly so implementation wins
    assert metrics["Sample.process"].parameter_count == 6
    assert metrics["Sample.process"].symbol_kind == "method"
    # Classmethod drops `cls`
    assert metrics["Sample.from_cfg"].parameter_count == 1
    # Staticmethod does NOT drop `cls`
    assert metrics["Sample.static_helper"].parameter_count == 2


def test_compute_complexity_delta_added_modified_removed_and_diff_filter() -> None:
    before_source = """
class AuthService:
    def login(self, user: str) -> bool:
        if not user:
            return False
        return True

    def logout(self, token: str, force: bool, audit: bool) -> bool:
        if not token:
            return False
        if force and audit:
            return True
        if force or audit:
            return True
        return False

def helper(x: int) -> int:
    return x + 1 if x > 0 else 0

def legacy_check(val: int) -> bool:
    if val < 0 or val > 100 or val == 42:
        return False
    return True
"""
    after_source = """
class AuthService:
    def login(self, user: str, role: str, mfa: bool) -> bool:
        if not user:
            return False
        if role == "admin" and mfa:
            return True
        if role == "guest" or mfa:
            return False
        return True

    def logout(self, token: str) -> bool:
        if not token:
            return False
        return True

def helper(x: int) -> int:
    return x + 1 if x > 0 else 0

def new_validator(items: list[int]) -> int:
    return sum(1 for item in items if item > 0)
"""
    summary = compute_complexity_delta(before_source, after_source, high_cc_threshold=4)
    deltas = {d.qualified_name: d for d in summary.deltas}

    assert "helper" not in deltas
    assert deltas["AuthService.login"].change_kind == "modified"
    assert deltas["AuthService.login"].before_cc == 2
    assert deltas["AuthService.login"].after_cc == 6
    assert deltas["AuthService.login"].delta_cc == 4

    assert deltas["AuthService.logout"].change_kind == "modified"
    assert deltas["AuthService.logout"].before_cc == 6
    assert deltas["AuthService.logout"].after_cc == 2
    assert deltas["AuthService.logout"].delta_cc == -4

    assert deltas["new_validator"].change_kind == "added"
    assert deltas["new_validator"].before_cc == 0
    assert deltas["new_validator"].after_cc == 3
    assert deltas["new_validator"].delta_cc == 3

    assert deltas["legacy_check"].change_kind == "removed"
    assert deltas["legacy_check"].before_cc == 4
    assert deltas["legacy_check"].after_cc == 0
    assert deltas["legacy_check"].delta_cc == -4

    assert summary.net_delta_cc == -1
    assert summary.max_delta_cc == 4
    assert summary.max_after_cc == 6
    assert [f.qualified_name for f in summary.high_complexity_functions] == ["AuthService.login"]

    # When changed_right_lines touches `helper`, `helper` is included with delta_cc == 0
    filtered = compute_complexity_delta(
        before_source,
        after_source,
        changed_right_lines={17},
    )
    filtered_qnames = {d.qualified_name for d in filtered.deltas}
    assert "helper" in filtered_qnames

    # Brand-new file (before_source=None) and deleted file (after_source=None)
    brand_new = compute_complexity_delta(None, "def a(x):\n    return 1 if x else 2\n")
    assert brand_new.net_delta_cc == 2
    assert brand_new.deltas[0].change_kind == "added"

    deleted_all = compute_complexity_delta("def a(x):\n    return 1 if x else 2\n", None)
    assert deleted_all.net_delta_cc == -2
    assert deleted_all.deltas[0].change_kind == "removed"


def _make_unified_diff(path: str, added_lines: list[str], *, deleted: bool = False) -> str:
    if deleted:
        body = "\n".join(f"-{line}" for line in added_lines)
        return (
            f"diff --git a/{path} b/{path}\n"
            "deleted file mode 100644\n"
            f"--- a/{path}\n"
            "+++ /dev/null\n"
            f"@@ -1,{len(added_lines)} +0,0 @@\n"
            f"{body}\n"
        )
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added_lines)} @@\n"
        f"{body}\n"
    )


def test_test_companion_detector_thresholds_docstrings_and_colocated_tests() -> None:
    assert test_companion_detector is detect_missing_test_companion
    diff_parser = SmartDiffParser()

    # 1. Exactly 50 logic lines -> NOT untested (strictly > 50)
    fifty_lines = [f"x_{i} = {i}" for i in range(50)]
    pr_50 = diff_parser.parse_diff(_make_unified_diff("src/codesight/payment.py", fifty_lines))
    rep_50 = test_companion_detector(pr_50)
    assert rep_50.added_source_logic_lines == 50
    assert rep_50.is_untested is False

    # 2. 55 logic lines without any test file -> P_untested triggered
    fifty_five_lines = [f"x_{i} = {i}" for i in range(55)]
    pr_55 = diff_parser.parse_diff(_make_unified_diff("src/codesight/payment.py", fifty_five_lines))
    rep_55 = test_companion_detector(pr_55)
    assert rep_55.added_source_logic_lines == 55
    assert rep_55.added_test_logic_lines == 0
    assert rep_55.is_untested is True
    assert rep_55.source_files == ("src/codesight/payment.py",)

    # 3. 75 raw `+` lines containing 35 comment/docstring/blank lines -> only 40 logic lines
    #    Note: multiline string assignment `sql = """..."""` STILL counts as logic!
    noisy_lines = (
        ['"""', "Module explanation.", "More docs.", '"""']
        + ["# inline comment"] * 15
        + [""] * 16
        + ['sql = """', "SELECT * FROM users", '"""']
        + [f"val_{i} = {i}" for i in range(37)]
    )
    pr_noisy = diff_parser.parse_diff(_make_unified_diff("src/codesight/noisy.py", noisy_lines))
    rep_noisy = test_companion_detector(pr_noisy)
    assert rep_noisy.added_source_logic_lines == 40
    assert rep_noisy.is_untested is False

    # 4. 60 source logic lines + companion test in tests/ -> NOT untested
    sixty_lines = [f"x_{i} = {i}" for i in range(60)]
    raw_with_test = _make_unified_diff(
        "src/codesight/payment.py", sixty_lines
    ) + _make_unified_diff("tests/unit/test_payment.py", ["def test_ok():", "    assert True"])
    pr_with_test = diff_parser.parse_diff(raw_with_test)
    rep_with_test = test_companion_detector(pr_with_test)
    assert rep_with_test.added_source_logic_lines == 60
    assert rep_with_test.added_test_logic_lines == 2
    assert rep_with_test.is_untested is False
    assert rep_with_test.test_files == ("tests/unit/test_payment.py",)

    # 5. Colocated test file inside src/ (`*.test.tsx`) counts as test, NOT source logic
    raw_colocated = _make_unified_diff(
        "src/ui/RadarChart.test.tsx",
        [f"expect(renderChart({i})).toBeTruthy();" for i in range(60)],
    )
    pr_colocated = diff_parser.parse_diff(raw_colocated)
    rep_colocated = test_companion_detector(pr_colocated.files)
    assert rep_colocated.added_source_logic_lines == 0
    assert rep_colocated.added_test_logic_lines == 60
    assert rep_colocated.is_untested is False
    assert rep_colocated.test_files == ("src/ui/RadarChart.test.tsx",)

    # 6. Deleted source file does not trigger P_untested
    pr_deleted = diff_parser.parse_diff(
        _make_unified_diff("src/codesight/old.py", sixty_lines, deleted=True)
    )
    rep_deleted = test_companion_detector(pr_deleted)
    assert rep_deleted.added_source_logic_lines == 0
    assert rep_deleted.is_untested is False


def test_complexity_and_companion_additional_edge_cases() -> None:
    analyzer = PythonComplexityAnalyzer()
    assert analyzer.analyze_source("   \n") == ()

    edge_source = '''
from typing import overload

@overload
def overloaded(x: int) -> int:
    """Overload doc."""
    ...

@overload
def overloaded_pass(x: int) -> int:
    pass

@overload
def overloaded_body(x: int) -> int:
    return x

def zero_params() -> int:
    return 1

class EdgeMethods:
    def no_self(x: int) -> int:
        return x

    def splat_first(*args: int) -> int:
        return len(args)
'''
    metrics = {m.qualified_name: m for m in analyzer.analyze_source(edge_source)}
    assert "overloaded" not in metrics
    assert "overloaded_pass" not in metrics
    assert metrics["overloaded_body"].cyclomatic_complexity == 1
    assert metrics["zero_params"].parameter_count == 0
    assert metrics["EdgeMethods.no_self"].parameter_count == 1
    assert metrics["EdgeMethods.splat_first"].parameter_count == 1

    # Context lines, block comments, single-line docstrings, and deleted lines in diff
    diff_text = (
        "diff --git a/src/ui/widget.ts b/src/ui/widget.ts\n"
        "--- a/src/ui/widget.ts\n"
        "+++ b/src/ui/widget.ts\n"
        "@@ -1,4 +1,12 @@\n"
        " export function init() {\n"
        "-  const oldVal = 1;\n"
        "+  /*\n"
        "+   * Multiline TS block comment\n"
        "+   */\n"
        '+  const msg = "ok";\n'
        "+  // single line comment\n"
        "+  /* single-line block comment */\n"
        "+  return msg;\n"
        " }\n"
        "diff --git a/src/codesight/doc_only.py b/src/codesight/doc_only.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/codesight/doc_only.py\n"
        "@@ -0,0 +1,3 @@\n"
        '+"""Single-line module docstring."""\n'
        "+def f():\n"
        "+    return 1\n"
    )
    diff_parser = SmartDiffParser()
    parsed = diff_parser.parse_diff(diff_text)
    report = detect_missing_test_companion(parsed)
    # 2 logic lines in widget.ts + 2 logic lines in doc_only.py = 4
    assert report.added_source_logic_lines == 4
    assert report.is_untested is False

    # Trailing inline comment containing triple quotes must not flip multiline expr mode
    inline_comment_diff = _make_unified_diff(
        "src/codesight/inline_note.py",
        [
            'x = 1  # note """',
            "# pure comment should not be counted as logic",
            "y = 2",
        ],
    )
    inline_report = detect_missing_test_companion(diff_parser.parse_diff(inline_comment_diff))
    assert inline_report.added_source_logic_lines == 2

    # Bool / negative threshold validation
    with pytest.raises(ValueError, match="high_cc_threshold"):
        compute_complexity_delta("def f(): return 1", "def f(): return 1", high_cc_threshold=True)
    with pytest.raises(ValueError, match="high_cc_threshold"):
        compute_complexity_delta("def f(): return 1", "def f(): return 1", high_cc_threshold=-1)
    with pytest.raises(ValueError, match="logic_line_threshold"):
        detect_missing_test_companion(parsed, logic_line_threshold=False)
    with pytest.raises(ValueError, match="logic_line_threshold"):
        detect_missing_test_companion(parsed, logic_line_threshold=-5)


def test_companion_detector_mid_docstring_hunk_ts_private_fields_and_triple_quote_expr() -> None:
    diff_parser = SmartDiffParser()

    # F-04a: Hunk begins inside a long docstring, closes with bare `"""`, then adds 60 logic lines
    body = "".join(f"+    total += compute_{i}(x)\n" for i in range(60))
    mid_doc_raw = (
        "diff --git a/src/svc.py b/src/svc.py\n"
        "--- a/src/svc.py\n"
        "+++ b/src/svc.py\n"
        f"@@ -20,4 +20,{4 + 1 + 60} @@ class Svc:\n"
        "     long docstring line a\n"
        "     long docstring line b\n"
        "+    added doc line\n"
        '     """\n'
        "     total = 0\n"
        f"{body}"
    )
    mid_doc_rep = detect_missing_test_companion(diff_parser.parse_diff(mid_doc_raw))
    assert mid_doc_rep.added_source_logic_lines == 60
    assert mid_doc_rep.is_untested is True

    # F-04b: Hunk begins inside a docstring with apostrophe and closes with `don't stop."""`
    prose_close_body = "".join(f"+    val_{i} = step({i})\n" for i in range(55))
    prose_close_raw = (
        "diff --git a/src/svc2.py b/src/svc2.py\n"
        "--- a/src/svc2.py\n"
        "+++ b/src/svc2.py\n"
        f"@@ -30,3 +30,{3 + 55} @@\n"
        "     continued docstring line\n"
        '     don\'t stop until here."""\n'
        "     base = 0\n"
        f"{prose_close_body}"
    )
    prose_rep = detect_missing_test_companion(diff_parser.parse_diff(prose_close_raw))
    assert prose_rep.added_source_logic_lines == 55
    assert prose_rep.is_untested is True

    # F-04c: Guard against false positives — 60 logic lines before an unclosed docstring opener,
    #        plus quoted `"""` literal and inline `# note """` comment at hunk new_start > 1
    before_open_body = "".join(f"+    x_{i} = {i}\n" for i in range(58))
    before_open_raw = (
        "diff --git a/src/svc3.py b/src/svc3.py\n"
        "--- a/src/svc3.py\n"
        "+++ b/src/svc3.py\n"
        f"@@ -10,3 +10,{3 + 60} @@\n"
        '+    marker = \'"""\'  # note """\n'
        '+    escaped = "\\""\n'
        f"{before_open_body}"
        " def bar():\n"
        '     r"""Start of existing docstring\n'
        "     second docstring line\n"
    )
    before_open_rep = detect_missing_test_companion(diff_parser.parse_diff(before_open_raw))
    assert before_open_rep.added_source_logic_lines == 60
    assert before_open_rep.is_untested is True

    # F-11: TS/JS ES private class fields starting with `#` MUST be counted as logic
    ts_private = [f"  #field_{i} = {i};" for i in range(55)]
    ts_rep = detect_missing_test_companion(
        diff_parser.parse_diff(_make_unified_diff("src/ui/store.ts", ts_private))
    )
    assert ts_rep.added_source_logic_lines == 55
    assert ts_rep.is_untested is True

    # F-16 (S6): Single-line triple-quoted expression (`"""...""".format(...)`) counts as logic
    expr_diff = _make_unified_diff(
        "src/codesight/fmt.py",
        [
            '"""Pure docstring."""',
            '"""SELECT {x}""".format(x=1)',
            '"""a""".join(items)',
        ],
    )
    expr_rep = detect_missing_test_companion(diff_parser.parse_diff(expr_diff))
    assert expr_rep.added_source_logic_lines == 2


def test_complexity_analyzer_deeply_nested_definitions_no_recursion_error() -> None:
    deep = "".join("    " * i + f"def f{i}():\n" for i in range(600)) + "    " * 600 + "pass\n"
    results = PythonComplexityAnalyzer().analyze_source(deep)
    assert isinstance(results, tuple)
    assert len(results) > 0


def test_compute_complexity_delta_flags_syntax_errors_and_avoids_fake_reduction() -> None:
    before = "def f(a):\n    if a:\n        return 1\n    return 2\n"
    after = "def f(a):\n    if a\n        return 1\n    return 2\n"
    summary = compute_complexity_delta(before, after)
    assert summary.has_syntax_error is True
    assert summary.deltas == ()
    assert summary.net_delta_cc == 0

    mixed_before = (
        "def valid_fn(x):\n    return x\n\n"
        "def broken_fn(a):\n    if a:\n        return 1\n    return 2\n"
    )
    mixed_after = (
        "def valid_fn(x):\n    return x if x else 0\n\n"
        "def broken_fn(a):\n    if a\n        return 1\n    return 2\n"
    )
    mixed_summary = compute_complexity_delta(mixed_before, mixed_after)
    assert mixed_summary.has_syntax_error is True
    assert [(d.qualified_name, d.delta_cc) for d in mixed_summary.deltas] == [("valid_fn", 1)]
    assert mixed_summary.net_delta_cc == 1

    # Broken function header disappearing in after_source must not emit a fake removal
    broken_header_summary = compute_complexity_delta(before, "def f(a, :\n    if a\n")
    assert broken_header_summary.has_syntax_error is True
    assert broken_header_summary.deltas == ()
    assert broken_header_summary.net_delta_cc == 0


def test_compute_complexity_delta_max_after_cc_respects_changed_right_lines() -> None:
    hot = (
        "def hot(a):\n"
        + "".join(f"    if a == {i}:\n        return {i}\n" for i in range(30))
        + "    return 0\n"
    )
    before = hot + "def tiny():\n    return 1\n"
    after = hot + "def tiny():\n    return 2\n"
    tiny_line = after.count("\n")

    summary = compute_complexity_delta(before, after, changed_right_lines={tiny_line})
    assert [(d.qualified_name, d.delta_cc) for d in summary.deltas] == [("tiny", 0)]
    assert summary.max_after_cc == 1
    assert summary.high_complexity_functions == ()


def test_detect_missing_test_companion_ignores_seed_json_yaml_and_supports_multi_layouts() -> None:
    diff_parser = SmartDiffParser()
    seed_lines = [f'  "key_{i}": {i},' for i in range(110)]
    yaml_lines = [f"field_{i}: value_{i}" for i in range(110)]
    non_code_diff = diff_parser.parse_diff(
        _make_unified_diff("src/data/seed.json", seed_lines)
        + _make_unified_diff("src/config/schema.yaml", yaml_lines)
    )
    non_code_report = detect_missing_test_companion(non_code_diff)
    assert non_code_report.added_source_logic_lines == 0
    assert non_code_report.source_files == ()
    assert non_code_report.is_untested is False

    app_lines = [f"order_step_{i} = {i}" for i in range(55)]
    app_diff = diff_parser.parse_diff(_make_unified_diff("app/services/order.py", app_lines))
    app_report = detect_missing_test_companion(app_diff)
    assert app_report.added_source_logic_lines == 55
    assert app_report.source_files == ("app/services/order.py",)
    assert app_report.is_untested is True

    multi_layout_diff = diff_parser.parse_diff(
        _make_unified_diff("backend/api/orders.py", [f"val_{i} = {i}" for i in range(20)])
        + _make_unified_diff("frontend/src/App.tsx", [f"const x{i} = {i};" for i in range(20)])
        + _make_unified_diff("packages/core/src/calc.ts", [f"const y{i} = {i};" for i in range(20)])
    )
    multi_report = detect_missing_test_companion(multi_layout_diff)
    assert multi_report.added_source_logic_lines == 60
    assert multi_report.source_files == (
        "backend/api/orders.py",
        "frontend/src/App.tsx",
        "packages/core/src/calc.ts",
    )
    assert multi_report.is_untested is True
