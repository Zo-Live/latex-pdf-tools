"""Convert extracted content to LaTeX document source."""

import re

from typing import List, Sequence

from ..complex_content import replace_unsupported_graphics_references
from ..document_class import LatexDocumentClass
from ..extract.base import ExtractedContent
from ..output_options import (
    BeamerBoxStyle,
    CtexFontProfile,
    DEFAULT_OUTPUT_OPTIONS,
    LatexOutputOptions,
)
from ..latex_text import normalize_latex_fragment_newlines


_DOCUMENTCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}")
_USEPACKAGE_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{[^}]+\}")

_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "^": r"\^{}",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
}
_ESCAPE_RE = re.compile("[" + re.escape("".join(_ESCAPE_MAP)) + "]")

_UNICODE_MATH_MAP = {
    "∉": r"\(\notin\)",
    "≠": r"\(\ne\)",
    "∉": r"\(\notin\)",
    "≤": r"\(\le\)",
    "≥": r"\(\ge\)",
    "≠": r"\(\ne\)",
    "∈": r"\(\in\)",
    "∅": r"\(\emptyset\)",
    "∀": r"\(\forall\)",
    "∃": r"\(\exists\)",
    "⊆": r"\(\subseteq\)",
    "⊂": r"\(\subset\)",
    "∩": r"\(\cap\)",
    "∪": r"\(\cup\)",
    "⇒": r"\(\Rightarrow\)",
    "⇐": r"\(\Leftarrow\)",
    "⇔": r"\(\Leftrightarrow\)",
    "↔": r"\(\leftrightarrow\)",
    "→": r"\(\to\)",
    "←": r"\(\leftarrow\)",
    "∨": r"\(\vee\)",
    "∧": r"\(\wedge\)",
    "¬": r"\(\neg\)",
    "ℵ": r"\(\aleph\)",
    "ϵ": r"\(\epsilon\)",
    "ε": r"\(\epsilon\)",
    "ϕ": r"\(\phi\)",
    "φ": r"\(\phi\)",
    "α": r"\(\alpha\)",
    "β": r"\(\beta\)",
    "γ": r"\(\gamma\)",
    "δ": r"\(\delta\)",
    "η": r"\(\eta\)",
    "θ": r"\(\theta\)",
    "κ": r"\(\kappa\)",
    "λ": r"\(\lambda\)",
    "ξ": r"\(\xi\)",
    "σ": r"\(\sigma\)",
    "τ": r"\(\tau\)",
    "ω": r"\(\omega\)",
    "Π": r"\(\Pi\)",
    "Θ": r"\(\Theta\)",
    "′": r"\(^{\prime}\)",
    "−": r"\(-\)",
    "×": r"\(\times\)",
    "±": r"\(\pm\)",
    "∗": r"\(\ast\)",
    "⋆": r"\(\star\)",
    "⊕": r"\(\oplus\)",
    "∫": r"\(\int\)",
    "∑": r"\(\sum\)",
    "√": r"\(\sqrt{\;}\)",
    "∼": r"\(\sim\)",
    "✓": r"\(\checkmark\)",
    "◦": r"\(\circ\)",
    "¯": r"\(\overline{\phantom{x}}\)",
    "\u0338": "",
}
_UNICODE_MATH_RE = re.compile(
    "|".join(
        re.escape(symbol)
        for symbol in sorted(_UNICODE_MATH_MAP, key=len, reverse=True)
    )
)

_INVALID_CHAR_TRANSLATION = {
    codepoint: None
    for codepoint in range(32)
    if codepoint not in (ord("\t"), ord("\n"), ord("\r"))
}


class LatexConverter:
    """Converts ExtractedContent to a complete LaTeX document string."""

    def __init__(
        self,
        use_ctex: bool = True,
        document_class: LatexDocumentClass | str | None = None,
        output_options: LatexOutputOptions | None = None,
    ):
        if document_class is None:
            self.document_class = (
                LatexDocumentClass.ctexart if use_ctex else LatexDocumentClass.article
            )
        elif isinstance(document_class, LatexDocumentClass):
            self.document_class = document_class
        else:
            self.document_class = LatexDocumentClass.from_value(str(document_class))
        self.use_ctex = self.document_class.is_ctex
        self.output_options = output_options or DEFAULT_OUTPUT_OPTIONS

    def convert(self, content: ExtractedContent, *, show_date: bool = False) -> str:
        lines: List[str] = []
        lines.append("% !TEX program = xelatex")
        lines.append(self.documentclass_line())
        lines.extend(self.preamble_lines())
        lines.append("")
        lines.append(r"\title{" + self._escape_latex(content.title) + "}")
        lines.append(self._date_line(show_date))
        lines.append("")
        lines.append(r"\begin{document}")
        lines.extend(self.title_page_lines())
        lines.append("")

        for block in content.blocks:
            text = self._escape_latex(block.text)

            if block.block_type == "heading":
                lines.append(r"\section{" + text + "}")
                lines.append("")
            elif block.block_type == "definition":
                lines.append(r"\begin{definition}")
                lines.append("  " + text)
                lines.append(r"\end{definition}")
                lines.append("")
            elif block.block_type == "theorem":
                lines.append(r"\begin{theorem}")
                lines.append("  " + text)
                lines.append(r"\end{theorem}")
                lines.append("")
            elif block.block_type == "lemma":
                lines.append(r"\begin{lemma}")
                lines.append("  " + text)
                lines.append(r"\end{lemma}")
                lines.append("")
            elif block.block_type == "property":
                lines.append(r"\begin{property}")
                lines.append("  " + text)
                lines.append(r"\end{property}")
                lines.append("")
            elif block.block_type == "corollary":
                lines.append(r"\begin{corollary}")
                lines.append("  " + text)
                lines.append(r"\end{corollary}")
                lines.append("")
            elif block.block_type == "example":
                lines.append(r"\begin{example}")
                lines.append("  " + text)
                lines.append(r"\end{example}")
                lines.append("")
            elif block.block_type == "proof":
                lines.append(r"\begin{proof}")
                lines.append("  " + text)
                lines.append(r"\end{proof}")
                lines.append("")
            else:
                lines.append(text)
                lines.append("")

        lines.append(r"\end{document}")
        lines.append("")
        return "\n".join(lines)

    def convert_fragments(
        self,
        *,
        title: str,
        fragments: Sequence[str],
        notes: Sequence[str] | None = None,
        show_date: bool = False,
    ) -> str:
        """Build a complete LaTeX document from trusted body fragments."""
        lines = self._document_header(title, show_date=show_date)
        note_lines = self.note_comment_lines(notes or [])
        if note_lines:
            lines.extend(note_lines)
            lines.append("")

        for cleaned in self.clean_body_fragments(fragments):
            lines.append(cleaned)
            lines.append("")

        lines.append(r"\end{document}")
        lines.append("")
        return "\n".join(lines)

    def documentclass_line(self) -> str:
        """Return the configured documentclass line."""
        if (
            self.document_class.is_ctex
            and self.output_options.ctex_font_profile == CtexFontProfile.local
        ):
            return rf"\documentclass[UTF8,fontset=none]{{{self.document_class.value}}}"
        return self.document_class.documentclass_line()

    def preamble_lines(self) -> list[str]:
        """Return package and theorem definitions shared by all output modes."""
        font_lines = self._ctex_font_lines()
        layout_lines = self._page_layout_lines()
        page_style_lines = self._article_page_style_lines()
        if self.document_class.is_beamer:
            lines = [
                *font_lines,
                *layout_lines,
                r"\usepackage{amsmath}",
                r"\usepackage{amssymb}",
                *self._beamer_box_lines(),
                r"\setbeamertemplate{navigation symbols}{}",
                r"\makeatletter",
                r"\@ifundefined{definition}{\newtheorem{definition}{定义}}{}",
                r"\@ifundefined{theorem}{\newtheorem{theorem}{定理}}{}",
                r"\@ifundefined{lemma}{\newtheorem{lemma}{引理}}{}",
                r"\@ifundefined{property}{\newtheorem{property}{性质}}{}",
                r"\@ifundefined{corollary}{\newtheorem{corollary}{推论}}{}",
                r"\@ifundefined{example}{\newtheorem{example}{例}}{}",
                r"\makeatother",
            ]
            return lines
        return [
            *font_lines,
            *layout_lines,
            *page_style_lines,
            r"\usepackage{amsmath}",
            r"\usepackage{amsthm}",
            r"\usepackage{amssymb}",
            r"\newtheorem{definition}{定义}",
            r"\newtheorem{theorem}{定理}",
            r"\newtheorem{lemma}{引理}",
            r"\newtheorem{property}{性质}",
            r"\newtheorem{corollary}{推论}",
            r"\newtheorem{example}{例}",
        ]

    def title_page_lines(self) -> list[str]:
        """Return document opening title page commands."""
        if self.document_class.is_beamer and self.output_options.beamer_title_page:
            return [
                r"\begin{frame}",
                r"\titlepage",
                r"\end{frame}",
            ]
        if self.document_class.is_beamer:
            return []
        return [r"\maketitle"]

    def title_block_lines(
        self,
        title: str,
        *,
        show_date: bool = False,
        subtitle: str | None = None,
    ) -> list[str]:
        """Return title and date lines for a LaTeX document wrapper."""
        lines = [r"\title{" + self._escape_latex(title) + "}"]
        if self.document_class.is_beamer and subtitle:
            lines.append(r"\subtitle{" + self._escape_latex(subtitle) + "}")
        lines.append(self._date_line(show_date))
        return lines

    def note_comment_lines(self, notes: Sequence[str]) -> list[str]:
        """Return sanitized LLM notes as LaTeX comments."""
        lines: list[str] = []
        for note in notes:
            sanitized_note = note.replace("\n", " ").strip()
            if sanitized_note:
                lines.append("% LLM note: " + sanitized_note)
        return lines

    def clean_body_fragment(self, fragment: str) -> str:
        """Strip complete-document wrappers from a trusted body fragment."""
        text = normalize_latex_fragment_newlines(fragment)
        text = _DOCUMENTCLASS_RE.sub("", text)
        text = _USEPACKAGE_RE.sub("", text)
        text = text.replace(r"\begin{document}", "")
        text = text.replace(r"\end{document}", "")
        text = replace_unsupported_graphics_references(text)
        if not self.document_class.is_beamer:
            text = self._articleize_beamer_fragment(text)
        return text.strip()

    def clean_body_fragments(self, fragments: Sequence[str]) -> list[str]:
        """Return non-empty cleaned body fragments in input order."""
        cleaned_fragments: list[str] = []
        for fragment in fragments:
            cleaned = self.clean_body_fragment(fragment)
            if cleaned:
                cleaned_fragments.append(cleaned)
        return cleaned_fragments

    def _document_header(self, title: str, *, show_date: bool = False) -> List[str]:
        title_page_lines = self.title_page_lines()
        return [
            "% !TEX program = xelatex",
            self.documentclass_line(),
            *self.preamble_lines(),
            "",
            *self.title_block_lines(title, show_date=show_date),
            "",
            r"\begin{document}",
            *title_page_lines,
            "",
        ]

    def _ctex_font_lines(self) -> list[str]:
        if (
            not self.document_class.is_ctex
            or self.output_options.ctex_font_profile != CtexFontProfile.local
        ):
            return []
        return [
            r"\setCJKmainfont[Script=Default,BoldFont={WenQuanYi Zen Hei},ItalicFont={AR PL KaitiM GB}]{AR PL UMing CN}",
            r"\setCJKsansfont[Script=Default,BoldFont={WenQuanYi Zen Hei},ItalicFont={AR PL KaitiM GB}]{WenQuanYi Zen Hei}",
            r"\setCJKmonofont[Script=Default]{WenQuanYi Zen Hei Mono}",
            r"\setCJKfamilyfont{zhsong}[Script=Default,BoldFont={WenQuanYi Zen Hei},ItalicFont={AR PL KaitiM GB}]{AR PL UMing CN}",
            r"\setCJKfamilyfont{zhhei}[Script=Default,BoldFont={WenQuanYi Zen Hei},ItalicFont={AR PL KaitiM GB}]{WenQuanYi Zen Hei}",
            r"\setCJKfamilyfont{zhkai}[Script=Default]{AR PL KaitiM GB}",
            r"\providecommand{\songti}{\CJKfamily{zhsong}}",
            r"\providecommand{\heiti}{\CJKfamily{zhhei}}",
            r"\providecommand{\kaishu}{\CJKfamily{zhkai}}",
        ]

    def _page_layout_lines(self) -> list[str]:
        if self.document_class.is_beamer:
            return [r"\setbeamersize{text margin left=1em,text margin right=1em}"]
        return [
            r"\setlength{\oddsidemargin}{\dimexpr(\paperwidth-\textwidth)/2-1in\relax}",
            r"\setlength{\evensidemargin}{\oddsidemargin}",
        ]

    def _article_page_style_lines(self) -> list[str]:
        if self.document_class.family != "article":
            return []
        return [
            r"\makeatletter",
            r"\def\ps@plain{\let\@oddhead\@empty\let\@evenhead\@empty\def\@oddfoot{\hfil\thepage\hfil}\def\@evenfoot{\hfil\thepage\hfil}}",
            r"\pagestyle{plain}",
            r"\makeatother",
        ]

    def _beamer_box_lines(self) -> list[str]:
        if self.output_options.beamer_box_style != BeamerBoxStyle.tcolorbox:
            return []
        return [
            r"\usepackage[most]{tcolorbox}",
            r"\tcbset{texbookbox/.style={enhanced,breakable,sharp corners,boxrule=0.4pt,left=1mm,right=1mm,top=1mm,bottom=1mm,fonttitle=\bfseries}}",
            r"\newtcolorbox{texbookinfobox}[2][]{texbookbox,colback=blue!4!white,colframe=blue!65!black,title={#2},#1}",
            r"\newtcolorbox{texbookexamplebox}[2][]{texbookbox,colback=green!5!white,colframe=green!45!black,title={#2},#1}",
            r"\newtcolorbox{texbookalertbox}[2][]{texbookbox,colback=red!4!white,colframe=red!65!black,title={#2},#1}",
            r"\newtcolorbox{texbookplainbox}[2][]{texbookbox,colback=black!3!white,colframe=black!50,title={#2},#1}",
        ]

    def _date_line(self, show_date: bool) -> str:
        return r"\date{\today}" if show_date else r"\date{}"

    def _clean_body_fragment(self, fragment: str) -> str:
        return self.clean_body_fragment(fragment)

    def _articleize_beamer_fragment(self, fragment: str) -> str:
        text = re.sub(r"\\begin\{frame\}(?:\[[^\]]*\])?", "", fragment)
        text = text.replace(r"\end{frame}", "")
        text = re.sub(
            r"\\frametitle\{([^{}\n]+)\}",
            lambda match: r"\subsection*{" + match.group(1).strip() + "}",
            text,
        )
        text = re.sub(
            r"\\begin\{(?:alertblock|exampleblock|block)\}\{([^{}\n]+)\}",
            lambda match: r"\paragraph{" + match.group(1).strip() + "}",
            text,
        )
        text = re.sub(r"\\end\{(?:alertblock|exampleblock|block)\}", "", text)
        return text

    def _escape_latex(self, text: str) -> str:
        text = self._strip_invalid_chars(text)
        text = _ESCAPE_RE.sub(lambda match: _ESCAPE_MAP[match.group(0)], text)
        return self._replace_unicode_math(text)

    def _strip_invalid_chars(self, text: str) -> str:
        return text.translate(_INVALID_CHAR_TRANSLATION)

    def _replace_unicode_math(self, text: str) -> str:
        return _UNICODE_MATH_RE.sub(
            lambda match: _UNICODE_MATH_MAP[match.group(0)],
            text,
        )
