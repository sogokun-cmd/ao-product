"""
出典分類・「不明」フォールバック・サマリ計算

ao_research の生 result（university_data）を後処理し、
各factに provenance（出典URL/種別/年度）を付与する。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


# 公式ドメインの判定（.ac.jp + 国公立大の go.jp 等）
_OFFICIAL_DOMAIN_SUFFIXES = (".ac.jp", ".go.jp")
# 補助情報源（公式ではないが信頼度はある程度ある）
_AGGREGATOR_DOMAINS = (
    "passnavi.evidus.com", "passnavi.com", "shingakunet.com",
    "manabi.benesse.ne.jp", "toshin.com", "wakatte.tv",
    "kawai-juku.ac.jp", "yozemi.ac.jp",
)


def classify_source(url: str) -> str:
    """戻り値: 'official_pdf' | 'official_html' | 'aggregator' | 'unknown'"""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return "unknown"
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "unknown"
    is_pdf = url.lower().split("?", 1)[0].endswith(".pdf")
    if any(host == s.lstrip(".") or host.endswith(s) for s in _OFFICIAL_DOMAIN_SUFFIXES):
        return "official_pdf" if is_pdf else "official_html"
    if any(host == d or host.endswith("." + d) for d in _AGGREGATOR_DOMAINS):
        return "aggregator"
    return "unknown"


_UNKNOWN_TOKENS = ("情報なし", "要確認", "不明", "未公表", "未発表", "非公開", "n/a", "—", "-")


def _is_unknown_value(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip().lower()
        return s == "" or any(t in s for t in _UNKNOWN_TOKENS)
    if isinstance(v, (list, tuple, set)):
        return len(v) == 0 or all(_is_unknown_value(x) for x in v)
    return False


_YEAR_RE = re.compile(r"(20\d{2})")


def _extract_year(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    m = _YEAR_RE.search(text)
    return m.group(1) if m else None


def _collect_provenance_for_uni(u: dict, ud: dict) -> list[dict]:
    """1大学（step_c[0]）に紐づく出典URLを収集して種別タグ付け。
    新スキーマ（official_sources / field_sources / references）と旧スキーマ
    （url / official_url / source_url / sources）の両方に対応する。"""
    out: list[dict] = []
    seen = set()

    def _add(url, year_hint=None, label=None):
        if not url or not isinstance(url, str):
            return
        if url in seen:
            return
        seen.add(url)
        entry = {
            "url": url,
            "type": classify_source(url),
            "year": year_hint or _extract_year(url),
        }
        if label:
            entry["label"] = label
        out.append(entry)

    # 新スキーマ: official_sources = [{label, url}]
    for src in (u.get("official_sources") or []):
        if isinstance(src, dict):
            _add(src.get("url"), label=src.get("label"))
        elif isinstance(src, str):
            _add(src)

    # 新スキーマ: field_sources = {field: url}
    for field, url in (u.get("field_sources") or {}).items():
        if isinstance(url, str):
            _add(url, label=field)

    # 新スキーマ: references = {field: [{source_url, year, source_label}, ...]}
    for field, refs in (u.get("references") or {}).items():
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, dict):
                    _add(r.get("source_url"), year_hint=r.get("year"), label=r.get("source_label"))

    # 旧スキーマ: 大学の URL 群
    for key in ("url", "official_url", "source_url"):
        _add(u.get(key))
    for src in (u.get("sources") or []):
        if isinstance(src, dict):
            _add(src.get("url"), src.get("year"))
        elif isinstance(src, str):
            _add(src)

    # ud.step_a (大学全体ページ) — 新旧両対応
    step_a = ud.get("step_a") or {}
    for src in (step_a.get("official_sources") or []):
        if isinstance(src, dict):
            _add(src.get("url"), label=src.get("label"))
    for field, url in (step_a.get("field_sources") or {}).items():
        if isinstance(url, str):
            _add(url, label=f"大学共通: {field}")
    _add(step_a.get("url") or step_a.get("official_url"))

    # step_b（学部）の出典も収集
    step_b = ud.get("step_b") or {}
    for fac in (step_b.get("faculties") or []):
        if not isinstance(fac, dict):
            continue
        for src in (fac.get("official_sources") or []):
            if isinstance(src, dict):
                _add(src.get("url"), label=src.get("label"))
        for field, url in (fac.get("field_sources") or {}).items():
            if isinstance(url, str):
                _add(url, label=f"学部: {field}")

    # PDF 取得ログ（年度別）
    log = ud.get("data_collection_log") or {}
    for src_name, info in log.items():
        if isinstance(info, dict):
            for k in ("url", "pdf_url", "source_url"):
                if k in info:
                    _add(info[k])
        elif isinstance(info, str) and info.startswith("http"):
            _add(info)
    return out


def _annotate_value(val, all_provenance: list[dict], field_url: str | None = None) -> dict:
    """単一フィールドを {value, label, confidence, provenance} 形式に変換。
    field_url が渡されたら、そのURLを provenance の先頭に優先表示する。"""
    if _is_unknown_value(val):
        return {
            "value": None,
            "label": "不明",
            "confidence": "unknown",
            "provenance": [],
        }

    # field_url が公式URLなら最優先で先頭に
    provenance = list(all_provenance)
    if field_url and isinstance(field_url, str) and field_url.startswith(("http://", "https://")):
        field_entry = {
            "url": field_url,
            "type": classify_source(field_url),
            "year": _extract_year(field_url),
        }
        # 重複除去
        provenance = [p for p in provenance if p.get("url") != field_url]
        provenance.insert(0, field_entry)

    has_official = any(p["type"] in ("official_pdf", "official_html") for p in provenance)
    has_aggregator = any(p["type"] == "aggregator" for p in provenance)
    if has_official:
        confidence = "high"
    elif has_aggregator:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "value": val,
        "label": None,
        "confidence": confidence,
        "provenance": provenance,
    }


# 注釈対象のフィールド
_FACT_FIELDS = [
    # 基本情報
    "campus", "formal_ao_name",
    # 総合型選抜 基本情報
    "quota", "gpa_requirement", "external_exam_requirements", "application_type",
    # スケジュール
    "application_period", "selection_schedule", "announcement_date",
    # 選考方法
    "selection_methods", "selection_phase_1", "selection_phase_2",
    "evaluation_criteria", "submitted_documents",
    # 出願資格
    "eligibility",
    # AP / 特徴
    "admission_policy", "features",
    # データ
    "ratio_history", "difficulty_facts",
    # v2 ノウハウ由来（独自性・素材・想定テーマ）
    "uniqueness_layers", "specific_materials",
    "key_phrases", "avoided_phrases",
    "interview_topics", "match_anchors",
    # 旧フィールド（後方互換）
    "selection_detail", "documents_required",
]


def annotate_facts(university_data: dict) -> dict:
    """university_data に provenance を付与した「強化版 result」を返す。
    元の構造は壊さず、追加で `_annotated` キーを各大学に付ける。"""
    if not isinstance(university_data, dict):
        return university_data
    ud = dict(university_data)
    unis = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
    annotated_unis = []
    for u in unis:
        if not isinstance(u, dict):
            annotated_unis.append(u)
            continue
        provenance = _collect_provenance_for_uni(u, ud)
        field_sources = u.get("field_sources") or {}
        annotated = {}
        for f in _FACT_FIELDS:
            annotated[f] = _annotate_value(
                u.get(f), provenance, field_url=field_sources.get(f),
            )
        # 元データに _annotated を付ける（既存UIを壊さない）
        u_copy = dict(u)
        u_copy["_annotated"] = annotated
        u_copy["_provenance_all"] = provenance
        annotated_unis.append(u_copy)

    if "step_c" in ud and isinstance(ud["step_c"], dict):
        ud["step_c"] = dict(ud["step_c"])
        ud["step_c"]["universities"] = annotated_unis
    else:
        ud["universities"] = annotated_unis
    return ud


def summarize_sources(university_data: dict) -> dict:
    """全ての provenance を集計。"""
    counts = {"official_pdf": 0, "official_html": 0, "aggregator": 0, "unknown": 0, "total": 0}
    seen = set()
    unis = (university_data.get("step_c") or {}).get("universities") or university_data.get("universities") or []
    for u in unis:
        for p in (u.get("_provenance_all") if isinstance(u, dict) else None) or []:
            if p["url"] in seen:
                continue
            seen.add(p["url"])
            counts[p["type"]] = counts.get(p["type"], 0) + 1
            counts["total"] += 1
    return counts


def count_unknowns(university_data: dict) -> int:
    n = 0
    unis = (university_data.get("step_c") or {}).get("universities") or university_data.get("universities") or []
    for u in unis:
        ann = (u.get("_annotated") if isinstance(u, dict) else None) or {}
        for v in ann.values():
            if isinstance(v, dict) and v.get("label") == "不明":
                n += 1
    return n
