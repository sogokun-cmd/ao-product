"""
タスク別 LLM ルーター。

各「情報収集ワークフロー工程」（タスク）に対して、
- プライマリ + バックアップの (provider, model) 順序を定義
- 利用可能な最初のプロバイダで実行（フォールバック）
- 必要に応じて複数モデルで並列実行 → 結果リスト/合意度を返す

設計原則:
- 単一モデル依存にしない（タスクごとに異なる providers を並べる）
- 環境にどのプロバイダが構成されているかで挙動が変わるが、コードは共通
- プランで品質を変えない（ルーティング設定は全プラン共通）
"""
from __future__ import annotations

import contextvars
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.llm import PROVIDERS, LLMResponse

# ── コスト計測コンテキスト（スレッド・async タスク横断で安全） ─────────────
_ctx_user_id: contextvars.ContextVar[int] = contextvars.ContextVar("llm_user_id", default=0)
_ctx_req_id:  contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_request_id", default=None)


def set_llm_context(user_id: int, request_id: str | None = None) -> None:
    """LLM コスト計測用コンテキストを設定。API エンドポイント先頭 / worker スレッド先頭で呼ぶ。"""
    _ctx_user_id.set(user_id)
    _ctx_req_id.set(request_id)


# ── タスク → 候補 (provider, model) リスト ───────────────────────────────────
# 上から順に試す。先頭が「使える」プロバイダで成功すれば返す。
TASK_ROUTES: dict[str, list[tuple[str, str]]] = {
    # 一次情報からの構造化抽出（最重要 / 高品質モデル中心）
    "extraction": [
        ("anthropic", "claude-opus-4-6"),
        ("openai",    "gpt-5.4"),
        ("google",    "gemini-3.1-pro-preview"),
        ("anthropic", "claude-sonnet-4-6"),
    ],
    # 過去問の出題傾向分析（断定回避が必要 → 高品質モデル）
    "analysis": [
        ("anthropic", "claude-opus-4-6"),
        ("openai",    "gpt-5.4"),
        ("google",    "gemini-3.1-pro-preview"),
        ("anthropic", "claude-sonnet-4-6"),
    ],
    # 類題作成（創造性が要るが暴走させない）
    "practice_generation": [
        ("anthropic", "claude-opus-4-6"),
        ("openai",    "gpt-5.4"),
        ("anthropic", "claude-sonnet-4-6"),
        ("google",    "gemini-3.1-pro-preview"),
    ],
    # 指導用メモ（Markdown 出力）
    "notes_generation": [
        ("anthropic", "claude-opus-4-6"),
        ("openai",    "gpt-5.4"),
        ("google",    "gemini-3.1-pro-preview"),
    ],
    # 要約（軽量モデル可）
    "summarization": [
        ("anthropic", "claude-sonnet-4-6"),
        ("openai",    "gpt-5.4-mini"),
        ("google",    "gemini-3-flash-preview"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
    # 矛盾検証（複数結果の比較）
    "verification": [
        ("anthropic", "claude-opus-4-6"),
        ("openai",    "gpt-5.4"),
        ("google",    "gemini-3.1-pro-preview"),
    ],
    # 事実検証（抽出結果を原文と照合）
    # → プライマリ抽出と別系統モデルで独立チェックするため Anthropic を除外
    "fact_verification": [
        ("openai", "gpt-5.4"),
        ("google", "gemini-3.1-pro-preview"),
    ],

    # ── リサーチ Step 別ルート ──────────────────────────────────
    # Step A/B: 大学・学部の概要抽出（高速モデル優先、品質は十分）
    "step_ab": [
        ("openai",    "gpt-5.4"),
        ("anthropic", "claude-sonnet-4-6"),
        ("google",    "gemini-3.1-pro-preview"),
    ],
    # Step C: 学科詳細抽出（最高品質、解像度最大）
    "step_c": [
        ("anthropic", "claude-opus-4-6"),
        ("openai",    "gpt-5.4"),
        ("google",    "gemini-3.1-pro-preview"),
    ],
}


class NoProviderAvailable(RuntimeError):
    pass


def _log_llm_cost(task: str, provider: str, model: str, usage: dict) -> None:
    """LLM 呼び出しコストを api_usage_log に記録。失敗はサイレントに無視。"""
    try:
        from core.pricing import calculate_cost_usd
        from database import get_db

        tokens_in  = usage.get("input_tokens")  or usage.get("prompt_tokens")     or 0
        tokens_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        cache_read = usage.get("cache_read_tokens") or 0
        cost_usd   = calculate_cost_usd(provider, model, tokens_in, tokens_out, cache_read)

        user_id    = _ctx_user_id.get()
        request_id = _ctx_req_id.get()

        db = get_db()
        try:
            db.execute(
                """INSERT INTO api_usage_log
                   (user_id, request_id, task, provider, model, tokens_in, tokens_out, cache_read, cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, request_id, task, provider, model, tokens_in, tokens_out, cache_read, cost_usd),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # ログ失敗はサイレントに無視


def _resolve(task: str) -> list[tuple[str, str]]:
    return TASK_ROUTES.get(task, TASK_ROUTES["extraction"])


def call(
    task: str,
    system: str,
    user: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    use_cache: bool = True,
) -> LLMResponse:
    """指定タスクの優先順で、利用可能なプロバイダを順に試す。
    ネットワーク/レート制限エラー時は次にフォールバック。
    各試行を stdout にログする（どのプロバイダが実際に使われたか可視化）。
    `use_cache=True` かつ system が 2000 字以上なら Anthropic ではプロンプトキャッシングが効く。"""
    last_error: Exception | None = None
    attempts: list[str] = []
    for provider_name, model in _resolve(task):
        provider = PROVIDERS.get(provider_name)
        if not provider:
            attempts.append(f"{provider_name}:未登録")
            continue
        if not provider.is_available():
            attempts.append(f"{provider_name}:利用不可（APIキー未設定 or ライブラリ未インストール）")
            continue
        try:
            resp = provider.complete(system, user, model, max_tokens, temperature, use_cache=use_cache)
            # キャッシュヒット率をログ
            cache_info = ""
            if resp.usage:
                cr = resp.usage.get("cache_read_tokens") or 0
                cc = resp.usage.get("cache_creation_tokens") or 0
                if cr or cc:
                    cache_info = f" [cache read={cr} create={cc}]"
            print(f"[llm] task={task} → {provider_name}/{model} OK{cache_info}", flush=True)
            # コスト計測ログ（Railway ログ & DB 集計用）
            if resp.usage:
                _log_llm_cost(task, provider_name, model, resp.usage)
            return resp
        except Exception as e:
            err_msg = f"{type(e).__name__}: {str(e)[:200]}"
            attempts.append(f"{provider_name}/{model}:失敗 ({err_msg})")
            print(f"[llm] task={task} → {provider_name}/{model} failed: {err_msg}", flush=True)
            last_error = e
            continue
    print(f"[llm] task={task} 全プロバイダ失敗: {attempts}", flush=True)
    raise NoProviderAvailable(
        f"task={task} で利用可能なプロバイダがありません。"
        f"試行履歴: {attempts}. "
        f"環境変数 (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY) を確認してください。"
        + (f" 直近エラー: {last_error}" if last_error else "")
    )


def call_json(
    task: str,
    system: str,
    user: str,
    max_tokens: int = 2500,
    temperature: float = 0.2,
    use_cache: bool = True,
) -> tuple[dict, LLMResponse]:
    """`call` の JSON 抽出ラッパー。失敗時は `{"_raw": ...}` を返す。"""
    resp = call(task, system, user, max_tokens, temperature, use_cache=use_cache)
    text = resp.text

    # 1) ```json ... ``` / ``` ... ``` フェンス内のコンテンツを優先して試す（最長一致）
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        inner = fence.group(1).strip()
        try:
            return json.loads(inner), resp
        except json.JSONDecodeError:
            # フェンス内で {} 抽出を再試行
            s2 = inner.find("{")
            e2 = inner.rfind("}")
            if s2 != -1 and e2 != -1 and e2 > s2:
                try:
                    return json.loads(inner[s2:e2 + 1]), resp
                except json.JSONDecodeError:
                    pass

    # 2) 全体から最初の { と最後の } を抜いてパース
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        candidate = text[s:e + 1]
        try:
            return json.loads(candidate), resp
        except json.JSONDecodeError:
            pass

    return {"_raw": text}, resp


def call_multi(
    task: str,
    system: str,
    user: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    max_models: int = 3,
) -> list[LLMResponse]:
    """このタスクで利用可能な複数モデルに並列で投げ、全結果を返す。
    呼び出し側で「合意度」「矛盾検出」等のロジックを組める。"""
    candidates: list[tuple[str, str]] = []
    seen_providers: set[str] = set()
    for provider_name, model in _resolve(task):
        if provider_name in seen_providers:
            continue  # 同一プロバイダの重複モデル呼び出しは避ける
        provider = PROVIDERS.get(provider_name)
        if not provider or not provider.is_available():
            continue
        candidates.append((provider_name, model))
        seen_providers.add(provider_name)
        if len(candidates) >= max_models:
            break

    if not candidates:
        return []

    results: list[LLMResponse] = []
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        futures = {
            pool.submit(
                PROVIDERS[pn].complete, system, user, model, max_tokens, temperature
            ): (pn, model)
            for pn, model in candidates
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                pass
    return results


def status() -> dict:
    """構成状態を返す（ヘルスチェック用）。"""
    return {
        "providers_available": [
            name for name, p in PROVIDERS.items() if p.is_available()
        ],
        "tasks": list(TASK_ROUTES.keys()),
    }
