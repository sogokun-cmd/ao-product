"""
LLM プロバイダ抽象化レイヤー。

複数の LLM プロバイダ（Anthropic / OpenAI / Google）を共通インターフェースで扱い、
情報収集ワークフローの各工程で切替・追加・複数モデル比較を可能にする。

UI上ではモデル名を主役にしない方針。価値は「高品質な一次情報リサーチ」。
プランによって使えるモデルは切り分けない（情報品質は全プラン共通）。
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict | None = None


class LLMProvider(ABC):
    name: str = ""

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
        use_cache: bool = True,
    ) -> LLMResponse: ...


# ── Anthropic ────────────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def is_available(self) -> bool:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    def complete(self, system, user, model, max_tokens=2000, temperature=0.2, use_cache: bool = True) -> LLMResponse:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        # system は常に list[block] 形式に統一（文字列混在による BadRequestError 対策）
        # キャッシュ対象: 2000文字以上のシステムプロンプト
        if isinstance(system, str):
            block = {"type": "text", "text": system}
            if use_cache and len(system) >= 2000:
                block["cache_control"] = {"type": "ephemeral"}
            system_blocks = [block]
        elif isinstance(system, list):
            system_blocks = system
        else:
            system_blocks = [{"type": "text", "text": str(system)}]
        # claude-opus-4-x 以降は temperature が廃止
        _no_temp = model.startswith("claude-opus-4")
        create_kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
        )
        if not _no_temp:
            create_kwargs["temperature"] = temperature
        resp = client.messages.create(**create_kwargs)
        text = "".join(getattr(b, "text", "") for b in resp.content)
        usage = None
        if hasattr(resp, "usage"):
            try:
                usage = {
                    "input_tokens":         getattr(resp.usage, "input_tokens", None),
                    "output_tokens":        getattr(resp.usage, "output_tokens", None),
                    "cache_creation_tokens": getattr(resp.usage, "cache_creation_input_tokens", None),
                    "cache_read_tokens":     getattr(resp.usage, "cache_read_input_tokens", None),
                }
            except Exception:
                usage = None
        return LLMResponse(text=text, model=model, provider=self.name, usage=usage)


# ── OpenAI ───────────────────────────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    name = "openai"

    def is_available(self) -> bool:
        if not os.environ.get("OPENAI_API_KEY"):
            return False
        try:
            import openai  # noqa: F401
            return True
        except ImportError:
            return False

    def complete(self, system, user, model, max_tokens=2000, temperature=0.2, use_cache: bool = True) -> LLMResponse:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        usage = None
        if hasattr(resp, "usage") and resp.usage:
            try:
                usage = {
                    "input_tokens":  resp.usage.prompt_tokens,
                    "output_tokens": resp.usage.completion_tokens,
                }
            except Exception:
                usage = None
        return LLMResponse(text=text, model=model, provider=self.name, usage=usage)


# ── Google (Gemini) ──────────────────────────────────────────────────────────

class GoogleProvider(LLMProvider):
    name = "google"

    def _api_key(self) -> str | None:
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

    def is_available(self) -> bool:
        if not self._api_key():
            return False
        try:
            import google.generativeai  # noqa: F401
            return True
        except ImportError:
            return False

    def complete(self, system, user, model, max_tokens=2000, temperature=0.2, use_cache: bool = True) -> LLMResponse:
        import google.generativeai as genai
        genai.configure(api_key=self._api_key())
        m = genai.GenerativeModel(model_name=model, system_instruction=system)
        resp = m.generate_content(
            user,
            generation_config={
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        text = getattr(resp, "text", "") or ""
        return LLMResponse(text=text, model=model, provider=self.name)


# ── レジストリ ──────────────────────────────────────────────────────────────

PROVIDERS: dict[str, LLMProvider] = {
    "anthropic": AnthropicProvider(),
    "openai":    OpenAIProvider(),
    "google":    GoogleProvider(),
}


def available_providers() -> list[str]:
    return [name for name, p in PROVIDERS.items() if p.is_available()]
