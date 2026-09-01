from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


_METRIC_FIELDS = {
    "validTextCount",
    "averageTextLength",
    "shortTextRatio",
    "mediumTextRatio",
    "questionRatio",
    "punctuationRatio",
    "mediaRatio",
    "stickerRatio",
    "continuationReplies",
    "threadBursts",
    "atMentions",
    "replyMarkers",
    "stickerIntentCount",
    "repeatedShortExpressionCount",
}
_MAX_METRIC_COUNT = 1_000_000_000


def _read_int(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if type(value) is not int:
        raise ValueError(f"History-derived character metric {key} must be an integer")
    return value


def _read_float(raw: dict[str, Any], key: str) -> float:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"History-derived character metric {key} must be a number")
    return float(value)


@dataclass(frozen=True)
class HistoryCharacterMetrics:
    valid_text_count: int
    average_text_length: float
    short_text_ratio: float
    medium_text_ratio: float
    question_ratio: float
    punctuation_ratio: float
    media_ratio: float
    sticker_ratio: float
    continuation_replies: int
    thread_bursts: int
    at_mentions: int
    reply_markers: int
    sticker_intent_count: int
    repeated_short_expression_count: int

    @classmethod
    def from_payload(cls, raw: Any) -> "HistoryCharacterMetrics":
        if not isinstance(raw, dict):
            raise ValueError("History-derived character metrics must be an object")
        missing = sorted(_METRIC_FIELDS - set(raw))
        unexpected = sorted(set(raw) - _METRIC_FIELDS)
        if missing or unexpected:
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unexpected:
                details.append("unexpected: " + ", ".join(unexpected))
            raise ValueError(
                "History-derived character metrics have an invalid schema ("
                + "; ".join(details)
                + ")"
            )
        metrics = cls(
            valid_text_count=_read_int(raw, "validTextCount"),
            average_text_length=_read_float(raw, "averageTextLength"),
            short_text_ratio=_read_float(raw, "shortTextRatio"),
            medium_text_ratio=_read_float(raw, "mediumTextRatio"),
            question_ratio=_read_float(raw, "questionRatio"),
            punctuation_ratio=_read_float(raw, "punctuationRatio"),
            media_ratio=_read_float(raw, "mediaRatio"),
            sticker_ratio=_read_float(raw, "stickerRatio"),
            continuation_replies=_read_int(raw, "continuationReplies"),
            thread_bursts=_read_int(raw, "threadBursts"),
            at_mentions=_read_int(raw, "atMentions"),
            reply_markers=_read_int(raw, "replyMarkers"),
            sticker_intent_count=_read_int(raw, "stickerIntentCount"),
            repeated_short_expression_count=_read_int(
                raw, "repeatedShortExpressionCount"
            ),
        )
        metrics.validate()
        return metrics

    def to_payload(self) -> dict[str, int | float]:
        return {
            "validTextCount": self.valid_text_count,
            "averageTextLength": self.average_text_length,
            "shortTextRatio": self.short_text_ratio,
            "mediumTextRatio": self.medium_text_ratio,
            "questionRatio": self.question_ratio,
            "punctuationRatio": self.punctuation_ratio,
            "mediaRatio": self.media_ratio,
            "stickerRatio": self.sticker_ratio,
            "continuationReplies": self.continuation_replies,
            "threadBursts": self.thread_bursts,
            "atMentions": self.at_mentions,
            "replyMarkers": self.reply_markers,
            "stickerIntentCount": self.sticker_intent_count,
            "repeatedShortExpressionCount": self.repeated_short_expression_count,
        }

    def validate(self) -> None:
        if (
            self.valid_text_count <= 0
            or not isfinite(self.average_text_length)
            or self.average_text_length <= 0
        ):
            raise ValueError("History-derived character metrics contain no valid text")
        ratios = (
            self.short_text_ratio,
            self.medium_text_ratio,
            self.question_ratio,
            self.punctuation_ratio,
            self.media_ratio,
            self.sticker_ratio,
        )
        if any(not isfinite(value) or value < 0 or value > 1 for value in ratios):
            raise ValueError("History-derived character metric ratios must be between 0 and 1")
        counts = (
            self.continuation_replies,
            self.thread_bursts,
            self.at_mentions,
            self.reply_markers,
            self.sticker_intent_count,
            self.repeated_short_expression_count,
        )
        if self.valid_text_count > _MAX_METRIC_COUNT or any(
            value < 0 or value > _MAX_METRIC_COUNT for value in counts
        ):
            raise ValueError("History-derived character metric counts are out of range")
        if self.short_text_ratio + self.medium_text_ratio > 1.0 + 1e-9:
            raise ValueError(
                "History-derived character short and medium text ratios must not exceed 1"
            )


def build_character_summary(metrics: HistoryCharacterMetrics) -> str:
    traits = [
        "说话偏短、直接"
        if metrics.short_text_ratio >= 0.5
        else "表达完整但不拖沓"
    ]
    if metrics.continuation_replies:
        traits.append("擅长顺着上下文快速接话")
    if metrics.thread_bursts:
        traits.append("需要补充时会偶尔追一小句")
    if metrics.punctuation_ratio < 0.5:
        traits.append("短回复常自然省略句号")
    if metrics.media_ratio >= 0.08:
        traits.append("会看气氛使用图片和表情回应")
    return (
        "这是从历史低敏聊天统计中形成的自有对话角色："
        + "，".join(traits)
        + "。这些统计只塑造整体表达习惯，不用于模仿、复述或冒充任何人。"
    )


def build_style_summary(metrics: HistoryCharacterMetrics) -> str:
    length_style = "整体以短句为主" if metrics.short_text_ratio >= 0.5 else "整体表达较完整"
    punctuation_style = (
        "短回复常省略收尾标点"
        if metrics.punctuation_ratio < 0.5
        else "会按语气自然使用标点"
    )
    return f"历史低敏样本{length_style}，{punctuation_style}；以当前语境为主，不照搬历史原句。"


def build_tone_rules(metrics: HistoryCharacterMetrics) -> list[str]:
    rules = [
        "多数闲聊先用一句短话接住"
        if metrics.short_text_ratio >= 0.5
        else "先把意思说完整，再按语境补充"
    ]
    if metrics.punctuation_ratio < 0.5:
        rules.append("短回复可以自然省略句号")
    if metrics.thread_bursts:
        rules.append("需要补充时可以偶尔拆成两小句，不要连续刷屏")
    if metrics.continuation_replies:
        rules.append("优先顺着当前上下文接话，不把闲聊变成正式问答")
    return rules


def build_reply_rules(metrics: HistoryCharacterMetrics) -> list[str]:
    rules = [
        "当前语境优先，历史画像不是词库或固定台词",
        "不要为了表现角色而硬塞历史口头禅、旧梗或示例句",
        "用户明确要求步骤、代码或长解释时，完整回答而不是强行缩短",
        "技术问题或 #chat 资料查询用一条完整消息回答，不套用普通闲聊短句限制",
    ]
    if metrics.short_text_ratio >= 0.5:
        rules.insert(0, "普通闲聊优先 1-2 句，能短就短")
    return rules


def build_behavior_profile(metrics: HistoryCharacterMetrics) -> dict[str, list[str]]:
    reply_cadence = [
        (
            "历史低敏文本平均长度和分布显示短句更常见"
            if metrics.short_text_ratio >= 0.5
            else "历史低敏文本平均长度和分布显示完整句更常见"
        ),
        "多数场景优先一句话接住，除非用户明确要步骤、代码或长解释",
    ]
    if metrics.question_ratio >= 0.25:
        reply_cadence.append("提问/追问式短句较多，可以自然用反问或追问接话")
    else:
        reply_cadence.append("不要为了显得活跃而频繁反问，先顺着当前话题回")

    interaction_habits = [
        "被 @、被引用、被戳时可以短促回应，不要每次都认真解释",
        "用户说继续/more时顺着上一条回答接着讲，不重复前文定义",
    ]
    if metrics.thread_bursts:
        interaction_habits.append("存在连续补充同一话题的习惯，适合偶尔分两小句追补")
    if metrics.continuation_replies:
        interaction_habits.append("习惯紧跟别人消息接话，优先像群友插话而不是正式答题")
    if metrics.repeated_short_expression_count:
        interaction_habits.append(
            "历史中存在重复短表达；只参考短促节奏，不把原句注入 Prompt"
        )

    chat_action_rules = [
        (
            "历史记录中图片/表情互动较常见"
            if metrics.media_ratio >= 0.08
            else "图片和表情按当前语境低频使用"
        ),
        "能用真实表情包链路时优先发图，不用文字假装发图",
        "戳一戳、+1、复读和斗图属于群聊动作，适合短促、低频、看气氛触发",
        "模型先分析图片/表情含义；匹配成功只回语义匹配表情包，失败才回短文本",
    ]
    if metrics.sticker_intent_count:
        chat_action_rules.append(
            "历史中识别到明确表情/斗图请求；按意图走真实媒体链路，不引用原句"
        )

    return {
        "replyCadence": reply_cadence,
        "punctuationProfile": [
            (
                "低敏文本中收尾标点相对少"
                if metrics.punctuation_ratio < 0.5
                else "低敏文本会按语气自然使用标点"
            ),
            "群聊短回复可以少用句号，允许无标点收尾",
        ],
        "interactionHabits": interaction_habits,
        "chatActionRules": chat_action_rules,
    }
