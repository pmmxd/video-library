from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from .constants import application_root
from .database import Database
from .media_tools import existing_video_path
from .subtitles import SubtitleCue, format_srt, parse_subtitle
from .applog import log
from .translation import TranslationCancelled, TranslationError, translate_cues


WHISPER_MODELS = ("large-v3", "medium")


def whisper_installation(root: Path | None = None) -> tuple[Path, Path]:
    base = root or application_root()
    executable = (
        base
        / "vendor"
        / "faster-whisper-xxl"
        / "Faster-Whisper-XXL"
        / "faster-whisper-xxl.exe"
    )
    model_root = base / "models" / "faster-whisper"
    return executable, model_root


def validate_whisper_installation(
    model: str,
    root: Path | None = None,
) -> list[str]:
    _executable, model_root = whisper_installation(root)
    model_dir = model_root / f"faster-whisper-{model}"
    missing: list[str] = []
    for name in ("config.json", "model.bin", "tokenizer.json"):
        if not (model_dir / name).is_file():
            missing.append(str(model_dir / name))
    if not (model_dir / "vocabulary.txt").is_file() and not (
        model_dir / "vocabulary.json"
    ).is_file():
        missing.append(str(model_dir / "vocabulary.txt | vocabulary.json"))
    return missing


def subtitle_target(video: dict[str, Any], translated: bool = False) -> Path:
    source = existing_video_path(video)
    if source is None:
        source = Path(str(video.get("path") or ""))
    if translated:
        return source.with_name(f"{source.stem}.zh-CN.srt")
    return source.with_suffix(".srt")


def subtitle_command(
    executable: Path,
    model_root: Path,
    model: str,
    inputs: list[Path],
    output_dir: Path,
    language: str,
) -> list[str]:
    args = [str(path) for path in inputs]
    args.extend(
        [
            "--model",
            model,
            "--model_dir",
            str(model_root),
            "--device",
            "cuda",
            "--compute_type",
            "float16",
            "--output_dir",
            str(output_dir),
            "--output_format",
            "srt",
            "--vad_filter",
            "True",
            "--word_timestamps",
            "True",
            "--print_progress",
            "--beep_off",
        ]
    )
    if language:
        args.extend(["--language", language])
    return args


def _result_text(output_dir: Path, source: Path) -> Path | None:
    exact = output_dir / f"{source.stem}.srt"
    if exact.is_file():
        return exact
    files = list(output_dir.glob("*.srt"))
    return files[0] if len(files) == 1 else None


def transcription_worker_command(job_path: Path, root: Path | None = None) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--transcribe-worker", str(job_path)]
    script = (root or application_root()) / "main.py"
    return [sys.executable, str(script), "--transcribe-worker", str(job_path)]


class SubtitleWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(int, int, int, bool, str)

    def __init__(
        self,
        db_path: str,
        videos: list[dict[str, Any]],
        language: str,
        model: str,
        overwrite: bool = False,
        translate: bool = False,
        translation_only: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.videos = videos
        self.language = language
        self.model = model
        self.overwrite = overwrite
        self.translate = translate
        self.translation_only = translation_only

    def run(self) -> None:
        generated = 0
        skipped = 0
        failed = 0
        cancelled = False
        messages: list[str] = []
        db = Database(self.db_path)
        log(
            f"字幕任务开始：{len(self.videos)} 个视频，model={self.model}，"
            f"translate={self.translate}，language={self.language}，"
            f"overwrite={self.overwrite}"
        )
        try:
            if self.translation_only:
                self._run_translation_only(db)
                return
            executable, model_root = whisper_installation()
            missing = validate_whisper_installation(self.model)
            if missing:
                log("字幕组件不完整：" + "；".join(missing[:3]))
                self.completed.emit(
                    0,
                    0,
                    len(self.videos),
                    False,
                    "离线字幕组件不完整：" + "；".join(missing[:3]),
                )
                return

            pending: list[tuple[dict[str, Any], Path, Path]] = []
            reusable: list[tuple[dict[str, Any], Path]] = []
            targets: set[str] = set()
            for video in self.videos:
                source = existing_video_path(video)
                if source is None:
                    failed += 1
                    messages.append(f"{video.get('file_name') or video.get('id')}：文件不存在")
                    continue
                target = source.with_suffix(".srt")
                translated_target = subtitle_target(video, translated=True)
                final_target = translated_target if self.translate else target
                if final_target.exists() and not self.overwrite:
                    skipped += 1
                    messages.append(f"{source.name}：已有字幕，已跳过")
                    continue
                if self.translate and target.exists() and not self.overwrite:
                    reusable.append((video, target))
                    continue
                target_key = str(final_target.resolve()).casefold()
                if target_key in targets:
                    failed += 1
                    messages.append(f"{source.name}：与同批次视频的输出字幕重名")
                    continue
                targets.add(target_key)
                pending.append((video, source, target))

            if pending and not self.isInterruptionRequested():
                with tempfile.TemporaryDirectory(prefix="video-library-asr-") as temp:
                    output_dir = Path(temp)
                    self.progress.emit(5, f"正在加载 {self.model} 模型")
                    success, detail, cancelled = self._run_process(
                        executable,
                        model_root,
                        [item[1] for item in pending],
                        output_dir,
                    )
                    if success:
                        for video, source, target in pending:
                            result = _result_text(output_dir, source)
                            if result is None or not parse_subtitle(result):
                                failed += 1
                                messages.append(f"{source.name}：没有生成有效字幕")
                                continue
                            try:
                                self._commit_result(db, video, result, target)
                                if not self.translate:
                                    generated += 1
                            except Exception as exc:
                                failed += 1
                                messages.append(f"{source.name}：{exc}")
                    elif not cancelled:
                        failed += len(pending)
                        messages.append(detail)
                        log(f"转写失败：{detail}")
                    if success:
                        reusable.extend(
                            (video, target) for video, _source, target in pending
                        )
            elif self.isInterruptionRequested():
                cancelled = True

            transcribed = sum(1 for _video, target in reusable if target.exists())
            if self.translate and reusable and not cancelled:
                for index, (video, subtitle_path) in enumerate(reusable):
                    if self.isInterruptionRequested():
                        cancelled = True
                        break
                    cues = parse_subtitle(subtitle_path)
                    if not cues:
                        failed += 1
                        messages.append(f"{subtitle_path.name}：没有有效字幕内容")
                        continue

                    def report(done: int, total: int) -> None:
                        position = (index + done / max(1, total)) / len(reusable)
                        self.progress.emit(
                            min(99, 95 + int(position * 4)),
                            f"正在翻译 {subtitle_path.stem} · {done}/{total}",
                        )

                    try:
                        translated_cues = translate_cues(
                            cues,
                            self.language,
                            on_progress=report,
                            cancelled=self.isInterruptionRequested,
                        )
                        self._commit_text(
                            db,
                            video,
                            format_srt(translated_cues),
                            subtitle_target(video, translated=True),
                        )
                        generated += 1
                    except TranslationCancelled:
                        cancelled = True
                        break
                    except (TranslationError, OSError) as exc:
                        failed += 1
                        messages.append(f"{subtitle_path.name}：翻译失败：{exc}")
                        log(
                            f"翻译失败：{subtitle_path.name}："
                            f"{type(exc).__name__}: {exc}"
                        )
            elif not self.translate:
                generated = transcribed

            if cancelled:
                detail = "字幕任务已取消，临时文件已清理"
            elif self.translate and (generated or skipped):
                detail = f"翻译 {generated}，跳过 {skipped}，失败 {failed}"
            elif generated or skipped:
                detail = f"生成 {generated}，跳过 {skipped}，失败 {failed}"
            elif failed:
                detail = messages[-1] if messages else "字幕生成失败"
            else:
                detail = "没有可处理的视频"
            log(f"字幕任务结束：{detail}")
            self.completed.emit(
                generated,
                skipped,
                failed,
                cancelled,
                detail,
            )
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜底，否则任务会静默卡住
            log(f"字幕任务异常：{type(exc).__name__}: {exc}")
            self.completed.emit(
                0,
                0,
                len(self.videos),
                False,
                f"字幕任务异常：{type(exc).__name__}: {exc}",
            )
        finally:
            db.close()

    def _run_translation_only(self, db: Database) -> None:
        video = self.videos[0]
        source = subtitle_target(video, translated=False)
        target = subtitle_target(video, translated=True)
        if not source.is_file():
            self.completed.emit(
                0,
                0,
                1,
                False,
                f"{source.name}：原文字幕不存在",
            )
            return
        if target.exists() and not self.overwrite:
            self.completed.emit(
                0,
                1,
                0,
                False,
                f"{target.name}：中文字幕已存在，已跳过",
            )
            return
        cues = parse_subtitle(source)
        if not cues:
            self.completed.emit(
                0,
                0,
                1,
                False,
                f"{source.name}：原文字幕为空或无法识别",
            )
            return

        def report(done: int, total: int) -> None:
            percent = min(99, 5 + int(done * 94 / max(1, total)))
            self.progress.emit(
                percent,
                f"正在翻译 {source.stem} · {done}/{total}",
            )

        try:
            translated = translate_cues(
                cues,
                self.language,
                on_progress=report,
                cancelled=self.isInterruptionRequested,
            )
            self._commit_text(
                db,
                video,
                format_srt(translated),
                target,
            )
        except TranslationCancelled:
            self.completed.emit(
                0,
                0,
                0,
                True,
                "字幕翻译已取消",
            )
            return
        except (TranslationError, OSError) as exc:
            log(f"翻译失败：{source.name}：{type(exc).__name__}: {exc}")
            self.completed.emit(
                0,
                0,
                1,
                False,
                f"{source.name}：翻译失败：{exc}",
            )
            return
        self.completed.emit(
            1,
            0,
            0,
            False,
            f"翻译完成：{target.name}",
        )

    def _run_process(
        self,
        executable: Path,
        model_root: Path,
        inputs: list[Path],
        output_dir: Path,
    ) -> tuple[bool, str, bool]:
        del executable
        job_path = output_dir / "transcription-job.json"
        job_path.write_text(
            json.dumps(
                {
                    "model": self.model,
                    "model_root": str(model_root),
                    "language": self.language,
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
        try:
            process = subprocess.Popen(
                transcription_worker_command(job_path),
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
        except Exception as exc:  # noqa: BLE001 - 缺依赖要回给用户
            return False, f"无法启动 GPU 转写子进程：{exc}", False
        messages: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if self.isInterruptionRequested():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return False, "已取消", True
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    messages.append(stripped)
                    continue
                if event.get("type") == "progress":
                    self.progress.emit(
                        int(event.get("percent") or 0),
                        str(event.get("detail") or "正在转写"),
                    )
                elif event.get("type") == "error":
                    messages.append(str(event.get("detail") or "GPU 转写失败"))
                else:
                    messages.append(stripped)
            exit_code = process.wait()
        except Exception as exc:  # noqa: BLE001 - 子进程通信失败不能让主程序退出
            if process.poll() is None:
                process.kill()
                process.wait()
            return False, f"GPU 转写子进程通信失败：{exc}", False
        if exit_code == 0:
            return True, "", False
        code = f"0x{exit_code & 0xFFFFFFFF:08X}"
        detail = messages[-1] if messages else "没有返回错误详情"
        return False, f"GPU 转写子进程异常退出（{code}）：{detail}", False

    def cancel(self) -> None:
        self.requestInterruption()

    @staticmethod
    def _commit_result(
        db: Database,
        video: dict[str, Any],
        generated: Path,
        target: Path,
    ) -> None:
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(generated, staging)
            os.replace(staging, target)
        finally:
            try:
                staging.unlink()
            except OSError:
                pass
        current = db.video(int(video["id"]))
        sidecars = list(current.get("sidecars") or []) if current else []
        if str(target) not in sidecars:
            sidecars.append(str(target))
        db.update_video(
            int(video["id"]),
            subtitle_path=str(target),
            sidecars_json=json.dumps(sidecars, ensure_ascii=False),
        )

    @staticmethod
    def _commit_text(
        db: Database,
        video: dict[str, Any],
        text: str,
        target: Path,
    ) -> None:
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            staging.write_text(text, encoding="utf-8")
            os.replace(staging, target)
        finally:
            try:
                staging.unlink()
            except OSError:
                pass
        current = db.video(int(video["id"]))
        sidecars = list(current.get("sidecars") or []) if current else []
        if str(target) not in sidecars:
            sidecars.append(str(target))
        db.update_video(
            int(video["id"]),
            subtitle_path=str(target),
            sidecars_json=json.dumps(sidecars, ensure_ascii=False),
        )
