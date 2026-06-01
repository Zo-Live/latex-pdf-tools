# TeXBook

TeXBook 是基于 WSLg 的 PDF 转 LaTeX 桌面应用。它面向数学讲义、教材和幻灯片，使用支持图片输入的 OpenAI-compatible 视觉模型识别 PDF 页面，并生成单个 `.tex` 文件或目录化 LaTeX 项目。

应用使用 PySide6/Qt6 构建，在 WSL 发行版中运行，通过 WSLg 显示桌面界面。

## 环境要求

- Windows 已启用 WSLg，且 WSL 发行版可以启动图形应用。
- Python 3.10+（开发验证环境使用 3.13）。
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖。
- 可访问的 OpenAI-compatible API，模型需要支持图片输入。

以下命令默认在 WSL 发行版终端中运行。

## 启用 WSLg

在 Windows PowerShell 中安装或更新 WSL：

```powershell
wsl --install
wsl --update
wsl --shutdown
```

重新打开 WSL 发行版后，进入仓库目录：

```bash
cd /path/to/your/workspace
```

如果 Qt 启动时报出 `xcb` 平台插件或光标库相关错误，可在 WSL 发行版中补齐常见运行库：

```bash
sudo apt update
sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0
```

## 构建与启动

安装运行依赖：

```bash
uv sync
```

启动桌面应用：

```bash
uv run texbook-gui
```

也可以使用模块入口：

```bash
uv run python -m texbook.gui
```

开发环境需要测试与打包工具时：

```bash
uv sync --group dev
```

使用 PyInstaller 构建桌面应用：

```bash
uv run pyinstaller packaging/texbook-gui.spec
```

打包配置使用 `docs/icon.ico` 作为应用图标，产物输出到 PyInstaller 默认的 `dist/` 目录。

## 模型配置

TeXBook 支持 OpenAI-compatible 接口。可以在界面中填写：

- 模型名
- Base URL
- API Key
- 额外转换要求

API Key 支持直接输入，也支持填写环境变量名。使用环境变量时，可在启动应用前设置：

```bash
export TEXBOOK_API_KEY="your-api-key"
```

如果接口地址不是 SDK 默认地址，可以在界面中填写 Base URL，或在启动前设置：

```bash
export TEXBOOK_BASE_URL="https://your-api.example/v1"
```

## 操作方法

1. 选择 PDF 输入：支持单个 PDF、多个 PDF 或 PDF 目录。
2. 选择输出目标：可以生成单个 `.tex` 文件，也可以生成目录化项目。
3. 配置转换参数：页面范围、文档类、结构规划、标题来源、模型、缓存和并发等。
4. 点击“添加任务”，把当前输入和参数加入任务队列。
5. 点击“开始转换”，后台任务会显示阶段、进度、缓存命中、重试和完成结果。
6. 需要中止时，可在任务行点击取消。
7. 目标文件或项目目录已存在时，按界面中的写盘策略确认覆盖或保留旧内容。

## 功能介绍

### 输入

- 单个 PDF：适合一次转换一本讲义或一个课件。
- 多个 PDF：一次选择多个文件，应用会按文件创建独立任务。
- 目录批量：选择目录并用 pattern 匹配 PDF，默认匹配 `*.pdf`。

### 输出

- 单个 `.tex`：每个 PDF 生成一个 LaTeX 文件。
- 目录化项目：每个 PDF 生成独立项目目录，包含入口文件、preamble 和章节文件。
- 覆盖前确认：目标已存在时可先弹窗确认；也可以按设置直接覆盖。
- 危险路径保护：应用会拒绝清理磁盘根、仓库根、源码包目录等高风险目标。

### 页面与文档结构

- 页面范围支持 `1,3-6` 这样的 1-based 页码表达。
- 文档类支持 `auto`、`article`、`book`、`beamer`、`ctexart`、`ctexbook`、`ctexbeamer`。
- 自动文档类会结合 PDF 页面图像、文本层、书签和标题线索判断输出外壳；如果你已经知道目标是讲义、教材还是幻灯片，建议直接手动指定对应文档类，`auto` 更适合拿不准时作为兜底，但它偶尔会判成与预期不同的文档类。
- 目录化项目支持结构规划：可使用 PDF 书签、本地标题线索或 LLM 规划章节。
- Beamer 输出支持标题页开关、原生 block 或 `tcolorbox` 风格强调块。
- CTeX 输出支持默认字体配置和本机字体配置。

### 模型与 Prompt

- GUI 固定使用内置 `math` Prompt 预设，面向数学讲义、教材或幻灯片。
- 额外要求会追加到当前 Prompt 后，用于一次性调整转换目标。
- 支持模型超时、最大 token、temperature、请求重试和退避参数。

### 缓存与并发

- 默认启用断点续传缓存，缓存目录为 `build/.texbook_cache/`。
- 相同 PDF、页码、模型、Prompt、图片参数和输出选项再次转换时会复用已完成结果。
- 可以在界面中清理当前参数匹配的缓存。
- 批量任务支持文件级 worker 并发。
- LLM 请求支持全局最大并发和最小请求间隔设置。

### 任务队列

- 每个 PDF 会成为独立任务。
- 任务行显示当前状态、阶段、进度、缓存命中、重试次数、失败原因和完成结果。
- 待处理任务可立即取消；运行中任务会在当前核心步骤收敛后取消。
- 队列完成后可继续添加新任务。

### 界面偏好

- 支持亮色模式和暗色模式。
- 支持中文和 English 界面切换。
- 设置页可调整 GUI 字号。
- 应用会记忆最近输入目录、输出目录、缓存目录和界面偏好。

### 复杂内容

- 清晰表格会尽量转换为可编译的 `tabular` 或 `array`。
- 图片化表格、图片、图表、边栏、多栏和旁注等复杂内容会以 TODO 注释和 notes 记录。
- 目录化项目会在 metadata 中保留复杂内容候选信息，便于后续扩展。

## 项目结构

```text
.
├─ .gitignore
├─ .python-version
├─ pyproject.toml
├─ uv.lock
├─ docs/
│  └─ icon.ico                    # 应用图标
├─ packaging/
│  └─ texbook-gui.spec            # PyInstaller 打包入口
├─ src/
│  └─ texbook/
│     ├─ gui/                     # WSLg 桌面应用入口、窗口、面板和任务执行
│     ├─ convert/                 # LaTeX 文档外壳与项目输出
│     ├─ extract/                 # PDF 文本、位置、字号与图像提取
│     └─ llm/                     # LLM 客户端、缓存、调度和 Prompt
└─ tests/                         # 单元测试
```

常用目录约定：

- `input/`、`docs/`：可放置待转换 PDF，默认不进入 Git。
- `output/`：可放置转换结果，默认不进入 Git。
- `build/`：默认缓存和构建临时目录，默认不进入 Git。
- `dist/`：PyInstaller 打包输出目录，默认不进入 Git。

## 开发验证

```bash
uv run pytest tests/test_gui_skeleton.py -q
uv run pytest
uv run ruff check
```

真实转换会调用模型服务。验证转换效果时，建议在界面中选择少量页面以节省耗时。

## 许可证

MIT
