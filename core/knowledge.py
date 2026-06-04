"""
大学ナレッジの蓄積と合成。

同一（大学×学部×学科×入試方式）に対するリサーチ結果をフィールド単位で蓄積。
新しいリサーチが完了するたびに、そのフィールドが「不明」だった過去値を上書きし、
逆に新結果で「不明」に戻ったフィールドは過去値を温存する。

データモデル（university_knowledge.fields_json）:
    {
        "<field_name>": {
            "value":             <任意>,
            "confidence":        "high" | "medium" | "low" | "unknown",
            "source":            "<出典URL or 表示用ラベル>",
            "source_type":       "official_pdf" | "official_html" | "aggregator" | "unknown",
            "updated_at":        "YYYY-MM-DD HH:MM:SS",
            "contributor_user_id": <int or null>,
            "request_id":        "<UUID>"
        },
        ...
    }

マージ戦略（新 vs 既存）:
    - 新が 不明 → 既存を温存
    - 新が非 不明 & 既存が 不明/未登録 → 新で上書き
    - 双方非 不明 → confidence + source_type を比較 + 鮮度を考慮して高い方を採用
    - 既存が STALE_MONTHS 以上古い → 新データが優先
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Iterable

from database import get_db

STALE_MONTHS = int(os.environ.get("KNOWLEDGE_STALE_MONTHS", "12"))


# 共有ナレッジ化するトップレベル「フィールド名」。UI で表示される主要情報のみ。
# 学内メタ情報（保存メモ・生徒紐付け等）は当然含めない。
KNOWLEDGE_FIELDS: tuple[str, ...] = (
    "campus", "formal_ao_name",
    "quota", "quota_history",
    "ratio_history", "applicants_history", "accepted_history",
    "application_period", "selection_schedule", "announcement_date",
    "selection_methods", "selection_phase_1", "selection_phase_2",
    "evaluation_criteria", "submitted_documents",
    "eligibility", "application_type",
    "gpa_requirement", "external_exam_requirements",
    "admission_policy", "features",
    "difficulty_facts", "department_detail",
    "uniqueness_layers", "specific_materials",
    "key_phrases", "avoided_phrases",
    "interview_topics", "match_anchors",
    "official_sources", "pdf_confirmed",
)

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
_SOURCE_TYPE_RANK = {"official_pdf": 3, "official_html": 2, "aggregator": 1, "unknown": 0}


def _is_unknown(v: Any) -> bool:
    """値が実質「不明」相当か判定（文字列・配列・辞書を考慮）"""
    if v is None:
        return True
    if isinstance(v, str):
        t = v.strip()
        if not t:
            return True
        # 厳格一致（理由付き不明「不明（記載なし）」等は非不明扱い）
        return t in ("不明", "情報なし", "要確認", "—", "-", "n/a", "N/A")
    if isinstance(v, list):
        return len(v) == 0 or all(_is_unknown(x) for x in v)
    if isinstance(v, dict):
        if not v:
            return True
        return all(_is_unknown(x) for x in v.values())
    return False


def _infer_source_type(source_url: str) -> str:
    if not source_url:
        return "unknown"
    s = source_url.lower()
    if s.endswith(".pdf") or "/pdf" in s:
        return "official_pdf"
    if ".ac.jp" in s or ".edu" in s or ".go.jp" in s:
        return "official_html"
    return "aggregator"


def _pick_source_for_field(u: dict, field: str) -> tuple[str, str]:
    """uni dict から該当フィールドの出典URLと出典タイプを推定。"""
    field_sources = u.get("field_sources") or {}
    url = ""
    if isinstance(field_sources, dict):
        v = field_sources.get(field)
        if isinstance(v, str) and v:
            url = v
    if not url:
        # fallback: official_sources の最初のもの
        official = u.get("official_sources") or []
        if isinstance(official, list) and official:
            first = official[0]
            if isinstance(first, dict):
                url = first.get("url") or ""
    return url, _infer_source_type(url)


def _new_entry(value: Any, source: str, source_type: str, user_id: int | None, request_id: str) -> dict:
    confidence = {
        "official_pdf": "high",
        "official_html": "high",
        "aggregator": "medium",
        "unknown": "low",
    }.get(source_type, "low")
    from datetime import datetime
    return {
        "value": value,
        "confidence": confidence,
        "source": source,
        "source_type": source_type,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "contributor_user_id": user_id,
        "request_id": request_id,
    }


def _is_stale(entry: dict, stale_months: int = STALE_MONTHS) -> bool:
    """エントリの updated_at が stale_months ヶ月以上前なら True（鮮度切れ）。"""
    updated_at = entry.get("updated_at", "")
    if not updated_at:
        return False
    try:
        dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() - dt > timedelta(days=stale_months * 30)
    except Exception:
        return False


def _is_better(new_entry: dict, existing: dict) -> bool:
    """新エントリが既存より優先されるか判定（鮮度を考慮）"""
    # 鮮度差がある場合はフレッシュなデータを優先
    new_stale = _is_stale(new_entry)
    ex_stale  = _is_stale(existing)
    if ex_stale and not new_stale:
        return True
    if not ex_stale and new_stale:
        return False
    # 同鮮度 → confidence + source_type ランクで判定
    new_rank = _CONFIDENCE_RANK.get(new_entry.get("confidence", "low"), 0) * 10 + \
               _SOURCE_TYPE_RANK.get(new_entry.get("source_type", "unknown"), 0)
    ex_rank  = _CONFIDENCE_RANK.get(existing.get("confidence", "low"), 0) * 10 + \
               _SOURCE_TYPE_RANK.get(existing.get("source_type", "unknown"), 0)
    if new_rank > ex_rank:
        return True
    if new_rank == ex_rank:
        return new_entry.get("updated_at", "") >= existing.get("updated_at", "")
    return False


def upsert_from_universities(
    unis: Iterable[dict],
    request_id: str,
    user_id: int | None = None,
    university_override: str | None = None,
    admission_method_override: str | None = None,
) -> int:
    """universities 配列を走査し、各 (大学, 学部, 学科, 方式) ごとに知識を upsert。
    戻り値: 更新/作成されたレコード件数。"""
    db = get_db()
    try:
        updated = 0
        for u in unis:
            if not isinstance(u, dict):
                continue
            uni  = university_override or u.get("university", "")
            fac  = u.get("faculty", "")
            dep  = u.get("department", "")
            meth = admission_method_override or u.get("admission_method", "") or u.get("formal_ao_name", "")
            if not uni:
                continue

            new_fields: dict[str, dict] = {}
            for field in KNOWLEDGE_FIELDS:
                v = u.get(field)
                if _is_unknown(v):
                    continue
                src, src_type = _pick_source_for_field(u, field)
                new_fields[field] = _new_entry(v, src, src_type, user_id, request_id)

            row = db.execute(
                """SELECT id, fields_json, run_count, contributor_ids
                     FROM university_knowledge
                    WHERE university=? AND faculty=? AND department=? AND admission_method=?""",
                (uni, fac, dep, meth),
            ).fetchone()

            if row:
                existing_fields = json.loads(row["fields_json"] or "{}")
                new_run_count = (row["run_count"] or 0) + 1
                for k, new_entry in new_fields.items():
                    if k not in existing_fields or _is_unknown(existing_fields[k].get("value")):
                        existing_fields[k] = new_entry
                    elif _is_better(new_entry, existing_fields[k]):
                        existing_fields[k] = new_entry
                    # 同じ値が複数回確認されたら confidence を high に昇格
                    if (k in existing_fields
                            and not _is_unknown(existing_fields[k].get("value"))
                            and str(existing_fields[k].get("value")) == str(new_entry.get("value"))
                            and new_run_count >= 3
                            and existing_fields[k].get("confidence") != "high"):
                        existing_fields[k]["confidence"] = "high"
                # contributor_ids 追記
                try:
                    contribs = json.loads(row["contributor_ids"] or "[]")
                except Exception:
                    contribs = []
                if user_id is not None and user_id not in contribs:
                    contribs.append(user_id)
                db.execute(
                    """UPDATE university_knowledge
                          SET fields_json=?, run_count=run_count+1,
                              contributor_ids=?, last_request_id=?,
                              updated_at=datetime('now')
                        WHERE id=?""",
                    (json.dumps(existing_fields, ensure_ascii=False),
                     json.dumps(contribs, ensure_ascii=False),
                     request_id, row["id"]),
                )
            else:
                contribs = [user_id] if user_id is not None else []
                db.execute(
                    """INSERT INTO university_knowledge
                       (university, faculty, department, admission_method,
                        fields_json, run_count, contributor_ids, last_request_id)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                    (uni, fac, dep, meth,
                     json.dumps(new_fields, ensure_ascii=False),
                     json.dumps(contribs, ensure_ascii=False),
                     request_id),
                )
            updated += 1
        db.commit()
        return updated
    finally:
        db.close()


def get_knowledge(
    university: str,
    faculty: str = "",
    department: str = "",
    admission_method: str = "",
) -> dict | None:
    """蓄積されている知識を取得。無ければ None。"""
    db = get_db()
    try:
        row = db.execute(
            """SELECT id, fields_json, run_count, contributor_ids,
                      last_request_id, created_at, updated_at
                 FROM university_knowledge
                WHERE university=? AND faculty=? AND department=? AND admission_method=?""",
            (university, faculty, department, admission_method),
        ).fetchone()
        if not row:
            return None
        return {
            "id":               row["id"],
            "fields":           json.loads(row["fields_json"] or "{}"),
            "run_count":        row["run_count"],
            "contributor_ids":  json.loads(row["contributor_ids"] or "[]"),
            "last_request_id":  row["last_request_id"],
            "created_at":       row["created_at"],
            "updated_at":       row["updated_at"],
        }
    finally:
        db.close()


def fill_gaps_from_knowledge(uni: dict, knowledge_fields: dict) -> int:
    """リサーチ結果の 不明 フィールドを知識で埋める。戻り値: 補完したフィールド数。"""
    if not knowledge_fields:
        return 0
    n = 0
    uni.setdefault("knowledge_fills", {})
    for field, entry in knowledge_fields.items():
        current = uni.get(field)
        if _is_unknown(current) and not _is_unknown(entry.get("value")):
            uni[field] = entry["value"]
            uni["knowledge_fills"][field] = {
                "source":      entry.get("source", ""),
                "source_type": entry.get("source_type", "unknown"),
                "updated_at":  entry.get("updated_at", ""),
                "request_id":  entry.get("request_id", ""),
                "is_stale":    _is_stale(entry),
            }
            n += 1
    return n


def get_knowledge_hierarchical(
    university: str,
    faculty: str = "",
    department: str = "",
    admission_method: str = "",
) -> dict | None:
    """階層的にナレッジを検索してマージして返す。
    優先順位: 大学レベル < 学部レベル < 学科レベル < 完全一致（後の方が上書き）。
    各レベルで値が見つかれば下位（より曖昧な）レベルの同フィールドを上書きする。
    """
    db = get_db()
    try:
        search_keys: list[tuple] = [(university, "", "", "")]
        if faculty:
            search_keys.append((university, faculty, "", ""))
        if faculty and department:
            search_keys.append((university, faculty, department, ""))
            if admission_method:
                search_keys.append((university, faculty, department, admission_method))

        merged_fields: dict[str, Any] = {}
        run_count_total = 0
        last_updated = ""

        for (uni, fac, dep, meth) in search_keys:
            row = db.execute(
                """SELECT fields_json, run_count, updated_at
                     FROM university_knowledge
                    WHERE university=? AND faculty=? AND department=? AND admission_method=?""",
                (uni, fac, dep, meth),
            ).fetchone()
            if not row:
                continue
            fields = json.loads(row["fields_json"] or "{}")
            run_count_total += row["run_count"]
            if row["updated_at"] > last_updated:
                last_updated = row["updated_at"]
            for k, v in fields.items():
                if not _is_unknown(v.get("value")):
                    merged_fields[k] = v  # 後（より specific）が上書き

        if not merged_fields:
            return None
        return {
            "fields":    merged_fields,
            "run_count": run_count_total,
            "updated_at": last_updated,
        }
    finally:
        db.close()


def list_knowledge_summary(limit: int = 100) -> list[dict]:
    """蓄積済み大学のサマリ一覧（管理画面・デバッグ用）"""
    db = get_db()
    try:
        rows = db.execute(
            """SELECT id, university, faculty, department, admission_method,
                      run_count, contributor_ids, updated_at, fields_json
                 FROM university_knowledge
                ORDER BY updated_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            fields = json.loads(r["fields_json"] or "{}")
            filled = 0
            stale_count = 0
            for v in fields.values():
                if isinstance(v, dict):
                    if not _is_unknown(v.get("value")):
                        filled += 1
                    if _is_stale(v):
                        stale_count += 1
            out.append({
                "id":               r["id"],
                "university":       r["university"],
                "faculty":          r["faculty"],
                "department":       r["department"],
                "admission_method": r["admission_method"],
                "run_count":        r["run_count"],
                "contributors":     len(json.loads(r["contributor_ids"] or "[]")),
                "filled_fields":    filled,
                "stale_fields_count": stale_count,
                "updated_at":       r["updated_at"],
            })
        return out
    finally:
        db.close()
