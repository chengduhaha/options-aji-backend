"""LLM provider routing for OpenAI-compatible chat models."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from pydantic import Field

from app.config import Settings


@dataclass(frozen=True)
class LLMProvider:
    name: str
    api_key: str
    base_url: str
    model: str


def configured_providers(
    cfg: Settings | Any | None = None,
    *,
    openrouter_model: str | None = None,
    xiaomi_model: str | None = None,
) -> list[LLMProvider]:
    cfg = cfg or Settings()
    providers: list[LLMProvider] = []

    openrouter_key = str(getattr(cfg, "openrouter_api_key", "") or "").strip()
    if openrouter_key:
        providers.append(
            LLMProvider(
                name="openrouter",
                api_key=openrouter_key,
                base_url=str(
                    getattr(cfg, "openrouter_base_url", "https://openrouter.ai/api/v1")
                    or "https://openrouter.ai/api/v1"
                ).strip(),
                model=(openrouter_model or str(getattr(cfg, "model_synthesis", "") or "")).strip()
                or "deepseek/deepseek-chat",
            )
        )

    xiaomi_key = str(getattr(cfg, "xiaomi_api_key", "") or "").strip()
    if xiaomi_key:
        providers.append(
            LLMProvider(
                name="xiaomi",
                api_key=xiaomi_key,
                base_url=str(getattr(cfg, "xiaomi_base_url", "https://api.mimo-v2.com/v1") or "").strip()
                or "https://api.mimo-v2.com/v1",
                model=(xiaomi_model or str(getattr(cfg, "xiaomi_model", "") or "")).strip()
                or "mimo-v2.5",
            )
        )

    return providers


def choose_provider(
    cfg: Settings | Any | None = None,
    *,
    openrouter_model: str | None = None,
    xiaomi_model: str | None = None,
) -> LLMProvider:
    providers = configured_providers(
        cfg,
        openrouter_model=openrouter_model,
        xiaomi_model=xiaomi_model,
    )
    if not providers:
        raise RuntimeError("no_llm_provider_configured")
    return providers[secrets.randbelow(len(providers))]


def provider_order(
    cfg: Settings | Any | None = None,
    *,
    openrouter_model: str | None = None,
    xiaomi_model: str | None = None,
) -> list[LLMProvider]:
    providers = configured_providers(
        cfg,
        openrouter_model=openrouter_model,
        xiaomi_model=xiaomi_model,
    )
    if not providers:
        raise RuntimeError("no_llm_provider_configured")
    start = secrets.randbelow(len(providers))
    return providers[start:] + providers[:start]


def has_llm_provider(cfg: Settings | Any | None = None) -> bool:
    return bool(configured_providers(cfg))


def build_chat_openai(
    cfg: Settings | Any | None = None,
    *,
    openrouter_model: str | None = None,
    xiaomi_model: str | None = None,
    source: str = "langchain",
    temperature: float = 0.2,
    **kwargs: Any,
) -> BaseChatModel:
    providers = provider_order(
        cfg,
        openrouter_model=openrouter_model,
        xiaomi_model=xiaomi_model,
    )
    return ProviderFallbackChatModel(
        providers=providers,
        source=source,
        temperature=temperature,
        client_kwargs=kwargs,
    )


class ProviderFallbackChatModel(BaseChatModel):
    providers: list[LLMProvider]
    source: str = "langchain"
    temperature: float = 0.2
    client_kwargs: dict[str, Any] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "optionsaji-provider-fallback-chat"

    def _model_for(self, provider: LLMProvider) -> ChatOpenAI:
        return ChatOpenAI(
            model=provider.model,
            base_url=provider.base_url,
            api_key=provider.api_key,
            temperature=self.temperature,
            **self.client_kwargs,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        errors: list[str] = []
        for provider in self.providers:
            started = time.perf_counter()
            try:
                result = self._model_for(provider)._generate(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
                _record_chat_result_usage(provider, self.source, result, True, started)
                return result
            except Exception as exc:
                _record_chat_failure(provider, self.source, exc, started)
                errors.append(f"{provider.name}: {type(exc).__name__}: {str(exc)[:240]}")
        raise RuntimeError("all_llm_providers_failed; " + " | ".join(errors))

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        errors: list[str] = []
        for provider in self.providers:
            started = time.perf_counter()
            try:
                result = await self._model_for(provider)._agenerate(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
                _record_chat_result_usage(provider, self.source, result, True, started)
                return result
            except Exception as exc:
                _record_chat_failure(provider, self.source, exc, started)
                errors.append(f"{provider.name}: {type(exc).__name__}: {str(exc)[:240]}")
        raise RuntimeError("all_llm_providers_failed; " + " | ".join(errors))


def chat_completions_url(provider: LLMProvider) -> str:
    return f"{provider.base_url.rstrip('/')}/chat/completions"


def auth_headers(provider: LLMProvider) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }


def post_chat_completions_with_fallback(
    payload: dict[str, Any],
    *,
    cfg: Settings | Any | None = None,
    openrouter_model: str | None = None,
    xiaomi_model: str | None = None,
    source: str = "http_chat",
    timeout: float = 90.0,
) -> tuple[dict[str, Any], LLMProvider]:
    errors: list[str] = []
    for provider in provider_order(
        cfg,
        openrouter_model=openrouter_model,
        xiaomi_model=xiaomi_model,
    ):
        request_payload = dict(payload)
        request_payload["model"] = provider.model
        if provider.name == "openrouter" and "usage" not in request_payload:
            request_payload["usage"] = {"include": True}
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    chat_completions_url(provider),
                    headers=auth_headers(provider),
                    json=request_payload,
                )
                resp.raise_for_status()
                data = resp.json()
                _record_payload_usage(provider, source, data, True, started)
                return data, provider
        except Exception as exc:
            _record_chat_failure(provider, source, exc, started)
            errors.append(f"{provider.name}: {type(exc).__name__}: {str(exc)[:240]}")
    raise RuntimeError("all_llm_providers_failed; " + " | ".join(errors))


def _latency_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _record_payload_usage(
    provider: LLMProvider,
    source: str,
    payload: dict[str, Any],
    success: bool,
    started: float,
) -> None:
    try:
        from app.db.session import SessionLocal
        from app.services.llm_usage import extract_usage_tokens, record_llm_usage

        input_tokens, output_tokens, total_tokens, cost = extract_usage_tokens(payload)
        with SessionLocal() as session:
            record_llm_usage(
                session,
                provider=provider.name,
                model=provider.model,
                source=source,
                success=success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
                latency_ms=_latency_ms(started),
            )
    except Exception:
        return


def _record_chat_result_usage(
    provider: LLMProvider,
    source: str,
    result: ChatResult,
    success: bool,
    started: float,
) -> None:
    payload: dict[str, Any] = {}
    if isinstance(result.llm_output, dict):
        payload["usage"] = result.llm_output.get("token_usage") or result.llm_output.get("usage") or {}
    _record_payload_usage(provider, source, payload, success, started)


def _record_chat_failure(provider: LLMProvider, source: str, exc: Exception, started: float) -> None:
    try:
        from app.db.session import SessionLocal
        from app.services.llm_usage import record_llm_usage

        with SessionLocal() as session:
            record_llm_usage(
                session,
                provider=provider.name,
                model=provider.model,
                source=source,
                success=False,
                latency_ms=_latency_ms(started),
                error_code=type(exc).__name__,
            )
    except Exception:
        return
