"""Tests for LLM response parsing and prompt generation."""

import sys
from types import SimpleNamespace

from texbook.document_class import LatexDocumentClass
from texbook.extract.base import PageTextBlock, PdfPageContext
from texbook.structure import StructureEvidence
from texbook.llm.client import (
    LLMResponseError,
    OpenAICompatibleClient,
    parse_chunk_response,
    parse_document_class_response,
    parse_title_response,
)
from texbook.llm.config import LLMConfig
from texbook.llm.prompts import (
    build_chunk_messages,
    build_document_class_messages,
    build_title_messages,
)
from texbook.llm.presets import PromptPreset, default_prompt_preset
from texbook.output_options import BeamerBoxStyle, LatexOutputOptions


def test_parse_chunk_response_accepts_fenced_json():
    result = parse_chunk_response(
        """```json
        {"latex": "\\section{集合}", "notes": ["ignored footer removed"]}
        ```"""
    )

    assert result.latex == "\\section{集合}"
    assert result.notes == ["ignored footer removed"]


def test_parse_chunk_response_restores_literal_newline_escapes():
    result = parse_chunk_response(
        '{"latex": "\\\\begin{frame}\\\\n  % TODO: figure pending_asset\\\\n\\\\end{frame}", "notes": []}'
    )

    assert result.latex == "\\begin{frame}\n  % TODO: figure pending_asset\n\\end{frame}"


def test_parse_chunk_response_accepts_fenced_latex_fragment():
    result = parse_chunk_response(
        "```tex\n"
        "\\begin{frame}\n"
        "  % TODO: figure pending_asset\n"
        "\\end{frame}\n"
        "```"
    )

    assert result.latex == "\\begin{frame}\n  % TODO: figure pending_asset\n\\end{frame}"
    assert result.notes == []


def test_parse_chunk_response_accepts_explanatory_fenced_latex_fragment():
    result = parse_chunk_response(
        "下面是转换后的正文片段：\n\n"
        "```latex\n"
        "\\section{幂级数}\n"
        "正文内容\n"
        "```\n"
        "如需继续请处理下一页。"
    )

    assert result.latex == "\\section{幂级数}\n正文内容"
    assert result.notes == []


def test_parse_chunk_response_accepts_bare_latex_fragment():
    result = parse_chunk_response(
        "\\section{集合}\n\n% TODO: figure pending_asset: logo\n"
    )

    assert result.latex == "\\section{集合}\n\n% TODO: figure pending_asset: logo"
    assert result.notes == []


def test_parse_chunk_response_accepts_explanatory_bare_latex_fragment():
    result = parse_chunk_response(
        "转换结果如下：\n"
        "\\subsection{收敛半径}\n"
        "\\[\n"
        "R=1\n"
        "\\]\n"
    )

    assert result.latex == "\\subsection{收敛半径}\n\\[\nR=1\n\\]"
    assert result.notes == []


def test_parse_chunk_response_rejects_empty_response_with_preview():
    try:
        parse_chunk_response("   \n")
    except LLMResponseError as exc:
        assert "empty" in str(exc)
        assert exc.raw_preview == "[empty response]"
    else:
        raise AssertionError("Expected LLMResponseError")


def test_parse_chunk_response_does_not_recover_truncated_json():
    raw = '{"latex": "\\\\section{集合}"'

    try:
        parse_chunk_response(raw)
    except LLMResponseError as exc:
        assert "JSON" in str(exc)
        assert exc.raw_preview == raw
    else:
        raise AssertionError("Expected LLMResponseError")


def test_parse_chunk_response_rejects_missing_latex():
    try:
        parse_chunk_response('{"notes": []}')
    except LLMResponseError as exc:
        assert "latex" in str(exc)
        assert exc.raw_preview == '{"notes": []}'
    else:
        raise AssertionError("Expected LLMResponseError")


def test_parse_chunk_response_rejects_non_latex_noise_and_exposes_preview():
    try:
        parse_chunk_response("not json and not latex")
    except LLMResponseError as exc:
        assert "JSON" in str(exc)
        assert exc.raw_preview == "not json and not latex"
    else:
        raise AssertionError("Expected LLMResponseError")


def test_parse_title_response_normalizes_title():
    title = parse_title_response('{"title": "  6.1\\n集合与映射  "}')

    assert title == "6.1 集合与映射"


def test_parse_title_response_rejects_missing_title():
    try:
        parse_title_response('{"latex": "\\\\section{集合}"}')
    except LLMResponseError as exc:
        assert "title" in str(exc)
    else:
        raise AssertionError("Expected LLMResponseError")


def test_parse_document_class_response_normalizes_supported_class():
    result = parse_document_class_response(
        '{"document_class": "ctexbeamer", "confidence": 0.9, "reason": "幻灯片", "notes": ["有导航"]}'
    )

    assert result.document_class == "ctexbeamer"
    assert result.confidence == 0.9
    assert result.reason == "幻灯片"
    assert result.normalized().document_class.value == "ctexbeamer"


def test_parse_document_class_response_rejects_unknown_class():
    try:
        parse_document_class_response('{"document_class": "memoir"}')
    except LLMResponseError as exc:
        assert "document class" in str(exc)
    else:
        raise AssertionError("Expected LLMResponseError")


def test_openai_client_uses_unlimited_read_timeout_by_default(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    OpenAICompatibleClient(
        LLMConfig(
            model="test-model",
            api_key="test-key",
        )
    )

    assert captured["timeout"].read is None


def test_openai_client_uses_explicit_read_timeout(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    OpenAICompatibleClient(
        LLMConfig(
            model="test-model",
            api_key="test-key",
            timeout=10.0,
        )
    )

    assert captured["timeout"].read == 10.0


def test_openai_client_rejects_truncated_chunk_response(monkeypatch):
    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(
                            content='{"latex": "\\\\section{集合}"'
                        ),
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    client = OpenAICompatibleClient(
        LLMConfig(
            model="test-model",
            api_key="test-key",
        )
    )

    try:
        client.generate_latex_chunk(
            document_title="测试",
            pages=[PdfPageContext(page_number=1, width=1, height=1)],
            chunk_index=1,
            total_chunks=1,
        )
    except LLMResponseError as exc:
        assert "truncated" in str(exc)
        assert exc.finish_reason == "length"
        assert exc.raw_preview == '{"latex": "\\\\section{集合}"'
    else:
        raise AssertionError("Expected LLMResponseError")


def test_build_chunk_messages_contains_page_text_and_image():
    page = PdfPageContext(
        page_number=2,
        width=640,
        height=360,
        text_blocks=[
            PageTextBlock(
                text="集合的定义",
                bbox=(10, 20, 200, 40),
                font_size=18,
                block_type="heading",
            )
        ],
        image_base64="ZmFrZQ==",
    )

    messages = build_chunk_messages(
        document_title="第六章",
        pages=[page],
        chunk_index=1,
        total_chunks=1,
        previous_latex_tail="\\section{引言}",
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    content = messages[1]["content"]
    assert any(item["type"] == "text" and "第六章" in item["text"] for item in content)
    assert any(item["type"] == "text" and "PAGE 2" in item["text"] for item in content)
    assert any(item["type"] == "image_url" for item in content)


def test_build_chunk_messages_contains_complex_content_policy():
    messages = build_chunk_messages(
        document_title="第六章",
        pages=[PdfPageContext(page_number=1, width=1, height=1)],
        chunk_index=1,
        total_chunks=1,
    )

    system = messages[0]["content"]
    assert "tabular" in system
    assert "% TODO: table" in system
    assert "% TODO: figure pending_asset" in system
    assert r"\includegraphics" in system
    assert "% TODO: layout" in system


def test_build_chunk_messages_contains_document_class_instruction():
    messages = build_chunk_messages(
        document_title="第六章",
        document_class=LatexDocumentClass.ctexbeamer,
        pages=[PdfPageContext(page_number=1, width=1, height=1)],
        chunk_index=1,
        total_chunks=1,
    )

    user_text = messages[1]["content"][0]["text"]
    assert "目标 document class：ctexbeamer" in user_text
    assert "frame" in user_text
    assert r"\frametitle" in user_text
    assert "提纲页保留为单独 frame" in user_text
    assert "[allowframebreaks]" in user_text
    assert "\\title" in user_text
    assert "Beamer 标题页由项目外壳根据文件名生成" in user_text


def test_build_chunk_messages_can_request_tcolorbox_style():
    messages = build_chunk_messages(
        document_title="第六章",
        document_class=LatexDocumentClass.ctexbeamer,
        pages=[PdfPageContext(page_number=1, width=1, height=1)],
        chunk_index=1,
        total_chunks=1,
        output_options=LatexOutputOptions(
            beamer_box_style=BeamerBoxStyle.tcolorbox,
        ),
    )

    user_text = messages[1]["content"][0]["text"]
    assert "texbookinfobox" in user_text
    assert "不要自行定义 tcolorbox 样式" in user_text


def test_build_chunk_messages_can_disable_beamer_title_page():
    messages = build_chunk_messages(
        document_title="第六章",
        document_class=LatexDocumentClass.ctexbeamer,
        pages=[PdfPageContext(page_number=1, width=1, height=1)],
        chunk_index=1,
        total_chunks=1,
        output_options=LatexOutputOptions(beamer_title_page=False),
    )

    user_text = messages[1]["content"][0]["text"]
    assert "不要额外生成 Beamer 标题页" in user_text


def test_build_document_class_messages_contains_evidence_and_image():
    page = PdfPageContext(
        page_number=1,
        width=16,
        height=9,
        text_blocks=[
            PageTextBlock(
                text="6.1 集合与映射",
                bbox=(0, 0, 10, 1),
                font_size=20,
                block_type="heading",
            )
        ],
        image_base64="ZmFrZQ==",
    )

    messages = build_document_class_messages(
        document_title="6.1 集合与映射",
        evidence=StructureEvidence(
            source_title="6.1 集合与映射",
            total_pages=2,
            selected_pages=[1, 2],
        ),
        pages=[page],
    )

    assert "document_class" in messages[0]["content"]
    user_content = messages[1]["content"]
    assert "ctexbeamer" in messages[0]["content"]
    assert any(item["type"] == "image_url" for item in user_content)


def test_build_title_messages_contains_fallback_and_evidence():
    messages = build_title_messages(
        fallback_title="6.1 集合与映射",
        title_evidence="\\section{集合}",
        extra_prompt="保持章节编号",
    )

    assert messages[0]["role"] == "system"
    assert "保持章节编号" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "6.1 集合与映射" in messages[1]["content"]
    assert "\\section{集合}" in messages[1]["content"]


def test_build_messages_use_custom_prompt_preset():
    base = default_prompt_preset()
    preset = PromptPreset(
        name="custom-client",
        description="Custom prompt",
        version="1",
        chunk_system_prompt="正文系统",
        chunk_user_template="正文用户 {document_title}{pages_text}",
        page_image_label_template="图像页 {page_number}",
        title_system_prompt="标题系统",
        title_user_template="标题用户 {fallback_title}\n{title_evidence}",
        extra_prompt="预设额外",
    )
    page = PdfPageContext(
        page_number=3,
        width=1,
        height=1,
        text_blocks=[
            PageTextBlock(
                text="页面文本",
                bbox=(0, 0, 1, 1),
                font_size=12,
            )
        ],
        image_base64="ZmFrZQ==",
    )

    chunk_messages = build_chunk_messages(
        document_title="自定义标题",
        pages=[page],
        chunk_index=1,
        total_chunks=1,
        extra_prompt="运行额外",
        prompt_preset=preset,
    )
    title_messages = build_title_messages(
        fallback_title="文件名",
        title_evidence="标题线索",
        prompt_preset=preset,
    )

    assert base.name == "math"
    assert "正文系统" in chunk_messages[0]["content"]
    assert "预设额外" in chunk_messages[0]["content"]
    assert "运行额外" in chunk_messages[0]["content"]
    assert "正文用户 自定义标题" in chunk_messages[1]["content"][0]["text"]
    assert "图像页 3" == chunk_messages[1]["content"][1]["text"]
    assert title_messages[0]["content"].startswith("标题系统")
    assert title_messages[1]["content"] == "标题用户 文件名\n标题线索"
