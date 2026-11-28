"""Shared Sprint 3 AI PR review pipeline for Celery Worker and CLI `codesight review`."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight.agents.graph import run_pr_review_graph
from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes.context_builder import PRReviewContext
from codesight.agents.nodes.critic_agent import deduplicate_verified_findings
from codesight.agents.schemas import TelemetryStatus, VerifiedFinding
from codesight.ast_engine.models import PRFileContext
from codesight.ast_engine.registry import ASTParserRegistry
from codesight.config_reader.repo_config import RepoConfig
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.db.models import PRReview, Repository, ReviewFinding, WebhookEvent
from codesight.db.repositories import upsert_repository
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedFileDiff, ParsedPullRequestDiff
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.retriever import AssembledPRContext, HybridContextRetriever
from codesight.scoring.debt_scorer import (
    DebtScoreBreakdown,
    FileComplexityInput,
    TechnicalDebtScorer,
)
from codesight.scoring.report_composer import (
    ReportComposer,
    SmartGateDecision,
    evaluate_smart_quality_gate,
)
from codesight.vcs.blob_fetcher import BlobSliceRequest, GitHubBlobFetcher
from codesight.vcs.github_auth import GitHubAuthError
from codesight.vcs.github_publisher import ReviewCommentDraft
from codesight.worker.sha_cache import (
    CachedFileReview,
    RedisBlobSHACache,
    build_reduced_parsed_diff,
    compute_combined_tokens_saved_pct,
    compute_patch_fingerprint,
    is_full_blob_sha,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

MAX_FULL_BLOB_LINES: Final[int] = 100_000

logger = structlog.get_logger("codesight.worker.ai_review_pipeline")


@dataclass(frozen=True, slots=True)
class PRReviewExecutionResult:
    """Complete in-memory AI review output, Debt Score, Smart Gate, and Markdown summary."""

    parsed_diff: ParsedPullRequestDiff
    verified_findings: tuple[VerifiedFinding, ...]
    inline_drafts: tuple[ReviewCommentDraft, ...]
    info_findings: tuple[VerifiedFinding, ...]
    breakdown: DebtScoreBreakdown
    gate: SmartGateDecision
    summary_markdown: str
    cache_hits: int
    cache_misses: int
    tokens_saved_pct: float
    latency_ms: int
    after_sources_by_file: dict[str, str]
    llm_calls: int
    trace_id: str
    telemetry: TelemetryStatus | None = None


def _normalize_text_sources(
    raw_sources: Mapping[str, str | bytes] | None,
) -> dict[str, str]:
    if not raw_sources:
        return {}
    normalized: dict[str, str] = {}
    for raw_path, content in raw_sources.items():
        norm = normalize_diff_path(raw_path).rstrip("/")
        if not norm:
            continue
        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        normalized[norm] = text
    return normalized


async def _fetch_file_sources_in_ram(
    *,
    repo_full_name: str,
    files: Sequence[ParsedFileDiff],
    head_sha: str,
    base_ref: str | None,
    blob_fetcher: GitHubBlobFetcher | None,
    installation_id: int | None,
    in_memory_after_sources: Mapping[str, str | bytes] | None,
    in_memory_before_sources: Mapping[str, str | bytes] | None,
    ast_registry: ASTParserRegistry | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Load after/before sources strictly in RAM for AST parsing and tree-sitter validation."""
    after_sources = _normalize_text_sources(in_memory_after_sources)
    before_sources = _normalize_text_sources(in_memory_before_sources)

    if blob_fetcher is None or not files:
        return after_sources, before_sources

    requests: list[BlobSliceRequest] = []
    targets: list[tuple[str, str]] = []
    clean_head = head_sha.strip()
    clean_base = base_ref.strip() if isinstance(base_ref, str) and base_ref.strip() else ""

    for pf in files:
        if pf.change_type == "deleted":
            continue
        norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
        if ast_registry is not None and ast_registry.resolve_language(norm_path) is None:
            continue
        if norm_path not in after_sources:
            sha = (
                pf.blob_sha.strip()
                if isinstance(pf.blob_sha, str) and is_full_blob_sha(pf.blob_sha)
                else None
            )
            if sha or clean_head:
                requests.append(
                    BlobSliceRequest(
                        repo_full_name=repo_full_name,
                        file_path=norm_path,
                        start_line=1,
                        end_line=MAX_FULL_BLOB_LINES,
                        blob_sha=sha,
                        ref=clean_head or None,
                        slice_id=f"after:{norm_path}",
                    )
                )
                targets.append(("after", norm_path))

        if pf.change_type != "added" and clean_base and norm_path not in before_sources:
            before_path = (
                normalize_diff_path(pf.old_path).rstrip("/") if pf.old_path else norm_path
            ) or norm_path
            requests.append(
                BlobSliceRequest(
                    repo_full_name=repo_full_name,
                    file_path=before_path,
                    start_line=1,
                    end_line=MAX_FULL_BLOB_LINES,
                    ref=clean_base,
                    slice_id=f"before:{norm_path}",
                )
            )
            targets.append(("before", norm_path))

    if not requests:
        return after_sources, before_sources

    report = await blob_fetcher.fetch_slices(requests, installation_id=installation_id)
    for (side, norm_path), slice_res in zip(targets, report.slices, strict=True):
        if slice_res.status != "ok":
            continue
        if side == "after":
            after_sources[norm_path] = slice_res.content
        else:
            before_sources[norm_path] = slice_res.content

    return after_sources, before_sources


def _resolve_ast_contexts(
    ast_registry: ASTParserRegistry,
    files: Sequence[ParsedFileDiff],
    after_sources: Mapping[str, str],
    before_sources: Mapping[str, str],
) -> tuple[list[PRFileContext], dict[str, FileComplexityInput]]:
    """Run synchronous tree-sitter AST & complexity extraction off the asyncio event loop."""
    file_contexts: list[PRFileContext] = []
    fresh_complexities: dict[str, FileComplexityInput] = {}
    for pf in files:
        norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
        file_ctx = ast_registry.resolve_pr_file(
            pf,
            after_source=after_sources.get(norm_path),
            before_source=before_sources.get(norm_path),
        )
        file_contexts.append(file_ctx)
        fresh_complexities[norm_path] = FileComplexityInput.from_summary(
            norm_path,
            file_ctx.complexity,
            from_cache=False,
        )
    return file_contexts, fresh_complexities


def parse_uuid(raw: str | None) -> uuid.UUID | None:
    """Parse an optional string as UUID, returning None when absent or malformed."""
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def execute_ai_pr_review(
    *,
    repo_full_name: str,
    repo_id: str,
    pr_number: int,
    head_sha: str,
    parsed_diff: ParsedPullRequestDiff,
    repo_config: RepoConfig,
    settings: Settings,
    trace_id: str,
    rag_repo_id: str | None = None,
    base_ref: str | None = None,
    installation_id: int | None = None,
    blob_fetcher: GitHubBlobFetcher | None = None,
    llm_factory: LLMFactory | None = None,
    sha_cache: RedisBlobSHACache | None = None,
    qdrant_store: QdrantZeroCodeStore | None = None,
    embedder: GeminiEmbedder | None = None,
    in_memory_after_sources: Mapping[str, str | bytes] | None = None,
    in_memory_before_sources: Mapping[str, str | bytes] | None = None,
    diff_source: str = "diff",
    span_exporter: SpanExporter | None = None,
) -> PRReviewExecutionResult:
    """Execute SHA-cached AST + Hybrid RAG + LangGraph review, Debt Scorer, and Smart Gate."""
    started = time.perf_counter()

    if sha_cache is not None:
        lookup = await sha_cache.get_many(
            repo_id=repo_id,
            config_hash=repo_config.config_hash,
            files=parsed_diff.files,
        )
        cached_hits = lookup.hits
        uncached_files = list(lookup.misses)
    else:
        cached_hits = {}
        uncached_files = list(parsed_diff.files)

    reviewable_uncached = [
        pf
        for pf in uncached_files
        if pf.change_type != "deleted" and pf.hunks and pf.annotated_diff.strip()
    ]

    ast_registry = ASTParserRegistry()
    after_sources: dict[str, str] = {}
    before_sources: dict[str, str] = {}
    fresh_findings: list[VerifiedFinding] = []
    fresh_complexities: dict[str, FileComplexityInput] = {}
    llm_calls = 0
    rag_degraded = False
    telemetry: TelemetryStatus | None = None

    try:
        if reviewable_uncached:
            after_sources, before_sources = await _fetch_file_sources_in_ram(
                repo_full_name=repo_full_name,
                files=reviewable_uncached,
                head_sha=head_sha,
                base_ref=base_ref,
                blob_fetcher=blob_fetcher,
                installation_id=installation_id,
                in_memory_after_sources=in_memory_after_sources,
                in_memory_before_sources=in_memory_before_sources,
                ast_registry=ast_registry,
            )

            try:
                file_contexts, fresh_complexities = await asyncio.to_thread(
                    _resolve_ast_contexts,
                    ast_registry,
                    reviewable_uncached,
                    after_sources,
                    before_sources,
                )
            finally:
                before_sources.clear()

            reduced_diff = build_reduced_parsed_diff(parsed_diff, reviewable_uncached)

            assembled_context: AssembledPRContext | None = None
            if qdrant_store is not None and blob_fetcher is not None:
                effective_rag_repo_id = (
                    rag_repo_id
                    if rag_repo_id is not None
                    else (repo_id if parse_uuid(repo_id) is not None else None)
                )
                try:
                    retriever = HybridContextRetriever(store=qdrant_store, embedder=embedder)
                    assembled_context = await retriever.retrieve_and_pack(
                        file_contexts,
                        repo_full_name=repo_full_name,
                        blob_fetcher=blob_fetcher,
                        repo_id=effective_rag_repo_id,
                        parsed_diff=reduced_diff,
                        head_ref=head_sha or None,
                        installation_id=installation_id,
                        in_memory_sources=after_sources,
                    )
                    if assembled_context.retrieval.doc_search_degraded:
                        rag_degraded = True
                except (GitHubAuthError, RateLimitTaskError):
                    raise
                except (RetryableTaskError, Exception) as exc:
                    rag_degraded = True
                    logger.warning(
                        "ai_pipeline_rag_degraded",
                        trace_id=trace_id,
                        repo=repo_full_name,
                        error_type=type(exc).__name__,
                    )

            active_llm_factory = (
                llm_factory if llm_factory is not None else LLMFactory(settings=settings)
            )
            review_context = PRReviewContext(
                file_contexts=tuple(file_contexts),
                assembled_context=assembled_context,
                repo_config=repo_config,
                after_sources_by_file=after_sources,
            )
            final_state = await run_pr_review_graph(
                parsed_diff=reduced_diff,
                llm_factory=active_llm_factory,
                review_context=review_context,
                pr_id=f"{repo_full_name}#{pr_number}",
                head_sha=head_sha,
                trace_id=trace_id,
                settings=settings,
                span_exporter=span_exporter,
            )
            fresh_findings = list(final_state.verified_findings)
            llm_calls = len(final_state.llm_metrics)
            telemetry = final_state.telemetry

            if sha_cache is not None and not rag_degraded:
                ineligible_paths: set[str] = {
                    normalize_diff_path(seg.file_path).rstrip("/") or seg.file_path
                    for seg in final_state.unreviewable_segments
                }
                batch_by_index = {batch.batch_index: batch for batch in final_state.batches}
                for br in final_state.batch_results:
                    if br.truncated_candidates_count > 0:
                        batch = batch_by_index.get(br.batch_index)
                        if batch is not None:
                            for fp in batch.file_paths:
                                ineligible_paths.add(normalize_diff_path(fp).rstrip("/") or fp)

                fresh_by_file: dict[str, list[VerifiedFinding]] = defaultdict(list)
                for vf in fresh_findings:
                    norm_vf_path = normalize_diff_path(vf.file_path).rstrip("/") or vf.file_path
                    fresh_by_file[norm_vf_path].append(vf)

                cache_entries: list[CachedFileReview] = []
                for pf in reviewable_uncached:
                    if not is_full_blob_sha(pf.blob_sha):
                        continue
                    norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
                    if norm_path in ineligible_paths:
                        continue
                    assert pf.blob_sha is not None
                    fc = fresh_complexities.get(norm_path) or FileComplexityInput(norm_path)
                    cache_entries.append(
                        CachedFileReview(
                            file_path=norm_path,
                            blob_sha=pf.blob_sha.strip().lower(),
                            config_hash=repo_config.config_hash,
                            patch_fingerprint=compute_patch_fingerprint(pf),
                            findings=tuple(fresh_by_file.get(norm_path, ())),
                            net_delta_cc=fc.net_delta_cc,
                            high_cc_count=fc.high_cc_count,
                        )
                    )
                if cache_entries:
                    await sha_cache.set_many(
                        repo_id=repo_id,
                        config_hash=repo_config.config_hash,
                        entries=cache_entries,
                    )

        cached_findings: list[VerifiedFinding] = []
        cached_files: list[ParsedFileDiff] = []
        cached_files_needing_sources: list[ParsedFileDiff] = []
        file_complexities: list[FileComplexityInput] = []
        for pf in parsed_diff.files:
            norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
            hit = cached_hits.get(norm_path)
            if hit is not None:
                cached_files.append(pf)
                cached_findings.extend(hit.findings)
                if (
                    ast_registry.resolve_language(norm_path) is not None
                    and norm_path not in after_sources
                    and any(
                        f.severity in {"CRITICAL", "WARNING"} and f.suggestion is not None
                        for f in hit.findings
                    )
                ):
                    cached_files_needing_sources.append(pf)
                file_complexities.append(
                    FileComplexityInput(
                        file_path=norm_path,
                        net_delta_cc=hit.net_delta_cc,
                        high_cc_count=hit.high_cc_count,
                        from_cache=True,
                    )
                )
            elif norm_path in fresh_complexities:
                file_complexities.append(fresh_complexities[norm_path])
            else:
                file_complexities.append(FileComplexityInput(file_path=norm_path, from_cache=False))

        if cached_files_needing_sources:
            cached_after_sources, _ = await _fetch_file_sources_in_ram(
                repo_full_name=repo_full_name,
                files=cached_files_needing_sources,
                head_sha=head_sha,
                base_ref=None,
                blob_fetcher=blob_fetcher,
                installation_id=installation_id,
                in_memory_after_sources=in_memory_after_sources,
                in_memory_before_sources=None,
                ast_registry=ast_registry,
            )
            after_sources.update(cached_after_sources)

        merged_verified = tuple(deduplicate_verified_findings([*cached_findings, *fresh_findings]))
        breakdown = TechnicalDebtScorer().score(
            merged_verified,
            file_complexities=file_complexities,
            parsed_diff=parsed_diff,
            source_globs=repo_config.source_globs,
        )
        gate = evaluate_smart_quality_gate(breakdown, repo_config)

        composer = ReportComposer()
        inline_drafts, info_findings = composer.partition_findings_for_github(merged_verified)
        tokens_saved_pct = compute_combined_tokens_saved_pct(
            parsed_diff,
            cached_files=cached_files,
        )
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        summary_markdown = composer.compose_pr_summary(
            breakdown=breakdown,
            gate=gate,
            findings=merged_verified,
            parsed_diff=parsed_diff,
            head_sha=head_sha,
            config_hash=repo_config.config_hash,
            trace_id=trace_id,
            file_complexities=file_complexities,
            cached_file_paths=set(cached_hits),
            tokens_saved_pct=tokens_saved_pct,
            latency_ms=latency_ms,
            diff_source=diff_source,
        )

        return PRReviewExecutionResult(
            parsed_diff=parsed_diff,
            verified_findings=merged_verified,
            inline_drafts=inline_drafts,
            info_findings=info_findings,
            breakdown=breakdown,
            gate=gate,
            summary_markdown=summary_markdown,
            cache_hits=len(cached_hits),
            cache_misses=len(uncached_files),
            tokens_saved_pct=tokens_saved_pct,
            latency_ms=latency_ms,
            after_sources_by_file=after_sources,
            llm_calls=llm_calls,
            trace_id=trace_id,
            telemetry=telemetry,
        )
    except BaseException:
        after_sources.clear()
        raise


async def persist_pr_review_result(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    result: PRReviewExecutionResult | None = None,
    repo_id: str | None = None,
    delivery_id: str | None = None,
    default_branch: str = "main",
    github_repo_id: int | None = None,
    installation_id: int | None = None,
    review_status: str = "completed",
    webhook_status: str = "processed",
    tokens_saved_pct: float | None = None,
    latency_ms: int | None = None,
    trace_id: str = "-",
) -> uuid.UUID | None:
    """Persist `PRReview` and `ReviewFinding` rows (plus `WebhookEvent` status) to PostgreSQL."""
    async with session_factory() as session:
        if delivery_id:
            await session.execute(
                sa.update(WebhookEvent)
                .where(WebhookEvent.delivery_id == delivery_id)
                .values(status=webhook_status)
            )

        target_repo_uuid: uuid.UUID | None = None
        parsed_repo_uuid = parse_uuid(repo_id)
        if parsed_repo_uuid is not None:
            existing_id = await session.scalar(
                sa.select(Repository.id).where(Repository.id == parsed_repo_uuid)
            )
            if existing_id is not None:
                target_repo_uuid = existing_id

        if target_repo_uuid is None:
            repo_row = await upsert_repository(
                session,
                github_repo_id=github_repo_id,
                full_name=repo_full_name,
                default_branch=(default_branch or "main")[:128],
                installation_id=installation_id,
            )
            target_repo_uuid = repo_row.id

        breakdown = result.breakdown if result is not None else None
        effective_trace = result.trace_id if result is not None else trace_id
        effective_tokens = result.tokens_saved_pct if result is not None else tokens_saved_pct
        effective_latency = (
            latency_ms
            if latency_ms is not None
            else (result.latency_ms if result is not None else 0)
        )

        pr_review = PRReview(
            repo_id=target_repo_uuid,
            pr_number=pr_number,
            head_sha=(head_sha or "unknown")[:64],
            debt_score=breakdown.score if breakdown is not None else None,
            critical_count=breakdown.critical_count if breakdown is not None else 0,
            warning_count=breakdown.warning_count if breakdown is not None else 0,
            info_count=breakdown.info_count if breakdown is not None else 0,
            tokens_saved_pct=effective_tokens,
            latency_ms=max(0, effective_latency),
            status=review_status,
            trace_id=effective_trace[:128] if effective_trace else None,
        )
        session.add(pr_review)
        await session.flush()

        if result is not None:
            for finding in result.verified_findings:
                session.add(
                    ReviewFinding(
                        pr_review_id=pr_review.id,
                        file_path=finding.file_path[:1024],
                        start_line=finding.start_line,
                        end_line=finding.end_line,
                        severity=finding.severity,
                        category=finding.category[:64],
                        confidence=finding.confidence,
                        message=finding.render_markdown_body(),
                        suggestion_code=(
                            finding.suggestion.replacement_code
                            if finding.suggestion is not None
                            else None
                        ),
                    )
                )

        await session.commit()
        return pr_review.id
