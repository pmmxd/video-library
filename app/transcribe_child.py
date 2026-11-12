from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from .subtitles import SubtitleCue, format_srt


ProgressCallback = Callable[[int, str], None]


def _load_model(model_dir: Path):
    from faster_whisper import WhisperModel

    return WhisperModel(str(model_dir), device="cuda", compute_type="float16")


def transcribe_job(
    job: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> None:
    inputs = [Path(str(value)) for value in job["inputs"]]
    output_dir = Path(str(job["output_dir"]))
    model_dir = Path(str(job["model_root"])) / f"faster-whisper-{job['model']}"
    language = str(job.get("language") or "") or None
    if progress:
        progress(5, f"正在加载 {job['model']} 模型")
    model = _load_model(model_dir)
    if progress:
        progress(8, "字幕模型已加载 · CUDA float16")

    total = max(1, len(inputs))
    for index, source in enumerate(inputs):
        segments, info = model.transcribe(
            str(source),
            language=language,
            vad_filter=True,
        )
        duration = max(0.001, float(getattr(info, "duration", 0) or 0))
        cues: list[SubtitleCue] = []
        for segment in segments:
            text = str(segment.text or "").strip()
            if text:
                cues.append(
                    SubtitleCue(
                        max(0, int(segment.start * 1000)),
                        max(0, int(segment.end * 1000)),
                        text,
                    )
                )
            within = min(1.0, float(segment.end) / duration)
            if progress:
                progress(
                    min(95, 8 + int(((index + within) / total) * 87)),
                    f"正在转写 {source.name} · {int(within * 100)}%",
                )
        (output_dir / f"{source.stem}.srt").write_text(
            format_srt(cues), encoding="utf-8"
        )


def _event_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True)


def _emit(payload: dict[str, Any]) -> None:
    if sys.stdout is not None:
        # The frozen child can use the Windows locale for stdout even when the
        # parent reads UTF-8. ASCII JSON escapes make the IPC encoding unambiguous.
        print(_event_line(payload), flush=True)


def _flush_stdio() -> None:
    if sys.stdout is not None:
        sys.stdout.flush()
    if sys.stderr is not None:
        sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 1:
            raise ValueError("转写子进程需要且只需要一个任务文件")
        job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        transcribe_job(
            job,
            lambda percent, detail: _emit(
                {"type": "progress", "percent": percent, "detail": detail}
            ),
        )
        _flush_stdio()
        os._exit(0)
    except Exception as exc:  # noqa: BLE001 - 子进程必须把错误交还主进程
        try:
            _emit({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
            _flush_stdio()
        finally:
            os._exit(1)
