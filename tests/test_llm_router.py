from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.services.llm_router import (
    build_chat_openai,
    choose_provider,
    configured_providers,
    post_chat_completions_with_fallback,
)


def test_configured_providers_include_openrouter_and_xiaomi_when_both_keys_exist() -> None:
    cfg = SimpleNamespace(
        openrouter_api_key="or-key",
        openrouter_base_url="https://openrouter.example/v1",
        model_synthesis="openrouter-model",
        xiaomi_api_key="xm-key",
        xiaomi_base_url="https://api.mimo-v2.com/v1",
        xiaomi_model="mimo-v2.5",
    )

    providers = configured_providers(cfg)

    assert [p.name for p in providers] == ["openrouter", "xiaomi"]
    assert providers[1].api_key == "xm-key"
    assert providers[1].base_url == "https://api.mimo-v2.com/v1"
    assert providers[1].model == "mimo-v2.5"


def test_choose_provider_randomly_selects_from_configured_providers(monkeypatch) -> None:
    cfg = SimpleNamespace(
        openrouter_api_key="or-key",
        openrouter_base_url="https://openrouter.example/v1",
        model_synthesis="openrouter-model",
        xiaomi_api_key="xm-key",
        xiaomi_base_url="https://api.mimo-v2.com/v1",
        xiaomi_model="mimo-v2.5",
    )

    monkeypatch.setattr("app.services.llm_router.secrets.randbelow", lambda n: n - 1)

    provider = choose_provider(cfg)

    assert provider.name == "xiaomi"
    assert provider.model == "mimo-v2.5"


def test_chat_completion_falls_back_to_second_provider(monkeypatch) -> None:
    cfg = SimpleNamespace(
        openrouter_api_key="or-key",
        openrouter_base_url="https://openrouter.example/v1",
        model_synthesis="openrouter-model",
        xiaomi_api_key="xm-key",
        xiaomi_base_url="https://xiaomi.example/v1",
        xiaomi_model="mimo-v2.5",
    )
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, content: dict):
            self._content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._content

    class FakeClient:
        def __init__(self, timeout: float):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url: str, headers: dict, json: dict):
            calls.append(url)
            if "openrouter" in url:
                raise RuntimeError("openrouter down")
            return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("app.services.llm_router.secrets.randbelow", lambda _n: 0)
    monkeypatch.setattr("app.services.llm_router.httpx.Client", FakeClient)

    data, provider = post_chat_completions_with_fallback(
        {"messages": [{"role": "user", "content": "hi"}]},
        cfg=cfg,
    )

    assert provider.name == "xiaomi"
    assert data["choices"][0]["message"]["content"] == "ok"
    assert calls == [
        "https://openrouter.example/v1/chat/completions",
        "https://xiaomi.example/v1/chat/completions",
    ]


def test_chat_completion_raises_after_all_providers_fail(monkeypatch) -> None:
    cfg = SimpleNamespace(
        openrouter_api_key="or-key",
        openrouter_base_url="https://openrouter.example/v1",
        model_synthesis="openrouter-model",
        xiaomi_api_key="xm-key",
        xiaomi_base_url="https://xiaomi.example/v1",
        xiaomi_model="mimo-v2.5",
    )

    class FakeClient:
        def __init__(self, timeout: float):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url: str, headers: dict, json: dict):
            raise RuntimeError(url)

    monkeypatch.setattr("app.services.llm_router.httpx.Client", FakeClient)

    with pytest.raises(RuntimeError, match="all_llm_providers_failed"):
        post_chat_completions_with_fallback({"messages": []}, cfg=cfg)


def test_langchain_chat_model_falls_back_to_second_provider(monkeypatch) -> None:
    cfg = SimpleNamespace(
        openrouter_api_key="or-key",
        openrouter_base_url="https://openrouter.example/v1",
        model_synthesis="openrouter-model",
        xiaomi_api_key="xm-key",
        xiaomi_base_url="https://xiaomi.example/v1",
        xiaomi_model="mimo-v2.5",
    )
    tried: list[str] = []

    class FakeChatOpenAI:
        def __init__(self, model: str, **_kwargs):
            self.model = model

        def _generate(self, *_args, **_kwargs):
            tried.append(self.model)
            if self.model == "openrouter-model":
                raise RuntimeError("primary failed")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="backup ok"))])

    monkeypatch.setattr("app.services.llm_router.secrets.randbelow", lambda _n: 0)
    monkeypatch.setattr("app.services.llm_router.ChatOpenAI", FakeChatOpenAI)

    model = build_chat_openai(cfg)
    result = model.invoke([HumanMessage(content="hi")])

    assert result.content == "backup ok"
    assert tried == ["openrouter-model", "mimo-v2.5"]
