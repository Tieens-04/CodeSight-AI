"""Audit Qdrant collections and point payloads for NFR-3 Zero-Code Persistence compliance.

Verifies:
1. Required keyword payload indexes exist (`symbol_name`, `called_symbols`, `blob_sha`).
2. No forbidden source-code keys (`code`, `source`, `content`, `body`, `text`, `document`, `snippet`, `docstring`).
3. `signature` is single-line header metadata (`<= 300` chars) and does not leak function bodies.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
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
BODY_LEAK_RE = re.compile(r"(\n\s+(return|await|raise|if|for|while|const|let)\b|\{\s*\n)")


def audit_payloads(points: list[dict[str, Any]]) -> list[str]:
    violations: list[str] = []
    for pt in points:
        pid = pt.get("id", "<unknown>")
        payload = pt.get("payload") or {}
        keys = set(payload.keys())

        forbidden_hit = keys & FORBIDDEN_KEYS
        if forbidden_hit:
            violations.append(f"Point {pid}: NFR-3 breach — forbidden raw-code keys {sorted(forbidden_hit)}.")

        unknown_hit = keys - ALLOWED_KEYS - FORBIDDEN_KEYS
        if unknown_hit:
            violations.append(f"Point {pid}: unapproved payload keys {sorted(unknown_hit)}.")

        sig = payload.get("signature", "")
        if isinstance(sig, str):
            if len(sig) > 300:
                violations.append(f"Point {pid}: `signature` length {len(sig)} > 300 chars (possible body leak).")
            if BODY_LEAK_RE.search(sig):
                violations.append(f"Point {pid}: `signature` contains multi-line code body statements.")
    return violations


def fetch_json(url: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def audit_live_collection(base_url: str, collection: str, sample_limit: int) -> int:
    base = base_url.rstrip("/")
    try:
        info = fetch_json(f"{base}/collections/{collection}")
    except urllib.error.URLError as exc:
        print(f"[ERROR] Cannot reach Qdrant collection '{collection}' at {base}: {exc}")
        return 1

    result = info.get("result", {})
    payload_schema = result.get("payload_schema", {})
    missing_indexes = REQUIRED_KEYWORD_INDEXES - set(payload_schema.keys())
    errors: list[str] = []
    if missing_indexes:
        errors.append(f"Missing required Qdrant payload keyword indexes: {sorted(missing_indexes)}")

    scroll = fetch_json(
        f"{base}/collections/{collection}/points/scroll",
        method="POST",
        body={"limit": sample_limit, "with_payload": True, "with_vector": False},
    )
    points = scroll.get("result", {}).get("points", [])
    errors.extend(audit_payloads(points))

    if errors:
        print(f"[FAIL] Qdrant Zero-Code Audit failed on '{collection}':")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(
        f"[PASS] Qdrant Zero-Code Audit passed on '{collection}' "
        f"({len(points)} points checked, indexes={sorted(payload_schema.keys())})."
    )
    return 0


def run_self_test() -> int:
    clean_points = [
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "payload": {
                "repo_full_name": "org/repo",
                "file_path": "src/auth.py",
                "blob_sha": "abc1234",
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
        }
    ]
    assert audit_payloads(clean_points) == []

    leaky_points = [
        {"id": "1", "payload": {"file_path": "a.py", "code": "def f(): return 1"}},
        {"id": "2", "payload": {"file_path": "b.py", "signature": "def f():\n    return secret_key"}},
    ]
    errs = audit_payloads(leaky_points)
    assert len(errs) == 2, f"Expected 2 violations, got {errs}"
    print("[PASS] zerocode-ast-rag self-test passed (clean allowlist + 2 NFR-3 leak detections).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Qdrant Zero-Code Persistence (NFR-3).")
    parser.add_argument("--self-test", action="store_true", help="Run built-in unit assertions")
    parser.add_argument("--url", default="http://localhost:6333", help="Qdrant HTTP URL (default: localhost:6333)")
    parser.add_argument(
        "--collection",
        default="codesight_ast_index",
        help="Collection name to inspect (default: codesight_ast_index)",
    )
    parser.add_argument("--limit", type=int, default=200, help="Number of points to sample")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    return audit_live_collection(args.url, args.collection, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
