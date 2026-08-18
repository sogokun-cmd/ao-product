"""
LLM プロバイダ抽象化レイヤー。

複数の LLM プロバイダ（Anthropic / OpenAI / Google）を共通インターフェースで扱い、
情報収集ワークフローの各工程で切替・追加・複数モデル比較を可能にする。

UI上ではモデル名を主役にしない方針。価値は「高品質な一次情報リサーチ」。
プランによって使えるモデルは切り分けない（情報品質は全プラン共通）。
"""
from __future__ import annotations

import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

_CLIENT_TIMEOUT = int(os.environ.get("LLM_CLIENT_TIMEOUT_SEC", "120"))

# temperature を受け付けない Anthropic モデル（4.7 以降の世代）
_ANTHROPIC_NO_TEMPERATURE = re.compile(
    r"^claude-(opus-4-[78]|opus-5|sonnet-5|fable-5|mythos-5)"
)


def _is_unsupported_param_error(exc: Exception, param: str) -> bool:
    """「そのパラメータはこのモデルでは使えない」旨の 400 かどうかを判定する。
    パラメータ名に言及する invalid_request_error のみ True。それ以外は False（＝再送しない）。"""
    if getattr(exc, "status_code", None) not in (400, None):
        return False
    msg = str(exc)
    if param not in msg:
        return False
    lowered = msg.lower()
    return any(
        marker in lowered
        for marker in ("invalid_request_error", "unsupported_parameter", "unsupported_value")
    )


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
    _client = None
    _lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import anthropic
                    self._client = anthropic.Anthropic(
                        api_key=os.environ["ANTHROPIC_API_KEY"],
                        timeout=_CLIENT_TIMEOUT,
                    )
        return self._client

    def is_available(self) -> bool:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    def complete(self, system, user, model, max_tokens=2000, temperature=0.2, use_cache: bool = True) -> LLMResponse:
        client = self._get_client()
        # プロンプトキャッシュ: system を blocks 形式にして cache_control を付与
        # 条件: system が 1024 トークン以上（概ね 2000 文字以上）である必要がある
        if use_cache and isinstance(system, str) and len(system) >= 2000:
            system_blocks = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_blocks = system
        create_kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
        )
        # 新しい世代（opus-4-7/4-8, opus-5, sonnet-5, fable-5 など）は temperature が廃止。
        # モデル名の前方一致だけに頼ると新モデル追加のたびに壊れるので、
        # 拒否されたら落として再試行する形にしておく。
        if not _ANTHROPIC_NO_TEMPERATURE.match(model):
            create_kwargs["temperature"] = temperature
        while True:
            try:
                resp = client.messages.create(**create_kwargs)
                break
            except Exception as e:
                if "temperature" in create_kwargs and _is_unsupported_param_error(e, "temperature"):
                    create_kwargs.pop("temperature")
                    continue
                raise
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
    _client = None
    _lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from openai import OpenAI
                    self._client = OpenAI(
                        api_key=os.environ["OPENAI_API_KEY"],
                        timeout=_CLIENT_TIMEOUT,
                    )
        return self._client

    def is_available(self) -> bool:
        if not os.environ.get("OPENAI_API_KEY"):
            return False
        try:
            import openai  # noqa: F401
            return True
        except ImportError:
            return False

    def complete(self, system, user, model, max_tokens=2000, temperature=0.2, use_cache: bool = True) -> LLMResponse:
        client = self._get_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        kwargs = {"max_completion_tokens": max_tokens, "temperature": temperature}
        while True:
            try:
                resp = client.chat.completions.create(model=model, messages=messages, **kwargs)
                break
            except Exception as e:
                # 新しいモデル（gpt-5.5 等）は max_tokens を廃止し max_completion_tokens を要求する。
                # 逆に古いモデルは max_completion_tokens を知らないので、その場合だけ入れ替える。
                if "max_completion_tokens" in kwargs and _is_unsupported_param_error(e, "max_completion_tokens"):
                    kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
                    continue
                # 新しいモデルは temperature の変更を受け付けない（既定値の 1 のみ）。
                if "temperature" in kwargs and _is_unsupported_param_error(e, "temperature"):
                    kwargs.pop("temperature")
                    continue
                raise
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
    _client = None
    _lock = threading.Lock()

    def _api_key(self) -> str | None:
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from google import genai
                    self._client = genai.Client(
                        api_key=self._api_key(),
                        # google-genai の HttpOptions.timeout はミリ秒指定
                        http_options={"timeout": _CLIENT_TIMEOUT * 1000},
                    )
        return self._client

    def is_available(self) -> bool:
        if not self._api_key():
            return False
        try:
            from google import genai  # noqa: F401
            return True
        except ImportError:
            return False

    def complete(self, system, user, model, max_tokens=2000, temperature=0.2, use_cache: bool = True) -> LLMResponse:
        client = self._get_client()
        from google.genai import types
        resp = client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
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
