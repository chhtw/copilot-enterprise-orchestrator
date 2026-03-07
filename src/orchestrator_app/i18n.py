from __future__ import annotations

import json
from typing import Any

DEFAULT_LANGUAGE = "zh-TW"
ENGLISH = "en-US"
TRADITIONAL_CHINESE = "zh-TW"

_ALIASES = {
    "zh": TRADITIONAL_CHINESE,
    "zh-tw": TRADITIONAL_CHINESE,
    "zh_tw": TRADITIONAL_CHINESE,
    "traditional chinese": TRADITIONAL_CHINESE,
    "繁中": TRADITIONAL_CHINESE,
    "繁體中文": TRADITIONAL_CHINESE,
    "中文": TRADITIONAL_CHINESE,
    "en": ENGLISH,
    "en-us": ENGLISH,
    "en_us": ENGLISH,
    "english": ENGLISH,
}


def normalize_language(value: Any) -> str:
    if not value:
        return DEFAULT_LANGUAGE
    text = str(value).strip()
    if not text:
        return DEFAULT_LANGUAGE
    return _ALIASES.get(text.lower(), DEFAULT_LANGUAGE)


def is_english(language: str | None) -> bool:
    return normalize_language(language) == ENGLISH


def tr(language: str | None, zh_tw: str, en_us: str) -> str:
    return en_us if is_english(language) else zh_tw


def language_display_name(language: str | None) -> str:
    return tr(language, "繁體中文", "English")


def human_language_instruction(language: str | None) -> str:
    lang = normalize_language(language)
    if lang == ENGLISH:
        return (
            "Use English for all human-readable text, questions, hints, summaries, and markdown content. "
            "Do not translate JSON keys, schema field names, code identifiers, filenames, or machine-readable values."
        )
    return (
        "所有人類可讀文字、提問、提示、摘要與 Markdown 說明請使用繁體中文。"
        "不要翻譯 JSON keys、schema 欄位名稱、程式碼識別字、檔名或機器可讀值。"
    )


def extract_language_from_spec_json(spec_json: str) -> str:
    try:
        data = json.loads(spec_json)
    except (json.JSONDecodeError, TypeError):
        return DEFAULT_LANGUAGE
    if not isinstance(data, dict):
        return DEFAULT_LANGUAGE
    return normalize_language(
        data.get("preferred_language") or data.get("language") or data.get("locale")
    )