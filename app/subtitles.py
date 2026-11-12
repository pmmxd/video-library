from __future__ import annotations

import bisect
import html
import re
from dataclasses import dataclass
from pathlib import Path

SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str


def find_default_subtitle(video_path: Path) -> Path | None:
    folder = video_path.parent
    stem = video_path.stem
    for suffix in (".srt", ".ass", ".ssa", ".vtt"):
        exact = folder / f"{stem}{suffix}"
        if exact.exists():
            return exact
    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUBTITLE_SUFFIXES
        and path.stem.casefold().startswith(stem.casefold())
    ]
    if not candidates:
        return None
    preferred = ("zh", "chs", "chi", "sc", "cht", "tc")
    candidates.sort(
        key=lambda path: (
            not any(code in path.name.casefold() for code in preferred),
            path.name.casefold(),
        )
    )
    return candidates[0]


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
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
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


def _parse_srt_vtt(text: str) -> list[SubtitleCue]:
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
                SubtitleCue(
                    _timestamp_ms(match.group("start")),
                    _timestamp_ms(match.group("end")),
                    body,
                )
            )
    return cues


def _parse_ass(text: str) -> list[SubtitleCue]:
    fields: list[str] = []
    cues = []
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
                "layer",
                "start",
                "end",
                "style",
                "name",
                "marginl",
                "marginr",
                "marginv",
                "effect",
                "text",
            ]
        parts = payload.split(",", len(fields) - 1)
        if len(parts) < len(fields):
            continue
        values = dict(zip(fields, parts))
        body = _clean_text(values.get("text", ""))
        if body:
            cues.append(
                SubtitleCue(
                    _timestamp_ms(values.get("start", "0:00:00.00")),
                    _timestamp_ms(values.get("end", "0:00:00.00")),
                    body,
                )
            )
    return cues


def parse_subtitle(path: Path) -> list[SubtitleCue]:
    try:
        text = _read_text(path)
    except OSError:
        return []
    if path.suffix.lower() in {".ass", ".ssa"}:
        cues = _parse_ass(text)
    else:
        cues = _parse_srt_vtt(text)
    cues = [cue for cue in cues if cue.end_ms > cue.start_ms]
    cues.sort(key=lambda cue: cue.start_ms)
    return cues


def format_srt(cues: list[SubtitleCue]) -> str:
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


def cue_at(cues: list[SubtitleCue], position_ms: int) -> SubtitleCue | None:
    if not cues:
        return None
    index = bisect.bisect_right(
        cues,
        position_ms,
        key=lambda cue: cue.start_ms,
    ) - 1
    if index < 0:
        return None
    cue = cues[index]
    return cue if cue.start_ms <= position_ms <= cue.end_ms else None
