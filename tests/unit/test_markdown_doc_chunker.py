"""Unit tests for Markdown architecture doc classification and MarkdownDocChunker (CA-14)."""

from __future__ import annotations

import itertools

import pytest

from codesight.rag.doc_chunker import (
    MAX_DOC_SECTION_CHARS,
    MarkdownDocChunker,
    is_architecture_doc_path,
)
from codesight.rag.models import validate_zero_code_payload


def test_is_architecture_doc_path_allowlist_and_noise_exclusion() -> None:
    allowed = [
        "README.md",
        "readme.md",
        "services/auth/README.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "CLAUDE.md",
        "ARCHITECTURE.md",
        "ADR/0001-zero-code-rag.md",
        "adr/0002-celery-locks.md",
        "docs/adr/0003-qdrant-grpc.md",
        "docs/adrs/0004-diff-mapping.md",
        "docs/architecture/overview.md",
        "docs/specs/2026-11-15-ca-5-day1-foundation-design.md",
        "./docs\\architecture\\rag\\hybrid.MD",
    ]
    for path in allowed:
        assert is_architecture_doc_path(path) is True, f"Expected allowed doc path: {path}"

    excluded = [
        "",
        "src/codesight/rag/indexer.py",
        "CHANGELOG.md",
        "docs/CHANGELOG.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/README.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        "notes/meeting.md",
        "docs/plans/scratch.md",
        "docs/adr/diagram.png",
    ]
    for path in excluded:
        assert is_architecture_doc_path(path) is False, f"Expected excluded path: {path}"


def test_markdown_doc_chunker_respects_fenced_code_blocks_and_frontmatter_lines() -> None:
    doc = (
        "---\n"
        "title: ADR 0001\n"
        "status: accepted\n"
        "---\n"
        "\n"
        "# Zero-Code RAG\n"
        "\n"
        "## Persistence Invariant\n"
        "Never store customer source code in Qdrant payloads.\n"
        "\n"
        "```python\n"
        "# This Python comment is NOT a Markdown heading\n"
        "def verify():\n"
        "    return True\n"
        "```\n"
        "\n"
        "~~~bash\n"
        "## Also not a Markdown heading\n"
        "echo ok\n"
        "~~~\n"
        "\n"
        "### Recovery Strategy\n"
        "Fetch blobs transiently in RAM via GitHub Blob API.\n"
    )
    chunker = MarkdownDocChunker()
    sections = chunker.chunk_markdown(doc, file_path="docs/adr/0001-zero-code.md")

    assert len(sections) == 2

    first, second = sections
    assert first.section_title == "Zero-Code RAG > Persistence Invariant"
    assert first.heading_level == 2
    assert first.start_line == 8
    assert first.end_line == 20
    assert "# This Python comment is NOT a Markdown heading" in first.transient_excerpt
    assert "## Also not a Markdown heading" in first.transient_excerpt

    assert second.section_title == "Zero-Code RAG > Persistence Invariant > Recovery Strategy"
    assert second.heading_level == 3
    assert second.start_line == 22
    assert second.end_line == 23
    assert "Fetch blobs transiently in RAM" in second.transient_excerpt


def test_markdown_doc_chunker_handles_preamble_and_headingless_docs() -> None:
    preamble_doc = (
        "Project overview before any heading.\n"
        "Second intro line.\n"
        "\n"
        "# Setup\n"
        "Run uv sync to install dependencies.\n"
    )
    chunker = MarkdownDocChunker()
    sections = chunker.chunk_markdown(preamble_doc, file_path="README.md")

    assert len(sections) == 2
    assert sections[0].section_title == "Document Overview (README.md)"
    assert sections[0].heading_level == 0
    assert sections[0].start_line == 1
    assert sections[0].end_line == 2
    assert sections[1].section_title == "Setup"
    assert sections[1].start_line == 4
    assert sections[1].end_line == 5

    plain = "Just architecture principles without headings.\nLine two."
    plain_sections = chunker.chunk_markdown(plain, file_path="ARCHITECTURE.md")
    assert len(plain_sections) == 1
    assert plain_sections[0].section_title == "Document Overview (ARCHITECTURE.md)"
    assert plain_sections[0].start_line == 1
    assert plain_sections[0].end_line == 2


def test_markdown_doc_chunker_splits_oversized_sections_with_exact_line_bounds() -> None:
    paragraphs = [
        f"Paragraph {idx}: " + ("architecture invariant detail " * 14) for idx in range(1, 10)
    ]
    lines = ["# System Design", "## Data Flow"]
    for para in paragraphs:
        lines.append(para)
        lines.append("")
    raw_doc = "\n".join(lines)

    chunker = MarkdownDocChunker(max_section_chars=MAX_DOC_SECTION_CHARS)
    sections = chunker.chunk_markdown(raw_doc, file_path="docs/architecture/flow.md")

    assert len(sections) >= 2
    assert sections[0].section_title == "System Design > Data Flow (part 1)"
    assert sections[1].section_title == "System Design > Data Flow (part 2)"
    for sec in sections:
        assert len(sec.transient_excerpt) <= MAX_DOC_SECTION_CHARS
        assert 1 <= sec.start_line <= sec.end_line
    assert sections[0].start_line == 2
    assert sections[-1].end_line == len(lines) - 1
    for prev_sec, next_sec in itertools.pairwise(sections):
        assert prev_sec.end_line < next_sec.start_line

    # Single huge line (> max_section_chars) must also split cleanly without losing content
    giant_line = "x" * (MAX_DOC_SECTION_CHARS + 500)
    giant_sections = chunker.chunk_markdown(
        f"# Huge\n{giant_line}\n", file_path="docs/architecture/huge.md"
    )
    assert len(giant_sections) == 2
    assert all(len(sec.transient_excerpt) <= MAX_DOC_SECTION_CHARS for sec in giant_sections)


def test_markdown_doc_section_converts_to_nfr3_compliant_transient_item() -> None:
    chunker = MarkdownDocChunker()
    sections = chunker.chunk_markdown(
        b"# Security\nAlways validate HMAC signatures.\n",
        file_path="AGENTS.md",
    )
    assert len(sections) == 1

    item = sections[0].to_transient_embedding_item(
        repo_full_name="Tieens-04/CodeSight-AI",
        blob_sha="deadbeef1234",
        repo_id="11111111-1111-1111-1111-111111111111",
        indexed_at="2026-11-21T12:00:00Z",
    )
    clean_payload = validate_zero_code_payload(item.payload, point_id=item.point_id)
    assert clean_payload["chunk_type"] == "architecture_doc"
    assert clean_payload["language"] == "markdown"
    assert clean_payload["symbol_kind"] == "section"
    assert clean_payload["symbol_name"] == "Security"
    assert clean_payload["start_line"] == 1
    assert clean_payload["end_line"] == 2
    assert "Always validate HMAC signatures." in (item.peek_embedding_text() or "")
    item.clear_transient_text()
    assert item.peek_embedding_text() is None


def test_markdown_doc_chunker_rejects_binary_empty_and_invalid_config() -> None:
    chunker = MarkdownDocChunker()
    assert chunker.chunk_markdown(b"\x00\x01binary", file_path="README.md") == ()
    assert chunker.chunk_markdown("   \n\n  ", file_path="README.md") == ()
    assert chunker.chunk_markdown("---\nonly: frontmatter\n---\n", file_path="README.md") == ()

    with pytest.raises(ValueError, match="max_section_chars"):
        MarkdownDocChunker(max_section_chars=0)
    with pytest.raises(ValueError, match="max_section_chars"):
        MarkdownDocChunker(max_section_chars=True)


def test_markdown_doc_chunker_f05_punctuation_heading_and_horizontal_rules() -> None:
    chunker = MarkdownDocChunker()

    # 1. Punctuation-only / multi-colon headings must not crash with empty section_title
    punct_doc = (
        "# :\n"
        "Architecture prose after single colon heading.\n"
        "# ::\n"
        "Architecture prose after double colon heading.\n"
        "## :::\n"
        "Architecture prose after triple colon heading.\n"
        "### : :  \n"
        "More details after spaced colons.\n"
        "## Clean Heading : ::\n"
        "Body under heading with trailing colons.\n"
    )
    punct_sections = chunker.chunk_markdown(punct_doc, file_path="README.md")
    assert len(punct_sections) >= 1
    for sec in punct_sections:
        assert sec.section_title.strip() != ""
        item = sec.to_transient_embedding_item(
            repo_full_name="acme/demo",
            blob_sha="sha1",
        )
        validate_zero_code_payload(item.payload, point_id=item.point_id)
    assert any(sec.section_title == "Clean Heading" for sec in punct_sections)

    # 2. CommonMark horizontal rules (`---`) at start/end must NOT be stripped as frontmatter
    hr_doc = "---\n\n# System Architecture\nNever persist raw source code.\n\n---\n"
    hr_sections = chunker.chunk_markdown(hr_doc, file_path="ARCHITECTURE.md")
    assert len(hr_sections) == 1
    assert hr_sections[0].section_title == "System Architecture"
    assert "Never persist raw source code." in hr_sections[0].transient_excerpt

    # 3. UTF-8 BOM + real YAML frontmatter must be stripped while preserving body line coordinates
    bom_doc = "\ufeff---\ntitle: ADR 0009\nstatus: accepted\n---\n# Decision\nUse gRPC.\n"
    bom_sections = chunker.chunk_markdown(bom_doc, file_path="docs/adr/0009-grpc.md")
    assert len(bom_sections) == 1
    assert bom_sections[0].section_title == "Decision"
    assert "title: ADR 0009" not in bom_sections[0].transient_excerpt
    assert bom_sections[0].start_line == 5
    assert bom_sections[0].end_line == 6
