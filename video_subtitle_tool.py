from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


MODEL_NAMES = ("large-v3", "medium")
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".dll", ".dat",
}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
TRANSLATION_MODEL = "translategemma:4b"
OLLAMA_API = "http://127.0.0.1:11434"
TRANSLATION_BATCH_SIZE = 4
SOURCE_ROOT = Path(__file__).resolve().parent

LANGUAGE_NAMES = {
    "ja": "Japanese",
    "en": "English",
    "ko": "Korean",
    "zh": "Chinese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "it": "Italian",
    "ar": "Arabic",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "tl": "Filipino",
    "tr": "Turkish",
    "pl": "Polish",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "cs": "Czech",
    "sk": "Slovak",
    "sl": "Slovenian",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "uk": "Ukrainian",
    "el": "Greek",
    "he": "Hebrew",
    "fa": "Persian",
    "ur": "Urdu",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "kn": "Kannada",
    "mr": "Marathi",
    "gu": "Gujarati",
    "sw": "Swahili",
    "af": "Afrikaans",
    "ca": "Catalan",
    "hr": "Croatian",
    "sr": "Serbian",
    "et": "Estonian",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "is": "Icelandic",
    "cy": "Welsh",
}

ProgressCallback = Callable[[int, str], None]


class SubtitleToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp936", "cp932", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    text = re.sub(r"\{[^}]*\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n")
    lines = [line.strip() for line in html.unescape(text).splitlines() if line.strip()]
    return "\n".join(lines)


def _timestamp_ms(value: str) -> int:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        return 0
    try:
        return int(
            (float(hours) * 3600 + float(minutes) * 60 + float(seconds)) * 1000
        )
    except ValueError:
        return 0


def _parse_srt_vtt(text: str) -> list[Cue]:
    pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})[^\n]*\n"
        r"(?P<body>.*?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    cues = []
    for match in pattern.finditer(text):
        body = _clean_text(match.group("body"))
        if body:
            cues.append(
                Cue(
                    _timestamp_ms(match.group("start")),
                    _timestamp_ms(match.group("end")),
                    body,
                )
            )
    return cues


def _parse_ass(text: str) -> list[Cue]:
    fields: list[str] = []
    cues: list[Cue] = []
    in_events = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.casefold() == "[events]":
            in_events = True
            continue
        if not in_events:
            continue
        if stripped.casefold().startswith("format:"):
            fields = [part.strip().casefold() for part in stripped[7:].split(",")]
            continue
        if not stripped.casefold().startswith("dialogue:"):
            continue
        payload = stripped.split(":", 1)[1].lstrip()
        if not fields:
            fields = [
                "layer", "start", "end", "style", "name", "marginl",
                "marginr", "marginv", "effect", "text",
            ]
        parts = payload.split(",", len(fields) - 1)
        if len(parts) < len(fields):
            continue
        values = dict(zip(fields, parts))
        body = _clean_text(values.get("text", ""))
        if body:
            cues.append(
                Cue(
                    _timestamp_ms(values.get("start", "0:00:00.00")),
                    _timestamp_ms(values.get("end", "0:00:00.00")),
                    body,
                )
            )
    return cues


def parse_subtitle(path: Path) -> list[Cue]:
    text = _read_text(path)
    cues = (
        _parse_ass(text)
        if path.suffix.lower() in {".ass", ".ssa"}
        else _parse_srt_vtt(text)
    )
    return sorted(
        (cue for cue in cues if cue.end_ms > cue.start_ms),
        key=lambda cue: cue.start_ms,
    )


def format_srt(cues: Iterable[Cue]) -> str:
    def timestamp(value_ms: int) -> str:
        hours, remainder = divmod(max(0, value_ms), 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

    blocks = [
        f"{index}\n{timestamp(cue.start_ms)} --> {timestamp(cue.end_ms)}\n{cue.text}"
        for index, cue in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _is_tool_root(path: Path) -> bool:
    return (
        (path / "VideoLibrary.exe").is_file() or (path / "main.py").is_file()
    ) and (path / "models" / "faster-whisper").is_dir()


def resolve_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not _is_tool_root(path):
            raise SubtitleToolError(f"不是有效的 VideoLibrary 目录：{path}")
        return path
    candidates = [
        os.environ.get("VIDEO_LIBRARY_ROOT"),
        SOURCE_ROOT if (SOURCE_ROOT / "VideoLibrary.exe").is_file() else None,
        SOURCE_ROOT,
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if _is_tool_root(path):
            return path
    raise SubtitleToolError(
        "没有找到 VideoLibrary。请设置 VIDEO_LIBRARY_ROOT 或传入 --root。"
    )


def model_path(root: Path, model: str) -> Path:
    if model not in MODEL_NAMES:
        raise SubtitleToolError(f"未知模型：{model}")
    return root / "models" / "faster-whisper" / f"faster-whisper-{model}"


def _missing_model_files(root: Path, model: str) -> list[str]:
    directory = model_path(root, model)
    missing = [
        str(directory / name)
        for name in ("config.json", "model.bin", "tokenizer.json")
        if not (directory / name).is_file()
    ]
    if not (directory / "vocabulary.txt").is_file() and not (
        directory / "vocabulary.json"
    ).is_file():
        missing.append(str(directory / "vocabulary.txt | vocabulary.json"))
    return missing


def _expand_paths(
    values: Iterable[str | Path],
    extensions: set[str],
) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if path.is_dir():
            for candidate in path.rglob("*"):
                relative = candidate.relative_to(path)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if candidate.is_file() and candidate.suffix.lower() in extensions:
                    paths.append(candidate.resolve())
        elif path.is_file():
            if path.suffix.lower() not in extensions:
                raise SubtitleToolError(f"不支持的文件类型：{path}")
            paths.append(path.resolve())
        else:
            raise FileNotFoundError(path)
    unique = {str(path).casefold(): path for path in paths}
    result = sorted(unique.values(), key=lambda item: str(item).casefold())
    if not result:
        raise SubtitleToolError("没有找到可处理的文件")
    return result


def _target_path(
    source: Path,
    output_dir: Path | None,
    translated: bool,
) -> Path:
    name = f"{source.stem}.zh-CN.srt" if translated else f"{source.stem}.srt"
    return (output_dir / name) if output_dir else source.with_name(name)


def _write_text_atomic(target: Path, text: str, overwrite: bool) -> str:
    if target.exists() and not overwrite:
        return "skipped"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return "completed"


def _worker_command(root: Path, job_path: Path) -> list[str]:
    executable = root / "VideoLibrary.exe"
    if executable.is_file():
        return [str(executable), "--transcribe-worker", str(job_path)]
    script = root / "main.py"
    if script.is_file():
        return [sys.executable, str(script), "--transcribe-worker", str(job_path)]
    raise SubtitleToolError(f"没有找到转写程序：{root}")


def _run_worker(
    root: Path,
    model: str,
    language: str,
    inputs: list[Path],
    progress: ProgressCallback | None,
) -> dict[Path, str]:
    stems = [path.stem.casefold() for path in inputs]
    if len(stems) != len(set(stems)):
        raise SubtitleToolError("同一批次中有重名视频，请分开处理以避免字幕覆盖")
    with tempfile.TemporaryDirectory(prefix="video-library-subtitle-") as temp:
        output_dir = Path(temp)
        job_path = output_dir / "job.json"
        job_path.write_text(
            json.dumps(
                {
                    "model": model,
                    "model_root": str(root / "models" / "faster-whisper"),
                    "language": language,
                    "inputs": [str(path) for path in inputs],
                    "output_dir": str(output_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["HF_HUB_OFFLINE"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            _worker_command(root, job_path),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        messages: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                messages.append(stripped)
                continue
            if event.get("type") == "progress":
                if progress:
                    progress(int(event.get("percent") or 0), str(event.get("detail") or ""))
            elif event.get("type") == "error":
                messages.append(str(event.get("detail") or "转写失败"))
        exit_code = process.wait()
        if exit_code:
            detail = messages[-1] if messages else "没有返回错误详情"
            raise SubtitleToolError(f"转写进程异常退出（{exit_code}）：{detail}")
        results = {}
        for source in inputs:
            generated = output_dir / f"{source.stem}.srt"
            if not generated.is_file():
                candidates = [
                    path
                    for path in output_dir.glob("*.srt")
                    if source.stem.casefold() in path.stem.casefold()
                ]
                if len(candidates) == 1:
                    generated = candidates[0]
            if not generated.is_file() or generated.stat().st_size == 0:
                raise SubtitleToolError(f"转写没有生成有效字幕：{source.name}")
            results[source] = generated.read_text(encoding="utf-8-sig")
        return results


def _request_json(path: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{OLLAMA_API}{path}",
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SubtitleToolError(
            f"本地翻译服务返回 HTTP {exc.code}，请检查 Ollama 日志"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SubtitleToolError(
                "本地翻译请求超时；Ollama 正在运行，但模型未能及时返回"
            ) from exc
        raise SubtitleToolError(
            f"本地翻译服务连接失败：{exc.reason}。请确认 Ollama 正在运行"
        ) from exc
    except TimeoutError as exc:
        raise SubtitleToolError(
            "本地翻译请求超时；Ollama 正在运行，但模型未能及时返回"
        ) from exc
    except OSError as exc:
        raise SubtitleToolError(f"本地翻译服务不可用：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise SubtitleToolError("本地翻译服务返回了无法解析的数据") from exc


def ensure_translation_model() -> None:
    result = _request_json("/api/tags", timeout=5)
    names = {str(item.get("name") or "") for item in result.get("models", [])}
    if TRANSLATION_MODEL not in names:
        raise SubtitleToolError(f"未找到本地翻译模型 {TRANSLATION_MODEL}")


def _prompt(texts: list[str], source_language: str, numbered: bool = True) -> str:
    source = LANGUAGE_NAMES.get(source_language, "the source language")
    direction = (
        "Detect the source language automatically"
        if not source_language
        else f"Translate from {source}"
    )
    if numbered:
        body = "\n".join(f"{index}. {text}" for index, text in enumerate(texts, 1))
        return (
            f"{direction} and translate each numbered subtitle line into "
            "Simplified Chinese. Keep the same numbering and one line per input. "
            "Return only the numbered translations, with no explanation.\n\n"
            + body
        )
    return (
        f"{direction} and translate the following subtitle into Simplified Chinese. "
        "Return only the translation, with no explanation and no quotes.\n\n"
        + texts[0]
    )


def _generate(prompt: str) -> str:
    result = _request_json(
        "/api/generate",
        {
            "model": TRANSLATION_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        },
    )
    text = str(result.get("response") or "").strip()
    if not text:
        raise SubtitleToolError("翻译模型没有返回内容")
    return text


def _parse_numbered(text: str, count: int) -> list[str] | None:
    values: dict[int, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\s*[.)、:]\s*(.*?)\s*$", line)
        if match:
            index = int(match.group(1))
            if 1 <= index <= count and match.group(2):
                values[index] = match.group(2)
    if len(values) != count:
        return None
    return [values[index] for index in range(1, count + 1)]


def translate_cues(
    cues: list[Cue],
    source_language: str = "",
    progress: Callable[[int, int], None] | None = None,
) -> list[Cue]:
    ensure_translation_model()
    translated: list[str] = []
    for offset in range(0, len(cues), TRANSLATION_BATCH_SIZE):
        batch = cues[offset : offset + TRANSLATION_BATCH_SIZE]
        texts = [cue.text.replace("\n", " ") for cue in batch]
        output = _generate(_prompt(texts, source_language))
        result = _parse_numbered(output, len(texts))
        if result is None:
            result = [_generate(_prompt([text], source_language, numbered=False)) for text in texts]
        translated.extend(result)
        if progress:
            progress(min(offset + len(batch), len(cues)), len(cues))
    return [
        Cue(cue.start_ms, cue.end_ms, text.strip() or cue.text)
        for cue, text in zip(cues, translated, strict=True)
    ]


def generate(
    paths: Iterable[str | Path],
    *,
    language: str = "",
    model: str = "large-v3",
    translate: bool = False,
    overwrite: bool = False,
    output_dir: str | Path | None = None,
    root: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    tool_root = resolve_root(root)
    missing = _missing_model_files(tool_root, model)
    if missing:
        raise SubtitleToolError("模型文件不完整：" + "；".join(missing))
    videos = _expand_paths(paths, VIDEO_EXTENSIONS)
    destination = Path(output_dir).resolve() if output_dir else None
    items: dict[Path, dict] = {}
    pending: list[tuple[Path, Path]] = []
    for source in videos:
        subtitle = _target_path(source, destination, False)
        translated = _target_path(source, destination, True)
        items[source] = {
            "input": str(source),
            "subtitle": str(subtitle),
            "translation": str(translated) if translate else "",
            "status": "pending",
            "error": "",
        }
        if translate and translated.exists() and not overwrite:
            items[source]["status"] = "skipped"
            items[source]["error"] = "中文翻译已存在"
        elif subtitle.exists() and not overwrite:
            items[source]["subtitle_status"] = "reused"
        else:
            pending.append((source, subtitle))
    if pending:
        generated = _run_worker(
            tool_root,
            model,
            language,
            [source for source, _target in pending],
            progress,
        )
        for source, target in pending:
            status = _write_text_atomic(target, generated[source], overwrite)
            items[source]["subtitle_status"] = status
    if translate:
        for index, source in enumerate(videos, 1):
            item = items[source]
            if item["status"] == "skipped":
                continue
            subtitle = Path(item["subtitle"])
            target = Path(item["translation"])
            try:
                cues = parse_subtitle(subtitle)
                if not cues:
                    raise SubtitleToolError("原始字幕为空或格式无法识别")
                translated_cues = translate_cues(
                    cues,
                    language,
                    progress=lambda done, total: (
                        progress(
                            min(98, int(((index - 1 + done / max(1, total)) / len(videos)) * 90) + 8),
                            f"正在翻译 {source.name} · {done}/{total}",
                        )
                        if progress
                        else None
                    ),
                )
                item["status"] = _write_text_atomic(
                    target,
                    format_srt(translated_cues),
                    overwrite,
                )
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = f"{type(exc).__name__}: {exc}"
    else:
        for source in videos:
            item = items[source]
            if item["status"] == "pending":
                item["status"] = (
                    "completed"
                    if item.get("subtitle_status") == "completed"
                    else "skipped"
                )
    result_items = [items[source] for source in videos]
    return {
        "ok": all(item["status"] != "failed" for item in result_items),
        "command": "generate",
        "root": str(tool_root),
        "model": model,
        "items": result_items,
        "completed": sum(item["status"] == "completed" for item in result_items),
        "skipped": sum(item["status"] == "skipped" for item in result_items),
        "failed": sum(item["status"] == "failed" for item in result_items),
    }


def translate(
    paths: Iterable[str | Path],
    *,
    language: str = "",
    overwrite: bool = False,
    output_dir: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    subtitles = _expand_paths(paths, SUBTITLE_EXTENSIONS)
    destination = Path(output_dir).resolve() if output_dir else None
    items = []
    for index, source in enumerate(subtitles, 1):
        target = _target_path(source, destination, True)
        item = {
            "input": str(source),
            "translation": str(target),
            "status": "pending",
            "error": "",
        }
        try:
            if target.exists() and not overwrite:
                item["status"] = "skipped"
                item["error"] = "中文翻译已存在"
            else:
                cues = parse_subtitle(source)
                if not cues:
                    raise SubtitleToolError("字幕为空或格式无法识别")
                translated = translate_cues(
                    cues,
                    language,
                    progress=lambda done, total: (
                        progress(
                            min(98, int(((index - 1 + done / max(1, total)) / len(subtitles)) * 90) + 8),
                            f"正在翻译 {source.name} · {done}/{total}",
                        )
                        if progress
                        else None
                    ),
                )
                item["status"] = _write_text_atomic(
                    target,
                    format_srt(translated),
                    overwrite,
                )
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = f"{type(exc).__name__}: {exc}"
        items.append(item)
    return {
        "ok": all(item["status"] != "failed" for item in items),
        "command": "translate",
        "items": items,
        "completed": sum(item["status"] == "completed" for item in items),
        "skipped": sum(item["status"] == "skipped" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
    }


def doctor(model: str = "large-v3", root: str | Path | None = None) -> dict:
    tool_root = resolve_root(root)
    missing = _missing_model_files(tool_root, model)
    translation = {"ok": False, "model": TRANSLATION_MODEL, "error": ""}
    try:
        ensure_translation_model()
        translation.update({"ok": True, "error": ""})
    except Exception as exc:
        translation["error"] = str(exc)
    return {
        "ok": not missing and translation["ok"],
        "generation_ok": not missing,
        "root": str(tool_root),
        "worker": _worker_command(tool_root, Path("job.json"))[0],
        "model": model,
        "model_path": str(model_path(tool_root, model)),
        "missing_model_files": missing,
        "translation": translation,
    }


def self_test() -> dict:
    cues = [
        Cue(0, 1200, "こんにちは"),
        Cue(1200, 2400, "テスト"),
    ]
    rebuilt = _parse_srt_vtt(format_srt(cues))
    return {
        "ok": rebuilt == cues,
        "cues": len(rebuilt),
        "translation_model": TRANSLATION_MODEL,
    }


def _print_progress(percent: int, detail: str, json_mode: bool) -> None:
    if json_mode:
        print(
            json.dumps(
                {"type": "progress", "percent": percent, "detail": detail},
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
    else:
        print(f"[{percent:3d}%] {detail}", file=sys.stderr, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VideoLibrary 字幕生成与翻译外部工具",
    )
    parser.add_argument("--root", help="VideoLibrary 目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    generate_parser = commands.add_parser("generate", help="从视频生成字幕")
    generate_parser.add_argument("paths", nargs="+", help="视频文件或目录")
    generate_parser.add_argument("--language", default="", help="源语言代码，空为自动")
    generate_parser.add_argument("--model", choices=MODEL_NAMES, default="large-v3")
    generate_parser.add_argument("--translate", action="store_true", help="同时翻译中文")
    generate_parser.add_argument("--overwrite", action="store_true")
    generate_parser.add_argument("--output-dir", type=Path)

    translate_parser = commands.add_parser("translate", help="翻译已有字幕")
    translate_parser.add_argument("paths", nargs="+", help="字幕文件或目录")
    translate_parser.add_argument("--language", default="", help="源语言代码，空为自动")
    translate_parser.add_argument("--overwrite", action="store_true")
    translate_parser.add_argument("--output-dir", type=Path)

    doctor_parser = commands.add_parser("doctor", help="检查运行环境")
    doctor_parser.add_argument("--model", choices=MODEL_NAMES, default="large-v3")
    commands.add_parser("self-test", help="运行内置解析自检")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate(
                args.paths,
                language=args.language,
                model=args.model,
                translate=args.translate,
                overwrite=args.overwrite,
                output_dir=args.output_dir,
                root=args.root,
                progress=lambda percent, detail: _print_progress(percent, detail, args.json),
            )
        elif args.command == "translate":
            result = translate(
                args.paths,
                language=args.language,
                overwrite=args.overwrite,
                output_dir=args.output_dir,
                progress=lambda percent, detail: _print_progress(percent, detail, args.json),
            )
        elif args.command == "doctor":
            result = doctor(args.model, args.root)
        else:
            result = self_test()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        result = {
            "ok": False,
            "command": args.command,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
