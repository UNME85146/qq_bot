from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.model.llm_client import LlmClient


_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class InformationLocalizationError(RuntimeError):
    pass


async def localize_information_fields(
    model_client: LlmClient | None,
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    normalized = [
        {
            "id": str(item.get("id") or index),
            "title": _clean(item.get("title"), 160),
            "summary": _clean(item.get("summary"), 320),
        }
        for index, item in enumerate(items, start=1)
    ]
    if not any(_contains_non_chinese_text(item["title"] + item["summary"]) for item in normalized):
        return normalized

    localized = None
    if model_client is not None:
        localized = await _localize_with_model(model_client, normalized)
    if localized is None:
        raise InformationLocalizationError("information localization unavailable")
    result = [
        {
            "id": original["id"],
            "title": _clean(candidate.get("title"), 160),
            "summary": _clean(candidate.get("summary"), 320),
        }
        for original, candidate in zip(normalized, localized, strict=True)
    ]
    if any(
        _contains_non_chinese_text(item["title"] + item["summary"])
        for item in result
    ):
        raise InformationLocalizationError("localized text still contains non-Chinese letters")
    return result


async def _localize_with_model(
    model_client: LlmClient,
    items: list[dict[str, str]],
) -> list[dict[str, str]] | None:
    messages = [
        {
            "role": "system",
            "content": (
                "你是信息中文化器。把每个 title 和 summary 准确翻译或改写成简体中文，"
                "保留数字和事实，不添加新事实，不输出拉丁字母；专有名词使用常见中文译名。"
                "只返回 JSON 对象，格式为 {\"items\":[{\"id\":\"1\","
                "\"title\":\"...\",\"summary\":\"...\"}]}，顺序和 id 必须不变。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"items": items}, ensure_ascii=False),
        },
    ]
    try:
        generated = await model_client.generate(messages)
        raw = _CODE_FENCE.sub("", generated.text.strip())
        payload = json.loads(raw)
        candidates = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(candidates, list) or len(candidates) != len(items):
            return None
        expected_ids = [item["id"] for item in items]
        actual_ids = [str(item.get("id")) for item in candidates if isinstance(item, dict)]
        if actual_ids != expected_ids:
            return None
        return [
            {
                "id": str(item["id"]),
                "title": _clean(item.get("title"), 160),
                "summary": _clean(item.get("summary"), 320),
            }
            for item in candidates
        ]
    except Exception:
        return None


def _contains_non_chinese_text(value: str) -> bool:
    return any(character.isalpha() and not _is_cjk(character) for character in value)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _clean(value: Any, limit: int) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]
