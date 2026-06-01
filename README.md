# TexBook

PDF 转 LaTeX 的 Python CLI 工具。它面向数学讲义、教材和幻灯片：先从 PDF 提取页面文本、位置、字号并渲染页面图像，再调用 OpenAI-compatible 视觉模型重建 LaTeX 正文片段，最后由本地代码生成完整 `.tex` 文档。

## 环境要求

- Python 3.10+（开发验证环境使用 3.13）
- 可访问的 OpenAI-compatible API，且模型需要支持图片输入
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖

TexBook 只安装 PDF 转 LaTeX 工具本身。生成的 `.tex` 默认会先判断适合的 LaTeX 文档类，并使用 `ctexart`、`ctexbook` 或 `ctexbeamer` 等外壳及常用数学宏包；如果需要在本地编译生成结果，请自行准备 TeX Live、`xelatex` 和 `latexmk`。

## 安装

推荐使用 uv：

```bash
uv sync
```

也可以只安装运行时依赖：

```bash
uv pip install -e .
```

或使用 pip：

```bash
pip install -e .
```

包源码位于 `src/texbook`，从仓库根目录以可编辑模式安装后即可使用 `texbook` 命令。

## 配置

转换前需要配置模型和密钥：

```bash
export TEXBOOK_MODEL="your-vision-model"
export TEXBOOK_API_KEY="your-api-key"
export TEXBOOK_BASE_URL="https://your-api.example/v1"
```

`TEXBOOK_BASE_URL` 是可选项；使用默认 OpenAI SDK 地址时可以不设置。

也可以在命令行中使用 `--model`、`--api-key`、`--base-url` 临时覆盖环境变量。

## 使用方法

单文件转换到 stdout：

```bash
uv run texbook extract "input/lecture.pdf"
```

单文件转换到指定文件：

```bash
uv run texbook extract "input/lecture.pdf" -o "output/lecture.tex"
```

只给 `-o` 文件名时，输出路径会解析到仓库根目录的 `src/` 下；带目录时按仓库根目录解析。路径包含空格时请使用引号。

当前 `extract` 的 `-o` 始终表示单个 `.tex` 输出文件，不会根据路径后缀或目标路径是否为目录自动切换输出模式。目录化项目输出使用显式入口：

```bash
uv run texbook extract "input/lecture.pdf" --project -o "lecture-project"
```

只给 `-o` 项目名时，项目目录会解析到仓库根目录的 `src/` 下，例如 `src/lecture-project/main.tex`。项目模式默认只写入不存在或空目录；目标项目目录非空时会拒绝覆盖。确认要清空该项目目录并重新生成时，添加 `--force`：

```bash
uv run texbook extract "input/lecture.pdf" --project -o "lecture-project" --force
```

默认 `--document-class auto` 会让 LLM 先判断 PDF 更适合 `article`、`book`、`beamer` 或对应 CTeX 类。中文教材、讲义和幻灯片通常会分别生成 `ctexbook`、`ctexart` 或 `ctexbeamer`。如果转换目的已经明确，建议直接用 `--document-class` 指定目标文档类；`auto` 更适合不确定时作为兜底，但它偶尔会把文档判成与预期不同的类：

```bash
uv run texbook extract "input/slides.pdf" --project -o "slides-project" \
  --document-class ctexbeamer
```

Beamer 输出默认使用原生 `block`、`exampleblock` 和 `alertblock`。如果希望幻灯片强调块更接近使用 `tcolorbox` 的课件风格，可以显式开启：

```bash
uv run texbook extract "input/slides.pdf" --project -o "slides-project" \
  --document-class ctexbeamer --beamer-box-style tcolorbox
```

Beamer 项目默认会根据文件名或 `--title-source` 生成一个额外标题页。如果原 PDF 已经自带封面页，不希望再插入文件名标题页，可以关闭该行为：

```bash
uv run texbook extract "input/slides.pdf" --project -o "slides-project" \
  --document-class ctexbeamer --no-beamer-title-page
```

CTeX 输出默认沿用 CTeX 自带字体配置。如果本机已安装中文字体，并希望避免默认 Fandol 字体在部分 XeLaTeX 环境中的 fontspec warning，可以使用本机字体配置：

```bash
uv run texbook extract "input/slides.pdf" --project -o "slides-project" \
  --document-class ctexbeamer --ctex-font-profile local
```

项目模式默认启用大型教材结构规划（`--structure auto`）：如果 PDF 有有效书签，直接按书签生成章级文件；书签缺失或只有页码等无效内容时，会让 LLM 先读取开头少量页面判断是否包含目录，必要时继续读取下一段；仍无法确认目录时，再根据全书标题字号和页面开头文本推断章级结构。自动规划失败会回退到按 LLM chunk 生成章节文件，并在 notes 中提示。需要完全保留旧的 chunk-based 项目输出时，使用：

```bash
uv run texbook extract "input/lecture.pdf" --project --structure off -o "lecture-project"
```

只转换部分页面：

```bash
uv run texbook extract "input/lecture.pdf" --pages 1,3-6 -o "output/partial.tex"
```

追加一次性转换要求：

```bash
uv run texbook extract "input/lecture.pdf" --pages 7 -o "output/formulas.tex" \
  --extra-prompt "只提取数学公式，忽略其他文字"
```

批量转换目录中的 PDF：

```bash
uv run texbook batch input/ -o output/
```

`batch` 默认匹配 `*.pdf`，并把每个 PDF 写成同名 `.tex`。单个文件失败时会记录原因并继续处理后续文件；如果没有匹配文件或存在失败文件，命令会以非零退出码结束。

批量项目输出使用 `batch --project`，每个 PDF 写入独立项目目录：

```bash
uv run texbook batch input/ --project -o src/
```

例如 `input/book.pdf` 会写入 `src/book/main.tex`。`batch --project --force` 只会清空对应 PDF 的项目目录，不会清空整个输出父目录。

需要排查转换过程时，`extract` 和 `batch` 都支持 `--log-file PATH`，命令启动时会创建或覆盖指定 JSONL 日志文件，并在运行期间持续追加诊断事件：

```bash
uv run texbook extract "input/lecture.pdf" -o "output/lecture.tex" --log-file "logs/lecture.jsonl"
uv run texbook batch input/ -o output/ --log-file "logs/batch.jsonl"
```

## 本地编译

如果本地存在 `.latexmkrc`、`src/.latexmkrc` 和 `scripts/post-build.sh`，可以从仓库根目录运行：

```bash
latexmk src/test.tex
latexmk "src/lecture 1.tex"
latexmk src/lecture-project/main.tex
```

也可以进入 `src/` 后运行：

```bash
cd src
latexmk test.tex
latexmk "lecture 1.tex"
latexmk lecture-project/main.tex
```

本地 `.latexmkrc` 使用 `xelatex`，把最终 PDF 输出到 `out/` 根部，把辅助产物输出或归位到 `build/<产物名>/`。普通单文件入口使用文件 stem 作为产物名，例如 `src/test.tex` 生成 `out/test.pdf` 与 `build/test/`；目录化项目入口 `src/lecture-project/main.tex` 使用直接父目录名作为产物名，生成 `out/lecture-project.pdf` 与 `build/lecture-project/`。这些目录默认不进入 Git。

## 标题、日期和 Prompt 预设

- 默认使用 PDF 文件名作为 `\title{}`。
- `extract --title "..."` 可手动指定单文件标题。
- `extract` 和 `batch` 都支持 `--title-source filename|llm`；`llm` 会结合文件名、页面 heading、页面开头文本和已生成 LaTeX 章节线索生成标题，失败时退回文件名。
- 默认生成 `\date{}` 隐藏日期；添加 `--show-date` 会生成 `\date{\today}`。
- 默认 Prompt 预设是 `math`，面向数学讲义。
- `--preset NAME` 可选择内置或仓库本地预设。
- `--extra-prompt` 会追加到预设自带额外说明之后。

预设管理命令：

```bash
uv run texbook presets list
uv run texbook presets show math
uv run texbook presets add --name math-lite
uv run texbook presets add --name math-lite --from-preset math --overwrite
```

仓库本地预设保存在 `config/texbook/presets/<name>.json`。名称需匹配 `[a-z0-9][a-z0-9_-]{1,63}`，且不能覆盖内置预设。

## 常用参数

- `--pages`：页码选择，例如 `1,3-6`，页码从 1 开始。
- `--chunk-pages`：每次发送给 LLM 的页数，默认 `4`。
- `--image-dpi`：页面图像渲染精度，默认 `160`。
- `--image-dpi-min`、`--image-dpi-max`：`auto` 图片模式的自适应 DPI 范围。
- `--image-format`：页面图像格式，支持 `png`、`jpeg`、`jpg`、`auto`。
- `--jpeg-quality`：JPEG 图像质量，默认 `85`。
- `--prefetch-chunks`：预渲染后续 chunk 数，默认 `1`；LLM 请求仍顺序发送。
- `--llm-retries`：可恢复 LLM 请求失败的重试次数，默认 `2`。
- `--llm-retry-initial-delay`、`--llm-retry-max-delay`：LLM 请求重试退避秒数，默认 `2.0` 与 `30.0`。
- `--llm-max-concurrency`：全局 LLM 请求最大并发数，默认 `1`。
- `--llm-min-request-interval`：LLM 请求开始之间的最小间隔秒数，默认 `0.0`。
- `batch --batch-workers`：批量模式中同时处理的 PDF 文件数，默认 `1`。
- `--timeout`：LLM 响应读取超时秒数，默认不限制等待时间。
- `--max-tokens`：LLM 响应最大 token 数，默认 `128000`。
- `--temperature`：LLM 采样温度，默认 `1.0`；`extract` 和 `batch` 都支持。
- `--document-class`：LaTeX 文档类，支持 `auto`、`article`、`book`、`beamer`、`ctexart`、`ctexbook`、`ctexbeamer`，默认 `auto`；`extract` 和 `batch`、单文件和项目模式都支持。
- `--beamer-box-style`：Beamer 强调块样式，支持 `block`、`tcolorbox`，默认 `block`。
- `--beamer-title-page/--no-beamer-title-page`：是否额外根据 LaTeX 标题块生成 Beamer 标题页，默认生成；关闭后原 PDF 封面页会作为普通 frame 保留。
- `--ctex-font-profile`：CTeX 字体配置，支持 `default`、`local`，默认 `default`；`local` 使用本机中文字体并生成 `fontset=none`。
- `--project`：输出目录化 LaTeX 项目；`extract` 需要同时指定 `-o <dir>`，`batch` 会为每个 PDF 创建独立项目目录。
- `--structure`：项目结构规划模式，支持 `auto`、`off`、`local`、`llm`，默认 `auto`，仅项目模式生效。
- `--structure-chunk-pages`：结构规划阶段每次发送给 LLM 的开头页数，默认 `8`。
- `--structure-max-pages`：结构规划阶段最多用页面图像检查的开头页数，默认 `32`。
- `--force`：仅与 `--project` 一起使用，清空目标项目目录后重新写入。
- `--cache-dir`：断点续传缓存目录，默认 `build/.texbook_cache/`。
- `--no-cache`：禁用结构规划与 chunk 缓存。
- `--clear-cache`：清理当前 PDF 和参数对应的缓存后再转换。
- `--log-file`：为 `extract` 或 `batch` 持续写入 JSONL 诊断日志，用于排查转换阶段、缓存、重试、失败和写盘问题。

断点续传缓存默认启用。相同 PDF、页码、模型、prompt、图片参数、最终文档类和结构规划参数重跑时会复用已完成 chunk；项目模式还会复用文档类判断和结构规划结果，避免大型教材重复规划。中途失败后再次运行可从已完成判断、规划或 chunk 继续。缓存目录中会保留 `document-class/document-class.json`、`evidence.json`、`structure-*.json` 等中间产物，便于检查文档类型、书签、标题线索、LLM 规划响应和最终结构计划。等待未命中缓存的 LLM 请求时，交互式终端会在 stderr 显示加载提示，stdout 仍只输出 LaTeX，便于重定向。

单个 PDF 的正文 chunk 会继续顺序发送给 LLM，因为后续 chunk 会收到前序 LaTeX 尾部作为上下文；`--prefetch-chunks` 只提前渲染页面图像。大量文件转换时可以用 `batch --batch-workers N` 做文件级并发，并用 `--llm-max-concurrency` 与 `--llm-min-request-interval` 控制共享 LLM 请求节奏。可恢复错误包括网络、超时、限流和服务端临时错误；已成功的结构规划和正文 chunk 会立即写入缓存，重新运行时继续复用。

## 诊断日志

诊断日志使用 JSONL 格式，每行一个事件。日志会记录命令启动、每个 PDF 的阶段进度、缓存命中、LLM 请求重试、失败、取消、写盘和命令完成等信息；`batch` 并发写入时会保持单行 JSON 完整。

日志会脱敏 API Key、授权头、token 等敏感字段。文档类、标题、结构规划或正文 chunk 的 LLM 响应解析失败时，日志只记录截断后的原始响应预览，便于定位模型输出格式问题，同时避免把完整响应无限制写入文件。

## 复杂内容处理

默认 prompt 会要求模型把清晰表格转换为可编译的 `tabular` 或 `array`。图片化表格、跨页表格、图片、图表、边栏、多栏和旁注等暂不可靠的复杂内容会降级为 `% TODO:` 注释，并通过 notes 或项目 metadata 记录；当前不会生成图片裁切资源，也不会输出指向不存在文件的 `\includegraphics`。

项目输出的 `metadata["complex_content"]` 只用于调试和后续扩展，不影响 `main.tex`、`preamble.tex` 和章节文件写盘。后续接入视觉定位和裁切流程后，可以复用这些候选信息生成 `figures/` 资源。

## 项目结构

```text
.
├─ .gitignore
├─ .python-version
├─ pyproject.toml
├─ uv.lock
├─ config/
│  └─ texbook/
│     └─ presets/                 # 可选：仓库本地 Prompt 预设
├─ src/
│  └─ texbook/                    # Python 包
│     ├─ cli.py                   # 命令行入口
│     ├─ convert/                 # LaTeX 文档外壳组装与输出
│     ├─ extract/                 # PDF 文本、位置、字号与图像提取
│     └─ llm/                     # LLM 客户端、缓存、流水线和 Prompt
└─ tests/                         # 单元测试
```

本地常用目录约定：

- `input/`、`docs/`：可放置待转换 PDF，默认不进入 Git。
- `output/`、`src/*.tex`：可放置生成的 `.tex`，默认不进入 Git。
- `build/`：默认缓存目录和本地编译辅助产物目录，默认不进入 Git。
- `out/`：本地编译最终 PDF 输出目录，默认不进入 Git。

## 开发

```bash
uv sync --group dev
uv run pytest
uv run ruff check
```

阶段验收需要真实转换时，优先使用少量页 PDF，避免误处理完整教材：

```bash
uv run texbook extract "docs/6.1 集合与映射.pdf" --project --structure off --pages 7 -o "stage9-smoke" --force
latexmk src/stage9-smoke/main.tex
```

## 许可证

MIT
