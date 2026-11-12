from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import video_subtitle_tool as tool


class SubtitleToolTests(unittest.TestCase):
    def test_srt_round_trip(self) -> None:
        cues = [
            tool.Cue(0, 1250, "第一句"),
            tool.Cue(1250, 2500, "第二句"),
        ]
        self.assertEqual(tool._parse_srt_vtt(tool.format_srt(cues)), cues)

    def test_ass_parser(self) -> None:
        text = """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\i1}测试字幕{\\i0}
"""
        self.assertEqual(
            tool._parse_ass(text),
            [tool.Cue(1000, 2500, "测试字幕")],
        )

    def test_translation_number_parser(self) -> None:
        self.assertEqual(
            tool._parse_numbered("1. 你好\n2. 世界", 2),
            ["你好", "世界"],
        )
        self.assertIsNone(tool._parse_numbered("1. 只有一句", 2))

    def test_translate_cues_preserves_timing(self) -> None:
        cues = [tool.Cue(0, 1000, "こんにちは"), tool.Cue(1000, 2000, "世界")]
        with (
            patch.object(tool, "ensure_translation_model"),
            patch.object(tool, "_generate", return_value="1. 你好\n2. 世界"),
        ):
            translated = tool.translate_cues(cues, "ja")
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text) for cue in translated],
            [(0, 1000, "你好"), (1000, 2000, "世界")],
        )

    def test_translation_timeout_is_reported_separately(self) -> None:
        with patch(
            "video_subtitle_tool.urllib.request.urlopen",
            side_effect=URLError(TimeoutError("timed out")),
        ):
            with self.assertRaises(tool.SubtitleToolError) as caught:
                tool._request_json("/api/generate", {"prompt": "x"})
        self.assertIn("超时", str(caught.exception))
        self.assertNotIn("不可用", str(caught.exception))

    def test_target_names(self) -> None:
        source = Path("clip.mov")
        self.assertEqual(tool._target_path(source, None, False).name, "clip.srt")
        self.assertEqual(
            tool._target_path(source, None, True).name,
            "clip.zh-CN.srt",
        )

    def test_self_test(self) -> None:
        self.assertTrue(tool.self_test()["ok"])


if __name__ == "__main__":
    unittest.main()
