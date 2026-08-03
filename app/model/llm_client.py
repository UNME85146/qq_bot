from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.models import GeneratedReply, ModelConfig


class LlmClient(Protocol):
    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        ...


class MockModelClient:
    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        last_user_message = next(
            (
                _content_to_text(message.get("content", ""))
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        text = "收到啦，我在。"
        if "真人" in last_user_message or "AI" in last_user_message:
            text = "小黄"
        return GeneratedReply(
            text=text,
            raw_model_text=text,
            model_name="mock",
            finish_reason="stop",
        )


class OpenAICompatibleClient:
    def __init__(self, config: ModelConfig) -> None:
        if not config.api_key:
            raise ValueError("Model API key is required for OpenAICompatibleClient.")
        self._config = config

    async def generate(self, messages: list[dict[str, Any]]) -> GeneratedReply:
        return await self.generate_with_options(messages)

    async def generate_with_options(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> GeneratedReply:
        payload = {
            "model": self._config.name,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": (
                self._config.max_tokens if max_tokens is None else max_tokens
            ),
        }
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        timeout = httpx.Timeout(self._config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._config.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        choice = data["choices"][0]
        text = choice["message"]["content"].strip()
        usage = data.get("usage", {})
        return GeneratedReply(
            text=text,
            raw_model_text=text,
            model_name=self._config.name,
            finish_reason=choice.get("finish_reason", "unknown"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


def create_model_client(config: ModelConfig) -> LlmClient:
    if config.use_mock:
        return MockModelClient()
    return OpenAICompatibleClient(config)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)
