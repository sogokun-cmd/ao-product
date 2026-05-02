"""
複数モデル結果の合意度・矛盾検出。

llm_router.call_multi() で取得した複数の LLM 応答を突き合わせ、
- フィールドごとの合意値 / 矛盾
- 全体の信頼度スコア
を返す。単一モデル依存を避け、情報品質を上げるための検証層。

UI上では「高品質な一次情報リサーチ」として価値を見せる方針。
モデル名や生の比較結果は原則ユーザーに露出しない。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from core import llm_router
from core.llm import LLMResponse


@dataclass
class VerificationResult:
    consensus: dict          # フィールド → 合意値（過半数一致）
    contradictions: dict     # フィールド → [各モデルの値]（不一致）
    confidence: float        # 0.0..1.0（全フィールド平均の合意率）
    sources: list[dict] = field(default_factory=list)  # [{provider, model}]
    raw: list[dict] = field(default_factory=list)      # 各モデルの生 JSON

    def to_dict(self) -> dict:
        return {
            "consensus": self.consensus,
            "contradictions": self.contradictions,
            "confidence": self.confidence,
            "sources": self.sources,
        }


def _is_meaningful(v) -> bool:
    """検証対象にする価値のある値かを判定。dict / list は再帰的に中身を見る。"""
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        return bool(s) and s not in ("情報なし", "不明", "要確認")
    if isinstance(v, (int, float, bool)):
        return True
    if isinstance(v, (list, tuple)):
        return any(_is_meaningful(x) for x in v)
    if isinstance(v, dict):
        return any(_is_meaningful(x) for x in v.values())
    return False


def _extract_json(text: str) -> dict | None:
    """LLM テキスト応答から JSON オブジェクトを抽出。"""
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _normalize(v):
    """比較のための値正規化。文字列は前後空白・大小無視、リストはソート済みタプル化。"""
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, list):
        try:
            return tuple(sorted(_normalize(x) for x in v))
        except TypeError:
            return tuple(_normalize(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _normalize(val)) for k, val in v.items()))
    return v


def compare(parsed_results: list[dict]) -> VerificationResult:
    """各モデルの JSON 応答リストを比較して合意 / 矛盾を返す。

    - フィールド単位で過半数一致していれば consensus に採用
    - 2モデル以上で値が異なる場合は contradictions に記録
    - 欠損フィールドは投票対象から除外
    """
    if not parsed_results:
        return VerificationResult(consensus={}, contradictions={}, confidence=0.0)

    all_keys: set[str] = set()
    for r in parsed_results:
        all_keys.update(r.keys())

    consensus: dict = {}
    contradictions: dict = {}
    agree_ratios: list[float] = []

    for key in all_keys:
        values = [r[key] for r in parsed_results if key in r]
        if not values:
            continue

        buckets: dict = {}
        for v in values:
            nk = _normalize(v)
            try:
                hashable = nk if isinstance(nk, (str, int, float, bool, tuple, type(None))) else json.dumps(nk, sort_keys=True, default=str)
            except TypeError:
                hashable = json.dumps(v, sort_keys=True, default=str)
            buckets.setdefault(hashable, []).append(v)

        top_key, top_vals = max(buckets.items(), key=lambda kv: len(kv[1]))
        agree_ratio = len(top_vals) / len(values)
        agree_ratios.append(agree_ratio)

        if len(buckets) == 1:
            consensus[key] = top_vals[0]
        elif agree_ratio > 0.5:
            consensus[key] = top_vals[0]
            contradictions[key] = [r.get(key) for r in parsed_results]
        else:
            contradictions[key] = [r.get(key) for r in parsed_results]

    confidence = sum(agree_ratios) / len(agree_ratios) if agree_ratios else 0.0

    return VerificationResult(
        consensus=consensus,
        contradictions=contradictions,
        confidence=round(confidence, 3),
    )


def verify_facts(
    facts: dict,
    source_text: str,
    fields: list[str] | None = None,
    source_max_chars: int = 10000,
    max_tokens: int = 1500,
) -> dict:
    """プライマリモデルが抽出した事実を、別系統モデル（GPT-4o + Gemini Pro）で原文と照合する。

    入力:
        facts: プライマリ抽出結果の JSON dict
        source_text: 照合対象の原文（先頭 source_max_chars 字のみ使用）
        fields: 検証対象フィールド名のリスト（None なら facts の全スカラー値）

    出力:
        {
          "field_name": {
            "supported": True|False|None,  # 2モデル合意で True/False、割れたら None
            "evidences": [... 各モデルが引用した根拠 ...],
            "verifiers": [{"provider", "model"}, ...]
          },
          ...
        }
    """
    if not facts:
        return {}

    if fields is None:
        fields = [k for k, v in facts.items()
                  if not k.startswith("_") and _is_meaningful(v)]

    if not fields:
        return {}

    target_facts = {k: facts.get(k) for k in fields if k in facts}
    if not target_facts:
        return {}

    system = (
        "あなたは事実照合の専門家です。"
        "提示された「原文抜粋」を唯一の根拠として、与えられた各フィールドの値が"
        "原文で明確に支持されるかを判定します。推測で補完しないこと。"
        "値が配列やオブジェクトの場合は主要な要素がすべて原文で支持されるかで判定し、"
        "1件でも原文に根拠がない要素があれば supported=false としてください。"
        "原文で言及されていない場合は supported=false とし evidence は空文字列にすること。"
        "必ず JSON のみを出力すること。"
    )
    user = (
        f"【原文抜粋】\n{source_text[:source_max_chars]}\n\n"
        f"【検証対象フィールド】\n{json.dumps(target_facts, ensure_ascii=False, indent=2)}\n\n"
        '出力形式: {"<field>": {"supported": true|false, "evidence": "原文からの短い引用"}}'
    )

    responses = llm_router.call_multi(
        "fact_verification", system, user,
        max_tokens=max_tokens, max_models=2,
    )

    parsed_per_model: list[tuple[dict, dict]] = []
    for r in responses:
        obj = _extract_json(r.text)
        if obj is None:
            continue
        parsed_per_model.append(
            (obj, {"provider": r.provider, "model": r.model})
        )

    if not parsed_per_model:
        return {k: {"supported": None, "evidences": [], "verifiers": []} for k in target_facts}

    out: dict = {}
    for field_name in target_facts:
        votes: list[bool] = []
        evidences: list[str] = []
        verifiers: list[dict] = []
        for obj, src in parsed_per_model:
            entry = obj.get(field_name)
            if not isinstance(entry, dict):
                continue
            sup = entry.get("supported")
            if isinstance(sup, bool):
                votes.append(sup)
                verifiers.append(src)
            ev = entry.get("evidence")
            if isinstance(ev, str) and ev.strip():
                evidences.append(ev.strip())

        if not votes:
            supported = None
        elif all(v for v in votes):
            supported = True
        elif not any(v for v in votes):
            supported = False
        else:
            supported = None  # 割れた

        out[field_name] = {
            "supported": supported,
            "evidences": evidences,
            "verifiers": verifiers,
        }

    return out


def verify(
    task: str,
    system: str,
    user: str,
    max_tokens: int = 2000,
    max_models: int = 3,
) -> VerificationResult:
    """複数モデルで並列抽出し、結果を突き合わせる。

    プロバイダが1つしか構成されていない場合は単一結果を返す（confidence=1.0 扱い）。
    利用可能プロバイダがゼロなら空結果を返す（呼び出し側でフォールバック）。
    """
    responses: list[LLMResponse] = llm_router.call_multi(
        task, system, user, max_tokens=max_tokens, max_models=max_models,
    )
    parsed: list[dict] = []
    sources: list[dict] = []
    for r in responses:
        obj = _extract_json(r.text)
        if obj is None:
            continue
        parsed.append(obj)
        sources.append({"provider": r.provider, "model": r.model})

    if not parsed:
        return VerificationResult(consensus={}, contradictions={}, confidence=0.0)

    if len(parsed) == 1:
        return VerificationResult(
            consensus=parsed[0],
            contradictions={},
            confidence=1.0,
            sources=sources,
            raw=parsed,
        )

    result = compare(parsed)
    result.sources = sources
    result.raw = parsed
    return result
