from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.safety.safety_service import SafetyService


STYLE_SUMMARY = (
    "短句、直接、群聊即时反应，常用一两句回复；少用中文句号，常见无标点；"
    "会夹杂技术词、英文缩写、数字梗和轻微吐槽；整体像熟人群里随手接话，"
    "不像客服或说明书。"
)

TONE_RULES = [
    "短、快、直接",
    "可以轻微调侃",
    "少解释",
    "不主动长篇分析",
    "遇到技术问题可以简短判断",
    "不主动列表化",
]

TOPIC_BIASES = [
    "编程、AI、API、Codex、JSON",
    "工作和加班",
    "游戏和群活动",
    "设备体验",
    "群友梗",
    "日常吐槽",
]

REPLY_RULES = [
    "回复尽量控制在 1-2 句",
    "像 QQ 好友即时聊天，不要客服腔",
    "能短就短，优先自然接话",
    "不要主动编造真实身份或经历",
    "聊天记录画像只是风格参考，不是必须使用的词库或固定台词",
    "根据当前语境自然选用表达，不要为了模仿而硬塞口头禅、数字梗、技术词或示例句",
]

AVOID_RULES = [
    "不要自称真实本人",
    "不要编造 SOURCE_QQ 的真实学校、公司、住址、手机号、财务和身份信息",
    "不要复述完整聊天记录",
    "不要过度攻击或辱骂",
    "不要客服腔",
]

CANDIDATE_LEXICON = [
    "6",
    "牛逼",
    "舒服",
    "神了",
    "点不了一点",
    "肯定是调用apikey啊",
    "自己训练也太麻烦了",
    "那就不知道还有谁了",
    "羡慕",
    "爽局",
    "幽默",
]

ATTACHMENT_ONLY_PATTERN = re.compile(
    r"^(?:\[(?:图片|表情\d*|视频|转发消息|语音|文件|动画表情|JSON消息)\]\s*)+$"
)
CQ_ONLY_PATTERN = re.compile(r"^(?:\[CQ:(?:image|face|record|video|file)[^\]]*\]\s*)+$")
LONG_NUMBER_PATTERN = re.compile(r"\d{7,}")
URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a local fixed QQ chat style profile from exported JSONL chunks."
    )
    parser.add_argument("--input-dir", required=True, help="Directory containing *.jsonl chunks.")
    parser.add_argument("--source-user-id", required=True, help="QQ user id to extract style from.")
    parser.add_argument("--output", required=True, help="Output persona_profile.local.json path.")
    args = parser.parse_args()

    result = build_style_profile(
        input_dir=Path(args.input_dir),
        source_user_id=str(args.source_user_id),
        output_path=Path(args.output),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_style_profile(
    *,
    input_dir: Path,
    source_user_id: str,
    output_path: Path,
) -> dict[str, Any]:
    safety_service = SafetyService(source_user_id=source_user_id)
    stats: dict[str, Any] = {
        "inputDir": str(input_dir),
        "sourceUserId": source_user_id,
        "files": 0,
        "totalRecords": 0,
        "targetRecords": 0,
        "nonEmptyTargetTexts": 0,
        "validLowSensitiveTexts": 0,
        "skippedAttachments": 0,
        "skippedSensitive": 0,
        "skippedSystemOrRecalled": 0,
        "output": str(output_path),
    }
    valid_texts: list[str] = []

    files = sorted(input_dir.glob("*.jsonl"))
    stats["files"] = len(files)
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if not files:
        raise FileNotFoundError(f"No *.jsonl files found in: {input_dir}")

    for file_path in files:
        with file_path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                stats["totalRecords"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at {file_path}:{line_number}") from exc

                if str(_sender_uin(record)) != source_user_id:
                    continue
                stats["targetRecords"] += 1

                if _is_system_or_recalled(record):
                    stats["skippedSystemOrRecalled"] += 1
                    continue

                text = _normalize_text(_extract_text(record))
                if not text:
                    continue
                stats["nonEmptyTargetTexts"] += 1

                if _is_pure_attachment(text):
                    stats["skippedAttachments"] += 1
                    continue
                if not _is_low_sensitive_style_text(text, safety_service):
                    stats["skippedSensitive"] += 1
                    continue
                valid_texts.append(text)

    stats["validLowSensitiveTexts"] = len(valid_texts)
    profile = _make_profile(source_user_id=source_user_id, valid_texts=valid_texts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def _make_profile(*, source_user_id: str, valid_texts: list[str]) -> dict[str, Any]:
    lexicon = _select_lexicon(valid_texts)
    few_shot_examples = lexicon[:7]
    return {
        "sourceUserId": source_user_id,
        "identityDisclosure": f"我是基于 {source_user_id} 的聊天风格调出来的测试号，不是本人。",
        "styleSummary": STYLE_SUMMARY,
        "toneRules": TONE_RULES,
        "topicBiases": TOPIC_BIASES,
        "lexicon": lexicon,
        "replyRules": REPLY_RULES,
        "avoidRules": AVOID_RULES,
        "fewShotExamples": few_shot_examples,
        "updatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _select_lexicon(valid_texts: list[str]) -> list[str]:
    counts = Counter(valid_texts)
    selected: list[str] = []
    for phrase in CANDIDATE_LEXICON:
        if counts[phrase] > 0 or any(phrase in text for text in valid_texts):
            selected.append(phrase)

    for text, _count in counts.most_common():
        if len(selected) >= 12:
            break
        if text in selected:
            continue
        if _is_short_style_phrase(text):
            selected.append(text)

    return selected or CANDIDATE_LEXICON[:7]


def _is_short_style_phrase(text: str) -> bool:
    if not 1 <= len(text) <= 18:
        return False
    if text.startswith("[") and text.endswith("]"):
        return False
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return False
    if URL_PATTERN.search(text) or LONG_NUMBER_PATTERN.search(text):
        return False
    if any(marker in text for marker in ("@", "http", "身份证", "手机号", "住址", "银行卡")):
        return False
    return True


def _sender_uin(record: dict[str, Any]) -> str:
    sender = record.get("sender") or {}
    if isinstance(sender, dict):
        return str(sender.get("uin") or sender.get("uid") or sender.get("user_id") or "")
    return ""


def _is_system_or_recalled(record: dict[str, Any]) -> bool:
    if bool(record.get("system")) or bool(record.get("recalled")):
        return True
    record_type = str(record.get("type", "")).lower()
    return record_type in {"system", "recalled", "recall"}


def _extract_text(record: dict[str, Any]) -> str:
    content = record.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text
        element_texts = []
        for element in content.get("elements", []):
            if not isinstance(element, dict):
                continue
            data = element.get("data")
            if isinstance(data, dict):
                value = data.get("text") or data.get("content")
                if isinstance(value, str):
                    element_texts.append(value)
            elif isinstance(data, str):
                element_texts.append(data)
        if element_texts:
            return " ".join(element_texts)
    elif isinstance(content, str):
        return content

    for key in ("message_text", "text", "message", "raw_message"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_pure_attachment(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(ATTACHMENT_ONLY_PATTERN.fullmatch(compact) or CQ_ONLY_PATTERN.fullmatch(compact))


def _is_low_sensitive_style_text(text: str, safety_service: SafetyService) -> bool:
    if safety_service.contains_high_sensitivity(text):
        return False
    if not safety_service.can_store_long_term_memory(text):
        return False
    if URL_PATTERN.search(text):
        return False
    if LONG_NUMBER_PATTERN.search(text):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
