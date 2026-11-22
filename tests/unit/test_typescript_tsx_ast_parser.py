"""Unit tests for TypeScriptASTParser, TypeScriptComplexityAnalyzer, and dual-lang speed."""

from __future__ import annotations

import importlib.util
import itertools
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from codesight.ast_engine import (
    ComplexityAnalyzer,
    PythonASTParser,
    TypeScriptASTParser,
    TypeScriptComplexityAnalyzer,
    compute_complexity_delta,
    detect_missing_test_companion,
)
from codesight.ast_engine.models import MAX_SIGNATURE_CHARS, ParsedASTFile
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.models import TransientEmbeddingItem, ZeroCodeViolationError


def _load_audit_payloads() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = (
        repo_root
        / ".agents"
        / "skills"
        / "zerocode-ast-rag"
        / "scripts"
        / "audit_qdrant_zerocode.py"
    )
    spec = importlib.util.spec_from_file_location("audit_qdrant_zerocode", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit_payloads


AUDIT_PAYLOADS = _load_audit_payloads()

REALISTIC_TS_MODULE = """
import axios, { AxiosRequestConfig, isAxiosError as checkAxiosErr } from "axios";
import type { ApiResponse, RetryPolicy } from "./types";
import * as logger from "./logger";
import "./polyfills";

// Tiếng Việt UTF-8 comment: kiểm tra byte offset không lệch 🚀

/** Options for paginated requests. */
export interface PageOptions<T extends string> extends RetryPolicy {
  cursor: T;
  apiSecretHeader: string;
}

export type Role = "admin" | "member";
export type UserId = string | number;

export type SecretVaultConfig<T> = {
  apiKey: string;
  token: "secret-should-not-leak";
  data: T;
};

function formatKey(input: string): string;
function formatKey(input: number): string;
function formatKey(input: string | number): string {
  return String(input);
}

/**
 * Fetch typed records with retry metadata.
 */
export async function fetchRecords<T>(
  this: Window,
  endpoint: string,
  policy: RetryPolicy = createDefaultPolicy(),
  ...tags: string[]
): Promise<ApiResponse<T>> {
  function normalizeEndpoint(raw: string): string {
    return sanitizeUrl(raw.trim());
  }

  const target = normalizeEndpoint(endpoint);
  const controller = new AbortController();
  const res = await axios.get<T>(target, { signal: controller.signal });
  logger.info(formatKey(tags.length));
  return res.data as ApiResponse<T>;
}

export const usePagination = <T,>(
  items: readonly T[],
  pageSize = getDefaultPageSize(),
) => {
  const mapped = items.map((item) => formatItem(item));
  return buildPage(mapped, pageSize);
};

@Injectable()
export abstract class OrderClient<T> extends BaseClient implements Disposable {
  abstract flush(batch: T[]): Promise<void>;

  @Retry(buildBackoff())
  private handleRetry = async (attempt: number) => {
    return await scheduleRetry(attempt);
  };

  @Trace("orders.submit")
  async submitOrder(orderId: string): Promise<T> {
    const err = new ns.ValidationError(orderId);
    if (!orderId) {
      throw err;
    }
    return (await this.handleRetry(1)) as T;
  }
}
"""

REALISTIC_TSX_MODULE = """
import React, { useEffect, useState } from "react";
import { fetchUserProfile } from "../api/users";

export interface UserCardProps {
  userId: string;
  title: string;
}

/** Displays a user profile card. */
export const UserCard = React.memo(({ userId, title }: UserCardProps) => {
  const [name, setName] = useState<string>("");

  const formatHeading = (raw: string) => {
    return normalizeHeading(raw);
  };

  useEffect(() => {
    fetchUserProfile(userId).then((u) => setName(u.name));
  }, [userId]);

  return (
    <section className="card">
      <h1>{formatHeading(title)}</h1>
      <Avatar userId={userId} />
      <Dialog.Panel>{name}</Dialog.Panel>
    </section>
  );
});

export const FancyInput = React.forwardRef<HTMLInputElement, UserCardProps>(
  function FancyInput(props, ref) {
    return (
      <div className="input-wrap">
        <UI.Field.Icon />
        <input ref={ref} defaultValue={props.title} />
      </div>
    );
  },
);

export default function DashboardPage({ ids }: { ids: string[] }) {
  return (
    <main>
      {ids.map((id) => (
        <UserCard key={id} userId={id} title="Bảng điều khiển" />
      ))}
    </main>
  );
}
"""


def test_typescript_parser_extracts_symbols_generics_imports_jsdoc_and_calls() -> None:
    parser = TypeScriptASTParser()
    parsed = parser.parse(REALISTIC_TS_MODULE, file_path="./src/clients/orders.ts")

    assert isinstance(parsed, ParsedASTFile)
    assert parsed.file_path == "src/clients/orders.ts"
    assert parsed.language == "typescript"
    assert parsed.has_syntax_error is False
    assert parsed.imports == (
        "axios.axios",
        "axios.AxiosRequestConfig",
        "axios.isAxiosError",
        "./types.ApiResponse",
        "./types.RetryPolicy",
        "./logger.*",
        "./polyfills",
    )

    by_qname = {chunk.qualified_name: chunk for chunk in parsed.chunks}
    assert list(by_qname.keys()) == [
        "PageOptions",
        "Role",
        "UserId",
        "SecretVaultConfig",
        "formatKey",
        "fetchRecords",
        "fetchRecords.normalizeEndpoint",
        "usePagination",
        "OrderClient",
        "OrderClient.handleRetry",
        "OrderClient.submitOrder",
    ]

    iface = by_qname["PageOptions"]
    assert iface.symbol_kind == "interface"
    assert iface.docstring == "Options for paginated requests."
    assert iface.signature == "export interface PageOptions<T extends string> extends RetryPolicy"
    assert "apiSecretHeader" not in iface.signature
    assert "{" not in iface.signature

    role_type = by_qname["Role"]
    assert role_type.symbol_kind == "type_alias"
    assert role_type.signature == "export type Role"

    user_id_type = by_qname["UserId"]
    assert user_id_type.symbol_kind == "type_alias"
    assert user_id_type.signature == "export type UserId = string | number"

    vault_type = by_qname["SecretVaultConfig"]
    assert vault_type.symbol_kind == "type_alias"
    assert vault_type.signature == "export type SecretVaultConfig<T>"
    assert "apiKey" not in vault_type.signature
    assert "secret-should-not-leak" not in vault_type.signature
    assert "{" not in vault_type.signature

    fetch_fn = by_qname["fetchRecords"]
    assert fetch_fn.symbol_kind == "function"
    assert fetch_fn.is_async is True
    assert fetch_fn.docstring == "Fetch typed records with retry metadata."
    assert "<T>" in fetch_fn.signature
    assert "{" not in fetch_fn.signature
    assert fetch_fn.called_symbols == (
        "createDefaultPolicy",
        "normalizeEndpoint",
        "AbortController",
        "get",
        "info",
        "formatKey",
    )
    # Nested function calls MUST NOT leak into parent
    assert "sanitizeUrl" not in fetch_fn.called_symbols
    assert by_qname["fetchRecords.normalizeEndpoint"].called_symbols == ("sanitizeUrl", "trim")

    hook = by_qname["usePagination"]
    assert hook.symbol_kind == "function"
    assert "<T,>" in hook.signature
    assert hook.called_symbols == ("getDefaultPageSize", "map", "formatItem", "buildPage")

    cls = by_qname["OrderClient"]
    assert cls.symbol_kind == "class"
    assert cls.decorators == ("@Injectable()",)
    assert cls.called_symbols == ("Injectable",)
    assert "scheduleRetry" not in cls.called_symbols

    arrow_method = by_qname["OrderClient.handleRetry"]
    assert arrow_method.symbol_kind == "method"
    assert arrow_method.is_async is True
    assert arrow_method.decorators == ("@Retry(buildBackoff())",)
    assert arrow_method.called_symbols == ("Retry", "buildBackoff", "scheduleRetry")

    submit = by_qname["OrderClient.submitOrder"]
    assert submit.symbol_kind == "method"
    assert submit.is_async is True
    assert submit.decorators == ('@Trace("orders.submit")',)
    assert submit.called_symbols == ("Trace", "ValidationError", "handleRetry")

    # Verify NFR-3 Qdrant payload compliance across all extracted chunks
    points = [
        {
            "id": chunk.build_point_id("org/repo", "deadbeef"),
            "payload": chunk.to_qdrant_payload(
                repo_full_name="org/repo",
                blob_sha="deadbeef",
                repo_id="repo-1",
            ),
        }
        for chunk in parsed.chunks
    ]
    assert AUDIT_PAYLOADS(points) == []
    assert len({pt["id"] for pt in points}) == len(points)
    for pt in points:
        uuid.UUID(pt["id"], version=5)


def test_tsx_parser_extracts_react_hoc_components_and_jsx_called_symbols() -> None:
    parser = TypeScriptASTParser()
    parsed = parser.parse(REALISTIC_TSX_MODULE, file_path="src/components/UserCard.tsx")

    assert parsed.language == "tsx"
    assert parsed.has_syntax_error is False

    by_qname = {chunk.qualified_name: chunk for chunk in parsed.chunks}
    assert list(by_qname.keys()) == [
        "UserCardProps",
        "UserCard",
        "UserCard.formatHeading",
        "FancyInput",
        "DashboardPage",
    ]

    card = by_qname["UserCard"]
    assert card.symbol_kind == "function"
    assert card.docstring == "Displays a user profile card."
    assert card.called_symbols == (
        "memo",
        "useState",
        "useEffect",
        "fetchUserProfile",
        "then",
        "setName",
        "formatHeading",
        "Avatar",
        "Panel",
    )
    # Intrinsic HTML tags and named nested helper calls must NOT appear in UserCard
    assert "section" not in card.called_symbols
    assert "h1" not in card.called_symbols
    assert "normalizeHeading" not in card.called_symbols
    assert by_qname["UserCard.formatHeading"].called_symbols == ("normalizeHeading",)

    fancy = by_qname["FancyInput"]
    assert fancy.symbol_kind == "function"
    assert "forwardRef" in fancy.called_symbols
    assert "Icon" in fancy.called_symbols
    assert "div" not in fancy.called_symbols
    assert "input" not in fancy.called_symbols

    dashboard = by_qname["DashboardPage"]
    assert dashboard.symbol_kind == "function"
    assert dashboard.called_symbols == ("map", "UserCard")
    assert "main" not in dashboard.called_symbols

    # F-08: Verify NFR-3 Qdrant payload compliance on .tsx chunks as well
    tsx_points = [
        {
            "id": chunk.build_point_id("org/web", "cafebabe"),
            "payload": chunk.to_qdrant_payload(
                repo_full_name="org/web",
                blob_sha="cafebabe",
                repo_id="repo-web",
            ),
        }
        for chunk in parsed.chunks
    ]
    assert AUDIT_PAYLOADS(tsx_points) == []
    assert len({pt["id"] for pt in tsx_points}) == len(tsx_points)


def test_typescript_signature_strips_header_comments_and_respects_max_length() -> None:
    source = """
export async function verifySecret( // secret=super-sensitive-1
  token: string, /* secret=super-sensitive-2 */
  extraParamOne: string,
  extraParamTwo: string,
  extraParamThree: string,
  extraParamFour: string,
  extraParamFive: string,
  extraParamSix: string,
  extraParamSeven: string,
  extraParamEight: string,
  extraParamNine: string,
  extraParamTen: string,
): Promise<boolean> { // trailing-secret
  return token.length > 0;
}

export const arrowWithComment = (
  a: string, // hidden-note
): boolean => /* inline-secret */ a.length > 0;
"""
    parsed = TypeScriptASTParser().parse(source, file_path="src/auth.ts")
    by_qname = {chunk.qualified_name: chunk for chunk in parsed.chunks}

    for chunk in by_qname.values():
        assert len(chunk.signature) <= MAX_SIGNATURE_CHARS
        assert "//" not in chunk.signature
        assert "/*" not in chunk.signature
        assert "secret" not in chunk.signature
        assert "hidden-note" not in chunk.signature
        assert "\n" not in chunk.signature

    points = [
        {
            "id": c.build_point_id("org/r", "sha1"),
            "payload": c.to_qdrant_payload(repo_full_name="org/r", blob_sha="sha1"),
        }
        for c in parsed.chunks
    ]
    assert AUDIT_PAYLOADS(points) == []


def test_typescript_large_function_splits_into_contiguous_subchunks() -> None:
    branch_lines = [f"    total += stepA_{i}(x);" for i in range(148)]
    else_lines = [f"    total += stepB_{i}(x);" for i in range(148)]
    lines = [
        "export function giantPipeline(x: number): number {",
        "  let total = 0;",
        "  if (checkAlpha(x)) {",
        *branch_lines,
        "  } else if (checkBeta(x)) {",
        *else_lines,
        "  }",
        "  return finalize(total);",
        "}",
    ]
    source = "\n".join(lines) + "\n"

    parsed = TypeScriptASTParser(max_chunk_lines=150).parse(source, file_path="src/pipeline.ts")
    subchunks = [c for c in parsed.chunks if c.symbol_name == "giantPipeline"]
    assert len(subchunks) >= 3
    assert all(c.is_subchunk for c in subchunks)
    assert [c.subchunk_index for c in subchunks] == list(range(len(subchunks)))
    assert all(c.line_count <= 150 for c in subchunks)
    assert all(c.signature == "export function giantPipeline(x: number): number" for c in subchunks)

    # Contiguous coverage across the entire function span without gaps
    assert subchunks[0].start_line == 1
    assert subchunks[-1].end_line == len(lines)
    for prev, nxt in itertools.pairwise(subchunks):
        assert nxt.start_line == prev.end_line + 1

    # No calls lost at compound statement headers
    all_calls = {sym for c in subchunks for sym in c.called_symbols}
    assert {"checkAlpha", "checkBeta", "stepA_0", "stepB_0", "finalize"} <= all_calls

    # Deterministic unique UUIDv5 point IDs
    ids = [c.build_point_id("org/repo", "sha-big") for c in subchunks]
    assert len(set(ids)) == len(ids)

    # Class > 150 lines must NOT be split into subchunks
    class_fields = [f"  field_{i}: number = {i};" for i in range(170)]
    big_class_src = "export class BigConfig {\n" + "\n".join(class_fields) + "\n}\n"
    big_cls_parsed = TypeScriptASTParser(max_chunk_lines=150).parse(
        big_class_src, file_path="src/big_config.ts"
    )
    assert len(big_cls_parsed.chunks) == 1
    assert big_cls_parsed.chunks[0].symbol_kind == "class"
    assert big_cls_parsed.chunks[0].is_subchunk is False


def test_typescript_parser_and_complexity_handle_deep_nesting_and_syntax_errors() -> None:
    depth = 600
    deep_src = (
        "".join(f"function f{i}() {{\n" for i in range(depth)) + "return 1;\n" + ("}\n" * depth)
    )
    parsed = TypeScriptASTParser().parse(deep_src, file_path="src/deep.ts")
    assert len({c.qualified_name for c in parsed.chunks}) == depth

    metrics = TypeScriptComplexityAnalyzer().analyze_source(deep_src)
    assert len(metrics) == depth

    broken = "export function broken(x: number { if (x > 0) { return 1; "
    broken_parsed = TypeScriptASTParser().parse(broken, file_path="src/broken.ts")
    assert broken_parsed.has_syntax_error is True

    empty_parsed = TypeScriptASTParser().parse("   \n\t ", file_path="src/empty.ts")
    assert empty_parsed.chunks == ()
    assert empty_parsed.has_syntax_error is False


def test_typescript_complexity_analyzer_branching_nesting_params_and_delta_cc() -> None:
    source = """
function overloaded(x: string): string;
function overloaded(x: number): number;
function overloaded(x: string | number): string | number {
  return x;
}

export function flatWithOptionalChain(
  this: Window,
  user?: { profile?: { name?: string } },
): string {
  return user?.profile?.name;
}

export const singleParamArrow = (x: number) => x + 1;
const unparenArrow = x => x + 1;

export function allBranches(
  items: number[],
  flag: boolean,
  code: number,
  fallback?: string,
): number {
  let total = flag ? 1 : 0;                  // +1 (ternary)
  if (code > 0 && flag) {                    // +1 (if) +1 (&&)
    total += 1;
  } else if (code < 0 || !flag) {            // +1 (else if) +1 (||)
    total -= 1;
  } else {
    total += 0;
  }

  let label = fallback ?? "default";         // +1 (??)
  label ??= "a";                             // +1 (??=)
  label ||= "b";                             // +1 (||=)
  label &&= "c";                             // +1 (&&=)

  for (let i = 0; i < items.length; i++) {   // +1 (for)
    total += i;
  }
  for (const idx in items) {                 // +1 (for..in)
    total += Number(idx);
  }
  for (const val of items) {                 // +1 (for..of)
    total += val;
  }
  while (total > 100) {                      // +1 (while)
    total -= 10;
  }
  do {                                       // +1 (do..while)
    total += 1;
  } while (total < 0);

  switch (code) {
    case 200:                                // +1 (switch_case)
      break;
    case 404:                                // +1 (switch_case)
      break;
    default:                                 // +0 (switch_default)
      break;
  }

  try {
    return total + label.length;
  } catch (err) {                            // +1 (catch_clause)
    return -1;
  }
}

export function flatElseIfChain(x: number): number {
  if (x === 1) {
    return 1;
  } else if (x === 2) {
    return 2;
  } else if (x === 3) {
    return 3;
  } else {
    return 4;
  }
}

export function nestedBlocks(items: number[]): number {
  if (items.length > 0) {                    // depth 1
    for (const item of items) {              // depth 2
      try {                                  // depth 3
        switch (item) {                      // depth 4
          case 1:
            return 1;
          default:
            return 0;
        }
      } catch {
        return -1;
      }
    }
  }
  return 0;
}

export function outerWithNamedNested(a: number): number {
  const inner = (b: number) => (b > 0 && b < 10 ? 1 : 0);
  return inner(a);
}
"""
    analyzer = TypeScriptComplexityAnalyzer()
    metrics = {m.qualified_name: m for m in analyzer.analyze_source(source)}

    # Overload signatures skipped; only implementation kept
    assert metrics["overloaded"].cyclomatic_complexity == 1
    assert metrics["overloaded"].parameter_count == 1

    # Optional chaining ?. MUST NOT inflate CC; `this: Window` excluded from parameter_count
    assert metrics["flatWithOptionalChain"].cyclomatic_complexity == 1
    assert metrics["flatWithOptionalChain"].parameter_count == 1

    assert metrics["singleParamArrow"].parameter_count == 1
    assert metrics["unparenArrow"].parameter_count == 1

    # Base 1 + 17 branches = 18
    assert metrics["allBranches"].cyclomatic_complexity == 18
    assert metrics["allBranches"].parameter_count == 4

    # Flat else-if chain has nesting depth 1 (not 3!)
    assert metrics["flatElseIfChain"].cyclomatic_complexity == 4
    assert metrics["flatElseIfChain"].max_nesting_depth == 1

    assert metrics["nestedBlocks"].max_nesting_depth == 4

    # Named nested arrow isolated from outer
    assert metrics["outerWithNamedNested"].cyclomatic_complexity == 1
    assert metrics["outerWithNamedNested.inner"].cyclomatic_complexity == 3

    # Unified ComplexityAnalyzer facade & delta CC
    unified = ComplexityAnalyzer()
    assert len(unified.analyze_source(source, file_path="src/math.ts")) == len(metrics)
    assert unified.analyze_source("SELECT 1;", file_path="db/schema.sql") == ()

    before_ts = """
export function calc(x: number): number {
  return x + 1;
}
"""
    after_ts = """
export function calc(x: number): number {
  if (x > 0 && x < 10) {
    return x ? x + 1 : 0;
  }
  return 0;
}
"""
    delta_summary = compute_complexity_delta(before_ts, after_ts, file_path="src/calc.ts")
    assert delta_summary.net_delta_cc == 3
    assert len(delta_summary.deltas) == 1
    assert delta_summary.deltas[0].qualified_name == "calc"
    assert delta_summary.deltas[0].before_cc == 1
    assert delta_summary.deltas[0].after_cc == 4
    assert delta_summary.deltas[0].delta_cc == 3

    # Unsupported file extension returns empty summary safely
    unsupported_delta = compute_complexity_delta("a=1", "a=2", file_path="config/app.yaml")
    assert unsupported_delta.deltas == ()
    assert unsupported_delta.net_delta_cc == 0


def test_bilingual_python_and_typescript_tsx_ast_benchmark_under_200ms() -> None:
    py_source = """
import os
from typing import Any

class Service:
    async def handle(self, items: list[int], flag: bool) -> int:
        total = 0
        for item in items:
            if item > 0 and flag:
                total += compute(item)
        return total
"""
    py_parser = PythonASTParser()
    ts_parser = TypeScriptASTParser()
    unified_cc = ComplexityAnalyzer()

    # Warm-up grammar handles once, then time full dual-language pass
    py_parser.parse(py_source, file_path="src/service.py")
    ts_parser.parse(REALISTIC_TS_MODULE, file_path="src/orders.ts")
    ts_parser.parse(REALISTIC_TSX_MODULE, file_path="src/UserCard.tsx")

    t0 = time.perf_counter()
    py_res = py_parser.parse(py_source, file_path="src/service.py")
    ts_res = ts_parser.parse(REALISTIC_TS_MODULE, file_path="src/orders.ts")
    tsx_res = ts_parser.parse(REALISTIC_TSX_MODULE, file_path="src/UserCard.tsx")
    py_cc = unified_cc.analyze_source(py_source, file_path="src/service.py")
    ts_cc = unified_cc.analyze_source(REALISTIC_TS_MODULE, file_path="src/orders.ts")
    tsx_cc = unified_cc.analyze_source(REALISTIC_TSX_MODULE, file_path="src/UserCard.tsx")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(py_res.chunks) == 2
    assert len(ts_res.chunks) == 11
    assert len(tsx_res.chunks) == 5
    assert len(py_cc) == 1
    assert len(ts_cc) == 6
    assert len(tsx_cc) == 4
    assert elapsed_ms < 200.0, f"Dual-language AST pass took {elapsed_ms:.2f}ms (budget < 200ms)"


def test_typescript_parser_rejects_invalid_max_chunk_lines() -> None:
    with pytest.raises(ValueError, match="max_chunk_lines"):
        TypeScriptASTParser(max_chunk_lines=5)
    with pytest.raises(ValueError, match="max_chunk_lines"):
        TypeScriptASTParser(max_chunk_lines=True)


def test_typescript_non_exported_hoc_nesting_class_members_and_default_export() -> None:
    source = """
interface LocalShape {
  id: string;
}

type LocalId = string | number;

@Entity()
class LocalRepo {
  @Column()
  title: string = defaultTitle();

  #secretStep() {
    return runInternal();
  }

  get size(): number {
    return this.#secretStep();
  }

  constructor(private readonly db: Db) {}
}

const firstFn = () => helperOne(), secondFn = (() => helperTwo());

export const MemoRefBox = React.memo(
  React.forwardRef<HTMLDivElement, LocalShape>((props, ref) => (
    <div ref={ref}><Badge id={props.id} /></div>
  )),
);

export default async () => {
  return await loadHome();
};
"""
    parsed = TypeScriptASTParser().parse(source, file_path="src/edge.tsx")
    by_qname = {c.qualified_name: c for c in parsed.chunks}

    assert "LocalShape" in by_qname
    assert by_qname["LocalShape"].symbol_kind == "interface"
    assert "LocalId" in by_qname
    assert by_qname["LocalId"].symbol_kind == "type_alias"

    repo = by_qname["LocalRepo"]
    assert repo.symbol_kind == "class"
    assert repo.decorators == ("@Entity()",)
    assert repo.called_symbols == ("Entity", "Column", "defaultTitle")
    assert "runInternal" not in repo.called_symbols

    assert by_qname["LocalRepo.#secretStep"].symbol_kind == "method"
    assert by_qname["LocalRepo.size"].called_symbols == ("#secretStep",)
    assert by_qname["LocalRepo.constructor"].symbol_kind == "method"

    assert by_qname["firstFn"].called_symbols == ("helperOne",)
    assert by_qname["secondFn"].called_symbols == ("helperTwo",)

    memo_ref = by_qname["MemoRefBox"]
    assert memo_ref.called_symbols == ("memo", "forwardRef", "Badge")

    default_export = by_qname["default"]
    assert default_export.is_async is True
    assert default_export.called_symbols == ("loadHome",)

    # Also verify empty source for TypeScriptComplexityAnalyzer and default language
    assert TypeScriptComplexityAnalyzer().analyze_source("   ") == ()
    assert ComplexityAnalyzer().analyze_source("def f():\n    return 1\n")[0].symbol_name == "f"
    assert len(ComplexityAnalyzer().analyze_source(source, language="tsx")) == 7


def test_nfr3_signature_strips_defaults_decorators_literals_and_object_types() -> None:
    """Regression test for F-01: no secrets, defaults, decorators, or object_types in signature."""
    source = """
export @Controller({ internalToken: "sk-live-ctrl" }) class PaymentGateway {
  async charge(
    @Headers("x-master-secret") authHeader: string,
    apiKey: string = "sk-live-9876543210abcdef",
    { retry = { secretSalt: "pepper" }, timeout: ms = 500 }: ChargeOpts = {},
    onFail: (err: Error) => void = (err) => {
      revokeKey("sk-live-fallback", err);
    },
  ): Promise<{ receiptId: string; secretHash: string }> {
    return executeCharge(authHeader, apiKey, retry, ms, onFail);
  }
}

export type ProdVaultEndpoint = "https://vault.corp.internal:8200/v1/secret/prod";
export type AuthBearerHeader = `Bearer sk-live-${string}`;
export type ExtractSecret<T extends { apiKey: "sk-live-123"; role: string }> = T["apiKey"];
export interface AdminSession extends BaseSession<{ masterKey: "sk-live-999" }> {
  id: string;
}
"""
    parsed = TypeScriptASTParser().parse(source, file_path="src/payments.ts")
    by_qname = {c.qualified_name: c for c in parsed.chunks}

    gw = by_qname["PaymentGateway"]
    assert gw.signature == "export class PaymentGateway"
    assert "sk-live-ctrl" not in gw.signature
    assert "@Controller" not in gw.signature

    charge = by_qname["PaymentGateway.charge"]
    for leaked in (
        "x-master-secret",
        "sk-live-9876543210abcdef",
        "secretSalt",
        "pepper",
        "sk-live-fallback",
        "revokeKey",
        "secretHash",
        "@Headers",
    ):
        assert leaked not in charge.signature
    assert "authHeader: string" in charge.signature
    assert "apiKey: string" in charge.signature
    # Default-parameter calls remain captured in RAM-only called_symbols
    assert "Headers" in charge.called_symbols
    assert "revokeKey" in charge.called_symbols
    assert "executeCharge" in charge.called_symbols

    assert by_qname["ProdVaultEndpoint"].signature == "export type ProdVaultEndpoint"
    assert by_qname["AuthBearerHeader"].signature == "export type AuthBearerHeader"
    extract_sig = by_qname["ExtractSecret"].signature
    assert "sk-live-123" not in extract_sig
    assert extract_sig == "export type ExtractSecret<T extends {...}>"
    admin_sig = by_qname["AdminSession"].signature
    assert "sk-live-999" not in admin_sig
    assert admin_sig == "export interface AdminSession extends BaseSession<{...}>"

    points = [
        {
            "id": c.build_point_id("org/sec", "sha-sec"),
            "payload": c.to_qdrant_payload(repo_full_name="org/sec", blob_sha="sha-sec"),
        }
        for c in parsed.chunks
    ]
    assert AUDIT_PAYLOADS(points) == []


def test_typescript_as_satisfies_type_assertions_and_default_memo_named() -> None:
    """Regression test for F-03 & F-08: as/satisfies/type_assertion, let/var, svc?.save()."""
    tsx_source = """
export const DataTable = React.memo(function DataTable<T>({ rows }: TableProps<T>) {
  return <Grid rows={rows} />;
}) as <T>(props: TableProps<T>) => React.ReactElement;

export const UserBadge = (<T,>(props: BadgeProps<T>) => (
  <Avatar name={props.name} />
)) satisfies React.FC<BadgeProps<string>>;

export default React.memo(function DefaultNamedCard() {
  return <CardBody />;
});

export function outerScope(svc?: ApiClient) {
  const inner = ((x: number) => {
    if (x > 0) {
      return innerOnlyCall(x);
    }
    return 0;
  }) satisfies InnerFn;
  let mutableHelper = (v: number) => svc?.save(v);
  var legacyHelper = function (v: number) { return fallbackRun(v); };
  return inner(1) + Number(mutableHelper(2)) + legacyHelper(3);
}
"""
    tsx_parsed = TypeScriptASTParser().parse(tsx_source, file_path="src/DataTable.tsx")
    by_qname = {c.qualified_name: c for c in tsx_parsed.chunks}
    assert "DataTable" in by_qname
    assert by_qname["DataTable"].called_symbols == ("memo", "Grid")
    assert "UserBadge" in by_qname
    assert by_qname["UserBadge"].called_symbols == ("Avatar",)
    assert "DefaultNamedCard" in by_qname
    assert by_qname["DefaultNamedCard"].called_symbols == ("memo", "CardBody")
    assert "outerScope.inner" in by_qname
    assert "outerScope.mutableHelper" in by_qname
    assert "outerScope.legacyHelper" in by_qname
    assert by_qname["outerScope.mutableHelper"].called_symbols == ("save",)
    # Nested `satisfies` helper must stay scope-isolated from outerScope
    assert "innerOnlyCall" not in by_qname["outerScope"].called_symbols
    assert by_qname["outerScope.inner"].called_symbols == ("innerOnlyCall",)

    cc_map = {
        m.qualified_name: m
        for m in TypeScriptComplexityAnalyzer(language="tsx").analyze_source(tsx_source)
    }
    assert cc_map["outerScope"].cyclomatic_complexity == 1
    assert cc_map["outerScope.inner"].cyclomatic_complexity == 2
    assert "DataTable" in cc_map
    assert "UserBadge" in cc_map

    # `.ts` angle-bracket `<Fn>(...)` type_assertion
    ts_assert_src = "export const asserted = <MyFn>((x: number) => compute(x));\n"
    ts_parsed = TypeScriptASTParser().parse(ts_assert_src, file_path="src/asserted.ts")
    assert [c.qualified_name for c in ts_parsed.chunks] == ["asserted"]
    assert ts_parsed.chunks[0].called_symbols == ("compute",)


def test_oversized_concise_arrow_tsx_and_curried_arrows_split_into_subchunks() -> None:
    """Regression test for F-04: concise JSX arrow & curried arrow > 150 lines split cleanly."""
    jsx_rows = [f"      <RowItem key={{{i}}} val={{compute_{i}()}} />" for i in range(160)]
    concise_src = (
        "export const GiantDashboard = () => (\n  <section>\n"
        + "\n".join(jsx_rows)
        + "\n  </section>\n);\n"
    )
    parsed = TypeScriptASTParser(max_chunk_lines=150).parse(
        concise_src, file_path="src/GiantDashboard.tsx"
    )
    assert parsed.has_syntax_error is False
    assert len(parsed.chunks) >= 2
    assert all(c.is_subchunk for c in parsed.chunks)
    assert all(c.line_count <= 150 for c in parsed.chunks)
    assert parsed.chunks[0].start_line == 1
    assert parsed.chunks[-1].end_line == len(concise_src.splitlines())
    for prev, nxt in itertools.pairwise(parsed.chunks):
        assert nxt.start_line == prev.end_line + 1

    body_lines = [f"    total += step_{i}(req);" for i in range(160)]
    curried_src = (
        "export const withAuth = (cfg: Cfg) => async (req: Req) => {\n"
        "  let total = 0;\n" + "\n".join(body_lines) + "\n  return total;\n};\n"
    )
    curried_parsed = TypeScriptASTParser(max_chunk_lines=150).parse(
        curried_src, file_path="src/withAuth.ts"
    )
    assert len(curried_parsed.chunks) >= 2
    assert all(c.is_subchunk for c in curried_parsed.chunks)
    assert all(c.line_count <= 150 for c in curried_parsed.chunks)


def test_object_literal_methods_ambient_declarations_namespaces_jsdoc_and_jsx_lowercase() -> None:
    """Regression tests for F-05, F-06, and F-09."""
    source = """
declare class AmbientSDK {
  connect(): void;
}
declare global {
  interface Window {
    __APP__: boolean;
  }
}

/** Order HTTP client with retry policy. */
@Injectable()
export abstract class DecoratedOrderClient<T> {
  /** Submit a single order. */
  @Trace("submit")
  async submit(item: T): Promise<void> {
    return send(item);
  }
}

/** Local entity with decorator. */
@Entity()
class DecoratedEntity {}

export const storeA = {
  run(x: number) {
    if (x > 0) return saveA(x);
    return 0;
  },
};
export const storeB = {
  run(x: number) {
    return saveB(x);
  },
};

export function fetchWithObjectCallback() {
  return request({
    onRetry(err: Error) {
      if (err) {
        return retryCall(err);
      }
      return null;
    },
  });
}

export namespace V1 {
  export function parse(raw: string) {
    return parseV1(raw);
  }
}
export namespace V2 {
  export function parse(raw: string) {
    return parseV2(raw);
  }
}

export const AnimatedBox = () => (
  <motion.div>
    <styled.button />
    <Dialog.Panel />
  </motion.div>
);
"""
    parsed = TypeScriptASTParser().parse(source, file_path="src/scopes.tsx")
    by_qname = {c.qualified_name: c for c in parsed.chunks}

    # F-05: ambient declarations skipped, object-literal `run` not emitted as top-level class method
    assert "AmbientSDK" not in by_qname
    assert "Window" not in by_qname
    assert "run" not in by_qname
    assert "fetchWithObjectCallback.onRetry" not in by_qname
    assert by_qname["fetchWithObjectCallback"].called_symbols == ("request", "retryCall")
    assert by_qname["fetchWithObjectCallback"].cyclomatic_complexity == 2

    # F-05: namespace qualification avoids V1.parse / V2.parse collision
    assert "V1.parse" in by_qname
    assert "V2.parse" in by_qname
    assert by_qname["V1.parse"].called_symbols == ("parseV1",)
    assert by_qname["V2.parse"].called_symbols == ("parseV2",)

    # F-06: JSDoc preserved on decorated exported & non-exported classes and methods
    assert by_qname["DecoratedOrderClient"].docstring == "Order HTTP client with retry policy."
    assert by_qname["DecoratedOrderClient.submit"].docstring == "Submit a single order."
    assert by_qname["DecoratedEntity"].docstring == "Local entity with decorator."

    # F-09: JSX member expressions with lowercase HTML props do not pollute called_symbols
    assert by_qname["AnimatedBox"].called_symbols == ("Panel",)
    assert "div" not in by_qname["AnimatedBox"].called_symbols
    assert "button" not in by_qname["AnimatedBox"].called_symbols


def test_detect_missing_test_companion_ignores_ambient_dts_pyi_and_jsx_comments() -> None:
    """Regression test for F-07: .d.ts/.pyi stubs and JSX `{/* ... */}` comments excluded."""
    dts_patch = "@@ -0,0 +1,55 @@\n" + "\n".join(
        f"+export interface GeneratedType{i} {{ id: string; }}" for i in range(55)
    )
    pyi_patch = "@@ -0,0 +1,55 @@\n" + "\n".join(f"+def stub_{i}() -> None: ..." for i in range(55))
    jsx_comment_patch = "@@ -0,0 +1,61 @@\n" + "\n".join(
        [
            "+{/* single line jsx comment */}",
            "+{/*",
            *[f"+  JSX documentation line {i}" for i in range(58)],
            "+*/}",
        ],
    )
    jsx_mixed_patch = "@@ -0,0 +1,1 @@\n+{/* inline */} <Badge />"
    pr_diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(filename="src/types/api.d.ts", patch=dts_patch, status="added"),
            PatchFileEntry(filename="src/core/types.pyi", patch=pyi_patch, status="added"),
            PatchFileEntry(filename="src/ui/Banner.tsx", patch=jsx_comment_patch, status="added"),
            PatchFileEntry(filename="src/ui/Mixed.tsx", patch=jsx_mixed_patch, status="added"),
        ]
    )
    report = detect_missing_test_companion(pr_diff)
    assert report.added_source_logic_lines == 1
    assert report.is_untested is False
    assert report.source_files == ("src/ui/Banner.tsx", "src/ui/Mixed.tsx")


def test_ts_string_literal_type_with_colon_keyword_passes_zerocode_guard() -> None:
    src = 'export const notify = (mode: "Note: if set" | "Error: return null"): string => mode;\n'
    chunks = TypeScriptASTParser().parse(src, file_path="src/notify.ts").chunks
    item = TransientEmbeddingItem.from_code_chunk(
        chunks[0], repo_full_name="acme/web", blob_sha="b2" * 20
    )
    assert item.payload["symbol_name"] == "notify"
    assert AUDIT_PAYLOADS([{"id": item.point_id, "payload": item.payload}]) == []

    # Backticks, single quotes, and escaped quotes in string-literal types also pass cleanly
    escaped_src = (
        "export const fmt = (tpl: `Warn: if \\`empty\\`` | 'Info: return 0'): string => tpl;\n"
    )
    escaped_chunks = TypeScriptASTParser().parse(escaped_src, file_path="src/fmt.ts").chunks
    escaped_item = TransientEmbeddingItem.from_code_chunk(
        escaped_chunks[0], repo_full_name="acme/web", blob_sha="b2" * 20
    )
    assert escaped_item.payload["symbol_name"] == "fmt"
    assert AUDIT_PAYLOADS([{"id": escaped_item.point_id, "payload": escaped_item.payload}]) == []

    # Genuine inline body leaks outside quotes remain strictly blocked
    with pytest.raises(ZeroCodeViolationError, match="signature"):
        TransientEmbeddingItem.from_code_chunk(
            chunks[0].__class__(
                **{
                    **{
                        f: getattr(chunks[0], f)
                        for f in (
                            "file_path",
                            "language",
                            "symbol_name",
                            "qualified_name",
                            "symbol_kind",
                            "start_line",
                            "end_line",
                        )
                    },
                    "signature": 'export const notify = (mode: "Note: if set") => { return mode; }',
                }
            ),
            repo_full_name="acme/web",
            blob_sha="b2" * 20,
        )
