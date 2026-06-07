from __future__ import annotations

import re

from app.models import SafetyCheckResult


class SafetyService:
    def __init__(
        self,
        *,
        identity_disclosure: str = "我是基于 SOURCE_QQ 的聊天风格调出来的测试号，不是本人。",
        robot_identity_disclosure: str | None = None,
        source_user_id: str = "SOURCE_QQ",
    ) -> None:
        self._identity_disclosure = identity_disclosure
        self._robot_identity_disclosure = (
            robot_identity_disclosure
            if robot_identity_disclosure is not None
            else _default_robot_identity_disclosure(identity_disclosure)
        )
        self._source_user_id = source_user_id

    _identity_patterns = (
        "你是真人吗",
        "你是人吗",
        "你是不是ai",
        "你是不是 AI",
        "你是ai吗",
        "你是 AI 吗",
        "你是不是机器人",
        "你是谁",
        "你是本人吗",
        "你是不是本人",
    )
    _illegal_keywords = (
        "盗号",
        "撞库",
        "木马",
        "钓鱼网站",
        "黑进",
        "窃取",
        "绕过登录",
        "破解密码",
    )
    _privacy_keywords = (
        "身份证",
        "手机号",
        "手机号码",
        "家庭住址",
        "家庭地址",
        "住址",
        "地址",
        "银行卡",
        "密码",
        "验证码",
        "聊天记录发给我",
        "把聊天记录",
    )
    _sensitive_value_patterns = (
        re.compile(r"\b\d{17}[\dXx]\b"),
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        re.compile(r"(?<!\d)\d{6}(?!\d)"),
    )

    def check_input(self, text: str, *, scope_type: str) -> SafetyCheckResult:
        normalized = _normalize(text)
        if self.contains_high_sensitivity(text):
            return SafetyCheckResult(
                action="block",
                reason="high_sensitive_privacy",
                replacement_text=_blocked_text(scope_type),
                safety_level="blocked",
            )
        identity_reply = self._identity_reply_text(normalized)
        if identity_reply is not None:
            return SafetyCheckResult(
                action="rewrite",
                reason="identity_disclosure",
                replacement_text=identity_reply,
                safety_level="rewritten",
            )
        if any(keyword in normalized for keyword in self._illegal_keywords):
            return SafetyCheckResult(
                action="block",
                reason="illegal_request",
                replacement_text=_blocked_text(scope_type),
                safety_level="blocked",
            )
        return SafetyCheckResult(action="allow", reason="safe")

    def check_output(self, text: str, *, scope_type: str) -> SafetyCheckResult:
        normalized = _normalize(text)
        if self.contains_high_sensitivity(text):
            return SafetyCheckResult(
                action="block",
                reason="output_high_sensitive_privacy",
                replacement_text=_blocked_text(scope_type),
                safety_level="blocked",
            )
        if self._overclaims_identity(normalized):
            return SafetyCheckResult(
                action="rewrite",
                reason="output_identity_overclaim",
                replacement_text=self._robot_identity_disclosure,
                safety_level="rewritten",
            )
        return SafetyCheckResult(action="allow", reason="safe")

    def contains_high_sensitivity(self, text: str) -> bool:
        normalized = _normalize(text)
        if any(keyword in normalized for keyword in self._privacy_keywords):
            return True
        return any(pattern.search(text) for pattern in self._sensitive_value_patterns)

    def can_store_long_term_memory(self, text: str) -> bool:
        normalized = _normalize(text)
        health_finance_markers = (
            "病历",
            "诊断",
            "借款",
            "负债",
            "工资",
            "收入",
            "余额",
            "银行卡",
            "未成年",
            "医疗诊断",
            "高敏健康",
            "财务精确信息",
        )
        if any(marker in normalized for marker in health_finance_markers):
            return False
        return not self.contains_high_sensitivity(text)

    def _identity_reply_text(self, normalized: str) -> str | None:
        if not self._mentions_identity(normalized):
            return None
        if self._asks_robot_identity(normalized):
            return self._robot_identity_disclosure
        return self._identity_disclosure

    def _mentions_identity(self, normalized: str) -> bool:
        normalized_lower = normalized.lower()
        if any(pattern.lower() in normalized_lower for pattern in self._identity_patterns):
            return True
        explicit_identity_patterns = (
            r"你[，,。！？!?\s]*是谁",
            r"你[，,。！？!?\s]*是什么",
            r"你叫什?么",
            r"你叫啥",
            r"怎么称呼你",
            r"你是(?:不是)?(?:机器人|bot|ai|真人|本人)",
            r"你是真人吗",
            r"你是人吗",
            r"你是不是人",
            r"你是不是ai",
            r"你是ai吗",
            r"你是不是机器人",
            r"你是机器人吗",
            r"你是不是本人",
            r"你是本人吗",
        )
        if any(
            re.search(pattern, normalized_lower, re.IGNORECASE)
            for pattern in explicit_identity_patterns
        ):
            return True
        if self._source_user_id in normalized and any(
            marker in normalized for marker in ("你是", "是不是", "本人", "真人", "吗")
        ):
            return True
        return False

    def _asks_robot_identity(self, normalized: str) -> bool:
        normalized_lower = normalized.lower()
        if any(
            marker in normalized_lower
            for marker in (
                "真人",
                "本人",
                "ai",
                "bot",
                "机器人",
                "是人吗",
                "不是人",
                "虚拟",
            )
        ):
            return True
        return self._source_user_id in normalized

    def _overclaims_identity(self, normalized: str) -> bool:
        overclaims = (
            "我是真人",
            "我不是虚拟",
            "我是本人",
            "我就是本人",
            "这就是我的真实账号",
            f"我是{self._source_user_id}",
            f"我就是{self._source_user_id}",
        )
        return any(pattern in normalized for pattern in overclaims)


def _blocked_text(scope_type: str) -> str:
    if scope_type == "group":
        return "这个不太适合在群里聊。"
    return "这个我不能帮你处理，我们换个安全点的话题吧。"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def _default_robot_identity_disclosure(identity_disclosure: str) -> str:
    if identity_disclosure.strip() in {"我是机器人", "我是 BOT", "我是bot"}:
        return "我是一个机器人"
    return identity_disclosure
