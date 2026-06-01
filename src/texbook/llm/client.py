"""OpenAI-compatible chat client for LaTeX generation."""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from ..diagnostics import truncate_preview
from ..latex_text import normalize_latex_fragment_newlines
from ..document_class import (
    DocumentClassResult,
    LatexDocumentClass,
    normalize_document_class_result,
)
from ..extract.base import PdfPageContext
from ..output_options import LatexOutputOptions
from ..structure import StructureEvidence
from .config import LLMConfig
from .prompts import (
    build_chunk_messages,
    build_document_class_messages,
    build_structure_messages,
    build_title_messages,
)
from .presets import PromptPreset


class LLMResponseError(RuntimeError):
    """Raised when the LLM response cannot be used."""

    def __init__(
        self,
        message: str,
        *,
        raw_preview: str = "",
        finish_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.raw_preview = raw_preview
        self.finish_reason = finish_reason


@dataclass
class LLMDocumentClassResult:
    """Parsed result for one document-class request."""

    document_class: str
    confidence: float = 0.0
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    def normalized(self) -> DocumentClassResult:
        """Return a validated core document class result."""
        return normalize_document_class_result(
            document_class=self.document_class,
            confidence=self.confidence,
            reason=self.reason,
            notes=self.notes,
            source="llm",
        )


@dataclass
class LLMChunkResult:
    """Parsed result for one converted PDF chunk."""

    latex: str
    notes: list[str] = field(default_factory=list)


@dataclass
class LLMStructurePlanResult:
    """Parsed result for one structure-planning request."""

    status: str
    plan: list[dict[str, object]] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    needed_pages: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class OpenAICompatibleClient:
    """Thin wrapper around the OpenAI Python SDK with compatible base_url."""

    def __init__(self, config: LLMConfig):
        self.config = config
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMResponseError(
                "The openai package is required. Run `uv sync` first."
            ) from exc

        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": httpx.Timeout(
                connect=30.0,
                read=config.timeout,
                write=120.0,
                pool=30.0,
            ),
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(**kwargs)

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
        messages = build_chunk_messages(
            document_title=document_title,
            document_class=document_class,
            pages=pages,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            previous_latex_tail=previous_latex_tail,
            extra_prompt=extra_prompt,
            prompt_preset=prompt_preset,
            output_options=output_options,
        )
        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        try:
            response = self._create_completion(request_kwargs, response_format=True)
        except Exception as exc:  # pragma: no cover - backend specific fallback
            if _is_temperature_one_error(exc):
                request_kwargs["temperature"] = 1.0
                response = self._create_completion(
                    request_kwargs,
                    response_format=True,
                )
            elif "response_format" not in str(exc):
                raise
            else:
                response = self._create_completion(
                    request_kwargs,
                    response_format=False,
                )
        content = _completion_content(
            response,
            empty_message="LLM response content is empty or not text.",
            truncated_message="LLM response was truncated before valid JSON could be read.",
        )
        return parse_chunk_response(content)

    def generate_document_class(
        self,
        *,
        document_title: str,
        evidence: StructureEvidence,
        pages: Sequence[PdfPageContext] = (),
        extra_prompt: str = "",
    ) -> LLMDocumentClassResult:
        """Generate the target LaTeX document class for a PDF."""
        messages = build_document_class_messages(
            document_title=document_title,
            evidence=evidence,
            pages=pages,
            extra_prompt=extra_prompt,
        )
        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": min(self.config.max_tokens, 1024),
        }
        try:
            response = self._create_completion(request_kwargs, response_format=True)
        except Exception as exc:  # pragma: no cover - backend specific fallback
            if _is_temperature_one_error(exc):
                request_kwargs["temperature"] = 1.0
                response = self._create_completion(
                    request_kwargs,
                    response_format=True,
                )
            elif "response_format" not in str(exc):
                raise
            else:
                response = self._create_completion(
                    request_kwargs,
                    response_format=False,
                )
        content = _completion_content(
            response,
            empty_message="LLM document-class response content is empty or not text.",
            truncated_message=(
                "LLM document-class response was truncated before valid JSON could be read."
            ),
        )
        return parse_document_class_response(content)

    def generate_document_title(
        self,
        *,
        fallback_title: str,
        title_evidence: str,
        extra_prompt: str = "",
        prompt_preset: PromptPreset | None = None,
    ) -> str:
        messages = build_title_messages(
            fallback_title=fallback_title,
            title_evidence=title_evidence,
            extra_prompt=extra_prompt,
            prompt_preset=prompt_preset,
        )
        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": min(self.config.max_tokens, 512),
        }
        try:
            response = self._create_completion(request_kwargs, response_format=True)
        except Exception as exc:  # pragma: no cover - backend specific fallback
            if _is_temperature_one_error(exc):
                request_kwargs["temperature"] = 1.0
                response = self._create_completion(
                    request_kwargs,
                    response_format=True,
                )
            elif "response_format" not in str(exc):
                raise
            else:
                response = self._create_completion(
                    request_kwargs,
                    response_format=False,
                )
        content = _completion_content(
            response,
            empty_message="LLM title response content is empty or not text.",
            truncated_message=(
                "LLM title response was truncated before valid JSON could be read."
            ),
        )
        return parse_title_response(content)

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
        """Generate a chapter-level structure plan or planning status."""
        messages = build_structure_messages(
            document_title=document_title,
            evidence=evidence,
            pages=pages,
            inspected_pages=inspected_pages,
            stage=stage,
            extra_prompt=extra_prompt,
        )
        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": min(self.config.max_tokens, 8192),
        }
        try:
            response = self._create_completion(request_kwargs, response_format=True)
        except Exception as exc:  # pragma: no cover - backend specific fallback
            if _is_temperature_one_error(exc):
                request_kwargs["temperature"] = 1.0
                response = self._create_completion(
                    request_kwargs,
                    response_format=True,
                )
            elif "response_format" not in str(exc):
                raise
            else:
                response = self._create_completion(
                    request_kwargs,
                    response_format=False,
                )
        content = _completion_content(
            response,
            empty_message="LLM structure response content is empty or not text.",
            truncated_message=(
                "LLM structure response was truncated before valid JSON could be read."
            ),
        )
        return parse_structure_plan_response(content)

    def _create_completion(self, request_kwargs: dict[str, Any], *, response_format: bool):
        if response_format:
            return self._client.chat.completions.create(
                **request_kwargs,
                response_format={"type": "json_object"},
            )
        return self._client.chat.completions.create(**request_kwargs)


_FENCED_BLOCK_RE = re.compile(r"```([^\n`]*)\n([\s\S]*?)```")


def parse_chunk_response(raw_content: str) -> LLMChunkResult:
    """Parse the expected JSON object from an LLM response."""
    stripped, fence_language = _strip_code_fence_with_language(raw_content)
    if not stripped:
        raise LLMResponseError(
            "LLM chunk response is empty.",
            raw_preview="[empty response]",
        )
    if fence_language in {"latex", "tex"}:
        return _recover_latex_chunk(stripped, raw_content)

    if not stripped.startswith(("{", "[")):
        fenced_latex = _extract_fenced_latex_fragment(raw_content)
        if fenced_latex is not None:
            return _recover_latex_chunk(fenced_latex, raw_content)

    try:
        data = _load_json_object(stripped, raw_content=raw_content)
    except LLMResponseError:
        latex_fragment = _extract_recoverable_latex_fragment(stripped)
        if latex_fragment is not None:
            return _recover_latex_chunk(latex_fragment, raw_content)
        raise

    latex = data.get("latex")
    if not isinstance(latex, str) or not latex.strip():
        raise _response_error(
            "LLM response JSON must contain a non-empty latex field.",
            raw_content,
        )

    notes_value = data.get("notes", [])
    if notes_value is None:
        notes = []
    elif isinstance(notes_value, list):
        notes = [str(note) for note in notes_value if str(note).strip()]
    else:
        notes = [str(notes_value)]

    return LLMChunkResult(
        latex=normalize_latex_fragment_newlines(latex),
        notes=notes,
    )


def parse_document_class_response(raw_content: str) -> LLMDocumentClassResult:
    """Parse the expected JSON object from an LLM document-class response."""
    data = _load_json_object(_strip_code_fence(raw_content), raw_content=raw_content)
    document_class = data.get("document_class")
    if not isinstance(document_class, str) or not document_class.strip():
        raise _response_error(
            "LLM document-class JSON must contain a non-empty document_class field.",
            raw_content,
        )

    notes_value = data.get("notes", [])
    if notes_value is None:
        notes = []
    elif isinstance(notes_value, list):
        notes = [str(note) for note in notes_value if str(note).strip()]
    else:
        notes = [str(notes_value)]

    confidence_value = data.get("confidence", 0.0)
    try:
        confidence = float(confidence_value)
    except (TypeError, ValueError):
        confidence = 0.0

    result = LLMDocumentClassResult(
        document_class=document_class.strip().lower(),
        confidence=min(1.0, max(0.0, confidence)),
        reason=str(data.get("reason", "")).strip(),
        notes=notes,
    )
    try:
        result.normalized()
    except ValueError as exc:
        raise _response_error(str(exc), raw_content) from exc
    return result


def parse_title_response(raw_content: str) -> str:
    """Parse the expected JSON object from an LLM title response."""
    data = _load_json_object(_strip_code_fence(raw_content), raw_content=raw_content)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _response_error(
            "LLM response JSON must contain a non-empty title field.",
            raw_content,
        )

    normalized = _normalize_response_title(title)
    if not normalized:
        raise _response_error(
            "LLM response JSON must contain a non-empty title field.",
            raw_content,
        )
    return normalized


def parse_structure_plan_response(raw_content: str) -> LLMStructurePlanResult:
    """Parse the expected JSON object from an LLM structure-planning response."""
    data = _load_json_object(_strip_code_fence(raw_content), raw_content=raw_content)
    status = str(data.get("status", "")).strip().lower()
    if status not in {"complete", "need_more", "insufficient"}:
        raise _response_error(
            "LLM structure JSON must contain status complete, need_more, or insufficient.",
            raw_content,
        )

    plan_value = data.get("plan", [])
    if plan_value is None:
        plan: list[dict[str, object]] = []
    elif isinstance(plan_value, list):
        plan = [
            dict(item)
            for item in plan_value
            if isinstance(item, Mapping)
        ]
    else:
        raise _response_error("LLM structure JSON plan must be a list.", raw_content)

    needed_value = data.get("needed_pages", [])
    needed_pages: list[int] = []
    if isinstance(needed_value, list):
        for value in needed_value:
            if isinstance(value, bool):
                continue
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if page > 0:
                needed_pages.append(page)

    notes_value = data.get("notes", [])
    if notes_value is None:
        notes = []
    elif isinstance(notes_value, list):
        notes = [str(note) for note in notes_value if str(note).strip()]
    else:
        notes = [str(notes_value)]

    confidence_value = data.get("confidence", 0.0)
    try:
        confidence = float(confidence_value)
    except (TypeError, ValueError):
        confidence = 0.0

    return LLMStructurePlanResult(
        status=status,
        plan=plan,
        confidence=min(1.0, max(0.0, confidence)),
        reason=str(data.get("reason", "")).strip(),
        needed_pages=sorted(set(needed_pages)),
        notes=notes,
    )


def _strip_code_fence(text: str) -> str:
    return _strip_code_fence_with_language(text)[0]


def _strip_code_fence_with_language(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped, ""

    lines = stripped.splitlines()
    language = ""
    if lines and lines[0].startswith("```"):
        language = lines[0][3:].strip().lower()
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip(), language


def _load_json_object(text: str, *, raw_content: str | None = None) -> Mapping[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _load_loose_json_object(text)
        if data is None:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise _response_error(
                    "LLM response is not valid JSON.",
                    raw_content or text,
                ) from None
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise _response_error(
                    "LLM response is not valid JSON.",
                    raw_content or text,
                ) from exc

    if not isinstance(data, dict):
        raise _response_error("LLM response JSON must be an object.", raw_content or text)
    if _latex_has_json_escape_damage(data.get("latex")):
        loose_data = _load_loose_json_object(text)
        if loose_data is not None:
            return loose_data
    return data


def _recover_latex_chunk(text: str, raw_content: str) -> LLMChunkResult:
    latex = text.strip()
    if not latex:
        raise _response_error(
            "LLM response JSON must contain a non-empty latex field.",
            raw_content,
        )
    return LLMChunkResult(
        latex=normalize_latex_fragment_newlines(latex),
        notes=[],
    )


def _extract_fenced_latex_fragment(text: str) -> str | None:
    for match in _FENCED_BLOCK_RE.finditer(text):
        language = match.group(1).strip().lower()
        if language in {"latex", "tex"}:
            latex = match.group(2).strip()
            if latex:
                return latex
    return None


def _extract_recoverable_latex_fragment(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith(("{", "[")):
        return None
    if _looks_like_latex_fragment(stripped):
        return stripped

    lines = stripped.splitlines()
    for index in range(1, len(lines)):
        candidate = "\n".join(lines[index:]).strip()
        if _looks_like_latex_fragment(candidate):
            return candidate
    return None


def _looks_like_latex_fragment(text: str) -> bool:
    line = _first_content_line(text)
    if not line:
        return False
    if line.startswith("\\"):
        return True
    if line.startswith(("$$", r"\[", r"\(")):
        return True
    if line.startswith("$") and line.count("$") >= 2:
        return True
    if line.startswith("% TODO:"):
        return True
    if line.startswith("%"):
        return any(
            candidate.lstrip().startswith("\\")
            for candidate in text.splitlines()[1:4]
        )
    return False


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _response_error(message: str, raw_content: str) -> LLMResponseError:
    return LLMResponseError(message, raw_preview=truncate_preview(raw_content))


def _completion_content(
    response: Any,
    *,
    empty_message: str,
    truncated_message: str,
) -> str:
    choice = response.choices[0]
    message = choice.message
    content = message.content
    finish_reason = str(getattr(choice, "finish_reason", "") or "")
    if finish_reason in {"length", "content_filter"}:
        raw_preview = truncate_preview(content) if isinstance(content, str) else ""
        raise LLMResponseError(
            truncated_message,
            raw_preview=raw_preview,
            finish_reason=finish_reason,
        )
    if not isinstance(content, str):
        raise LLMResponseError(
            empty_message,
            raw_preview="[empty response]",
            finish_reason=finish_reason,
        )
    return content


def _normalize_response_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def _load_loose_json_object(text: str) -> Mapping[str, Any] | None:
    latex = _extract_loose_string(text, "latex")
    if latex is None:
        return None

    notes: list[str] = []
    notes_match = re.search(r'"notes"\s*:\s*(\[[\s\S]*?\])', text)
    if notes_match:
        try:
            notes_value = json.loads(notes_match.group(1))
        except json.JSONDecodeError:
            notes_value = []
        if isinstance(notes_value, list):
            notes = [str(note) for note in notes_value if str(note).strip()]

    return {"latex": latex, "notes": notes}


def _extract_loose_string(text: str, key: str) -> str | None:
    key_match = re.search(rf'"{re.escape(key)}"\s*:', text)
    if not key_match:
        return None
    start = text.find('"', key_match.end())
    if start < 0:
        return None

    chars: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == '"' and _looks_like_string_end(text, index):
            return "".join(chars)
        if char == "\\" and index + 1 < len(text) and text[index + 1] == '"':
            chars.append('"')
            index += 2
            continue
        chars.append(char)
        index += 1
    return None


def _looks_like_string_end(text: str, quote_index: int) -> bool:
    tail = text[quote_index + 1 :].lstrip()
    return tail.startswith(",") or tail.startswith("}")


def _latex_has_json_escape_damage(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(char in value for char in ("\b", "\f", "\r"))


def _is_temperature_one_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "temperature" in message and "only 1" in message
