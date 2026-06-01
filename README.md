# TeXBook

TeXBook 是一款 Windows 桌面应用，用于把数学讲义、教材和幻灯片 PDF 转换为 LaTeX。

WSLg 版本位于 `WSLg` 分支，CLI 版本位于 `cli` 分支。

## 快速开始

直接运行 `TeXBook.exe` 即可启动应用。

首次使用时建议按下列顺序配置：

1. 在“模型配置”区域填写模型名、Base URL 和 API Key。
2. 在“输入”区域选择单个 PDF、多个 PDF 或目录批量。
3. 在“输出”区域选择单个 `.tex` 或目录化项目，并选择输出目标。
4. 点击“添加任务”，确认任务出现在右侧任务队列中。
5. 点击“开始转换”，等待任务完成；完成后可在任务行查看输出位置。

真实转换需要可访问的 OpenAI-compatible API，模型需要支持图片输入。

## 主要功能

- PDF 输入：支持单个 PDF、多个 PDF 和目录批量处理。
- 输出形式：支持单个 `.tex` 文件和目录化 LaTeX 项目。
- 转换参数：支持页面范围、文档类、结构规划、标题来源、日期、Beamer 标题页、Beamer 块样式和 CTeX 字体配置。
- 模型配置：支持模型名、Base URL、API Key、额外转换要求、temperature、最大 token、超时和重试。
- 缓存与恢复：默认使用 Windows 本机应用缓存目录缓存已完成的结构规划和正文 chunk，重复转换时可复用结果。
- 批量任务：多个 PDF 会加入同一个任务队列，可按文件级 worker 并发执行。
- 写盘保护：目标存在时可以覆盖前确认；目录化项目覆盖只清理当前项目目录，不清理批量输出父目录。
- 日志与排错：GUI 可导出当前会话 JSONL 诊断日志；CLI 的 `extract` 和 `batch` 可用 `--log-file` 持续写入日志。
- 界面偏好：支持亮色/暗色模式、中文/English 切换、字号设置和路径记忆。

## 基本使用流程

1. 在“输入”区域选择输入类型：单个 PDF、多个 PDF 或目录批量。
2. 选择 PDF 文件或 PDF 目录；目录批量时可设置匹配模式，默认是 `*.pdf`。
3. 在“输出”区域选择输出形式：单个 `.tex` 或目录化项目。
4. 选择输出目标：单个 PDF 输出可选择 `.tex` 文件或项目目录；批量输入输出到目录下的同名 `.tex` 或同名项目目录。
5. 在“模型配置”区域填写模型名、Base URL 和 API Key。
6. 按需调整页面范围、文档类、结构规划、缓存、并发和高级参数。
7. 点击“添加任务”，确认任务出现在右侧任务队列中。
8. 点击“开始转换”，等待任务完成；完成后可在任务行查看输出位置。

## 输入与输出

### 输入类型

- 单个 PDF：适合转换一本讲义、一个课件或一个短文档。
- 多个 PDF：一次选择多个文件，每个 PDF 会成为一个独立任务。
- 目录批量：选择一个目录并用 pattern 匹配 PDF，适合批量转换课件或章节文件。

### 输出形式

- 单个 `.tex`：每个 PDF 生成一个 LaTeX 文件。
- 目录化项目：每个 PDF 生成独立项目目录，包含入口文件、preamble 和章节文件。

批量输入时，TeXBook 会按 PDF 文件名推导输出目标。若多个输入会写入同一路径，应用会拒绝创建任务并提示冲突。

## 转换参数

### 页面与文档结构

- 页面范围支持 `1,3-6` 这样的 1-based 页码表达；留空表示全部页面。
- 文档类支持 `auto`、`article`、`book`、`beamer`、`ctexart`、`ctexbook`、`ctexbeamer`。
- `auto` 会结合 PDF 页面图像、文本层、书签和标题线索判断文档外壳；如果你已经知道目标是讲义、教材还是幻灯片，建议直接手动指定对应文档类，`auto` 更适合拿不准时作为兜底，但它偶尔会判成与预期不同的文档类。
- 目录化项目支持结构规划，可使用 PDF 书签、本地标题线索或 LLM 规划章节。
- Beamer 输出支持自动标题页开关、原生 block 或 `tcolorbox` 强调块样式。
- CTeX 输出支持默认字体配置和本机字体配置。

### 模型与 Prompt

- GUI 固定使用内置 `math` Prompt 预设，适合数学讲义、教材和幻灯片。
- API Key 可以直接输入，也可以填写环境变量名。
- Base URL 用于 OpenAI-compatible 接口；不填写时使用 SDK 默认地址。
- 额外要求会追加到当前 Prompt 后，用于本次任务的转换偏好。
- 高级参数支持 temperature、最大 token、请求超时、重试次数和重试退避。

环境变量方式适合不想在应用设置中保存密钥的场景。选择环境变量模式时，请在系统环境变量中提前配置真实密钥，并在界面中填写变量名。

### 缓存与并发

- 默认启用断点续传缓存，缓存目录位于 Windows 本机用户的应用缓存位置，例如 Local AppData 下的 TeXBook 缓存目录。
- 相同 PDF、页码、模型、Prompt、图片参数和输出选项再次转换时会复用缓存。
- 可以在界面中清理当前参数匹配的缓存。
- 批量 worker 控制文件级并发；LLM 最大并发和请求间隔控制模型请求节奏。

## 任务队列与写盘

每个 PDF 会成为一个独立任务。任务行会显示：

- 当前状态：待处理、运行中、取消中、已取消、完成或失败。
- 当前阶段：提取页面、判断文档类、结构规划、转换正文、生成标题或收尾。
- 进度、缓存命中次数、重试次数、失败原因和完成结果。

待处理任务可以立即取消；运行中的任务会在当前核心步骤结束后协作式取消。后台运行时不能继续添加新任务；队列空闲后可以继续添加或清空任务列表。

目标文件或项目目录已存在时，TeXBook 会按界面中的“覆盖前确认”设置处理。单个 `.tex` 输出只替换目标文件；目录化项目覆盖只清理当前项目目录，不清理批量输出父目录。应用会拒绝清理磁盘根、仓库根、源码包目录等高风险目标。

## 日志与排错

GUI 会在当前会话中记录诊断日志，包括任务创建、阶段进度、缓存命中、请求重试、失败、取消、写盘和日志导出事件。可在左侧“日志”区域选择输出路径，然后通过帮助菜单的“输出日志”导出 JSONL 日志文件。

CLI 的 `extract` 和 `batch` 命令支持 `--log-file PATH`。指定后命令启动时会创建或覆盖该文件，并在转换过程中持续追加 JSONL 诊断事件，例如：

```powershell
uv run texbook extract "input/lecture.pdf" -o "output/lecture.tex" --log-file "logs/lecture.jsonl"
uv run texbook batch input/ -o output/ --log-file "logs/batch.jsonl"
```

日志会脱敏 API Key、授权头、token 等敏感字段；LLM 响应解析失败时，只记录截断后的原始响应预览，便于排查模型输出格式问题。

## 界面偏好

- 支持亮色模式和暗色模式。
- 支持中文和 English 界面切换。
- 设置页可调整 GUI 字号。
- 应用会记忆最近输入目录、输出目录、缓存目录和界面偏好。

## 复杂内容

- 清晰表格会尽量转换为可编译的 `tabular`、`array` 或数学环境。
- 图片化表格、图片、图表、边栏、多栏和旁注等复杂内容暂以 TODO 注释和 notes 记录。
- 目录化项目会在 metadata 中保留复杂内容候选信息，待后续扩展。

## 开发与打包

### 从源码启动

开发环境可在 Windows PowerShell 中从仓库根目录启动：

```powershell
uv sync
uv run texbook-gui
```

也可以使用模块入口：

```powershell
uv run python -m texbook.gui
```

如果同一份仓库也曾在 WSL 中使用过，不要让 Windows PowerShell 与 WSL 共用同一个虚拟环境。Windows 端建议使用独立环境，例如：

```powershell
uv venv .venv-win
uv sync
uv run texbook-gui
```

从 PowerShell 启动源码版且需要临时设置模型接口时，可在启动前设置环境变量：

```powershell
$env:TEXBOOK_API_KEY = "your-api-key"
$env:TEXBOOK_BASE_URL = "https://your-api.example/v1"
uv run texbook-gui
```

### 打包发布

TeXBook 使用 PyInstaller 生成 Windows GUI 应用。开发环境中可运行：

```powershell
uv sync --group dev
uv run pyinstaller packaging/texbook-gui.spec
```

打包配置使用 `docs/icon.ico` 作为窗口和可执行文件图标，并把图标作为运行时资源放入发布包。发布产物输出到 PyInstaller 默认的 `dist/` 目录。

### 项目结构

```text
.
├─ pyproject.toml
├─ uv.lock
├─ README.md
├─ docs/
│  └─ icon.ico                    # 应用图标
├─ packaging/
│  └─ texbook-gui.spec            # Windows GUI 打包入口
├─ src/
│  └─ texbook/
│     ├─ gui/                     # 桌面应用入口、窗口、面板和任务执行
│     ├─ convert/                 # LaTeX 文档外壳与项目输出
│     ├─ extract/                 # PDF 文本、位置、字号与图像提取
│     └─ llm/                     # LLM 客户端、缓存、调度和 Prompt
└─ tests/                         # 单元测试与 GUI 回归测试
```

常用生成目录：

- `input/`：可放置待转换 PDF，默认不进入 Git。
- `output/`：可放置转换结果，默认不进入 Git。
- `build/`：开发构建临时目录；CLI 默认缓存仍使用该目录，GUI 默认缓存使用系统应用缓存目录。
- `dist/`：PyInstaller 打包输出目录，默认不进入 Git。

### 开发验证

```powershell
uv run pytest tests/test_gui_skeleton.py -q
uv run pytest
uv run ruff check
```

真实转换会调用模型服务。验证转换效果时，建议在界面中选择少量页面以节省时间和费用。

## 许可证

MIT
