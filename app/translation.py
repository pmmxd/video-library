from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

from .subtitles import SubtitleCue


TRANSLATION_MODEL = "translategemma:4b"
OLLAMA_API = "http://127.0.0.1:11434"
BATCH_SIZE = 4

SOURCE_LANGUAGES = (
    ("", "自动识别"),
    ("ja", "日语"),
    ("en", "英语"),
    ("ko", "韩语"),
    ("zh", "中文"),
    ("fr", "法语"),
    ("de", "德语"),
    ("es", "西班牙语"),
    ("pt", "葡萄牙语"),
    ("ru", "俄语"),
    ("it", "意大利语"),
    ("ar", "阿拉伯语"),
    ("hi", "印地语"),
    ("th", "泰语"),
    ("vi", "越南语"),
    ("id", "印度尼西亚语"),
    ("ms", "马来语"),
    ("tl", "菲律宾语"),
    ("tr", "土耳其语"),
    ("pl", "波兰语"),
    ("nl", "荷兰语"),
    ("sv", "瑞典语"),
    ("da", "丹麦语"),
    ("fi", "芬兰语"),
    ("no", "挪威语"),
    ("cs", "捷克语"),
    ("sk", "斯洛伐克语"),
    ("sl", "斯洛文尼亚语"),
    ("hu", "匈牙利语"),
    ("ro", "罗马尼亚语"),
    ("bg", "保加利亚语"),
    ("uk", "乌克兰语"),
    ("el", "希腊语"),
    ("he", "希伯来语"),
    ("fa", "波斯语"),
    ("ur", "乌尔都语"),
    ("bn", "孟加拉语"),
    ("ta", "泰米尔语"),
    ("te", "泰卢固语"),
    ("ml", "马拉雅拉姆语"),
    ("kn", "卡纳达语"),
    ("mr", "马拉地语"),
    ("gu", "古吉拉特语"),
    ("sw", "斯瓦希里语"),
    ("af", "南非荷兰语"),
    ("ca", "加泰罗尼亚语"),
    ("hr", "克罗地亚语"),
    ("sr", "塞尔维亚语"),
    ("et", "爱沙尼亚语"),
    ("lv", "拉脱维亚语"),
    ("lt", "立陶宛语"),
    ("is", "冰岛语"),
    ("cy", "威尔士语"),
)

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


class TranslationError(RuntimeError):
    pass


class TranslationCancelled(TranslationError):
    pass


def _request_json(path: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        OLLAMA_API + path,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TranslationError(
            f"本地翻译服务返回 HTTP {exc.code}，请检查 Ollama 日志"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TranslationError(
                "本地翻译请求超时；Ollama 正在运行，但模型未能及时返回"
            ) from exc
        raise TranslationError(
            f"本地翻译服务连接失败：{exc.reason}。请确认 Ollama 正在运行"
        ) from exc
    except TimeoutError as exc:
        raise TranslationError(
            "本地翻译请求超时；Ollama 正在运行，但模型未能及时返回"
        ) from exc
    except OSError as exc:
        raise TranslationError(f"本地翻译服务不可用：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationError("本地翻译服务返回了无法解析的数据") from exc


def ensure_translation_model() -> None:
    result = _request_json("/api/tags", timeout=5)
    names = {str(item.get("name") or "") for item in result.get("models", [])}
    if TRANSLATION_MODEL not in names:
        raise TranslationError(f"未找到本地翻译模型 {TRANSLATION_MODEL}")


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
        raise TranslationError("翻译模型没有返回内容")
    return text


def _translate_batch(texts: list[str], source_language: str) -> list[str] | None:
    return _parse_numbered(_generate(_prompt(texts, source_language)), len(texts))


def translate_cues(
    cues: list[SubtitleCue],
    source_language: str,
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[SubtitleCue]:
    ensure_translation_model()
    translated: list[str] = []
    for offset in range(0, len(cues), BATCH_SIZE):
        if cancelled and cancelled():
            raise TranslationCancelled("翻译已取消")
        batch = cues[offset : offset + BATCH_SIZE]
        texts = [cue.text.replace("\n", " ") for cue in batch]
        result = _translate_batch(texts, source_language)
        if result is None:
            # 逐条兜底很慢（每条约一次完整推理），期间要能取消并持续回报进度
            result = []
            for text in texts:
                if cancelled and cancelled():
                    raise TranslationCancelled("翻译已取消")
                result.append(
                    _generate(_prompt([text], source_language, numbered=False))
                )
                if on_progress:
                    done = min(offset + len(result), len(cues))
                    on_progress(done, len(cues))
        translated.extend(result)
        if on_progress:
            on_progress(min(offset + len(batch), len(cues)), len(cues))
    return [
        SubtitleCue(cue.start_ms, cue.end_ms, text.strip() or cue.text)
        for cue, text in zip(cues, translated, strict=True)
    ]
