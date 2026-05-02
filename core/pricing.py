"""
LLM プロバイダ別単価表とコスト計算。
単価は USD per million tokens（2026-04 確認）。
"""

# 公式単価 (USD / 1M tokens)
# Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
# OpenAI:    https://openai.com/api/pricing/
# Google:    https://ai.google.dev/gemini-api/docs/pricing
PRICING: dict[str, dict[str, dict[str, float]]] = {
    "anthropic": {
        "claude-opus-4-6": {
            "input":        5.00,
            "output":      25.00,
            "cached_input": 0.50,   # prompt caching 90% off
        },
        "claude-opus-4-7-20251201": {
            "input":        5.00,
            "output":      25.00,
            "cached_input": 0.50,
        },
        "claude-sonnet-4-6": {
            "input":        3.00,
            "output":      15.00,
            "cached_input": 0.30,
        },
        "claude-haiku-4-5-20251001": {
            "input":        1.00,
            "output":       5.00,
            "cached_input": 0.10,
        },
    },
    "openai": {
        "gpt-5.4": {
            "input":        2.50,
            "output":      10.00,
            "cached_input": 0.25,   # 90% off cached
        },
        "gpt-5.4-mini": {
            "input":        0.75,
            "output":       4.50,
            "cached_input": 0.075,
        },
        "gpt-5": {
            "input":        0.625,
            "output":       5.00,
            "cached_input": 0.0625,
        },
        "gpt-5-mini": {
            "input":        0.25,
            "output":       2.00,
            "cached_input": 0.025,
        },
    },
    "google": {
        # gemini-3.x-preview は Gemini 2.5 ファミリーに相当すると推定
        "gemini-3.1-pro-preview": {
            "input":        1.25,
            "output":      10.00,
            "cached_input": 0.3125,
        },
        "gemini-3-flash-preview": {
            "input":        0.30,
            "output":       2.50,
            "cached_input": 0.075,
        },
        "gemini-2.5-pro": {
            "input":        1.25,
            "output":      10.00,
            "cached_input": 0.3125,
        },
        "gemini-2.5-flash": {
            "input":        0.30,
            "output":       2.50,
            "cached_input": 0.075,
        },
    },
}


def calculate_cost_usd(
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_read: int = 0,
) -> float:
    """トークン数からコスト(USD)を計算。不明モデルは 0.0 を返す。"""
    rates = PRICING.get(provider, {}).get(model)
    if not rates:
        return 0.0
    return (
        tokens_in   * rates["input"]        / 1_000_000
        + tokens_out  * rates["output"]       / 1_000_000
        + cache_read  * rates["cached_input"] / 1_000_000
    )
