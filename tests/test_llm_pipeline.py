"""Tests for the LLM PDF conversion pipeline."""

import io
import json
from pathlib import Path, PurePosixPath
import threading

import pytest

from texbook.extract.base import (
    ImageRenderOptions,
    PageTextBlock,
    PdfDocumentChunk,
    PdfPageContext,
)
from texbook.document_class import DocumentClassMode, normalize_document_class_result
from texbook.output_options import BeamerBoxStyle, LatexOutputOptions
from texbook.structure import (
    BookmarkEntry,
    PageHeadingCandidate,
    StructureEvidence,
    StructureMode,
    StructurePlannerOptions,
)
from texbook.llm.client import LLMStructurePlanResult
from texbook.llm.cache import ChunkCacheOptions, ChunkCacheRun
from texbook.llm.pipeline import (
    LLMPdfConverter,
    _append_tail,
    _iter_prefetched_chunks,
    _tail,
)
from texbook.llm.presets import PromptPreset, default_prompt_preset
from texbook.llm.scheduler import LLMScheduler, RetryOptions


class FakeExtractor:
    def __init__(self, pages, *, bookmarks=None, heading_candidates=None):
        self.pages = pages
        self.bookmarks = bookmarks or []
        self.heading_candidates = heading_candidates or []
        self.calls = []
        self.chunks = []
        self.structure_calls = []

    def extract_context(self, pdf_path, pages=None, image_dpi=160, include_images=True):
        raise AssertionError("LLMPdfConverter should use iter_context_chunks.")

    def iter_context_chunks(
        self,
        pdf_path,
        *,
        pages=None,
        image_dpi=160,
        include_images=True,
        image_options=None,
        chunk_size=4,
    ):
        self.calls.append(
            {
                "pdf_path": pdf_path,
                "pages": pages,
                "image_dpi": image_dpi,
                "include_images": include_images,
                "image_options": image_options,
                "chunk_size": chunk_size,
            }
        )
        wanted_pages = set(pages) if pages is not None else None
        selected_pages = [
            page
            for page in self.pages
            if wanted_pages is None or page.page_number in wanted_pages
        ]
        total_chunks = (len(selected_pages) + chunk_size - 1) // chunk_size

        for offset in range(0, len(selected_pages), chunk_size):
            chunk = PdfDocumentChunk(
                source_file=pdf_path,
                title=pdf_path.stem,
                chunk_index=offset // chunk_size + 1,
                total_chunks=total_chunks,
                pages=list(selected_pages[offset : offset + chunk_size]),
            )
            self.chunks.append(chunk)
            yield chunk

    def extract_structure_evidence(self, pdf_path, *, pages=None):
        self.structure_calls.append({"pdf_path": pdf_path, "pages": pages})
        wanted_pages = set(pages) if pages is not None else None
        selected_pages = [
            page.page_number
            for page in self.pages
            if wanted_pages is None or page.page_number in wanted_pages
        ]
        return StructureEvidence(
            source_title=pdf_path.stem,
            total_pages=len(self.pages),
            selected_pages=selected_pages,
            bookmarks=list(self.bookmarks),
            heading_candidates=list(self.heading_candidates),
            page_starts={
                page.page_number: page.plain_text[:300]
                for page in self.pages
                if wanted_pages is None or page.page_number in wanted_pages
            },
        )


class FakeClient:
    def __init__(
        self,
        latex_fragments=None,
        *,
        title_response="LLM 生成标题",
        title_exception=None,
    ):
        self.calls = []
        self.title_calls = []
        self.document_class_calls = []
        self.structure_calls = []
        self.latex_fragments = latex_fragments
        self.title_response = title_response
        self.title_exception = title_exception

    def generate_latex_chunk(
        self,
        *,
        document_title,
        document_class=None,
        pages,
        chunk_index,
        total_chunks,
        previous_latex_tail="",
        extra_prompt="",
        prompt_preset=None,
        output_options=None,
    ):
        self.calls.append(
            {
                "document_title": document_title,
                "document_class": document_class,
                "pages": [page.page_number for page in pages],
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "previous_latex_tail": previous_latex_tail,
                "extra_prompt": extra_prompt,
                "prompt_preset": prompt_preset,
                "output_options": output_options,
            }
        )
        latex = (
            self.latex_fragments[chunk_index - 1]
            if self.latex_fragments is not None
            else f"\\section{{Chunk {chunk_index}}}"
        )
        return type(
            "Result",
            (),
            {
                "latex": latex,
                "notes": [f"chunk-{chunk_index}"],
            },
        )()

    def generate_document_title(
        self,
        *,
        fallback_title,
        title_evidence,
        extra_prompt="",
        prompt_preset=None,
    ):
        self.title_calls.append(
            {
                "fallback_title": fallback_title,
                "title_evidence": title_evidence,
                "extra_prompt": extra_prompt,
                "prompt_preset": prompt_preset,
            }
        )
        if self.title_exception is not None:
            raise self.title_exception
        return self.title_response

    def generate_document_class(
        self,
        *,
        document_title,
        evidence,
        pages=(),
        extra_prompt="",
    ):
        self.document_class_calls.append(
            {
                "document_title": document_title,
                "pages": [page.page_number for page in pages],
                "extra_prompt": extra_prompt,
            }
        )
        class Result:
            document_class = "ctexart"
            confidence = 0.8
            reason = "短篇中文讲义"
            notes = ["文档类判断 note"]

            def normalized(self):
                return normalize_document_class_result(
                    document_class=self.document_class,
                    confidence=self.confidence,
                    reason=self.reason,
                    notes=self.notes,
                    source="llm",
                )

        return Result()

    def generate_structure_plan(
        self,
        *,
        document_title,
        evidence,
        pages=(),
        inspected_pages=(),
        stage="toc",
        extra_prompt="",
    ):
        self.structure_calls.append(
            {
                "document_title": document_title,
                "pages": [page.page_number for page in pages],
                "inspected_pages": list(inspected_pages),
                "stage": stage,
                "extra_prompt": extra_prompt,
            }
        )
        if stage == "toc":
            return LLMStructurePlanResult(
                status="insufficient",
                confidence=0.0,
                reason="no toc",
            )
        return LLMStructurePlanResult(
            status="complete",
            confidence=0.8,
            plan=[
                {
                    "kind": "chapter",
                    "title": "第一章",
                    "start_page": 1,
                    "end_page": max(evidence.effective_pages),
                    "confidence": 0.8,
                }
            ],
        )


class RaisingClient:
    def generate_latex_chunk(self, **kwargs):
        raise RuntimeError("LLM failed")

    def generate_document_class(self, **kwargs):
        raise RuntimeError("document-class LLM failed")

    def generate_structure_plan(self, **kwargs):
        raise RuntimeError("structure LLM failed")


class WaitingRaisingClient:
    def __init__(self, wait_for_event):
        self.wait_for_event = wait_for_event

    def generate_latex_chunk(self, **kwargs):
        assert self.wait_for_event.wait(timeout=1)
        raise RuntimeError("LLM failed")

    def generate_document_class(self, **kwargs):
        class Result:
            document_class = "ctexart"
            confidence = 0.8
            reason = "短篇中文讲义"
            notes = []

            def normalized(self):
                return normalize_document_class_result(
                    document_class=self.document_class,
                    confidence=self.confidence,
                    reason=self.reason,
                    notes=self.notes,
                    source="llm",
                )

        return Result()


class FailingSecondChunkClient(FakeClient):
    def generate_latex_chunk(self, **kwargs):
        if kwargs["chunk_index"] == 2:
            raise RuntimeError("LLM failed")
        return super().generate_latex_chunk(**kwargs)


class RetryableFailingSecondChunkClient(FakeClient):
    class RateLimitError(RuntimeError):
        status_code = 429

    def generate_latex_chunk(self, **kwargs):
        if kwargs["chunk_index"] == 2:
            raise self.RateLimitError("rate limited")
        return super().generate_latex_chunk(**kwargs)


class TransientSecondChunkClient(FakeClient):
    class RateLimitError(RuntimeError):
        status_code = 429

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = 0

    def generate_latex_chunk(self, **kwargs):
        if kwargs["chunk_index"] == 2 and self.failures == 0:
            self.failures += 1
            raise self.RateLimitError("rate limited")
        return super().generate_latex_chunk(**kwargs)


class EventingExtractor(FakeExtractor):
    def __init__(self, pages, second_chunk_ready):
        super().__init__(pages)
        self.second_chunk_ready = second_chunk_ready

    def iter_context_chunks(self, *args, **kwargs):
        for chunk in super().iter_context_chunks(*args, **kwargs):
            if chunk.chunk_index == 2:
                self.second_chunk_ready.set()
            yield chunk


class FakeTtyStream(io.StringIO):
    def isatty(self):
        return True


def _chunk(index, page):
    return PdfDocumentChunk(
        source_file=Path("docs/sample.pdf"),
        title="sample",
        chunk_index=index,
        total_chunks=2,
        pages=[page],
    )


def _page(page_number, *, image_base64="image"):
    return PdfPageContext(
        page_number=page_number,
        width=1,
        height=1,
        image_base64=image_base64,
    )


def _custom_preset(name="custom-preset", *, suffix=""):
    base = default_prompt_preset()
    return PromptPreset(
        name=name,
        description="Custom preset",
        version="1",
        chunk_system_prompt=base.chunk_system_prompt + suffix,
        chunk_user_template=base.chunk_user_template,
        page_image_label_template=base.page_image_label_template,
        title_system_prompt=base.title_system_prompt,
        title_user_template=base.title_user_template,
        extra_prompt=base.extra_prompt,
    )


def _build_cache_run(
    tmp_path,
    *,
    pages=None,
    document_title="sample",
    chunk_pages=1,
    image_dpi=160,
    image_options=None,
    extra_prompt="",
    model="test-model",
    base_url=None,
    temperature=1.0,
    max_tokens=128000,
    prompt_preset=None,
    title_source="filename",
    output_options=None,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    return ChunkCacheRun(
        options=ChunkCacheOptions(
            cache_dir=tmp_path / "cache",
            llm_model=model,
            llm_base_url=base_url,
            llm_temperature=temperature,
            llm_max_tokens=max_tokens,
        ),
        pdf_path=pdf_path,
        pages=pages,
        document_title=document_title,
        chunk_pages=chunk_pages,
        image_dpi=image_dpi,
        image_options=image_options
        or ImageRenderOptions(
            dpi=image_dpi,
            dpi_min=100,
            dpi_max=image_dpi,
            image_format="png",
            jpeg_quality=85,
        ),
        extra_prompt=extra_prompt,
        prompt_preset=prompt_preset,
        title_source=title_source,
        output_options=output_options,
    )


def test_pipeline_chunks_pages_and_builds_document():
    pages = [
        PdfPageContext(
            page_number=1,
            width=1,
            height=1,
            text_blocks=[
                PageTextBlock(
                    text="第一页",
                    bbox=(0, 0, 1, 1),
                    font_size=12,
                )
            ],
            image_base64="aGVsbG8=",
        ),
        PdfPageContext(
            page_number=2,
            width=1,
            height=1,
            text_blocks=[
                PageTextBlock(
                    text="第二页",
                    bbox=(0, 0, 1, 1),
                    font_size=12,
                )
            ],
            image_base64="aGVsbG8=",
        ),
        PdfPageContext(
            page_number=3,
            width=1,
            height=1,
            text_blocks=[
                PageTextBlock(
                    text="第三页",
                    bbox=(0, 0, 1, 1),
                    font_size=12,
                )
            ],
            image_base64="aGVsbG8=",
        ),
    ]
    extractor = FakeExtractor(pages)
    client = FakeClient()
    converter = LLMPdfConverter(client, extractor=extractor, chunk_pages=2, image_dpi=144)

    result = converter.convert(Path("docs/sample.pdf"))

    assert "\\documentclass[UTF8]{ctexart}" in result.latex
    assert "\\section{Chunk 1}" in result.latex
    assert "\\section{Chunk 2}" in result.latex
    assert result.notes == [
        "document class: ctexart（短篇中文讲义）",
        "文档类判断 note",
        "chunk-1",
        "chunk-2",
    ]
    assert converter.prefetch_chunks == 1
    assert extractor.calls[0]["image_dpi"] == 144
    assert extractor.calls[0]["chunk_size"] == 2
    assert extractor.calls[0]["image_options"].dpi == 144
    assert extractor.calls[0]["image_options"].image_format == "png"
    assert client.calls[0]["pages"] == [1, 2]
    assert client.calls[1]["pages"] == [3]
    assert client.calls[1]["previous_latex_tail"]
    assert all(page.image_base64 is None for chunk in extractor.chunks for page in chunk.pages)


def test_pipeline_can_build_project_output():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
    ]
    extractor = FakeExtractor(pages)
    client = FakeClient(
        latex_fragments=[
            r"\documentclass{article}\begin{document}\section{Chunk 1}\end{document}",
            r"\section{Chunk 2}",
        ]
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        manual_title=" 项目标题 ",
        show_date=True,
        structure_options=StructurePlannerOptions(mode=StructureMode.off),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    assert project.entrypoint == PurePosixPath("main.tex")
    assert project.notes == [
        "document class: ctexart（短篇中文讲义）",
        "文档类判断 note",
        "chunk-1",
        "chunk-2",
    ]
    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("chapters/chapter02.tex"),
    }
    assert r"\title{项目标题}" in project.files[PurePosixPath("main.tex")]
    assert r"\date{\today}" in project.files[PurePosixPath("main.tex")]
    assert r"\input{chapters/chapter01}" in project.files[PurePosixPath("main.tex")]
    assert project.files[PurePosixPath("chapters/chapter01.tex")] == "\\section{Chunk 1}\n"
    assert project.files[PurePosixPath("chapters/chapter02.tex")] == "\\section{Chunk 2}\n"
    assert project.metadata["document_class"] == "ctexart"
    assert client.calls[0]["document_title"] == "项目标题"
    assert client.calls[1]["previous_latex_tail"]
    assert all(page.image_base64 is None for chunk in extractor.chunks for page in chunk.pages)


def test_pipeline_normalizes_literal_newline_escapes_in_project_output():
    pages = [PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1")]
    extractor = FakeExtractor(pages)
    client = FakeClient(
        latex_fragments=[
            r"\begin{frame}\n  % TODO: figure pending_asset: logo\n\end{frame}",
        ]
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        structure_options=StructurePlannerOptions(mode=StructureMode.off),
        document_class=DocumentClassMode.ctexbeamer,
        output_options=LatexOutputOptions(
            beamer_box_style=BeamerBoxStyle.tcolorbox,
            beamer_title_page=False,
        ),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))
    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]

    assert chapter == (
        "\\begin{frame}\n  % TODO: figure pending_asset: logo\n\\end{frame}\n"
    )


def test_pipeline_can_disable_beamer_title_page_in_project_output():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
    ]
    extractor = FakeExtractor(
        pages,
        bookmarks=[
            BookmarkEntry(level=1, title="第六章 线性空间", page_number=1),
            BookmarkEntry(level=1, title="6.1 集合与映射", page_number=2),
        ],
    )
    client = FakeClient(
        latex_fragments=[
            r"\section*{第六章 线性空间}\begin{frame}\frametitle{第六章 线性空间}\end{frame}",
            r"\begin{frame}\frametitle{6.1 集合与映射}\begin{itemize}\item 集合\item 映射\end{itemize}\end{frame}",
        ]
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        output_options=LatexOutputOptions(beamer_title_page=False),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    main = project.files[PurePosixPath("main.tex")]
    assert r"\titlepage" not in main
    assert r"\subtitle{第六章 线性空间}" not in main
    assert r"\input{chapters/chapter01}" in main
    assert project.metadata["output_options"]["beamer_title_page"] is False


def test_project_output_records_complex_content_metadata_from_chunks():
    pages = [PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1")]
    extractor = FakeExtractor(pages)
    client = FakeClient(
        latex_fragments=[
            r"""
            正文

            % TODO: figure pending_asset 第 1 页图像需要裁切
            \includegraphics{figures/missing.png}
            """
        ]
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        structure_options=StructurePlannerOptions(mode=StructureMode.off),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]
    candidates = project.metadata["complex_content"]["candidates"]
    assert r"\includegraphics" not in chapter
    assert "% TODO: figure pending_asset" in chapter
    assert candidates[0]["kind"] == "figure"
    assert candidates[0]["strategy"] == "pending_asset"
    assert candidates[0]["page_number"] == 1


def test_project_output_uses_valid_bookmark_structure_plan():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
        PdfPageContext(page_number=4, width=1, height=1, image_base64="page-4"),
    ]
    extractor = FakeExtractor(
        pages,
        bookmarks=[
            BookmarkEntry(level=1, title="第一章 集合", page_number=1),
            BookmarkEntry(level=1, title="第二章 映射", page_number=3),
        ],
    )
    client = FakeClient(
        latex_fragments=[
            r"\section{第一章 集合}\n第一页",
            "第三页",
        ]
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    assert client.structure_calls == []
    assert [call["pages"] for call in client.calls] == [[1, 2], [3, 4]]
    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("chapters/chapter02.tex"),
    }
    assert project.metadata["structure_plan"]["source"] == "bookmark"
    assert r"\input{chapters/chapter01}" in project.files[PurePosixPath("main.tex")]
    assert project.files[PurePosixPath("chapters/chapter01.tex")].startswith(
        r"\section{第一章 集合}"
    )
    assert project.files[PurePosixPath("chapters/chapter02.tex")].startswith(
        r"\section{第二章 映射}"
    )


def test_project_output_uses_local_heading_plan_without_llm_structure_call():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
        PdfPageContext(page_number=4, width=1, height=1, image_base64="page-4"),
    ]
    extractor = FakeExtractor(
        pages,
        heading_candidates=[
            PageHeadingCandidate(page_number=1, text="第一章 集合", font_size=20),
            PageHeadingCandidate(page_number=3, text="第二章 映射", font_size=20),
        ],
    )
    client = FakeClient(latex_fragments=["集合正文", "映射正文"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
        structure_options=StructurePlannerOptions(mode=StructureMode.local),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    main = project.files[PurePosixPath("main.tex")]
    assert client.structure_calls == []
    assert [call["pages"] for call in client.calls] == [[1, 2], [3, 4]]
    assert project.metadata["structure_plan"]["source"] == "local-headings"
    assert r"\input{chapters/chapter01}" in main
    assert r"\input{chapters/chapter02}" in main
    assert project.files[PurePosixPath("chapters/chapter01.tex")].startswith(
        r"\section{第一章 集合}"
    )
    assert project.files[PurePosixPath("chapters/chapter02.tex")].startswith(
        r"\section{第二章 映射}"
    )
    assert any("置信度较低" in note for note in project.notes)


def test_project_output_continues_llm_structure_planning_when_more_pages_needed():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
        PdfPageContext(page_number=4, width=1, height=1, image_base64="page-4"),
    ]
    extractor = FakeExtractor(pages)

    class PlanningClient(FakeClient):
        def generate_structure_plan(self, **kwargs):
            self.structure_calls.append(
                {
                    "pages": [page.page_number for page in kwargs["pages"]],
                    "inspected_pages": list(kwargs["inspected_pages"]),
                    "stage": kwargs["stage"],
                }
            )
            if len(self.structure_calls) == 1:
                return LLMStructurePlanResult(
                    status="need_more",
                    reason="目录还没结束",
                    needed_pages=[3, 4],
                )
            return LLMStructurePlanResult(
                status="complete",
                confidence=0.84,
                reason="目录完整",
                plan=[
                    {
                        "kind": "chapter",
                        "title": "第一章",
                        "start_page": 1,
                        "end_page": 2,
                        "confidence": 0.8,
                    },
                    {
                        "kind": "chapter",
                        "title": "第二章",
                        "start_page": 3,
                        "end_page": 4,
                        "confidence": 0.8,
                    },
                ],
            )

    client = PlanningClient(latex_fragments=["body-1", "body-2"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
        structure_options=StructurePlannerOptions(
            mode=StructureMode.auto,
            chunk_pages=2,
            max_pages=4,
        ),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    assert client.structure_calls == [
        {"pages": [1, 2], "inspected_pages": [1, 2], "stage": "toc"},
        {"pages": [3, 4], "inspected_pages": [1, 2, 3, 4], "stage": "toc"},
    ]
    assert [call["pages"] for call in client.calls] == [[1, 2], [3, 4]]
    assert project.metadata["structure_plan"]["source"] == "llm-toc"


def test_project_output_falls_back_to_chunk_files_when_auto_planning_fails():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
    ]
    extractor = FakeExtractor(pages)

    class InsufficientClient(FakeClient):
        def generate_structure_plan(self, **kwargs):
            self.structure_calls.append({"stage": kwargs["stage"]})
            return LLMStructurePlanResult(
                status="insufficient",
                reason="没有结构线索",
            )

    client = InsufficientClient(latex_fragments=["chunk-1", "chunk-2"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
        structure_options=StructurePlannerOptions(mode=StructureMode.auto),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    assert "structure_plan" not in project.metadata
    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("chapters/chapter02.tex"),
    }
    assert any("回退" in note for note in project.notes)


def test_project_output_falls_back_to_chunk_files_when_llm_planning_has_no_plan():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
    ]
    extractor = FakeExtractor(pages)

    class InsufficientClient(FakeClient):
        def generate_structure_plan(self, **kwargs):
            self.structure_calls.append({"stage": kwargs["stage"]})
            return LLMStructurePlanResult(
                status="insufficient",
                reason="没有结构线索",
            )

    client = InsufficientClient(latex_fragments=["chunk-1", "chunk-2"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
        structure_options=StructurePlannerOptions(mode=StructureMode.llm),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    assert client.structure_calls == [{"stage": "toc"}, {"stage": "headings"}]
    assert "structure_plan" not in project.metadata
    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("chapters/chapter02.tex"),
    }
    assert any("LLM 未返回有效结构规划" in note for note in project.notes)


def test_project_output_falls_back_to_chunk_files_when_llm_plan_is_unusable():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
    ]
    extractor = FakeExtractor(pages)

    class EmptyPlanClient(FakeClient):
        def generate_structure_plan(self, **kwargs):
            self.structure_calls.append({"stage": kwargs["stage"]})
            return LLMStructurePlanResult(
                status="complete",
                confidence=0.8,
                reason="空计划",
                plan=[],
            )

    client = EmptyPlanClient(latex_fragments=["chunk-1", "chunk-2"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
        structure_options=StructurePlannerOptions(mode=StructureMode.llm),
    )

    project = converter.convert_project(Path("docs/sample.pdf"))

    assert client.structure_calls == [{"stage": "toc"}, {"stage": "headings"}]
    assert "structure_plan" not in project.metadata
    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("chapters/chapter02.tex"),
    }
    assert any("回退" in note for note in project.notes)


def test_project_structure_respects_non_contiguous_page_selection():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
        PdfPageContext(page_number=3, width=1, height=1, image_base64="page-3"),
        PdfPageContext(page_number=4, width=1, height=1, image_base64="page-4"),
    ]
    extractor = FakeExtractor(
        pages,
        bookmarks=[
            BookmarkEntry(level=1, title="第一章", page_number=1),
            BookmarkEntry(level=1, title="第二章", page_number=3),
        ],
    )
    client = FakeClient(latex_fragments=["page-1", "page-3"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=2,
    )

    converter.convert_project(Path("docs/sample.pdf"), pages=[1, 3])

    assert [call["pages"] for call in client.calls] == [[1], [3]]


def test_pipeline_uses_manual_title_for_prompt_and_document():
    pages = [PdfPageContext(page_number=1, width=1, height=1)]
    extractor = FakeExtractor(pages)
    client = FakeClient()
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        manual_title=" 手动标题 ",
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert r"\title{手动标题}" in result.latex
    assert client.calls[0]["document_title"] == "手动标题"
    assert client.title_calls == []


def test_pipeline_passes_prompt_preset_to_chunk_and_title_client():
    pages = [PdfPageContext(page_number=1, width=1, height=1)]
    extractor = FakeExtractor(pages)
    client = FakeClient(title_response="自定义标题")
    preset = _custom_preset()
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        prompt_preset=preset,
        title_source="llm",
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert r"\title{自定义标题}" in result.latex
    assert client.calls[0]["prompt_preset"] is preset
    assert client.title_calls[0]["prompt_preset"] is preset


def test_pipeline_generates_title_after_all_chunks():
    pages = [
        PdfPageContext(
            page_number=1,
            width=1,
            height=1,
            text_blocks=[
                PageTextBlock(
                    text="集合的基本概念",
                    bbox=(0, 0, 1, 1),
                    font_size=18,
                    block_type="heading",
                )
            ],
        ),
        PdfPageContext(page_number=2, width=1, height=1),
    ]
    extractor = FakeExtractor(pages)
    client = FakeClient(
        latex_fragments=[
            r"\section{集合}",
            r"\section{映射}",
        ],
        title_response="集合与映射",
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        title_source="llm",
        extra_prompt="保持章节编号",
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert r"\title{集合与映射}" in result.latex
    assert [call["document_title"] for call in client.calls] == ["sample", "sample"]
    assert len(client.title_calls) == 1
    assert client.title_calls[0]["fallback_title"] == "sample"
    assert "集合的基本概念" in client.title_calls[0]["title_evidence"]
    assert "集合" in client.title_calls[0]["title_evidence"]
    assert client.title_calls[0]["extra_prompt"] == "保持章节编号"


def test_pipeline_falls_back_to_filename_when_llm_title_fails():
    pages = [PdfPageContext(page_number=1, width=1, height=1)]
    extractor = FakeExtractor(pages)
    client = FakeClient(
        latex_fragments=[r"\section{集合}"],
        title_exception=RuntimeError("title failed"),
    )
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        title_source="llm",
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert r"\title{sample}" in result.latex
    assert len(client.title_calls) == 1
    assert result.notes == [
        "document class: ctexart（短篇中文讲义）",
        "文档类判断 note",
        "chunk-1",
    ]


def test_pipeline_can_show_today_date():
    pages = [PdfPageContext(page_number=1, width=1, height=1)]
    extractor = FakeExtractor(pages)
    client = FakeClient()
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        show_date=True,
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert r"\date{\today}" in result.latex


def test_pipeline_does_not_write_to_progress_stream_for_uncached_llm_chunk():
    stream = FakeTtyStream()
    pages = [PdfPageContext(page_number=1, width=1, height=1)]
    extractor = FakeExtractor(pages)
    client = FakeClient()
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        progress_stream=stream,
    )

    converter.convert(Path("docs/sample.pdf"))

    assert stream.getvalue() == ""


def test_pipeline_does_not_write_to_progress_stream_for_cached_chunk(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["cached"]),
        extractor=FakeExtractor([_page(1)]),
        chunk_pages=1,
        cache_options=cache_options,
        progress_stream=io.StringIO(),
    )
    first_converter.convert(pdf_path)

    stream = FakeTtyStream()
    second_converter = LLMPdfConverter(
        RaisingClient(),
        extractor=FakeExtractor([_page(1)]),
        chunk_pages=1,
        cache_options=cache_options,
        progress_stream=stream,
    )

    second_converter.convert(pdf_path)

    assert stream.getvalue() == ""


def test_pipeline_emits_progress_for_uncached_and_cached_chunks(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_events = []
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["cached"]),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        progress_reporter=first_events.append,
    )

    first_converter.convert(pdf_path)

    assert [event.kind for event in first_events if event.kind.startswith("request")] == [
        "request_started",
        "request_completed",
        "request_started",
        "request_completed",
    ]
    assert ("stage_started", "conversion") in [
        (event.kind, event.operation) for event in first_events
    ]
    assert ("stage_started", "extract") in [
        (event.kind, event.operation) for event in first_events
    ]
    assert ("stage_started", "chunk") in [
        (event.kind, event.operation) for event in first_events
    ]

    second_events = []
    second_converter = LLMPdfConverter(
        RaisingClient(),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        progress_reporter=second_events.append,
    )

    second_converter.convert(pdf_path)

    cache_hit_events = [event for event in second_events if event.kind == "cache_hit"]
    assert [event.operation for event in cache_hit_events] == ["document_class", "chunk"]
    assert ("stage_started", "document_class") in [
        (event.kind, event.operation) for event in second_events
    ]


def test_pipeline_reuses_chunk_cache_after_successful_run(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_pages = [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    first_extractor = FakeExtractor(first_pages)
    first_client = FakeClient(
        latex_fragments=[
            "first-cached",
            "second-cached",
        ]
    )
    first_converter = LLMPdfConverter(
        first_client,
        extractor=first_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    first_result = first_converter.convert(pdf_path)

    assert "first-cached" in first_result.latex
    assert "second-cached" in first_result.latex
    assert len(first_client.calls) == 2
    assert len(list((cache_options.cache_dir).rglob("chunk-*.json"))) == 2

    second_pages = [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    second_extractor = FakeExtractor(second_pages)
    second_converter = LLMPdfConverter(
        RaisingClient(),
        extractor=second_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    second_result = second_converter.convert(pdf_path)

    assert "first-cached" in second_result.latex
    assert "second-cached" in second_result.latex
    assert second_extractor.calls[0]["chunk_size"] == 1
    assert all(page.image_base64 is None for chunk in second_extractor.chunks for page in chunk.pages)


def test_chunk_cache_restores_literal_newline_escapes(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    page = _page(1, image_base64="page-1")
    cache_run = _build_cache_run(
        tmp_path,
        pages=[1],
        document_title="sample",
        chunk_pages=1,
    )
    chunk = _chunk(1, page)
    cache_run.write(
        chunk,
        "",
        type("Result", (), {"latex": r"\begin{frame}\n\end{frame}", "notes": []})(),
    )

    restored = cache_run.read(chunk, "")

    assert restored is not None
    assert restored.latex == "\\begin{frame}\n\\end{frame}"


def test_pipeline_reuses_chunk_cache_when_llm_title_falls_back(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_client = FakeClient(
        latex_fragments=["cached-body"],
        title_exception=RuntimeError("title failed"),
    )
    first_converter = LLMPdfConverter(
        first_client,
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        title_source="llm",
    )

    first_result = first_converter.convert(pdf_path)

    assert "cached-body" in first_result.latex
    assert r"\title{sample}" in first_result.latex
    assert len(first_client.calls) == 1
    assert len(first_client.title_calls) == 1

    second_converter = LLMPdfConverter(
        RaisingClient(),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        title_source="llm",
    )

    second_result = second_converter.convert(pdf_path)

    assert "cached-body" in second_result.latex
    assert r"\title{sample}" in second_result.latex


def test_pipeline_reuses_completed_cache_after_later_chunk_failure(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_pages = [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    first_extractor = FakeExtractor(first_pages)
    first_client = FailingSecondChunkClient(
        latex_fragments=[
            "first-cached",
            "unused",
        ]
    )
    first_converter = LLMPdfConverter(
        first_client,
        extractor=first_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    with pytest.raises(RuntimeError, match="LLM failed"):
        first_converter.convert(pdf_path)

    cache_files = list((cache_options.cache_dir).rglob("chunk-*.json"))
    assert len(cache_files) == 1

    second_pages = [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    second_extractor = FakeExtractor(second_pages)
    second_client = FakeClient(
        latex_fragments=[
            "unused",
            "second-fresh",
        ]
    )
    second_converter = LLMPdfConverter(
        second_client,
        extractor=second_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    second_result = second_converter.convert(pdf_path)

    assert len(second_client.calls) == 1
    assert second_client.calls[0]["chunk_index"] == 2
    assert second_client.calls[0]["previous_latex_tail"] == _tail("first-cached")
    assert "first-cached" in second_result.latex
    assert "second-fresh" in second_result.latex


def test_pipeline_retries_recoverable_chunk_failure():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="page-1"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="page-2"),
    ]
    client = TransientSecondChunkClient(
        latex_fragments=[
            "first",
            "second-after-retry",
        ]
    )
    events = []
    scheduler = LLMScheduler(
        retry_options=RetryOptions(retries=1, initial_delay=0.0, max_delay=0.0),
        reporter=events.append,
    )
    converter = LLMPdfConverter(
        client,
        extractor=FakeExtractor(pages),
        chunk_pages=1,
        scheduler=scheduler,
        progress_reporter=events.append,
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert "second-after-retry" in result.latex
    assert client.failures == 1
    assert "retry_scheduled" in [event.kind for event in events]
    assert client.calls[-1]["previous_latex_tail"] == _tail("first")


def test_pipeline_keeps_successful_cache_when_retry_exhausts_later_chunk(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_client = RetryableFailingSecondChunkClient(
        latex_fragments=[
            "first-cached",
            "unused",
        ]
    )
    first_converter = LLMPdfConverter(
        first_client,
        extractor=FakeExtractor(
            [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
        ),
        chunk_pages=1,
        cache_options=cache_options,
        scheduler=LLMScheduler(
            retry_options=RetryOptions(retries=1, initial_delay=0.0, max_delay=0.0)
        ),
    )

    with pytest.raises(RetryableFailingSecondChunkClient.RateLimitError):
        first_converter.convert(pdf_path)

    assert len(list(cache_options.cache_dir.rglob("chunk-*.json"))) == 1

    second_client = FakeClient(
        latex_fragments=[
            "unused",
            "second-fresh",
        ]
    )
    second_converter = LLMPdfConverter(
        second_client,
        extractor=FakeExtractor(
            [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
        ),
        chunk_pages=1,
        cache_options=cache_options,
    )

    result = second_converter.convert(pdf_path)

    assert len(second_client.calls) == 1
    assert second_client.calls[0]["chunk_index"] == 2
    assert "first-cached" in result.latex
    assert "second-fresh" in result.latex


def test_pipeline_misses_later_cache_when_previous_tail_changes(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_extractor = FakeExtractor(
        [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    )
    first_client = FakeClient(
        latex_fragments=[
            "old-first",
            "old-second",
        ]
    )
    first_converter = LLMPdfConverter(
        first_client,
        extractor=first_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    first_converter.convert(pdf_path)

    first_cache_file = sorted((cache_options.cache_dir).rglob("chunk-*.json"))[0]
    cache_data = json.loads(first_cache_file.read_text(encoding="utf-8"))
    cache_data["latex"] = "new-first"
    first_cache_file.write_text(
        json.dumps(cache_data, ensure_ascii=False),
        encoding="utf-8",
    )

    second_extractor = FakeExtractor(
        [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    )
    second_client = FakeClient(
        latex_fragments=[
            "unused",
            "fresh-second",
        ]
    )
    second_converter = LLMPdfConverter(
        second_client,
        extractor=second_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    second_result = second_converter.convert(pdf_path)

    assert len(second_client.calls) == 1
    assert second_client.calls[0]["chunk_index"] == 2
    assert second_client.calls[0]["previous_latex_tail"] == _tail("new-first")
    assert "new-first" in second_result.latex
    assert "fresh-second" in second_result.latex
    assert "old-second" not in second_result.latex


def test_pipeline_rebuilds_cache_when_entries_are_corrupted(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    pages = [_page(1, image_base64="page-1")]
    extractor = FakeExtractor(pages)
    client = FakeClient(latex_fragments=["cached"])
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    converter.convert(pdf_path)

    cache_file = next((cache_options.cache_dir).rglob("chunk-*.json"))
    cache_file.write_text("{not-json", encoding="utf-8")

    fresh_extractor = FakeExtractor([_page(1, image_base64="page-1")])
    fresh_client = FakeClient(latex_fragments=["fresh"])
    fresh_converter = LLMPdfConverter(
        fresh_client,
        extractor=fresh_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    fresh_result = fresh_converter.convert(pdf_path)

    assert len(fresh_client.calls) == 1
    assert "fresh" in fresh_result.latex
    assert "fresh" in cache_file.read_text(encoding="utf-8")


def test_project_reuses_cached_structure_plan(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_client = FakeClient(latex_fragments=["first-body"])
    first_converter = LLMPdfConverter(
        first_client,
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
    )

    first_project = first_converter.convert_project(pdf_path)

    assert first_client.structure_calls
    assert "第一章" in first_project.files[PurePosixPath("chapters/chapter01.tex")]
    assert len(list(cache_options.cache_dir.rglob("evidence.json"))) == 1
    assert len(list(cache_options.cache_dir.rglob("structure-fallback.json"))) == 0
    assert len(list(cache_options.cache_dir.rglob("structure-headings-01.json"))) == 1
    assert len(list(cache_options.cache_dir.rglob("chunk-*.json"))) == 1
    structure_payload = json.loads(
        next(cache_options.cache_dir.rglob("structure-headings-01.json")).read_text(
            encoding="utf-8"
        )
    )
    assert structure_payload["normalized_plan"]["source"] == "llm-headings"
    assert structure_payload["result"]["status"] == "complete"

    second_client = RaisingClient()
    second_converter = LLMPdfConverter(
        second_client,
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
    )

    second_project = second_converter.convert_project(pdf_path)

    assert "first-body" in second_project.files[PurePosixPath("chapters/chapter01.tex")]


def test_project_structure_cache_respects_no_cache(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["cached-body"]),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        structure_options=StructurePlannerOptions(mode=StructureMode.llm),
    )
    first_converter.convert_project(pdf_path)

    disabled_converter = LLMPdfConverter(
        RaisingClient(),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=None,
        document_class=DocumentClassMode.ctexart,
        structure_options=StructurePlannerOptions(mode=StructureMode.llm),
    )

    with pytest.raises(RuntimeError, match="structure LLM failed"):
        disabled_converter.convert_project(pdf_path)


def test_project_structure_cache_clears_when_requested(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_dir = tmp_path / "cache"
    first_options = ChunkCacheOptions(
        cache_dir=cache_dir,
        llm_model="test-model",
    )
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["old-body"]),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=first_options,
    )
    first_converter.convert_project(pdf_path)

    clear_options = ChunkCacheOptions(
        cache_dir=cache_dir,
        clear=True,
        llm_model="test-model",
    )
    second_client = FakeClient(latex_fragments=["new-body"])
    second_converter = LLMPdfConverter(
        second_client,
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=clear_options,
    )

    project = second_converter.convert_project(pdf_path)

    assert second_client.structure_calls
    assert "new-body" in project.files[PurePosixPath("chapters/chapter01.tex")]
    assert "old-body" not in project.files[PurePosixPath("chapters/chapter01.tex")]


def test_project_structure_cache_misses_when_planning_parameter_changes(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["cached-body"]),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        structure_options=StructurePlannerOptions(
            mode=StructureMode.llm,
            chunk_pages=1,
            max_pages=1,
        ),
    )
    first_converter.convert_project(pdf_path)

    changed_converter = LLMPdfConverter(
        RaisingClient(),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
        structure_options=StructurePlannerOptions(
            mode=StructureMode.llm,
            chunk_pages=1,
            max_pages=2,
        ),
    )

    with pytest.raises(RuntimeError, match="structure LLM failed"):
        changed_converter.convert_project(pdf_path)


def test_project_rebuilds_corrupted_structure_cache(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["old-body"]),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
    )
    first_converter.convert_project(pdf_path)

    structure_file = next(cache_options.cache_dir.rglob("structure-headings-01.json"))
    structure_file.write_text("{not-json", encoding="utf-8")
    fresh_client = FakeClient(latex_fragments=["fresh-body"])
    fresh_converter = LLMPdfConverter(
        fresh_client,
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
    )

    project = fresh_converter.convert_project(pdf_path)

    assert fresh_client.structure_calls
    assert "old-body" in project.files[PurePosixPath("chapters/chapter01.tex")]
    assert json.loads(structure_file.read_text(encoding="utf-8"))["result"][
        "status"
    ] == "complete"


def test_project_rebuilds_structure_cache_with_bad_normalized_plan(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    first_converter = LLMPdfConverter(
        FakeClient(latex_fragments=["old-body"]),
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
    )
    first_converter.convert_project(pdf_path)

    structure_file = next(cache_options.cache_dir.rglob("structure-headings-01.json"))
    cache_data = json.loads(structure_file.read_text(encoding="utf-8"))
    cache_data["normalized_plan"] = {"source": "llm-headings", "items": []}
    structure_file.write_text(
        json.dumps(cache_data, ensure_ascii=False),
        encoding="utf-8",
    )
    fresh_client = FakeClient(latex_fragments=["fresh-body"])
    fresh_converter = LLMPdfConverter(
        fresh_client,
        extractor=FakeExtractor([_page(1, image_base64="page-1")]),
        chunk_pages=1,
        cache_options=cache_options,
    )

    fresh_converter.convert_project(pdf_path)

    assert fresh_client.structure_calls
    assert json.loads(structure_file.read_text(encoding="utf-8"))["normalized_plan"][
        "items"
    ]


def test_project_writes_structure_debug_artifacts_for_bookmarks(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_options = ChunkCacheOptions(
        cache_dir=tmp_path / "cache",
        llm_model="test-model",
    )
    pages = [_page(1, image_base64="page-1"), _page(2, image_base64="page-2")]
    extractor = FakeExtractor(
        pages,
        bookmarks=[
            BookmarkEntry(level=1, title="第一章", page_number=1),
            BookmarkEntry(level=1, title="第二章", page_number=2),
        ],
    )
    converter = LLMPdfConverter(
        FakeClient(latex_fragments=["body-1", "body-2"]),
        extractor=extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    converter.convert_project(pdf_path)

    evidence_file = next(cache_options.cache_dir.rglob("evidence.json"))
    local_plan_file = next(cache_options.cache_dir.rglob("structure-local.json"))
    evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    local_plan = json.loads(local_plan_file.read_text(encoding="utf-8"))
    assert evidence["evidence"]["bookmarks"][0]["title"] == "第一章"
    assert local_plan["plan"]["source"] == "bookmark"


def test_pipeline_clears_cache_when_requested(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"sample-pdf-bytes")
    cache_dir = tmp_path / "cache"
    cache_options = ChunkCacheOptions(
        cache_dir=cache_dir,
        llm_model="test-model",
    )
    first_extractor = FakeExtractor([_page(1, image_base64="page-1")])
    first_client = FakeClient(latex_fragments=["old"])
    first_converter = LLMPdfConverter(
        first_client,
        extractor=first_extractor,
        chunk_pages=1,
        cache_options=cache_options,
    )

    first_converter.convert(pdf_path)

    clear_options = ChunkCacheOptions(
        cache_dir=cache_dir,
        clear=True,
        llm_model="test-model",
    )
    second_extractor = FakeExtractor([_page(1, image_base64="page-1")])
    second_client = FakeClient(latex_fragments=["new"])
    second_converter = LLMPdfConverter(
        second_client,
        extractor=second_extractor,
        chunk_pages=1,
        cache_options=clear_options,
    )

    second_result = second_converter.convert(pdf_path)

    assert len(second_client.calls) == 1
    assert "new" in second_result.latex
    assert "old" not in second_result.latex


def test_prefetch_iterator_starts_next_chunk_after_yielding_current():
    second_started = threading.Event()
    release_second = threading.Event()

    def source():
        yield _chunk(1, PdfPageContext(page_number=1, width=1, height=1))
        second_started.set()
        assert release_second.wait(timeout=1)
        yield _chunk(2, PdfPageContext(page_number=2, width=1, height=1))

    iterator = _iter_prefetched_chunks(source(), prefetch_chunks=1)
    try:
        first = next(iterator)
        assert first.chunk_index == 1
        assert second_started.wait(timeout=1)
        release_second.set()
        second = next(iterator)
        assert second.chunk_index == 2
        with pytest.raises(StopIteration):
            next(iterator)
    finally:
        release_second.set()
        iterator.close()


def test_prefetch_iterator_reraises_extraction_errors():
    def source():
        yield _chunk(1, PdfPageContext(page_number=1, width=1, height=1))
        raise RuntimeError("extract failed")

    iterator = _iter_prefetched_chunks(source(), prefetch_chunks=1)
    try:
        assert next(iterator).chunk_index == 1
        with pytest.raises(RuntimeError, match="extract failed"):
            next(iterator)
    finally:
        iterator.close()


def test_append_tail_matches_tail_of_joined_fragments():
    fragments = [
        "a" * 8,
        "b" * 8,
        "c" * 8,
    ]
    previous_latex_tail = ""

    for index, fragment in enumerate(fragments):
        previous_latex_tail = _append_tail(
            previous_latex_tail,
            fragment,
            has_previous_fragment=index > 0,
            max_chars=12,
        )
        assert previous_latex_tail == _tail("\n\n".join(fragments[: index + 1]), 12)


def test_pipeline_passes_incremental_tail_to_later_chunks():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1),
        PdfPageContext(page_number=2, width=1, height=1),
        PdfPageContext(page_number=3, width=1, height=1),
    ]
    fragments = [
        "first-fragment",
        "second-fragment",
        "third-fragment",
    ]
    extractor = FakeExtractor(pages)
    client = FakeClient(latex_fragments=fragments)
    converter = LLMPdfConverter(client, extractor=extractor, chunk_pages=1)

    converter.convert(Path("docs/sample.pdf"))

    assert client.calls[0]["previous_latex_tail"] == ""
    assert client.calls[1]["previous_latex_tail"] == _tail(fragments[0])
    assert client.calls[2]["previous_latex_tail"] == _tail("\n\n".join(fragments[:2]))


def test_pipeline_raises_when_stream_selects_no_pages():
    extractor = FakeExtractor([])
    client = FakeClient()
    converter = LLMPdfConverter(client, extractor=extractor, chunk_pages=2)

    with pytest.raises(ValueError, match="No pages were selected"):
        converter.convert(Path("docs/sample.pdf"))

    assert client.calls == []


def test_pipeline_releases_chunk_images_when_client_fails():
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="aGVsbG8="),
    ]
    extractor = FakeExtractor(pages)
    converter = LLMPdfConverter(RaisingClient(), extractor=extractor, chunk_pages=1)

    with pytest.raises(RuntimeError, match="LLM failed"):
        converter.convert(Path("docs/sample.pdf"))

    assert extractor.chunks[0].pages[0].image_base64 is None


def test_pipeline_releases_prefetched_chunk_images_when_client_fails():
    second_chunk_ready = threading.Event()
    pages = [
        PdfPageContext(page_number=1, width=1, height=1, image_base64="first"),
        PdfPageContext(page_number=2, width=1, height=1, image_base64="second"),
    ]
    extractor = EventingExtractor(pages, second_chunk_ready)
    converter = LLMPdfConverter(
        WaitingRaisingClient(second_chunk_ready),
        extractor=extractor,
        chunk_pages=1,
        prefetch_chunks=1,
    )

    with pytest.raises(RuntimeError, match="LLM failed"):
        converter.convert(Path("docs/sample.pdf"))

    assert all(
        page.image_base64 is None
        for chunk in extractor.chunks
        for page in chunk.pages
    )


def test_pipeline_uses_custom_image_options():
    pages = [PdfPageContext(page_number=1, width=1, height=1)]
    image_options = ImageRenderOptions(
        dpi=120,
        dpi_min=90,
        dpi_max=180,
        image_format="auto",
        jpeg_quality=92,
    )
    extractor = FakeExtractor(pages)
    client = FakeClient()
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        image_dpi=120,
        image_options=image_options,
    )

    converter.convert(Path("docs/sample.pdf"))

    assert extractor.calls[0]["image_options"] is image_options


def test_chunk_cache_run_key_changes_with_inputs(tmp_path):
    base = _build_cache_run(tmp_path)

    assert _build_cache_run(tmp_path, pages=[2, 1]).run_key == _build_cache_run(
        tmp_path,
        pages=[1, 2],
    ).run_key
    assert base.run_key != _build_cache_run(tmp_path, pages=[2]).run_key
    assert base.run_key != _build_cache_run(
        tmp_path,
        document_title="手动标题",
    ).run_key
    assert base.run_key != _build_cache_run(tmp_path, chunk_pages=2).run_key
    assert base.run_key != _build_cache_run(tmp_path, extra_prompt="额外要求").run_key
    assert base.run_key != _build_cache_run(
        tmp_path,
        prompt_preset=_custom_preset(suffix="\n自定义规则"),
    ).run_key
    assert base.run_key != _build_cache_run(tmp_path, title_source="llm").run_key
    assert base.run_key != _build_cache_run(
        tmp_path,
        output_options=LatexOutputOptions(
            beamer_box_style=BeamerBoxStyle.tcolorbox,
        ),
    ).run_key
    assert base.run_key != _build_cache_run(
        tmp_path,
        output_options=LatexOutputOptions(beamer_title_page=False),
    ).run_key
    assert base.run_key != _build_cache_run(
        tmp_path,
        image_options=ImageRenderOptions(
            dpi=120,
            dpi_min=90,
            dpi_max=180,
            image_format="jpeg",
            jpeg_quality=92,
        ),
        image_dpi=120,
    ).run_key
    assert base.run_key != _build_cache_run(tmp_path, model="other-model").run_key
    assert base.run_key != _build_cache_run(
        tmp_path,
        base_url="https://example.com",
    ).run_key
    assert base.run_key != _build_cache_run(tmp_path, temperature=0.5).run_key
    assert base.run_key != _build_cache_run(tmp_path, max_tokens=4096).run_key


def test_pipeline_supports_disabled_prefetch():
    pages = [PdfPageContext(page_number=1, width=1, height=1, image_base64="image")]
    extractor = FakeExtractor(pages)
    client = FakeClient()
    converter = LLMPdfConverter(
        client,
        extractor=extractor,
        chunk_pages=1,
        prefetch_chunks=0,
    )

    result = converter.convert(Path("docs/sample.pdf"))

    assert converter.prefetch_chunks == 0
    assert "\\section{Chunk 1}" in result.latex
    assert extractor.chunks[0].pages[0].image_base64 is None


def test_pipeline_rejects_negative_prefetch():
    with pytest.raises(ValueError, match="prefetch_chunks"):
        LLMPdfConverter(FakeClient(), prefetch_chunks=-1)
