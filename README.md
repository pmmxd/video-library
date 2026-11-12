# VideoLibrary / 影库

**中文（默认）** | [English](#english)

一个本地优先的 Windows 视频资源管理器，基于 Python、PySide6、SQLite 和
QtMultimedia。软件只管理用户主动添加的资源库，视频文件和元数据默认保留在本机。

> 本仓库只包含源码、测试和必要图标。数据库、模型、CUDA 运行库、截图和构建产物
> 均不提交。

## 功能

- 多资源库、独立分类、标签、收藏、评分、播放进度和视频组。
- 支持普通视频目录、`movie.nfo` 影视库和摄影拍摄目录。
- 读取 NFO、海报、背景图、截图、字幕及视频技术元数据。
- 搜索、排序、元数据筛选、NFO 筛选、演员筛选和时间轴视图。
- 独立播放器、硬件解码、字幕挂载、快速定位、倍速播放和播放进度记忆。
- 可选本地 Faster-Whisper 字幕生成与 Ollama 中文字幕翻译。
- 可选后缀伪装、隔离资源库和界面访问密码。
- FFmpeg 裁剪、LosslessCut 外部编辑、导出、移动和回收站删除。
- 文件变化监听、手动增量刷新和 SQLite 索引。
<img width="3160" height="1854" alt="image" src="https://github.com/user-attachments/assets/c888d1ee-cced-4ffa-99b2-55d74a9e6f4b" />

## 系统要求

- Windows 10/11
- Python 3.11 或更新版本
- FFmpeg 与 ffprobe
- 可选：ExifTool、LosslessCut、NVIDIA CUDA

## 从源码运行

```powershell
git clone https://github.com/pmmxd/video-library.git
cd video-library
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

数据默认写入项目下的 `data/`，该目录已被 Git 忽略。也可以指定独立数据位置：

```powershell
$env:VIDEO_MANAGER_DATA_DIR = "$env:TEMP\video-library-data"
python main.py
```

### 外部工具

FFmpeg、ffprobe、ExifTool 和 LosslessCut 不包含在仓库中。可以加入系统 `PATH`，
也可以设置：

```powershell
$env:FFMPEG_PATH = "C:\tools\ffmpeg.exe"
$env:FFPROBE_PATH = "C:\tools\ffprobe.exe"
$env:EXIFTOOL_PATH = "C:\tools\exiftool.exe"
$env:LOSSLESSCUT_PATH = "C:\tools\LosslessCut.exe"
```

字幕生成仅在点击任务时加载本地模型，不影响普通启动。模型不提交到 GitHub，
需要时从以下页面下载：

- [Faster-Whisper Large V3](https://huggingface.co/Systran/faster-whisper-large-v3)
- [Faster-Whisper Medium](https://huggingface.co/Systran/faster-whisper-medium)
- [TranslateGemma 4B（Ollama）](https://ollama.com/library/translategemma)

本地 Faster-Whisper 模型目录结构：

```text
models/
  faster-whisper/
    faster-whisper-large-v3/
    faster-whisper-medium/
```

每个模型目录应包含 `config.json`、`model.bin`、`tokenizer.json`，以及
`vocabulary.txt` 或 `vocabulary.json`。字幕翻译通过本机 Ollama 使用
`translategemma:4b`，不会上传视频或字幕。

## 配合 LocalDock

桌面端本体不依赖 LocalDock。若需要管理相关的本地静态页面，可使用同一账号下的
公开项目 [LocalDock](https://github.com/pmmxd/localdock)：

1. 从 LocalDock 仓库获取并启动 `LocalDock.exe`。
2. 将需要浏览的静态站点目录添加到 LocalDock。
3. 在 LocalDock 管理页启动站点，通过它分配的本地地址访问。

## 数据与隐私

- 不包含遥测、云同步或账号系统。
- 视频、字幕、NFO、图片和 SQLite 数据库默认只保存在本机。
- `.gitignore` 已排除数据库、模型、运行库、日志、缓存、截图和环境变量文件。
- 后缀伪装和隔离密码属于界面访问控制，不替代 BitLocker/VeraCrypt 等磁盘加密。

## 测试

```powershell
python _run_tests.py
```

## 项目结构

```text
app/
  database.py       SQLite 数据层
  scanner.py        资源库扫描
  media_tools.py    媒体元数据与工具调用
  transcription.py  本地字幕任务
  translation.py    本地字幕翻译
  ui/               PySide6 界面
assets/             图标
tests/              自动化测试
main.py             程序入口
```

## 已知限制

- 主要在 Windows 上开发和测试。
- 本地字幕生成需要兼容的 NVIDIA CUDA 环境。
- 后缀伪装不会加密文件内容。
- 视频格式、字幕生成和外部工具能力取决于本机安装的编解码器与程序。

---

<a id="english"></a>

# English

A local-first Windows video library manager built with Python, PySide6, SQLite, and
QtMultimedia. It indexes only folders added by the user and keeps media files,
history, tags, and metadata on the local machine.

## Highlights

- Multiple libraries, categories, tags, ratings, favorites, playback history, and groups.
- Plain video folders, `movie.nfo` libraries, and shooting-project folders.
- NFO, artwork, subtitle, technical metadata, search, sorting, filters, and timeline views.
- Dedicated hardware-accelerated player with subtitle loading and resume support.
- Optional local Faster-Whisper transcription, Ollama translation, suffix masking,
  isolated libraries, and FFmpeg/LosslessCut workflows.

## Run

```powershell
git clone https://github.com/pmmxd/video-library.git
cd video-library
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Install FFmpeg, ffprobe, and optionally ExifTool/LosslessCut. Configure custom tool
locations with `FFMPEG_PATH`, `FFPROBE_PATH`, `EXIFTOOL_PATH`, and
`LOSSLESSCUT_PATH`.

Models are not included. Download only what you need:

- [Faster-Whisper Large V3](https://huggingface.co/Systran/faster-whisper-large-v3)
- [Faster-Whisper Medium](https://huggingface.co/Systran/faster-whisper-medium)
- [TranslateGemma 4B](https://ollama.com/library/translategemma)

For local static companion pages, see the public
[LocalDock project](https://github.com/pmmxd/localdock).

The application has no telemetry or cloud sync. Databases, models, CUDA runtimes,
logs, caches, and screenshots are excluded from this repository.
