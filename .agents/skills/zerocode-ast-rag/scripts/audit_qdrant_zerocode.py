"""Audit Qdrant collections, payloads, and container storage for NFR-3 Zero-Code Persistence.

Verifies:
1. Required keyword payload indexes exist (`symbol_name`, `called_symbols`, `blob_sha`, or all 7
   mandatory indexes when `strict_indexes=True`).
2. No forbidden source-code keys (`code`, `raw_code`, `source`, `content`, `body`, `text`,
   `document`, `snippet`, `docstring`).
3. `signature` is single-line header metadata (`<= 300` chars) and does not leak function bodies.
4. Optional canary substrings (planted in source bodies, docstrings, comments, defaults, docs)
   occur in `0` bytes across 100% of paginated Qdrant payloads and `/qdrant/storage` on disk.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from typing import Any

ALLOWED_KEYS = {
    "repo_id",
    "repo_full_name",
    "file_path",
    "blob_sha",
    "chunk_type",
    "language",
    "symbol_name",
    "symbol_kind",
    "start_line",
    "end_line",
    "signature",
    "called_symbols",
    "imports",
    "cyclomatic_complexity",
    "indexed_at",
}
FORBIDDEN_KEYS = {
    "code",
    "raw_code",
    "source",
    "content",
    "body",
    "text",
    "document",
    "snippet",
    "docstring",
}
REQUIRED_KEYWORD_INDEXES = {"symbol_name", "called_symbols", "blob_sha"}
ALL_REQUIRED_KEYWORD_INDEXES = {
    "repo_id",
    "repo_full_name",
    "file_path",
    "symbol_name",
    "called_symbols",
    "blob_sha",
    "chunk_type",
}
FORBIDDEN_CONTAINER_PREFIXES = ("flintflow-", "documind-")
BODY_LEAK_RE = re.compile(r"(\n\s+(return|await|raise|if|for|while|const|let)\b|\{\s*\n)")
INLINE_BODY_LEAK_RE = re.compile(
    r"(?:"
    r"(?:[:;]|=>)\s*(?:return|await|yield|if|for|while)\b(?!\s*(?:[,:=?]|\}(?!\s*;?\s*$)))"
    r"|(?:;|=>|\{(?!\.\.\.\}))\s*(?:const|let|var)\s+"
    r"(?:[A-Za-z_$][\w$]*\s*(?:[,=:;]|\}\s*;?\s*$)|[\[{])"
    r"|(?:;|=>|\{(?!\.\.\.\}))\s*throw\b(?!\s*(?:[,:=?]|\}(?!\s*;?\s*$)))"
    r"|\{(?!\.\.\.\})\s*(?:return|await|yield|if|for|while)\b(?!\s*(?:[,:=?]|\}(?!\s*;?\s*$)))"
    r")"
)
_PY_ONLY_INLINE_BODY_LEAK_RE = re.compile(r"[:;]\s*(?:pass|raise)\b(?!\s*[,:=?}\]])")
SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def mask_quoted_literals(text: str) -> str:
    """Replace characters inside quoted literals (`'...'`, `\"...\"`, `` `...` ``) with spaces."""
    if "'" not in text and '"' not in text and "`" not in text:
        return text
    out: list[str] = []
    active_quote: str | None = None
    escaped = False
    for ch in text:
        if active_quote is None:
            if ch in ("'", '"', "`"):
                active_quote = ch
            out.append(ch)
            continue
        if escaped:
            escaped = False
            out.append(" ")
        elif ch == "\\":
            escaped = True
            out.append(" ")
        elif ch == active_quote:
            active_quote = None
            out.append(ch)
        else:
            out.append(" ")
    return "".join(out)


def _has_inline_body_leak(signature: str, *, language: str) -> bool:
    masked = mask_quoted_literals(signature)
    if INLINE_BODY_LEAK_RE.search(masked) is not None:
        return True
    return (
        language not in {"typescript", "tsx"}
        and _PY_ONLY_INLINE_BODY_LEAK_RE.search(masked) is not None
    )


def _validate_collection_name(collection: str) -> str:
    if (
        not isinstance(collection, str)
        or not SAFE_IDENT_RE.fullmatch(collection)
        or ".." in collection
    ):
        raise ValueError(f"Unsafe collection name: {collection!r}")
    return collection


def _validate_container_name(container_name: str) -> str:
    if (
        not isinstance(container_name, str)
        or not SAFE_IDENT_RE.fullmatch(container_name)
        or ".." in container_name
    ):
        raise ValueError(f"Unsafe container name: {container_name!r}")
    if container_name.lower().startswith(FORBIDDEN_CONTAINER_PREFIXES):
        raise ValueError(
            f"Refusing to access sibling project container {container_name!r} "
            "(AGENTS.md port/container coexistence rule)."
        )
    return container_name


def _validate_canaries(
    canary_substrings: Sequence[str],
    *,
    require_non_empty: bool = False,
) -> list[str]:
    cleaned: list[str] = []
    for raw in canary_substrings:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"Invalid empty or non-string canary marker: {raw!r}")
        if "\n" in raw or "\r" in raw or "\x00" in raw:
            raise ValueError(f"Canary marker must be single-line without CR/LF/NUL bytes: {raw!r}")
        cleaned.append(raw)
    if require_non_empty and not cleaned:
        raise ValueError("canary_substrings must contain at least one non-empty canary marker.")
    return cleaned


def _iter_payload_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_payload_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_payload_strings(item)


def audit_payloads(
    points: list[dict[str, Any]],
    *,
    canary_substrings: Sequence[str] = (),
) -> list[str]:
    cleaned_canaries = _validate_canaries(canary_substrings, require_non_empty=False)
    violations: list[str] = []
    for pt in points:
        pid = pt.get("id", "<unknown>")
        payload = pt.get("payload") or {}
        keys = set(payload.keys())

        forbidden_hit = keys & FORBIDDEN_KEYS
        if forbidden_hit:
            violations.append(
                f"Point {pid}: NFR-3 breach — forbidden raw-code keys {sorted(forbidden_hit)}."
            )

        unknown_hit = keys - ALLOWED_KEYS - FORBIDDEN_KEYS
        if unknown_hit:
            violations.append(f"Point {pid}: unapproved payload keys {sorted(unknown_hit)}.")

        if "signature" in payload:
            sig = payload["signature"]
            if not isinstance(sig, str):
                violations.append(
                    f"Point {pid}: `signature` must be a string, got {type(sig).__name__}."
                )
            else:
                if len(sig) > 300:
                    violations.append(
                        f"Point {pid}: `signature` length {len(sig)} > 300 chars "
                        "(possible body leak)."
                    )
                chunk_type = str(payload.get("chunk_type", "code")).strip().lower()
                language = str(payload.get("language", "")).strip().lower()
                if (
                    "\n" in sig
                    or "\r" in sig
                    or BODY_LEAK_RE.search(sig)
                    or (chunk_type == "code" and _has_inline_body_leak(sig, language=language))
                ):
                    violations.append(
                        f"Point {pid}: `signature` must be single-line header without code body."
                    )

        if cleaned_canaries:
            raw_strings = list(_iter_payload_strings(payload))
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            for idx, canary in enumerate(cleaned_canaries):
                if any(canary in s for s in raw_strings) or canary in serialized:
                    violations.append(
                        f"Point {pid}: NFR-3 breach — raw-code canary #{idx + 1} leaked in payload."
                    )
    return violations


def fetch_json(url: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scroll_all_points(
    base_url: str,
    collection: str,
    *,
    page_size: int = 256,
    max_points: int | None = None,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_json,
) -> list[dict[str, Any]]:
    """Paginate `/points/scroll` across a Qdrant collection until exhaustion or `max_points`."""
    safe_collection = _validate_collection_name(collection)
    if max_points is not None and (isinstance(max_points, bool) or max_points <= 0):
        raise ValueError("max_points must be a positive integer when provided.")

    base = base_url.rstrip("/")
    encoded_col = urllib.parse.quote(safe_collection, safe="")
    points: list[dict[str, Any]] = []
    offset: Any = None
    seen_offsets: set[str] = set()
    eff_page_size = max(1, page_size)

    while True:
        remaining = max_points - len(points) if max_points is not None else eff_page_size
        if max_points is not None and remaining <= 0:
            break
        batch_limit = min(eff_page_size, remaining) if max_points is not None else eff_page_size
        body: dict[str, Any] = {
            "limit": batch_limit,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        resp = fetch_fn(f"{base}/collections/{encoded_col}/points/scroll", method="POST", body=body)
        result = resp.get("result") or {}
        batch = result.get("points") or []
        points.extend(batch)

        next_offset = result.get("next_page_offset")
        if next_offset is None or not batch:
            break
        offset_key = json.dumps(next_offset, sort_keys=True)
        if offset_key in seen_offsets:
            break
        seen_offsets.add(offset_key)
        offset = next_offset

    return points


def scan_container_storage_for_canaries(
    container_name: str,
    collection: str,
    canary_substrings: Sequence[str],
) -> list[str]:
    """Scan `/qdrant/storage/collections/<collection>` inside a Qdrant container for raw code.

    Returns a list of error/violation messages (empty list means 0 bytes leaked on disk).
    """
    safe_container = _validate_container_name(container_name)
    safe_collection = _validate_collection_name(collection)
    cleaned_canaries = _validate_canaries(canary_substrings, require_non_empty=True)

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return ["`docker` executable not found on PATH; cannot audit Qdrant container disk."]

    collection_dir = f"/qdrant/storage/collections/{safe_collection}"
    try:
        check_proc = subprocess.run(
            [docker_bin, "exec", safe_container, "find", collection_dir, "-type", "f"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [
            f"Failed checking Qdrant storage directory '{collection_dir}' "
            f"in container '{safe_container}': {exc}"
        ]
    if check_proc.returncode != 0:
        return [
            f"Qdrant storage directory '{collection_dir}' not accessible in container "
            f"'{safe_container}': {check_proc.stderr.strip()}"
        ]
    persisted_files = [ln.strip() for ln in check_proc.stdout.splitlines() if ln.strip()]
    if not persisted_files:
        return [
            f"Qdrant storage directory '{collection_dir}' contains 0 persisted files on disk; "
            "cannot certify zero-code disk compliance."
        ]

    # Feed LF-delimited raw UTF-8 bytes via stdin (`-f -`) so Windows never translates `\n` to
    # `\r\n` for Linux `grep -F` inside Qdrant. Exclude ephemeral `.atomicwrite*` files and retry
    # briefly if Qdrant background segment compaction renames a file mid-traversal (rc=2).
    pattern_payload = ("\n".join(cleaned_canaries) + "\n").encode("utf-8")
    grep_cmd = [
        docker_bin,
        "exec",
        "-i",
        safe_container,
        "grep",
        "-a",
        "-r",
        "-F",
        "-l",
        "--exclude=.atomicwrite*",
        "-f",
        "-",
        "--",
        collection_dir,
    ]
    last_rc = 2
    last_stderr = ""
    for attempt in range(3):
        try:
            grep_proc = subprocess.run(
                grep_cmd,
                input=pattern_payload,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return [f"Failed scanning Qdrant container storage '{collection_dir}': {exc}"]
        stdout_text = grep_proc.stdout.decode("utf-8", errors="replace")
        stderr_text = grep_proc.stderr.decode("utf-8", errors="replace")
        matched_paths = [ln.strip() for ln in stdout_text.splitlines() if ln.strip()]
        if matched_paths:
            return [
                f"NFR-3 disk breach — raw-code canary matched on Qdrant disk file: {path}"
                for path in matched_paths
            ]
        if grep_proc.returncode == 1:
            # grep exit code 1 == 0 matches across all files in collection_dir
            return []
        if grep_proc.returncode == 0:
            return [f"NFR-3 disk breach — raw-code canary matched inside '{collection_dir}'."]
        last_rc = grep_proc.returncode
        last_stderr = stderr_text.strip()
        if last_rc == 2 and "No such file or directory" in last_stderr and attempt < 2:
            time.sleep(0.25)
            continue
        break
    return [
        f"Failed scanning Qdrant container storage '{collection_dir}' (rc={last_rc}): {last_stderr}"
    ]


def audit_live_collection(
    base_url: str,
    collection: str,
    sample_limit: int = 200,
    *,
    scan_all: bool = False,
    strict_indexes: bool = False,
    canary_substrings: Sequence[str] = (),
) -> int:
    safe_collection = _validate_collection_name(collection)
    if not scan_all and (isinstance(sample_limit, bool) or sample_limit <= 0):
        raise ValueError("sample_limit must be a positive integer when scan_all is False.")
    cleaned_canaries = _validate_canaries(canary_substrings, require_non_empty=False)

    base = base_url.rstrip("/")
    encoded_col = urllib.parse.quote(safe_collection, safe="")
    try:
        info = fetch_json(f"{base}/collections/{encoded_col}")
        max_pts = None if scan_all else sample_limit
        points = scroll_all_points(base, safe_collection, page_size=256, max_points=max_pts)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Cannot audit Qdrant collection '{safe_collection}' at {base}: {exc}")
        return 1

    result = info.get("result", {})
    payload_schema = result.get("payload_schema", {})
    expected_indexes = ALL_REQUIRED_KEYWORD_INDEXES if strict_indexes else REQUIRED_KEYWORD_INDEXES
    missing_indexes = expected_indexes - set(payload_schema.keys())
    errors: list[str] = []
    if missing_indexes:
        errors.append(f"Missing required Qdrant payload keyword indexes: {sorted(missing_indexes)}")
    if strict_indexes:
        for field_name in sorted(expected_indexes & set(payload_schema.keys())):
            field_info = payload_schema.get(field_name) or {}
            data_type = str(field_info.get("data_type", "")).strip().lower()
            if data_type != "keyword":
                errors.append(
                    f"Qdrant payload index '{field_name}' has data_type={data_type!r}; "
                    "expected 'keyword'."
                )

    errors.extend(audit_payloads(points, canary_substrings=cleaned_canaries))

    if errors:
        print(f"[FAIL] Qdrant Zero-Code Audit failed on '{safe_collection}':")
        for err in errors:
            print(f"  - {err}")
        return 1

    mode = "full-scroll" if scan_all else f"sample<={sample_limit}"
    print(
        f"[PASS] Qdrant Zero-Code Audit passed on '{safe_collection}' "
        f"({len(points)} points checked [{mode}], indexes={sorted(payload_schema.keys())})."
    )
    return 0


def run_self_test() -> int:
    clean_points = [
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "payload": {
                "repo_id": "repo-1",
                "repo_full_name": "org/repo",
                "file_path": "src/auth.py",
                "blob_sha": "abc1234",
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "verify_hmac",
                "symbol_kind": "function",
                "start_line": 12,
                "end_line": 28,
                "signature": "def verify_hmac(payload: bytes, sig: str, secret: str) -> bool",
                "called_symbols": ["compare_digest", "new"],
                "imports": ["hmac", "hashlib"],
                "cyclomatic_complexity": 3,
            },
        },
        {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "payload": {
                "repo_id": "repo-1",
                "repo_full_name": "org/repo",
                "file_path": "src/notify.ts",
                "blob_sha": "def5678",
                "chunk_type": "code",
                "language": "typescript",
                "symbol_name": "notify",
                "symbol_kind": "function",
                "start_line": 1,
                "end_line": 3,
                "signature": (
                    'export const notify = (mode: "Note: if set" | "Error: return null"): '
                    "string => mode"
                ),
                "called_symbols": [],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
        },
    ]
    assert audit_payloads(clean_points, canary_substrings=("CODESIGHT_CANARY_SECRET_1",)) == []

    leaky_points = [
        {"id": "1", "payload": {"file_path": "a.py", "code": "def f(): return 1"}},
        {"id": "2", "payload": {"file_path": "b.py", "signature": "def f():\n    x = secret_key"}},
        {"id": "3", "payload": {"file_path": "b2.py", "signature": "def f(): return secret_key"}},
        {
            "id": "4",
            "payload": {
                "file_path": "c.py",
                "signature": 'def f(tok: str = "CODESIGHT_CANARY_SECRET_1")',
            },
        },
    ]
    errs = audit_payloads(leaky_points, canary_substrings=('"CODESIGHT_CANARY_SECRET_1"',))
    assert len(errs) == 4, f"Expected 4 violations, got {errs}"

    # Verify multi-page scroll pagination stops cleanly at next_page_offset=None
    pages = {
        None: {
            "result": {
                "points": [{"id": "p1", "payload": {}}, {"id": "p2", "payload": {}}],
                "next_page_offset": "cursor-2",
            }
        },
        "cursor-2": {
            "result": {
                "points": [{"id": "p3", "payload": {}}],
                "next_page_offset": None,
            }
        },
    }

    def _fake_fetch(
        url: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cursor = (body or {}).get("offset")
        return pages[cursor]

    scrolled = scroll_all_points(
        "http://127.0.0.1:6333",
        "codesight_ast_index",
        page_size=2,
        max_points=None,
        fetch_fn=_fake_fetch,
    )
    assert [pt["id"] for pt in scrolled] == ["p1", "p2", "p3"]

    # Verify unsafe identifiers, sibling containers, CRLF canaries, and zero limit are rejected
    for bad_ctr in ("codesight;rm -rf /", "--help", "..", "flintflow-api", "documind-qdrant"):
        try:
            scan_container_storage_for_canaries(bad_ctr, "col", ("canary",))
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for unsafe container name {bad_ctr!r}")

    for bad_col in ("..", "a..b", "--flag"):
        try:
            scroll_all_points("http://127.0.0.1:6333", bad_col, fetch_fn=_fake_fetch)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for unsafe collection name {bad_col!r}")

    for bad_canary in ("SECRET\r", "SECRET\n", "   "):
        try:
            audit_payloads([], canary_substrings=(bad_canary,))
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for unsafe canary {bad_canary!r}")

    try:
        scroll_all_points("http://127.0.0.1:6333", "col", max_points=0, fetch_fn=_fake_fetch)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for max_points=0")

    print(
        "[PASS] zerocode-ast-rag self-test passed "
        "(clean allowlist + 4 NFR-3 leak/canary detections + pagination + strict input guards)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Qdrant Zero-Code Persistence (NFR-3).")
    parser.add_argument("--self-test", action="store_true", help="Run built-in unit assertions")
    parser.add_argument(
        "--url",
        default="http://localhost:6333",
        help="Qdrant HTTP URL (default: localhost:6333)",
    )
    parser.add_argument(
        "--collection",
        default="codesight_ast_index",
        help="Collection name to inspect (default: codesight_ast_index)",
    )
    parser.add_argument("--limit", type=int, default=200, help="Number of points to sample")
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Paginate and audit 100%% of points in the collection",
    )
    parser.add_argument(
        "--strict-indexes",
        action="store_true",
        help="Require all 7 mandatory keyword payload indexes",
    )
    parser.add_argument(
        "--container",
        default=None,
        help="Optional Qdrant Docker container name to scan `/qdrant/storage` for canaries",
    )
    parser.add_argument(
        "--canary",
        action="append",
        default=[],
        help="Canary string expected to occur 0 times in Qdrant payloads & container storage",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    if args.container and not args.canary:
        parser.error("--container requires at least one --canary marker to scan on disk.")
    if not args.scan_all and args.limit <= 0:
        parser.error("--limit must be a positive integer when --scan-all is not set.")

    try:
        rc = audit_live_collection(
            args.url,
            args.collection,
            args.limit,
            scan_all=args.scan_all,
            strict_indexes=args.strict_indexes,
            canary_substrings=args.canary,
        )
        if rc != 0:
            return rc
        if args.container and args.canary:
            disk_errs = scan_container_storage_for_canaries(
                args.container,
                args.collection,
                args.canary,
            )
            if disk_errs:
                print(f"[FAIL] Qdrant container disk scan failed on '{args.container}':")
                for err in disk_errs:
                    print(f"  - {err}")
                return 1
            print(
                f"[PASS] Qdrant container disk scan passed on '{args.container}' "
                f"({len(args.canary)} canaries checked, 0 bytes leaked)."
            )
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
