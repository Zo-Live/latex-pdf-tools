"""LLM-driven PDF to LaTeX conversion pipeline."""

import re
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Protocol, Sequence, TextIO

from ..document_class import (
    DocumentClassMode,
    DocumentClassResult,
    LatexDocumentClass,
    manual_document_class_result,
)
from ..convert.latex_converter import LatexConverter
from ..convert.project import LatexProjectBuilder, LatexProjectResult, LatexProjectSection
from ..extract.base import ImageRenderOptions, PdfDocumentChunk, PdfPageContext
from ..output_options import DEFAULT_OUTPUT_OPTIONS, LatexOutputOptions
from ..extract.text_extractor import TextExtractor
from ..structure import (
    StructureEvidence,
    StructureMode,
    StructurePlan,
    StructurePlanItem,
    StructurePlanSource,
    StructurePlannerOptions,
    build_chunk_fallback_plan,
    build_local_heading_plan,
    build_plan_from_bookmarks,
    normalize_llm_structure_plan,
    summarize_pages_for_structure,
)
from .cache import (
    ChunkCacheOptions,
    ChunkCacheRun,
    DocumentClassCacheRun,
    StructurePlanCacheRun,
)
from .client import LLMChunkResult, LLMDocumentClassResult, LLMStructurePlanResult
from .presets import PromptPreset, default_prompt_preset
from .scheduler import LLMScheduler, ProgressEvent, ProgressReporter


class LatexChunkClient(Protocol):
    """Client interface used by the conversion pipeline."""

    def generate_latex_chunk(
        self,
        *,
        document_title: str,
        document_class: LatexDocumentClass = LatexDocumentClass.ctexart,
        pages: Sequence[PdfPageContext],
        chunk_index: int,
        total_chunks: int,
        previous_latex_tail: str = "",
        extra_prompt: str = "",
        prompt_preset: PromptPreset | None = None,
        output_options: LatexOutputOptions | None = None,
    ) -> LLMChunkResult:
        """Generate LaTeX for one page chunk."""

    def generate_document_title(
        self,
        *,
        fallback_title: str,
        title_evidence: str,
        extra_prompt: str = "",
        prompt_preset: PromptPreset | None = None,
    ) -> str:
        """Generate a document title from collected conversion evidence."""

    def generate_document_class(
        self,
        *,
        document_title: str,
        evidence: StructureEvidence,
        pages: Sequence[PdfPageContext] = (),
        extra_prompt: str = "",
    ) -> LLMDocumentClassResult:
        """Generate a target LaTeX document class."""

    def generate_structure_plan(
        self,
        *,
        document_title: str,
        evidence: StructureEvidence,
        pages: Sequence[PdfPageContext] = (),
        inspected_pages: Sequence[int] = (),
        stage: str = "toc",
        extra_prompt: str = "",
    ) -> LLMStructurePlanResult:
        """Generate or assess a chapter-level structure plan."""


@dataclass
class LLMConversionResult:
    """Complete LLM conversion output."""

    latex: str
    notes: list[str] = field(default_factory=list)


@dataclass
class _CollectedConversion:
    """Shared output collected before final LaTeX assembly."""

    title: str
    document_class_result: DocumentClassResult
    fragments: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _CollectedProjectConversion:
    """Project conversion output grouped by semantic sections."""

    title: str
    document_class_result: DocumentClassResult
    sections: list[LatexProjectSection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    structure_plan: StructurePlan | None = None


class LLMPdfConverter:
    """Convert PDF lecture notes to LaTeX using page context and an LLM."""

    def __init__(
        self,
        client: LatexChunkClient,
        *,
        extractor: TextExtractor | None = None,
        chunk_pages: int = 4,
        image_dpi: int = 160,
        image_options: ImageRenderOptions | None = None,
        prefetch_chunks: int = 1,
        cache_options: ChunkCacheOptions | None = None,
        extra_prompt: str = "",
        prompt_preset: PromptPreset | None = None,
        title_source: str = "filename",
        manual_title: str | None = None,
        show_date: bool = False,
        document_class: DocumentClassMode | str = DocumentClassMode.auto,
        structure_options: StructurePlannerOptions | None = None,
        progress_stream: TextIO | None = None,
        progress_interval: float = 0.1,
        scheduler: LLMScheduler | None = None,
        progress_reporter: ProgressReporter | None = None,
        output_options: LatexOutputOptions | None = None,
    ):
        if chunk_pages <= 0:
            raise ValueError("chunk_pages must be positive.")
        if image_dpi <= 0:
            raise ValueError("image_dpi must be positive.")
        if prefetch_chunks < 0:
            raise ValueError("prefetch_chunks must be non-negative.")
        if title_source not in {"filename", "llm"}:
            raise ValueError("title_source must be filename or llm.")
        resolved_document_class = (
            document_class
            if isinstance(document_class, DocumentClassMode)
            else DocumentClassMode.from_value(str(document_class))
        )

        resolved_manual_title = None
        if manual_title is not None:
            resolved_manual_title = _normalize_title_text(manual_title)
            if not resolved_manual_title:
                raise ValueError("manual_title must not be empty.")
        if resolved_manual_title is not None and title_source == "llm":
            raise ValueError("manual_title cannot be used with title_source=llm.")

        self.client = client
        self.extractor = extractor or TextExtractor()
        self.chunk_pages = chunk_pages
        self.image_dpi = image_dpi
        self.image_options = image_options or ImageRenderOptions(
            dpi=image_dpi,
            dpi_max=image_dpi,
        )
        self.prefetch_chunks = prefetch_chunks
        self.cache_options = (
            cache_options if cache_options is not None and cache_options.enabled else None
        )
        self.extra_prompt = extra_prompt
        self.prompt_preset = prompt_preset or default_prompt_preset()
        self.title_source = title_source
        self.manual_title = resolved_manual_title
        self.show_date = show_date
        self.document_class_mode = resolved_document_class
        self.structure_options = structure_options or StructurePlannerOptions(
            mode=StructureMode.auto
        )
        self.progress_reporter = progress_reporter
        self.scheduler = scheduler or LLMScheduler(reporter=progress_reporter)
        self.output_options = output_options or DEFAULT_OUTPUT_OPTIONS
        self.document_builder = LatexConverter(output_options=self.output_options)
        self.project_builder = LatexProjectBuilder(output_options=self.output_options)

    def convert(
        self,
        pdf_path: Path,
        *,
        pages: Sequence[int] | None = None,
    ) -> LLMConversionResult:
        self._emit_stage_started(
            operation="conversion",
            label=pdf_path.name,
            metadata={"mode": "single", "pages": list(pages or [])},
        )
        collected = self._collect_conversion(pdf_path, pages=pages)
        result = LLMConversionResult(
            latex=LatexConverter(
                document_class=collected.document_class_result.document_class,
                output_options=self.output_options,
            ).convert_fragments(
                title=collected.title,
                fragments=collected.fragments,
                notes=collected.notes,
                show_date=self.show_date,
            ),
            notes=collected.notes,
        )
        self._emit_stage_completed(
            operation="conversion",
            label=pdf_path.name,
            metadata={"mode": "single", "pages": list(pages or [])},
        )
        return result

    def convert_project(
        self,
        pdf_path: Path,
        *,
        pages: Sequence[int] | None = None,
    ) -> LatexProjectResult:
        """Convert a PDF into an in-memory directory-style LaTeX project."""
        self._emit_stage_started(
            operation="conversion",
            label=pdf_path.name,
            metadata={"mode": "project", "pages": list(pages or [])},
        )
        if self.structure_options.mode == StructureMode.off:
            collected = self._collect_conversion(pdf_path, pages=pages)
            project = self.project_builder.build(
                title=collected.title,
                document_class=collected.document_class_result.document_class,
                fragments=collected.fragments,
                notes=collected.notes,
                show_date=self.show_date,
            )
            self._emit_stage_completed(
                operation="conversion",
                label=pdf_path.name,
                metadata={"mode": "project", "pages": list(pages or [])},
            )
            return project

        collected = self._collect_project_conversion(pdf_path, pages=pages)
        if collected.structure_plan is None:
            project = self.project_builder.build(
                title=collected.title,
                document_class=collected.document_class_result.document_class,
                fragments=[
                    fragment
                    for section in collected.sections
                    for fragment in section.fragments
                ],
                notes=collected.notes,
                show_date=self.show_date,
            )
            self._emit_stage_completed(
                operation="conversion",
                label=pdf_path.name,
                metadata={"mode": "project", "pages": list(pages or [])},
            )
            return project
        project = self.project_builder.build_from_plan(
            title=collected.title,
            document_class=collected.document_class_result.document_class,
            sections=collected.sections,
            structure_plan=collected.structure_plan,
            notes=collected.notes,
            show_date=self.show_date,
        )
        self._emit_stage_completed(
            operation="conversion",
            label=pdf_path.name,
            metadata={"mode": "project", "pages": list(pages or [])},
        )
        return project

    def _collect_conversion(
        self,
        pdf_path: Path,
        *,
        pages: Sequence[int] | None = None,
    ) -> _CollectedConversion:
        fragments: list[str] = []
        notes: list[str] = []
        previous_latex_tail = ""
        fallback_title = pdf_path.stem
        working_title = self.manual_title or fallback_title
        title_evidence = _TitleEvidenceCollector(filename_title=fallback_title)
        saw_chunk = False
        cache_run: ChunkCacheRun | None = None
        document_class_result: DocumentClassResult | None = None

        chunks = self.extractor.iter_context_chunks(
            pdf_path,
            pages=pages,
            image_dpi=self.image_dpi,
            include_images=True,
            image_options=self.image_options,
            chunk_size=self.chunk_pages,
        )

        chunk_iterator = _iter_prefetched_chunks(chunks, self.prefetch_chunks)
        try:
            for chunk in chunk_iterator:
                if not saw_chunk:
                    fallback_title = chunk.title
                    working_title = self.manual_title or fallback_title
                    document_class_result = self._resolve_document_class(
                        pdf_path=pdf_path,
                        fallback_title=fallback_title,
                        working_title=working_title,
                        pages=pages,
                    )
                    title_evidence = _TitleEvidenceCollector(
                        filename_title=fallback_title,
                    )
                    if self.cache_options is not None:
                        cache_run = ChunkCacheRun(
                            options=self.cache_options,
                            pdf_path=pdf_path,
                            pages=pages,
                            document_title=working_title,
                            chunk_pages=self.chunk_pages,
                            image_dpi=self.image_dpi,
                            image_options=self.image_options,
                            extra_prompt=self.extra_prompt,
                            prompt_preset=self.prompt_preset,
                            title_source=self.title_source,
                            document_class=document_class_result.document_class,
                            output_options=self.output_options,
                        )
                saw_chunk = True
                chunk_label = f"chunk {chunk.chunk_index}/{chunk.total_chunks}"
                chunk_metadata = {
                    "chunk_index": chunk.chunk_index,
                    "total_chunks": chunk.total_chunks,
                    "pages": [page.page_number for page in chunk.pages],
                }
                self._emit_stage_started(
                    operation="chunk",
                    label=chunk_label,
                    metadata=chunk_metadata,
                )
                try:
                    result = (
                        cache_run.read(chunk, previous_latex_tail)
                        if cache_run is not None
                        else None
                    )
                    if result is None:
                        result = self.scheduler.run(
                            operation="chunk",
                            label=chunk_label,
                            metadata=chunk_metadata,
                            request=lambda chunk=chunk, previous_latex_tail=previous_latex_tail: self.client.generate_latex_chunk(
                                document_title=working_title,
                                document_class=document_class_result.document_class,
                                pages=chunk.pages,
                                chunk_index=chunk.chunk_index,
                                total_chunks=chunk.total_chunks,
                                previous_latex_tail=previous_latex_tail,
                                extra_prompt=self.extra_prompt,
                                prompt_preset=self.prompt_preset,
                                output_options=self.output_options,
                            ),
                        )
                        if cache_run is not None:
                            cache_run.write(chunk, previous_latex_tail, result)
                    else:
                        self._emit_cache_hit(
                            operation="chunk",
                            label=chunk_label,
                            metadata=chunk_metadata,
                        )
                finally:
                    _release_page_images(chunk.pages)
                self._emit_stage_completed(
                    operation="chunk",
                    label=chunk_label,
                    metadata=chunk_metadata,
                )

                previous_latex_tail = _append_tail(
                    previous_latex_tail,
                    result.latex,
                    has_previous_fragment=bool(fragments),
                )
                title_evidence.add_chunk(chunk, result.latex)
                fragments.append(result.latex)
                notes.extend(result.notes)
        finally:
            close = getattr(chunk_iterator, "close", None)
            if close is not None:
                close()

        if not saw_chunk:
            raise ValueError("No pages were selected for conversion.")
        if document_class_result is None:
            raise ValueError("Document class was not resolved.")

        document_title = self._resolve_document_title(
            fallback_title=fallback_title,
            working_title=working_title,
            title_evidence=title_evidence.build(),
        )
        return _CollectedConversion(
            title=document_title,
            document_class_result=document_class_result,
            fragments=fragments,
            notes=[*document_class_result.notes, *notes],
        )

    def _collect_project_conversion(
        self,
        pdf_path: Path,
        *,
        pages: Sequence[int] | None = None,
    ) -> _CollectedProjectConversion:
        fallback_title = pdf_path.stem
        working_title = self.manual_title or fallback_title
        title_evidence = _TitleEvidenceCollector(filename_title=fallback_title)
        evidence_metadata = {"pages": list(pages or []), "mode": "project"}
        self._emit_stage_started(
            operation="extract",
            label="structure evidence",
            metadata=evidence_metadata,
        )
        evidence = self.extractor.extract_structure_evidence(pdf_path, pages=pages)
        self._emit_stage_completed(
            operation="extract",
            label="structure evidence",
            metadata={
                **evidence_metadata,
                "effective_pages": list(evidence.effective_pages),
            },
        )
        selected_pages = evidence.effective_pages
        if not selected_pages:
            raise ValueError("No pages were selected for conversion.")
        document_class_result = self._resolve_document_class_from_evidence(
            pdf_path=pdf_path,
            evidence=evidence,
            document_title=working_title,
            pages=pages,
        )

        structure_cache_run: StructurePlanCacheRun | None = None
        if self.cache_options is not None:
            structure_cache_run = StructurePlanCacheRun(
                options=self.cache_options,
                pdf_path=pdf_path,
                pages=pages,
                document_title=working_title,
                image_dpi=self.image_dpi,
                image_options=self.image_options,
                extra_prompt=self.extra_prompt,
                structure_options=self.structure_options,
                evidence=evidence,
            )
            structure_cache_run.write_evidence()

        structure_metadata = {
            "mode": self.structure_options.mode.value,
            "pages": list(pages or []),
        }
        self._emit_stage_started(
            operation="structure",
            label="structure plan",
            metadata=structure_metadata,
        )
        structure_plan = self._resolve_structure_plan(
            pdf_path=pdf_path,
            evidence=evidence,
            document_title=working_title,
            pages=pages,
            cache_run=structure_cache_run,
        )
        self._emit_stage_completed(
            operation="structure",
            label="structure plan",
            metadata={**structure_metadata, "source": structure_plan.source.value},
        )
        chunk_groups = _plan_chunk_groups(
            structure_plan,
            chunk_pages=self.chunk_pages,
            allowed_pages=selected_pages,
        )
        fallback_plan_used = structure_plan.source.value == "chunk-fallback"
        plan_notes = list(structure_plan.notes)

        sections: list[LatexProjectSection] = []
        notes: list[str] = []
        previous_latex_tail = ""
        cache_run: ChunkCacheRun | None = None
        if self.cache_options is not None:
            cache_run = ChunkCacheRun(
                options=self.cache_options,
                pdf_path=pdf_path,
                pages=pages,
                document_title=working_title,
                chunk_pages=self.chunk_pages,
                image_dpi=self.image_dpi,
                image_options=self.image_options,
                extra_prompt=self.extra_prompt,
                prompt_preset=self.prompt_preset,
                title_source=self.title_source,
                structure_plan=structure_plan,
                document_class=document_class_result.document_class,
                output_options=self.output_options,
            )

        total_chunks = sum(len(groups) for _, groups in chunk_groups)
        chunk_index = 0
        for item, groups in chunk_groups:
            section_fragments: list[str] = []
            for group in groups:
                chunk_index += 1
                chunk = self._load_context_chunk(
                    pdf_path,
                    page_numbers=group,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                )
                chunk_label = f"chunk {chunk_index}/{total_chunks}"
                chunk_metadata = {
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "pages": [page.page_number for page in chunk.pages],
                    "section": item.title,
                }
                self._emit_stage_started(
                    operation="chunk",
                    label=chunk_label,
                    metadata=chunk_metadata,
                )
                try:
                    result = (
                        cache_run.read(chunk, previous_latex_tail)
                        if cache_run is not None
                        else None
                    )
                    if result is None:
                        result = self.scheduler.run(
                            operation="chunk",
                            label=chunk_label,
                            metadata=chunk_metadata,
                            request=lambda chunk=chunk, previous_latex_tail=previous_latex_tail: self.client.generate_latex_chunk(
                                document_title=working_title,
                                document_class=document_class_result.document_class,
                                pages=chunk.pages,
                                chunk_index=chunk_index,
                                total_chunks=total_chunks,
                                previous_latex_tail=previous_latex_tail,
                                extra_prompt=self.extra_prompt,
                                prompt_preset=self.prompt_preset,
                                output_options=self.output_options,
                            ),
                        )
                        if cache_run is not None:
                            cache_run.write(chunk, previous_latex_tail, result)
                    else:
                        self._emit_cache_hit(
                            operation="chunk",
                            label=chunk_label,
                            metadata=chunk_metadata,
                        )
                finally:
                    _release_page_images(chunk.pages)
                self._emit_stage_completed(
                    operation="chunk",
                    label=chunk_label,
                    metadata=chunk_metadata,
                )

                previous_latex_tail = _append_tail(
                    previous_latex_tail,
                    result.latex,
                    has_previous_fragment=bool(section_fragments or sections),
                )
                title_evidence.add_chunk(chunk, result.latex)
                section_fragments.append(result.latex)
                notes.extend(result.notes)
            sections.append(LatexProjectSection(item=item, fragments=section_fragments))

        document_title = self._resolve_document_title(
            fallback_title=fallback_title,
            working_title=working_title,
            title_evidence=title_evidence.build(),
        )
        return _CollectedProjectConversion(
            title=document_title,
            document_class_result=document_class_result,
            sections=sections,
            notes=[*document_class_result.notes, *plan_notes, *notes],
            structure_plan=None if fallback_plan_used else structure_plan,
        )

    def _resolve_document_class(
        self,
        *,
        pdf_path: Path,
        fallback_title: str,
        working_title: str,
        pages: Sequence[int] | None,
    ) -> DocumentClassResult:
        evidence_metadata = {"pages": list(pages or []), "mode": "single"}
        self._emit_stage_started(
            operation="extract",
            label="structure evidence",
            metadata=evidence_metadata,
        )
        evidence = self.extractor.extract_structure_evidence(pdf_path, pages=pages)
        self._emit_stage_completed(
            operation="extract",
            label="structure evidence",
            metadata={
                **evidence_metadata,
                "effective_pages": list(evidence.effective_pages),
            },
        )
        if not evidence.effective_pages:
            raise ValueError("No pages were selected for conversion.")
        return self._resolve_document_class_from_evidence(
            pdf_path=pdf_path,
            evidence=evidence,
            document_title=working_title or fallback_title,
            pages=pages,
        )

    def _resolve_document_class_from_evidence(
        self,
        *,
        pdf_path: Path,
        evidence: StructureEvidence,
        document_title: str,
        pages: Sequence[int] | None,
    ) -> DocumentClassResult:
        stage_metadata = {
            "mode": self.document_class_mode.value,
            "pages": list(pages or []),
        }
        self._emit_stage_started(
            operation="document_class",
            label="document class",
            metadata=stage_metadata,
        )
        manual_class = self.document_class_mode.resolved_class()
        if manual_class is not None:
            result = manual_document_class_result(manual_class)
            self._emit_stage_completed(
                operation="document_class",
                label="document class",
                metadata={**stage_metadata, "document_class": result.document_class.value},
            )
            return result

        page_numbers = evidence.effective_pages[: min(self.structure_options.chunk_pages, 8)]
        class_pages = self._load_structure_pages(
            pdf_path,
            page_numbers=page_numbers,
        )
        cache_run: DocumentClassCacheRun | None = None
        if self.cache_options is not None:
            cache_run = DocumentClassCacheRun(
                options=self.cache_options,
                pdf_path=pdf_path,
                pages=pages,
                document_title=document_title,
                image_dpi=self.image_dpi,
                image_options=self.image_options,
                extra_prompt=self.extra_prompt,
                evidence=evidence,
            )
        try:
            result = (
                cache_run.read(pages=class_pages)
                if cache_run is not None
                else None
            )
            if result is None:
                result = self.scheduler.run(
                    operation="document_class",
                    label="document class",
                    metadata={
                        "pages": [page.page_number for page in class_pages],
                    },
                    request=lambda: self.client.generate_document_class(
                        document_title=document_title,
                        evidence=evidence,
                        pages=class_pages,
                        extra_prompt=self.extra_prompt,
                    ),
                )
                normalized = result.normalized()
                if cache_run is not None:
                    cache_run.write(
                        pages=class_pages,
                        result=result,
                        normalized_result=normalized,
                    )
                resolved = self._with_document_class_note(normalized)
                self._emit_stage_completed(
                    operation="document_class",
                    label="document class",
                    metadata={
                        **stage_metadata,
                        "document_class": resolved.document_class.value,
                        "source": resolved.source,
                    },
                )
                return resolved

            self._emit_cache_hit(
                operation="document_class",
                label="document class",
                metadata={"pages": [page.page_number for page in class_pages]},
            )
            resolved = self._with_document_class_note(result.normalized())
            self._emit_stage_completed(
                operation="document_class",
                label="document class",
                metadata={
                    **stage_metadata,
                    "document_class": resolved.document_class.value,
                    "source": resolved.source,
                },
            )
            return resolved
        finally:
            _release_page_images(class_pages)

    def _with_document_class_note(
        self,
        result: DocumentClassResult,
    ) -> DocumentClassResult:
        reason = f"document class: {result.document_class.value}"
        if result.reason:
            reason += f"（{result.reason}）"
        notes = [reason, *result.notes]
        return DocumentClassResult(
            document_class=result.document_class,
            confidence=result.confidence,
            reason=result.reason,
            notes=notes,
            source=result.source,
        )

    def _resolve_structure_plan(
        self,
        *,
        pdf_path: Path,
        evidence: StructureEvidence,
        document_title: str,
        pages: Sequence[int] | None,
        cache_run: StructurePlanCacheRun | None = None,
    ) -> StructurePlan:
        options = self.structure_options
        selected_pages = evidence.effective_pages
        if options.mode in {StructureMode.auto, StructureMode.local}:
            bookmark_plan = build_plan_from_bookmarks(evidence)
            if bookmark_plan is not None:
                if cache_run is not None:
                    cache_run.write_local_plan(bookmark_plan)
                return bookmark_plan
            if options.mode == StructureMode.local:
                local_plan = build_local_heading_plan(evidence)
                if local_plan is not None:
                    if cache_run is not None:
                        cache_run.write_local_plan(local_plan)
                    return local_plan
                fallback_plan = build_chunk_fallback_plan(
                    _chunk_page_numbers(selected_pages, self.chunk_pages),
                    note="本地结构线索不足，已回退到按 LLM chunk 划分章节文件。",
                )
                if cache_run is not None:
                    cache_run.write_local_plan(
                        fallback_plan,
                        filename="structure-fallback.json",
                    )
                return fallback_plan

        if options.mode in {StructureMode.auto, StructureMode.llm}:
            try:
                llm_plan = self._generate_llm_structure_plan(
                    pdf_path=pdf_path,
                    evidence=evidence,
                    document_title=document_title,
                    pages=pages,
                    cache_run=cache_run,
                )
            except Exception:
                if options.mode == StructureMode.llm:
                    raise
                llm_plan = None
            if llm_plan is not None:
                return llm_plan
            if options.mode == StructureMode.llm:
                fallback_plan = build_chunk_fallback_plan(
                    _chunk_page_numbers(selected_pages, self.chunk_pages),
                    note="LLM 未返回有效结构规划，已回退到按 chunk 划分章节文件。",
                )
                if cache_run is not None:
                    cache_run.write_local_plan(
                        fallback_plan,
                        filename="structure-fallback.json",
                    )
                return fallback_plan

        local_plan = build_local_heading_plan(evidence)
        if local_plan is not None and options.mode == StructureMode.local:
            if cache_run is not None:
                cache_run.write_local_plan(local_plan)
            return local_plan
        fallback_plan = build_chunk_fallback_plan(
            _chunk_page_numbers(selected_pages, self.chunk_pages),
        )
        if cache_run is not None:
            cache_run.write_local_plan(
                fallback_plan,
                filename="structure-fallback.json",
            )
        return fallback_plan

    def _generate_llm_structure_plan(
        self,
        *,
        pdf_path: Path,
        evidence: StructureEvidence,
        document_title: str,
        pages: Sequence[int] | None,
        cache_run: StructurePlanCacheRun | None = None,
    ) -> StructurePlan | None:
        selected_pages = evidence.effective_pages
        page_batches = _chunk_page_numbers(
            selected_pages[: self.structure_options.max_pages],
            self.structure_options.chunk_pages,
        )
        inspected_pages: list[int] = []
        last_result: LLMStructurePlanResult | None = None
        request_index = 0

        for batch in page_batches:
            request_index += 1
            chunk_pages = self._load_structure_pages(
                pdf_path,
                page_numbers=batch,
            )
            inspected_pages.extend(summarize_pages_for_structure(chunk_pages))
            try:
                result = (
                    cache_run.read(
                        stage="toc",
                        request_index=request_index,
                        pages=chunk_pages,
                        inspected_pages=inspected_pages,
                    )
                    if cache_run is not None
                    else None
                )
                if result is None:
                    result = self.scheduler.run(
                        operation="structure",
                        label=f"structure toc {request_index}",
                        metadata={
                            "stage": "toc",
                            "request_index": request_index,
                            "pages": [page.page_number for page in chunk_pages],
                            "inspected_pages": list(inspected_pages),
                        },
                        request=lambda chunk_pages=chunk_pages, inspected_pages=tuple(inspected_pages): self.client.generate_structure_plan(
                            document_title=document_title,
                            evidence=evidence,
                            pages=chunk_pages,
                            inspected_pages=inspected_pages,
                            stage="toc",
                            extra_prompt=self.extra_prompt,
                        ),
                    )
                else:
                    self._emit_cache_hit(
                        operation="structure",
                        label=f"structure toc {request_index}",
                        metadata={
                            "stage": "toc",
                            "request_index": request_index,
                            "pages": [page.page_number for page in chunk_pages],
                            "inspected_pages": list(inspected_pages),
                        },
                    )
            finally:
                _release_page_images(chunk_pages)

            last_result = result
            if result.status == "complete":
                try:
                    plan = normalize_llm_structure_plan(
                        items=result.plan,
                        source=StructurePlanSource.llm_toc,
                        confidence=result.confidence,
                        selected_pages=selected_pages,
                        inspected_pages=inspected_pages,
                        notes=[*result.notes, result.reason],
                    )
                except ValueError:
                    break
                if cache_run is not None:
                    cache_run.write(
                        stage="toc",
                        request_index=request_index,
                        pages=chunk_pages,
                        inspected_pages=inspected_pages,
                        result=result,
                        normalized_plan=plan,
                    )
                return plan
            if cache_run is not None:
                cache_run.write(
                    stage="toc",
                    request_index=request_index,
                    pages=chunk_pages,
                    inspected_pages=inspected_pages,
                    result=result,
                )
            if result.status == "insufficient":
                break

        heading_result = (
            cache_run.read(
                stage="headings",
                request_index=1,
                pages=[],
                inspected_pages=inspected_pages,
            )
            if cache_run is not None
            else None
        )
        if heading_result is None:
            heading_result = self.scheduler.run(
                operation="structure",
                label="structure headings 1",
                metadata={
                    "stage": "headings",
                    "request_index": 1,
                    "pages": [],
                    "inspected_pages": list(inspected_pages),
                },
                request=lambda inspected_pages=tuple(inspected_pages): self.client.generate_structure_plan(
                    document_title=document_title,
                    evidence=evidence,
                    pages=[],
                    inspected_pages=inspected_pages,
                    stage="headings",
                    extra_prompt=self.extra_prompt,
                ),
            )
        else:
            self._emit_cache_hit(
                operation="structure",
                label="structure headings 1",
                metadata={
                    "stage": "headings",
                    "request_index": 1,
                    "pages": [],
                    "inspected_pages": list(inspected_pages),
                },
            )
        if heading_result.status != "complete":
            if cache_run is not None:
                cache_run.write(
                    stage="headings",
                    request_index=1,
                    pages=[],
                    inspected_pages=inspected_pages,
                    result=heading_result,
                )
            if last_result is not None and last_result.reason:
                return None
            return None

        try:
            plan = normalize_llm_structure_plan(
                items=heading_result.plan,
                source=StructurePlanSource.llm_headings,
                confidence=heading_result.confidence,
                selected_pages=selected_pages,
                inspected_pages=inspected_pages,
                notes=[*heading_result.notes, heading_result.reason],
            )
        except ValueError:
            return None
        if cache_run is not None:
            cache_run.write(
                stage="headings",
                request_index=1,
                pages=[],
                inspected_pages=inspected_pages,
                result=heading_result,
                normalized_plan=plan,
            )
        return plan

    def _load_context_chunk(
        self,
        pdf_path: Path,
        *,
        page_numbers: Sequence[int],
        chunk_index: int,
        total_chunks: int,
    ) -> PdfDocumentChunk:
        chunks = self.extractor.iter_context_chunks(
            pdf_path,
            pages=page_numbers,
            image_dpi=self.image_dpi,
            include_images=True,
            image_options=self.image_options,
            chunk_size=len(page_numbers),
        )
        iterator = iter(chunks)
        try:
            chunk = next(iterator)
        except StopIteration as exc:
            raise ValueError("No pages were selected for conversion.") from exc
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return PdfDocumentChunk(
            source_file=chunk.source_file,
            title=chunk.title,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            pages=chunk.pages,
        )

    def _load_structure_pages(
        self,
        pdf_path: Path,
        *,
        page_numbers: Sequence[int],
    ) -> list[PdfPageContext]:
        chunks = self.extractor.iter_context_chunks(
            pdf_path,
            pages=page_numbers,
            image_dpi=self.image_dpi,
            include_images=True,
            image_options=self.image_options,
            chunk_size=max(1, len(page_numbers)),
        )
        iterator = iter(chunks)
        try:
            chunk = next(iterator)
        except StopIteration:
            return []
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return chunk.pages

    def _resolve_document_title(
        self,
        *,
        fallback_title: str,
        working_title: str,
        title_evidence: str,
    ) -> str:
        if self.manual_title is not None:
            return self.manual_title
        if self.title_source != "llm":
            return working_title

        self._emit_stage_started(
            operation="title",
            label="document title",
            metadata={"fallback_title": fallback_title},
        )
        try:
            title = self.scheduler.run(
                operation="title",
                label="document title",
                request=lambda: self.client.generate_document_title(
                    fallback_title=fallback_title,
                    title_evidence=title_evidence,
                    extra_prompt=self.extra_prompt,
                    prompt_preset=self.prompt_preset,
                ),
            )
        except Exception:
            self._emit_stage_completed(
                operation="title",
                label="document title",
                metadata={"fallback_title": fallback_title, "source": "fallback"},
            )
            return fallback_title

        resolved_title = _normalize_title_text(title) or fallback_title
        self._emit_stage_completed(
            operation="title",
            label="document title",
            metadata={"fallback_title": fallback_title, "source": "llm"},
        )
        return resolved_title

    def _emit_cache_hit(
        self,
        *,
        operation: str,
        label: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.scheduler.emit(
            ProgressEvent(
                kind="cache_hit",
                operation=operation,
                label=label,
                metadata=dict(metadata or {}),
            )
        )

    def _emit_stage_started(
        self,
        *,
        operation: str,
        label: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self._emit_stage_event(
            kind="stage_started",
            operation=operation,
            label=label,
            metadata=metadata,
        )

    def _emit_stage_completed(
        self,
        *,
        operation: str,
        label: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self._emit_stage_event(
            kind="stage_completed",
            operation=operation,
            label=label,
            metadata=metadata,
        )

    def _emit_stage_event(
        self,
        *,
        kind: str,
        operation: str,
        label: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.scheduler.emit(
            ProgressEvent(
                kind=kind,
                operation=operation,
                label=label,
                metadata=dict(metadata or {}),
            )
        )


def _chunk_pages(
    pages: Sequence[PdfPageContext],
    chunk_size: int,
) -> list[list[PdfPageContext]]:
    return [list(pages[index : index + chunk_size]) for index in range(0, len(pages), chunk_size)]


def _chunk_page_numbers(
    pages: Sequence[int],
    chunk_size: int,
) -> list[list[int]]:
    return [
        list(pages[index : index + chunk_size])
        for index in range(0, len(pages), chunk_size)
    ]


def _plan_chunk_groups(
    plan: StructurePlan,
    *,
    chunk_pages: int,
    allowed_pages: Sequence[int] | None = None,
) -> list[tuple[StructurePlanItem, list[list[int]]]]:
    allowed = list(allowed_pages) if allowed_pages is not None else plan.page_numbers
    groups: list[tuple[StructurePlanItem, list[list[int]]]] = []
    for item in plan.items:
        item_pages = [
            page
            for page in allowed
            if item.start_page <= page <= item.end_page
        ]
        if not item_pages:
            continue
        groups.append((item, _chunk_page_numbers(item_pages, chunk_pages)))
    return groups


def _tail(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _append_tail(
    previous_tail: str,
    fragment: str,
    *,
    has_previous_fragment: bool,
    max_chars: int = 2000,
) -> str:
    if not has_previous_fragment:
        return _tail(fragment, max_chars)
    return _tail(previous_tail + "\n\n" + fragment, max_chars)


def _release_page_images(pages: Sequence[PdfPageContext]) -> None:
    for page in pages:
        page.image_base64 = None


_SECTION_TITLE_RE = re.compile(r"\\(?:sub)*section\*?\{([^{}\n]+)\}")


class _TitleEvidenceCollector:
    """Collect compact title clues without retaining page images."""

    def __init__(self, *, filename_title: str):
        self.filename_title = filename_title
        self._headings: list[str] = []
        self._page_starts: list[str] = []
        self._latex_sections: list[str] = []
        self._seen: set[str] = set()

    def add_chunk(self, chunk: PdfDocumentChunk, latex_fragment: str) -> None:
        for page in chunk.pages:
            for block in page.text_blocks:
                if block.block_type != "heading":
                    continue
                self._add_unique(
                    self._headings,
                    f"第 {page.page_number} 页 heading：{block.text}",
                    max_items=40,
                )

            plain_text = _normalize_title_text(page.plain_text)
            if plain_text:
                self._add_unique(
                    self._page_starts,
                    f"第 {page.page_number} 页开头：{plain_text[:300]}",
                    max_items=40,
                )

        for match in _SECTION_TITLE_RE.finditer(latex_fragment):
            self._add_unique(
                self._latex_sections,
                match.group(1),
                max_items=40,
            )

    def build(self, max_chars: int = 12000) -> str:
        lines = [f"PDF 文件名：{self.filename_title}"]
        if self._headings:
            lines.extend(["", "页面 heading 线索：", *self._headings])
        if self._latex_sections:
            lines.extend(["", "已生成 LaTeX 章节线索：", *self._latex_sections])
        if self._page_starts:
            lines.extend(["", "页面开头文本线索：", *self._page_starts])

        evidence = "\n".join(lines)
        if len(evidence) <= max_chars:
            return evidence
        return evidence[:max_chars].rstrip() + "\n[标题线索已截断]"

    def _add_unique(self, target: list[str], value: str, *, max_items: int) -> None:
        normalized = _normalize_title_text(value)
        if not normalized or normalized in self._seen or len(target) >= max_items:
            return
        self._seen.add(normalized)
        target.append(normalized)


def _normalize_title_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _iter_prefetched_chunks(
    chunks: Iterable[PdfDocumentChunk],
    prefetch_chunks: int,
) -> Iterator[PdfDocumentChunk]:
    if prefetch_chunks <= 0:
        yield from chunks
        return

    iterator = iter(chunks)
    executor = ThreadPoolExecutor(max_workers=1)
    futures: deque[Future[PdfDocumentChunk]] = deque()

    def fill_prefetch_queue() -> None:
        while len(futures) < prefetch_chunks:
            futures.append(executor.submit(next, iterator))

    try:
        futures.append(executor.submit(next, iterator))
        while futures:
            future = futures.popleft()
            try:
                chunk = future.result()
            except StopIteration:
                break
            fill_prefetch_queue()
            yield chunk
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        _release_future_chunk_images(futures)

        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def _release_future_chunk_images(futures: Iterable[Future[PdfDocumentChunk]]) -> None:
    for future in futures:
        if future.cancelled() or not future.done():
            continue
        try:
            chunk = future.result()
        except Exception:
            continue
        _release_page_images(chunk.pages)
