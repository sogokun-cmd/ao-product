"""
ao_research を呼び出して research_requests / research_results に保存。
provenance 注釈・「不明」フォールバック・フラグ算出も担う。
"""

import sys
import os
import re
import json
import uuid
from pathlib import Path
from typing import Literal

from database import get_db
from core.provenance import (
    annotate_facts, summarize_sources, count_unknowns,
)

# ao_research.py はプロジェクトルートに同梱
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

JobStatus = Literal["pending", "running", "done", "error"]


# ── DB ヘルパー ──────────────────────────────────────────────────────────────

def _row_to_request(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {
        "id":               row["id"],
        "user_id":          row["user_id"],
        "team_id":          row["team_id"],
        "university":       row["university"],
        "faculty":          row["faculty"],
        "department":       row["department"],
        "admission_method": row["admission_method"],
        "keywords":         row["keywords"],
        "pdf_url":          row["pdf_url"],
        "status":           row["status"],
        "progress":         json.loads(row["progress"] or "[]"),
        "error":            row["error"],
        "created_at":       row["created_at"],
        "updated_at":       row["updated_at"],
        "tags":             json.loads(row["tags"] or "[]") if "tags" in keys else [],
    }


def create_request(
    user_id: int,
    university: str,
    faculty: str = "",
    department: str = "",
    admission_method: str = "",
    keywords: str = "",
    pdf_url: str = "",
    pdf_text: str = "",
    team_id: int | None = None,
) -> str:
    rid = str(uuid.uuid4())
    db = get_db()
    try:
        # 結果キャッシュ: 同一条件の done リクエストが N 日以内にあれば結果を流用
        # ただし「失敗・空結果」は再利用しない（unknown_count > 10 or universities 空）
        _reuse_days = int(os.environ.get("RESEARCH_REUSE_DAYS", "30"))
        _reuse_unknown_threshold = int(os.environ.get("RESEARCH_REUSE_UNKNOWN_MAX", "10"))
        reused_from = None
        if _reuse_days > 0:
            _cached = db.execute(
                """SELECT rr.id, rres.result_json, rres.flags_json,
                          rres.source_summary, rres.unknown_count
                     FROM research_requests rr
                     JOIN research_results rres ON rres.request_id = rr.id
                    WHERE rr.status = 'done'
                      AND rr.university = ?
                      AND IFNULL(rr.faculty,'') = IFNULL(?,'')
                      AND IFNULL(rr.department,'') = IFNULL(?,'')
                      AND IFNULL(rr.admission_method,'') = IFNULL(?,'')
                      AND (strftime('%s','now') - strftime('%s', rr.updated_at)) < ?
                      AND IFNULL(rres.unknown_count, 999) <= ?
                    ORDER BY rr.updated_at DESC
                    LIMIT 1""",
                (university, faculty, department, admission_method,
                 _reuse_days * 86400, _reuse_unknown_threshold),
            ).fetchone()
            if _cached:
                # universities 配列が空でないことを追加確認
                try:
                    _result = json.loads(_cached["result_json"])
                    _ud = _result.get("university_data", {}) or {}
                    _unis_check = (_ud.get("step_c") or {}).get("universities") or _ud.get("universities") or []
                    # universities に少なくとも1件あり、かつ主要フィールドが埋まっていることを確認
                    _has_content = False
                    for _u in _unis_check:
                        for _f in ("quota", "application_period", "eligibility"):
                            _v = _u.get(_f)
                            if _v and str(_v).strip() not in ("不明", "情報なし", ""):
                                _has_content = True
                                break
                        if _has_content:
                            break
                    if _has_content:
                        reused_from = _cached["id"]
                except Exception:
                    pass
        db.execute(
            """INSERT INTO research_requests
               (id, user_id, team_id, university, faculty, department,
                admission_method, keywords, pdf_url, pdf_text, status, progress)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, user_id, team_id, university, faculty, department,
             admission_method, keywords, pdf_url, pdf_text,
             "done" if reused_from else "pending",
             json.dumps([f"キャッシュ再利用（過去30日以内の同一条件リサーチから）"] if reused_from else [], ensure_ascii=False)),
        )
        if reused_from:
            db.execute(
                """INSERT INTO research_results
                     (request_id, result_json, flags_json, source_summary, unknown_count)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     result_json=excluded.result_json,
                     flags_json=excluded.flags_json,
                     source_summary=excluded.source_summary,
                     unknown_count=excluded.unknown_count""",
                (rid,
                 _cached["result_json"], _cached["flags_json"],
                 _cached["source_summary"], _cached["unknown_count"]),
            )
        db.commit()
    finally:
        db.close()
    return rid


def get_request(request_id: str) -> dict | None:
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM research_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return _row_to_request(row) if row else None
    finally:
        db.close()


def get_result(request_id: str) -> dict | None:
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM research_results WHERE request_id = ?", (request_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "request_id":     row["request_id"],
            "result":         json.loads(row["result_json"]),
            "flags":          json.loads(row["flags_json"] or "{}"),
            "source_summary": json.loads(row["source_summary"] or "{}"),
            "unknown_count":  row["unknown_count"],
        }
    finally:
        db.close()


def list_requests_for_user(user_id: int, limit: int = 100) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute(
            """SELECT r.id, r.university, r.faculty, r.department, r.admission_method,
                      r.status, r.created_at, r.updated_at, r.tags,
                      res.flags_json, res.unknown_count, res.source_summary
               FROM research_requests r
               LEFT JOIN research_results res ON res.request_id = r.id
               WHERE r.user_id = ?
               ORDER BY r.created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["flags"] = json.loads(d.pop("flags_json") or "{}")
            except json.JSONDecodeError:
                d["flags"] = {}
            try:
                d["source_summary"] = json.loads(d.get("source_summary") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["source_summary"] = {}
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            out.append(d)
        return out
    finally:
        db.close()


def update_tags(request_id: str, user_id: int, tags: list[str]) -> bool:
    """タグを更新。成功時 True。アクセス権なし時 False。"""
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE research_requests SET tags=?, updated_at=datetime('now') WHERE id=? AND user_id=?",
            (json.dumps(tags, ensure_ascii=False), request_id, user_id),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def _update_status(request_id: str, status: JobStatus) -> None:
    db = get_db()
    try:
        db.execute(
            "UPDATE research_requests SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, request_id),
        )
        db.commit()
    finally:
        db.close()


def _append_progress(request_id: str, message: str) -> None:
    db = get_db()
    try:
        row = db.execute(
            "SELECT progress FROM research_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return
        arr = json.loads(row["progress"] or "[]")
        arr.append(message)
        db.execute(
            "UPDATE research_requests SET progress=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(arr, ensure_ascii=False), request_id),
        )
        db.commit()
    finally:
        db.close()


def _set_error(request_id: str, error: str) -> None:
    db = get_db()
    try:
        db.execute(
            """UPDATE research_requests
               SET error=?, status='error', updated_at=datetime('now')
               WHERE id=?""",
            (error, request_id),
        )
        db.commit()
    finally:
        db.close()


# ── フラグ抽出（既存ロジックを維持） ────────────────────────────────────────

_PRESENTATION_KWS = ["プレゼン", "プレゼンテーション", "発表"]
_ESSAY_KWS = ["小論文", "論文"]
_INTERVIEW_KWS = ["面接", "面談", "口頭試問"]
_ENGLISH_KWS = ["英検", "TOEIC", "TOEFL", "IELTS", "GTEC", "ケンブリッジ", "英語外部", "英語資格"]
_ACTIVITY_KWS = ["活動実績", "課外活動", "実績", "課題研究", "高校時代の活動"]
_ACTIVITY_OPT_KWS = ["不問", "問わない", "任意"]


def _norm_text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " / ".join(str(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_norm_text(x) for x in v.values())
    return ""


def _contains_any(text: str, kws: list[str]) -> bool:
    return any(k in text for k in kws)


def _compute_flags(annotated_data: dict) -> dict:
    flags: dict = {
        "interview": False, "presentation": False, "essay": False,
        "interview_only": False, "no_presentation": True,
        "english_required": False, "activity_required": False,
        "gpa_min": None,
    }
    try:
        unis = (annotated_data.get("step_c") or {}).get("universities") \
               or annotated_data.get("universities") or []
        if not unis:
            return flags
        u = unis[0]
        methods = u.get("selection_methods") or []
        if isinstance(methods, str):
            methods = [methods]
        methods_text = " / ".join(str(m) for m in methods) + " " + _norm_text(u.get("selection_detail"))
        flags["interview"]    = _contains_any(methods_text, _INTERVIEW_KWS)
        flags["presentation"] = _contains_any(methods_text, _PRESENTATION_KWS)
        flags["essay"]        = _contains_any(methods_text, _ESSAY_KWS)
        flags["no_presentation"] = not flags["presentation"]
        flags["interview_only"]  = flags["interview"] and not flags["presentation"] and not flags["essay"]

        eng_text = _norm_text(u.get("external_exam_requirements")) + " " + _norm_text(u.get("eligibility"))
        if eng_text and _contains_any(eng_text, _ENGLISH_KWS):
            if not any(w in eng_text for w in ["不要", "不問", "任意", "なし", "無し"]):
                flags["english_required"] = True

        gpa_text = _norm_text(u.get("gpa_requirement")) + " " + _norm_text(u.get("eligibility"))
        m = re.search(r"(\d(?:\.\d+)?)\s*(?:以上|程度|~)", gpa_text)
        if m:
            try:
                val = float(m.group(1))
                if 2.0 <= val <= 5.0:
                    flags["gpa_min"] = val
            except ValueError:
                pass

        elig = _norm_text(u.get("eligibility"))
        if _contains_any(elig, _ACTIVITY_KWS):
            if not _contains_any(elig, _ACTIVITY_OPT_KWS):
                flags["activity_required"] = True
    except Exception:
        pass
    return flags


# ── 不明フィールドの後処理補完 ────────────────────────────────────────────────

_UNKNOWN_VALS = ("不明", "情報なし", "要確認", "公式記載なし", "n/a", "—", "-", "")

def _is_unknown_str(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s == "" or any(t in s for t in _UNKNOWN_VALS)
    return False


def _backfill_unknown_fields(university_data: dict) -> None:
    """LLMが他フィールドに書いた情報を不明フィールドに転記するポスト処理。"""
    unis = (university_data.get("step_c") or {}).get("universities") or university_data.get("universities") or []
    for u in unis:
        if not isinstance(u, dict):
            continue

        # ── quota: 不明のとき他フィールドのテキストから人数を抽出 ──────────
        if _is_unknown_str(u.get("quota")):
            # 探索対象テキストフィールド
            search_texts = [
                u.get("features") or "",
                u.get("difficulty_facts") or "",
                u.get("selection_phase_1") or "",
                u.get("selection_phase_2") or "",
                u.get("eligibility") or "",
                str(u.get("specific_materials") or ""),
            ]
            combined = " ".join(str(t) for t in search_texts)
            # 「合計N名」「計N名」「N名程度」「定員N名」「募集人員N名」などを探す
            patterns = [
                r"合計\s*(\d+)\s*名",
                r"計\s*(\d+)\s*名",
                r"募集人員\s*[：:]\s*(\d+)\s*名",
                r"募集人員\s*(\d+)\s*名",
                r"定員\s*(\d+)\s*名",
                r"(\d+)\s*名程度",
                r"約\s*(\d+)\s*名",
                r"若干名",
            ]
            for pat in patterns:
                m = re.search(pat, combined)
                if m:
                    if pat == r"若干名":
                        u["quota"] = "若干名"
                    else:
                        u["quota"] = f"{m.group(1)}名（他フィールドから補完）"
                    break

        # ── admission_policy: 不明のとき features から補完 ────────────────
        ap = u.get("admission_policy")
        if _is_unknown_str(ap) or (isinstance(ap, dict) and _is_unknown_str(ap.get("summary"))):
            features = u.get("features") or ""
            if isinstance(features, str) and len(features) > 30:
                if isinstance(ap, dict):
                    u["admission_policy"]["summary"] = features[:800]
                else:
                    u["admission_policy"] = {"summary": features[:800], "keywords": []}


# ── 結果保存（provenance + flags + summary + unknown） ─────────────────────

def _save_result(request_id: str, university_data: dict, news_data: dict, summary_input: dict) -> None:
    from core import knowledge as kn

    # ── 蓄積知識で 不明 フィールドを補完（保存前）──────────────────────
    unis = university_data.get("universities") or []
    knowledge_merged_count = 0
    if unis:
        for u in unis:
            k = kn.get_knowledge(
                u.get("university", summary_input.get("university", "")),
                u.get("faculty", summary_input.get("faculty", "")),
                u.get("department", summary_input.get("department", "")),
                summary_input.get("admission_method", "") or u.get("formal_ao_name", ""),
            )
            if k and k.get("fields"):
                knowledge_merged_count += kn.fill_gaps_from_knowledge(u, k["fields"])

    _backfill_unknown_fields(university_data)
    annotated_ud = annotate_facts(university_data)
    flags        = _compute_flags(annotated_ud)
    src_summary  = summarize_sources(annotated_ud)
    unknowns     = count_unknowns(annotated_ud)

    # ── 逆方向: 今回のリサーチ結果を知識ベースに書き戻し（積み上げ）────
    try:
        db_user = get_db()
        try:
            row = db_user.execute(
                "SELECT user_id FROM research_requests WHERE id=?", (request_id,)
            ).fetchone()
            user_id = row["user_id"] if row else None
        finally:
            db_user.close()
        kn.upsert_from_universities(
            annotated_ud.get("universities") or [],
            request_id=request_id,
            user_id=user_id,
            university_override=summary_input.get("university"),
            admission_method_override=summary_input.get("admission_method"),
        )
    except Exception as e:
        print(f"[knowledge] upsert failed: {e}", flush=True)

    payload = {
        **summary_input,
        "university_data": annotated_ud,
        "news_data":       news_data,
        "knowledge_merged_count": knowledge_merged_count,
    }
    db = get_db()
    try:
        db.execute(
            """INSERT INTO research_results
               (request_id, result_json, flags_json, source_summary, unknown_count)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(request_id) DO UPDATE SET
                 result_json=excluded.result_json,
                 flags_json=excluded.flags_json,
                 source_summary=excluded.source_summary,
                 unknown_count=excluded.unknown_count""",
            (
                request_id,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(flags, ensure_ascii=False),
                json.dumps(src_summary, ensure_ascii=False),
                unknowns,
            ),
        )
        db.execute(
            "UPDATE research_requests SET status='done', updated_at=datetime('now') WHERE id=?",
            (request_id,),
        )
        db.commit()
    finally:
        db.close()


# ── 実行 ────────────────────────────────────────────────────────────────────

def _run_sync(
    request_id: str, university: str, faculty: str, department: str,
    admission_method: str, keyword: str, pdf_url: str,
    pdf_text: str = "",
    enable_deep_research: bool = False,
):
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path.home() / ".env")
    load_dotenv()

    import ao_research as ao

    # ao_research は内部で core.llm_router 経由で複数プロバイダを呼ぶ。
    # client 引数は後方互換のため残っているが使用されない。
    client = None

    def _on_progress(msg: str) -> None:
        _append_progress(request_id, msg)

    _append_progress(request_id, f"収集開始: {keyword}")
    university_data = ao.run_university_analysis(client, keyword, pdf_url=pdf_url, pdf_text=pdf_text, progress_cb=_on_progress, enable_deep_research=enable_deep_research)

    faculties_count = len((university_data.get("step_b") or {}).get("faculties", []))
    unis_count = len(university_data.get("universities") or [])
    _append_progress(request_id, f"✓ 大学情報取得（{faculties_count}学部 / {unis_count}学科）")

    _append_progress(request_id, "前年比・変更点を分析中...")
    news_data = ao.run_news_analysis(client, keyword, progress_cb=_on_progress)
    _append_progress(request_id, "✓ 前年比分析完了")

    _append_progress(request_id, "✅ 全分析完了 — 出典と「不明」を整理しています")
    _save_result(
        request_id,
        university_data, news_data,
        summary_input={
            "university": university, "faculty": faculty, "department": department,
            "admission_method": admission_method, "keyword": keyword,
        },
    )
