"""Tests for LaTeX document assembly."""

from pathlib import PurePosixPath

from texbook.complex_content import (
    ComplexContentCandidate,
    ComplexContentKind,
    ComplexContentSource,
    ComplexContentStrategy,
    collect_complex_content_candidates,
    complex_content_metadata,
)
from texbook.document_class import LatexDocumentClass
from texbook.convert.latex_converter import LatexConverter
from texbook.convert.project import (
    LatexProjectBuilder,
    LatexProjectResult,
    LatexProjectSection,
)
from texbook.output_options import (
    BeamerBoxStyle,
    CtexFontProfile,
    LatexOutputOptions,
)
from texbook.structure import (
    StructureItemKind,
    StructurePlan,
    StructurePlanItem,
    StructurePlanSource,
)


_CENTERED_TEXT_BLOCK_LINES = [
    r"\setlength{\oddsidemargin}{\dimexpr(\paperwidth-\textwidth)/2-1in\relax}",
    r"\setlength{\evensidemargin}{\oddsidemargin}",
]
_BEAMER_CENTERED_TEXT_MARGIN_LINE = (
    r"\setbeamersize{text margin left=1em,text margin right=1em}"
)
_ARTICLE_FOOTER_PAGE_NUMBER_LINES = [
    r"\makeatletter",
    r"\def\ps@plain{\let\@oddhead\@empty\let\@evenhead\@empty\def\@oddfoot{\hfil\thepage\hfil}\def\@evenfoot{\hfil\thepage\hfil}}",
    r"\pagestyle{plain}",
    r"\makeatother",
]
_ARTICLE_FOOTER_PAGE_NUMBER_STYLE_LINES = [
    _ARTICLE_FOOTER_PAGE_NUMBER_LINES[1],
    _ARTICLE_FOOTER_PAGE_NUMBER_LINES[2],
]


def test_convert_fragments_strips_document_wrappers():
    converter = LatexConverter()
    latex = converter.convert_fragments(
        title="讲义",
        fragments=[
            r"\documentclass{article}\begin{document}\section{集合}\end{document}",
            r"\begin{definition}定义内容\end{definition}",
        ],
        notes=["removed header/footer"],
    )

    assert latex.count(r"\documentclass") == 1
    assert r"\section{集合}" in latex
    assert r"\begin{definition}定义内容\end{definition}" in latex
    assert "% LLM note: removed header/footer" in latex
    assert r"\date{}" in latex


def test_convert_fragments_hides_date_by_default():
    converter = LatexConverter()

    latex = converter.convert_fragments(title="讲义", fragments=[])

    assert r"\date{}" in latex
    assert r"\date{\today}" not in latex


def test_convert_fragments_can_show_today_date():
    converter = LatexConverter()

    latex = converter.convert_fragments(
        title="讲义",
        fragments=[],
        show_date=True,
    )

    assert r"\date{\today}" in latex


def test_escape_latex_escapes_special_characters_once():
    converter = LatexConverter()

    assert (
        converter._escape_latex(r"\{}$&#^_%~")
        == r"\textbackslash{}\{\}\$\&\#\^{}\_\%\textasciitilde{}"
    )


def test_escape_latex_replaces_unicode_math_symbols():
    converter = LatexConverter()

    assert converter._escape_latex("A∈B") == r"A\(\in\)B"
    assert converter._escape_latex("A∉B") == r"A\(\notin\)B"
    assert converter._escape_latex("x≠y") == r"x\(\ne\)y"
    assert converter._escape_latex("αβ≤γ") == (
        r"\(\alpha\)\(\beta\)\(\le\)\(\gamma\)"
    )


def test_strip_invalid_chars_removes_control_chars_but_keeps_whitespace():
    converter = LatexConverter()

    assert converter._strip_invalid_chars("a\x00b\tc\nd\re\x1ff") == "ab\tc\nd\ref"


def test_non_beamer_document_classes_center_text_block():
    for document_class in (
        LatexDocumentClass.article,
        LatexDocumentClass.book,
        LatexDocumentClass.ctexart,
        LatexDocumentClass.ctexbook,
    ):
        converter = LatexConverter(document_class=document_class)
        preamble = "\n".join(converter.preamble_lines())

        for line in _CENTERED_TEXT_BLOCK_LINES:
            assert line in preamble
        assert _BEAMER_CENTERED_TEXT_MARGIN_LINE not in preamble


def test_beamer_document_classes_use_symmetric_text_margins():
    for document_class in (
        LatexDocumentClass.beamer,
        LatexDocumentClass.ctexbeamer,
    ):
        converter = LatexConverter(document_class=document_class)
        preamble = "\n".join(converter.preamble_lines())

        assert _BEAMER_CENTERED_TEXT_MARGIN_LINE in preamble
        for line in _CENTERED_TEXT_BLOCK_LINES:
            assert line not in preamble


def test_article_document_classes_use_centered_footer_page_numbers():
    for document_class in (
        LatexDocumentClass.article,
        LatexDocumentClass.ctexart,
    ):
        converter = LatexConverter(document_class=document_class)
        preamble = "\n".join(converter.preamble_lines())

        for line in _ARTICLE_FOOTER_PAGE_NUMBER_LINES:
            assert line in preamble


def test_book_and_beamer_document_classes_keep_existing_page_style():
    for document_class in (
        LatexDocumentClass.book,
        LatexDocumentClass.ctexbook,
        LatexDocumentClass.beamer,
        LatexDocumentClass.ctexbeamer,
    ):
        converter = LatexConverter(document_class=document_class)
        preamble = "\n".join(converter.preamble_lines())

        for line in _ARTICLE_FOOTER_PAGE_NUMBER_STYLE_LINES:
            assert line not in preamble


def test_clean_body_fragment_strips_document_wrappers_with_options():
    converter = LatexConverter()
    cleaned = converter._clean_body_fragment(
        r"""
        \documentclass[UTF8]{ctexart}
        \usepackage{amsmath}
        \usepackage[margin=1in]{geometry}
        \begin{document}
        \section{集合}
        \end{document}
        """
    )

    assert r"\documentclass" not in cleaned
    assert r"\usepackage" not in cleaned
    assert r"\begin{document}" not in cleaned
    assert r"\end{document}" not in cleaned
    assert cleaned == r"\section{集合}"


def test_clean_body_fragment_replaces_unsupported_graphics_reference():
    converter = LatexConverter()

    cleaned = converter.clean_body_fragment(
        r"正文前\includegraphics[width=.8\textwidth]{figures/missing.png}正文后"
    )

    assert r"\includegraphics" not in cleaned
    assert "% TODO: figure pending_asset" in cleaned
    assert "missing.png" in cleaned


def test_clean_body_fragment_restores_literal_newline_escapes():
    converter = LatexConverter(document_class=LatexDocumentClass.ctexbeamer)

    cleaned = converter.clean_body_fragment(
        r"\begin{frame}\n  % TODO: figure pending_asset: logo\n\end{frame}"
    )

    assert cleaned == (
        "\\begin{frame}\n  % TODO: figure pending_asset: logo\n\\end{frame}"
    )


def test_complex_content_candidate_metadata_roundtrip():
    candidate = ComplexContentCandidate(
        kind=ComplexContentKind.figure,
        strategy=ComplexContentStrategy.pending_asset,
        page_number=7,
        bbox=(1, 2, 3, 4),
        source=ComplexContentSource.llm_note,
        confidence=1.5,
        note="  第 7 页图片待裁切\n",
    )

    metadata = candidate.to_metadata()
    restored = ComplexContentCandidate.from_metadata(metadata)

    assert metadata == {
        "kind": "figure",
        "strategy": "pending_asset",
        "source": "llm_note",
        "confidence": 1.0,
        "page_number": 7,
        "bbox": [1.0, 2.0, 3.0, 4.0],
        "note": "第 7 页图片待裁切",
    }
    assert restored == candidate


def test_collect_complex_content_candidates_from_latex_and_notes():
    candidates = collect_complex_content_candidates(
        fragments=[
            r"""
            \begin{tabular}{cc}
            a & b
            \end{tabular}

            % TODO: figure pending_asset 第 3 页图像需要裁切
            % TODO: layout 第 4 页多栏顺序需人工复核
            """
        ],
        notes=["第 5 页图片结构不可靠"],
    )
    metadata = complex_content_metadata(candidates)

    assert [candidate.kind for candidate in candidates] == [
        ComplexContentKind.table,
        ComplexContentKind.figure,
        ComplexContentKind.layout_note,
        ComplexContentKind.figure,
    ]
    assert metadata["complex_content"]["schema_version"] == 1
    assert metadata["complex_content"]["candidates"][1]["page_number"] == 3


def test_project_builder_builds_main_preamble_and_chapters():
    builder = LatexProjectBuilder()

    project = builder.build(
        title="讲义",
        fragments=[
            r"\documentclass{article}\begin{document}\section{集合}\end{document}",
            r"\usepackage{amsmath}\begin{definition}定义内容\end{definition}",
        ],
        notes=["removed header/footer"],
    )

    assert project.entrypoint == PurePosixPath("main.tex")
    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("chapters/chapter02.tex"),
    }
    assert project.notes == ["removed header/footer"]
    assert project.metadata["document_class"] == "ctexart"
    assert project.metadata["output_options"] == {
        "beamer_box_style": "block",
        "ctex_font_profile": "default",
        "beamer_title_page": True,
    }

    main = project.files[PurePosixPath("main.tex")]
    assert "% !TEX program = xelatex" in main
    assert r"\documentclass[UTF8]{ctexart}" in main
    assert r"\input{preamble}" in main
    assert r"\title{讲义}" in main
    assert r"\date{}" in main
    assert r"\begin{document}" in main
    assert "% LLM note: removed header/footer" in main
    assert r"\input{chapters/chapter01}" in main
    assert r"\input{chapters/chapter02}" in main
    assert main.rstrip().endswith(r"\end{document}")

    preamble = project.files[PurePosixPath("preamble.tex")]
    assert r"\usepackage{amsmath}" in preamble
    assert r"\newtheorem{definition}{定义}" in preamble
    for line in _CENTERED_TEXT_BLOCK_LINES:
        assert line in preamble
    for line in _ARTICLE_FOOTER_PAGE_NUMBER_LINES:
        assert line in preamble
    assert r"\documentclass" not in preamble
    assert r"\begin{document}" not in preamble

    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]
    assert chapter == "\\section{集合}\n"
    assert r"\documentclass" not in chapter
    assert r"\begin{document}" not in chapter
    assert r"\end{document}" not in chapter


def test_project_builder_records_complex_content_metadata():
    builder = LatexProjectBuilder()

    project = builder.build(
        title="讲义",
        fragments=[
            r"""
            正文

            % TODO: table 第 2 页表格结构不可靠
            \includegraphics{figures/raw.png}
            """
        ],
        notes=["第 3 页旁注需要人工复核"],
    )

    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]
    candidates = project.metadata["complex_content"]["candidates"]

    assert r"\includegraphics" not in chapter
    assert "% TODO: figure pending_asset" in chapter
    assert {candidate["kind"] for candidate in candidates} == {
        "table",
        "figure",
        "layout_note",
    }


def test_project_builder_can_emit_ctexbeamer_project():
    builder = LatexProjectBuilder()

    project = builder.build(
        title="幻灯片",
        document_class=LatexDocumentClass.ctexbeamer,
        fragments=[
            r"""
            \begin{frame}
            \frametitle{集合}
            \begin{block}{注意}
            正文
            \end{block}
            \end{frame}
            """
        ],
    )

    main = project.files[PurePosixPath("main.tex")]
    preamble = project.files[PurePosixPath("preamble.tex")]
    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]

    assert r"\documentclass[UTF8]{ctexbeamer}" in main
    assert r"\begin{frame}" in main
    assert r"\titlepage" in main
    assert r"\@ifundefined{definition}" in preamble
    assert _BEAMER_CENTERED_TEXT_MARGIN_LINE in preamble
    assert r"\setbeamertemplate{navigation symbols}{}" in preamble
    assert r"\begin{frame}" in chapter
    assert r"\frametitle{集合}" in chapter
    assert r"\begin{block}{注意}" in chapter
    assert project.metadata["document_class"] == "ctexbeamer"


def test_ctex_local_font_profile_uses_fontset_none_and_local_fonts():
    converter = LatexConverter(
        document_class=LatexDocumentClass.ctexbeamer,
        output_options=LatexOutputOptions(
            ctex_font_profile=CtexFontProfile.local,
        ),
    )

    assert converter.documentclass_line() == (
        r"\documentclass[UTF8,fontset=none]{ctexbeamer}"
    )
    preamble = "\n".join(converter.preamble_lines())
    assert "AR PL UMing CN" in preamble
    assert "WenQuanYi Zen Hei" in preamble
    assert "Fandol" not in preamble


def test_project_builder_can_emit_tcolorbox_beamer_project():
    builder = LatexProjectBuilder(
        output_options=LatexOutputOptions(
            beamer_box_style=BeamerBoxStyle.tcolorbox,
        )
    )

    project = builder.build(
        title="幻灯片",
        document_class=LatexDocumentClass.ctexbeamer,
        fragments=[
            r"""
            \begin{frame}
            \frametitle{集合}
            \begin{texbookinfobox}{注意}
            正文
            \end{texbookinfobox}
            \end{frame}
            """
        ],
    )

    preamble = project.files[PurePosixPath("preamble.tex")]
    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]

    assert r"\usepackage[most]{tcolorbox}" in preamble
    assert r"\newtcolorbox{texbookinfobox}" in preamble
    assert r"\begin{texbookinfobox}{注意}" in chapter
    assert project.metadata["output_options"]["beamer_box_style"] == "tcolorbox"


def test_project_builder_folds_beamer_title_only_frontmatter_into_subtitle():
    builder = LatexProjectBuilder()
    plan = StructurePlan(
        source=StructurePlanSource.llm_toc,
        confidence=0.8,
        items=[
            StructurePlanItem(
                kind=StructureItemKind.frontmatter,
                title="第六章 线性空间",
                start_page=1,
                end_page=1,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
            StructurePlanItem(
                kind=StructureItemKind.chapter,
                title="集合",
                start_page=2,
                end_page=4,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
        ],
    )

    project = builder.build_from_plan(
        title="6.1 集合与映射",
        document_class=LatexDocumentClass.ctexbeamer,
        structure_plan=plan,
        sections=[
            LatexProjectSection(
                item=plan.items[0],
                fragments=[
                    r"""
                    \section*{第六章 线性空间}
                    \begin{frame}
                    \frametitle{第六章 线性空间}
                    % TODO: figure pending_asset: logo
                    \end{frame}
                    """
                ],
            ),
            LatexProjectSection(
                item=plan.items[1],
                fragments=[
                    r"""
                    \begin{frame}
                    \frametitle{6.1 集合与映射}
                    \begin{itemize}
                    \item 集合
                    \item 映射
                    \end{itemize}
                    \end{frame}
                    """
                ],
            ),
        ],
    )

    main = project.files[PurePosixPath("main.tex")]

    assert PurePosixPath("chapters/frontmatter.tex") not in project.files
    assert r"\subtitle{第六章 线性空间}" in main
    assert r"\input{chapters/chapter01}" in main
    assert "frontmatter" not in main


def test_project_builder_can_disable_beamer_title_page():
    builder = LatexProjectBuilder(
        output_options=LatexOutputOptions(beamer_title_page=False),
    )
    plan = StructurePlan(
        source=StructurePlanSource.llm_toc,
        confidence=0.8,
        items=[
            StructurePlanItem(
                kind=StructureItemKind.frontmatter,
                title="第六章 线性空间",
                start_page=1,
                end_page=1,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
            StructurePlanItem(
                kind=StructureItemKind.chapter,
                title="集合",
                start_page=2,
                end_page=4,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
        ],
    )

    project = builder.build_from_plan(
        title="6.1 集合与映射",
        document_class=LatexDocumentClass.ctexbeamer,
        structure_plan=plan,
        sections=[
            LatexProjectSection(
                item=plan.items[0],
                fragments=[
                    r"""
                    \section*{第六章 线性空间}
                    \begin{frame}
                    \frametitle{第六章 线性空间}
                    \end{frame}
                    """
                ],
            ),
            LatexProjectSection(
                item=plan.items[1],
                fragments=[
                    r"""
                    \begin{frame}
                    \frametitle{6.1 集合与映射}
                    \begin{itemize}
                    \item 集合
                    \item 映射
                    \end{itemize}
                    \end{frame}
                    """
                ],
            ),
        ],
    )

    main = project.files[PurePosixPath("main.tex")]
    assert r"\titlepage" not in main
    assert r"\subtitle{第六章 线性空间}" not in main
    assert r"\input{chapters/frontmatter}" in main
    assert project.metadata["output_options"]["beamer_title_page"] is False


def test_non_beamer_project_cleans_beamer_only_wrappers():
    builder = LatexProjectBuilder()

    project = builder.build(
        title="讲义",
        fragments=[
            r"""
            \begin{frame}
            \frametitle{集合}
            \begin{block}{注意}
            正文
            \end{block}
            \end{frame}
            """
        ],
    )

    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]

    assert r"\begin{frame}" not in chapter
    assert r"\frametitle" not in chapter
    assert r"\begin{block}" not in chapter
    assert r"\subsection*{集合}" in chapter
    assert r"\paragraph{注意}" in chapter


def test_project_builder_skips_empty_chapters_and_can_show_today_date():
    builder = LatexProjectBuilder()

    project = builder.build(
        title="讲义",
        fragments=[
            r"\documentclass{article}\begin{document}\end{document}",
            r"\section{非空}",
        ],
        show_date=True,
    )

    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/chapter01.tex"),
    }
    assert r"\date{\today}" in project.files[PurePosixPath("main.tex")]
    assert project.files[PurePosixPath("chapters/chapter01.tex")] == "\\section{非空}\n"


def test_project_builder_builds_semantic_plan_files_with_appendix():
    builder = LatexProjectBuilder()
    plan = StructurePlan(
        source=StructurePlanSource.llm_toc,
        confidence=0.8,
        items=[
            StructurePlanItem(
                kind=StructureItemKind.frontmatter,
                title="前言",
                start_page=1,
                end_page=1,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
            StructurePlanItem(
                kind=StructureItemKind.chapter,
                title="第一章 集合",
                start_page=2,
                end_page=5,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
            StructurePlanItem(
                kind=StructureItemKind.appendix,
                title="附录 A",
                start_page=6,
                end_page=7,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
        ],
    )

    project = builder.build_from_plan(
        title="教材",
        structure_plan=plan,
        sections=[
            LatexProjectSection(item=plan.items[0], fragments=["前置说明"]),
            LatexProjectSection(
                item=plan.items[1],
                fragments=[r"\section{第一章 集合}" + "\n正文"],
            ),
            LatexProjectSection(item=plan.items[2], fragments=["附录正文"]),
        ],
        notes=["plan note"],
    )

    assert set(project.files) == {
        PurePosixPath("main.tex"),
        PurePosixPath("preamble.tex"),
        PurePosixPath("chapters/frontmatter.tex"),
        PurePosixPath("chapters/chapter01.tex"),
        PurePosixPath("appendices/appendix01.tex"),
    }
    main = project.files[PurePosixPath("main.tex")]
    assert r"\input{chapters/frontmatter}" in main
    assert r"\input{chapters/chapter01}" in main
    assert r"\appendix" in main
    assert r"\input{appendices/appendix01}" in main
    assert project.files[PurePosixPath("chapters/frontmatter.tex")].startswith(
        r"\section*{前言}"
    )
    assert project.files[PurePosixPath("chapters/chapter01.tex")].count(
        r"\section{第一章 集合}"
    ) == 1
    assert project.metadata["structure_plan"]["source"] == "llm-toc"


def test_project_builder_promotes_planned_book_sections_to_chapters():
    builder = LatexProjectBuilder()
    plan = StructurePlan(
        source=StructurePlanSource.llm_toc,
        confidence=0.8,
        items=[
            StructurePlanItem(
                kind=StructureItemKind.chapter,
                title="第一章 集合",
                start_page=1,
                end_page=2,
                confidence=0.8,
                source=StructurePlanSource.llm_toc,
            ),
        ],
    )

    project = builder.build_from_plan(
        title="教材",
        document_class=LatexDocumentClass.ctexbook,
        structure_plan=plan,
        sections=[
            LatexProjectSection(
                item=plan.items[0],
                fragments=[r"\section{第一章 集合}" + "\n正文"],
            ),
        ],
    )

    main = project.files[PurePosixPath("main.tex")]
    chapter = project.files[PurePosixPath("chapters/chapter01.tex")]

    assert r"\documentclass[UTF8]{ctexbook}" in main
    assert chapter.startswith(r"\chapter{第一章 集合}")
    assert r"\section{第一章 集合}" not in chapter
    assert project.metadata["document_class"] == "ctexbook"


def test_project_result_rejects_missing_entrypoint():
    try:
        LatexProjectResult(
            files={PurePosixPath("preamble.tex"): ""},
            entrypoint=PurePosixPath("main.tex"),
        )
    except ValueError as exc:
        assert "entrypoint" in str(exc)
    else:
        raise AssertionError("LatexProjectResult should reject missing entrypoint")


def test_project_result_rejects_unsafe_file_paths():
    try:
        LatexProjectResult(
            files={
                PurePosixPath("main.tex"): "",
                PurePosixPath("../outside.tex"): "",
            },
            entrypoint=PurePosixPath("main.tex"),
        )
    except ValueError as exc:
        assert "relative POSIX" in str(exc)
    else:
        raise AssertionError("LatexProjectResult should reject unsafe paths")
