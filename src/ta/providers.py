"""Who answers a structured model call, and what happens when they do not.

`LLM` (in `llm.py`) knows the tasks — what to ask. A provider knows one vendor —
how to ask it. Each task is routed to a provider by the config, with one fallback,
and a provider that fails is an expected path rather than an exception
(ADR 0018).

The two differ where it matters. Gemini validates against the schema on its side
(`response_schema`). DeepSeek only promises *some* JSON (`json_object` mode), so the
schema goes into the prompt and the validation happens here: parse with the same
Pydantic model, give it one chance to correct itself, then give up so the caller
can fall back. Its docs also warn that the content may come back empty; that is
just another invalid answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError

from . import i18n

log = logging.getLogger("ta.llm")

GEMINI_DEFAULT_MODEL = "gemini-flash-latest"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_URL = "https://api.deepseek.com"

# The names `config.toml` may use. A typo there must not silently route a task
# to nothing, so `config.llm_routing` checks against this.
PROVIDER_NAMES = ("deepseek", "gemini")


class LLMUnavailable(RuntimeError):
    """No key, no SDK, no network, or no valid answer. The message is for a human."""


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.tokens_in + other.tokens_in, self.tokens_out + other.tokens_out)


class Provider(Protocol):
    name: str
    model: str

    @property
    def configured(self) -> bool: ...

    async def structured(
        self, prompt: str, schema: type[BaseModel], system: str
    ) -> tuple[BaseModel, Usage]: ...

    async def list_models(self) -> list[str]: ...


class GeminiProvider:
    """The code `llm.py` always had, moved behind the interface unchanged."""

    name = "gemini"

    def __init__(self, api_key: str | None, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model or GEMINI_DEFAULT_MODEL
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LLMUnavailable(i18n.t("ai.no_key"))
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover - declared dependency
            raise LLMUnavailable(i18n.t("ai.no_sdk")) from e
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def structured(self, prompt, schema, system):
        from google.genai import types

        client = self._get()
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system,
        )
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content, model=self.model, contents=prompt, config=cfg
            )
        except Exception as e:
            raise LLMUnavailable(i18n.t("ai.failed", erro=e)) from e

        # `parsed` is the validated instance; the SDK fills it when there is a schema.
        if getattr(resp, "parsed", None) is None:
            raise LLMUnavailable(i18n.t("ai.off_schema"))
        meta = getattr(resp, "usage_metadata", None)
        usage = Usage(
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
        )
        return resp.parsed, usage

    async def list_models(self) -> list[str]:
        client = self._get()
        models = await asyncio.to_thread(lambda: list(client.models.list()))
        return [
            m.name.replace("models/", "")
            for m in models
            if "generateContent" in (m.supported_actions or [])
        ]


class DeepSeekProvider:
    """DeepSeek over its OpenAI-compatible HTTP API, with our own validation."""

    name = "deepseek"

    # Short on purpose. The CLI waits 120s on model routes, and a failed DeepSeek
    # call is followed by its retry and then by the fallback; a long timeout here
    # would spend the whole budget before Gemini is ever asked.
    TIMEOUT = 30.0
    # Its docs warn that a low `max_tokens` truncates the JSON mid-string, which
    # then fails validation for a reason that has nothing to do with the model.
    MAX_TOKENS = 8192

    def __init__(
        self,
        api_key: str | None,
        model: str | None = None,
        *,
        base_url: str = DEEPSEEK_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model or DEEPSEEK_DEFAULT_MODEL
        self.base_url = base_url
        # Injectable so tests answer with `httpx.MockTransport` and never reach
        # the network.
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _client(self) -> httpx.AsyncClient:
        if not self.api_key:
            raise LLMUnavailable(i18n.t("ai.no_key"))
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.TIMEOUT,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    @staticmethod
    def _schema_instruction(schema: type[BaseModel]) -> str:
        # JSON mode requires the word "json" in the prompt, and works better with
        # the expected shape spelled out. The schema is the shape.
        return (
            "Answer with a single JSON object, and nothing else, that validates "
            "against this JSON Schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )

    async def structured(self, prompt, schema, system):
        messages = [
            {"role": "system", "content": f"{system}\n\n{self._schema_instruction(schema)}"},
            {"role": "user", "content": prompt},
        ]
        usage = Usage()
        async with self._client() as client:
            for attempt in (1, 2):
                try:
                    r = await client.post(
                        "/chat/completions",
                        json={
                            "model": self.model,
                            "messages": messages,
                            "response_format": {"type": "json_object"},
                            "max_tokens": self.MAX_TOKENS,
                        },
                    )
                    r.raise_for_status()
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    raise LLMUnavailable(i18n.t("ai.failed", erro=e)) from e

                u = data.get("usage") or {}
                usage += Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                try:
                    return schema.model_validate_json(content), usage
                except ValidationError as e:
                    log.info("deepseek answered off-schema (attempt %d): %s", attempt, e)
                    # The second chance sees its own answer and what was wrong
                    # with it, which fixes a missing field far more often than
                    # asking the same question again.
                    messages += [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": "That did not validate against the schema: "
                            f"{e.errors(include_url=False)[:3]}. "
                            "Answer again with the corrected JSON object only.",
                        },
                    ]
        raise LLMUnavailable(i18n.t("ai.off_schema"))

    async def list_models(self) -> list[str]:
        async with self._client() as client:
            try:
                r = await client.get("/models")
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise LLMUnavailable(i18n.t("ai.failed", erro=e)) from e
            return [m["id"] for m in r.json().get("data", [])]
