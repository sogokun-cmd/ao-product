"""
/api/research エンドポイント群 + /api/me + /api/compare + /api/research/filter
"""
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from core.university import (
    create_request, get_request, get_result, list_requests_for_user, update_tags,
)
from auth.deps import (
    get_current_user, require_user, get_active_plan,
    check_quota, log_usage,
)

router = APIRouter(prefix="/api", tags=["research"])
limiter = Limiter(key_func=get_remote_address)


class ResearchRequest(BaseModel):
    university:       str = Field(..., min_length=1, max_length=100)
    faculty:          str = Field(..., min_length=1, max_length=100)
    department:       str = Field(..., min_length=1, max_length=100)
    admission_method: str = Field(default="", max_length=100)
    keywords:         str = Field(default="", max_length=200)
    pdf_url:          str = Field(default="", max_length=500)
    pdf_text:         str = Field(default="", max_length=200000)


class TagsRequest(BaseModel):
    tags: List[str] = Field(..., max_length=20)


class CompareRequest(BaseModel):
    request_ids: list[str] = Field(..., min_length=2, max_length=4)


def _ensure_request_access(req: dict, user_id: int) -> None:
    if req["user_id"] == user_id:
        return
    # チーム経由のアクセスチェック
    team_id = req.get("team_id")
    if team_id:
        from database import get_db
        db = get_db()
        try:
            member = db.execute(
                "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
                (team_id, user_id),
            ).fetchone()
            if member:
                return
        finally:
            db.close()
    raise HTTPException(status_code=403, detail="このリクエストへのアクセス権がありません")


@router.post("/research", summary="調査リクエストを作成")
@limiter.limit("10/minute")
async def start_research(req: ResearchRequest, request: Request):
    user = require_user(request)
    check_quota(user["user_id"], "research")
    rid = create_request(
        user_id=user["user_id"],
        university=req.university, faculty=req.faculty, department=req.department,
        admission_method=req.admission_method, keywords=req.keywords,
        pdf_url=req.pdf_url, pdf_text=req.pdf_text,
    )
    log_usage(user["user_id"], "research", ref_id=rid)
    # worker thread が pending を拾って実行する
    return {"request_id": rid, "message": "調査を開始しました"}


@router.get("/research", summary="自分の調査履歴一覧")
async def list_my_research(request: Request, limit: int = 100):
    user = require_user(request)
    return list_requests_for_user(user["user_id"], limit=min(limit, 200))


@router.post("/research/{request_id}/cancel", summary="実行中/待機中の調査をキャンセル（error に遷移）")
async def cancel_research(request_id: str, request: Request):
    user = require_user(request)
    req = get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="リクエストが見つかりません")
    _ensure_request_access(req, user["user_id"])
    if req["status"] not in ("pending", "running"):
        return {"cancelled": False, "status": req["status"]}
    from database import get_db
    db = get_db()
    try:
        db.execute(
            "UPDATE research_requests SET status='error', error='手動キャンセル', updated_at=datetime('now') WHERE id=? AND status IN ('pending','running')",
            (request_id,),
        )
        db.commit()
    finally:
        db.close()
    return {"cancelled": True, "status": "error"}


@router.put("/research/{request_id}/tags", summary="タグを更新")
async def set_tags(request_id: str, body: TagsRequest, request: Request):
    user = require_user(request)
    req = get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="リクエストが見つかりません")
    _ensure_request_access(req, user["user_id"])
    # タグは各20文字以内、最大20件
    clean = [t.strip()[:20] for t in body.tags if t.strip()][:20]
    updated = update_tags(request_id, user["user_id"], clean)
    if not updated:
        raise HTTPException(status_code=403, detail="更新権限がありません")
    return {"tags": clean}


@router.get("/research/tags/all", summary="自分のリサーチで使われているタグ一覧")
async def list_my_tags(request: Request):
    user = require_user(request)
    from database import get_db
    import json as _json
    db = get_db()
    try:
        rows = db.execute(
            "SELECT tags FROM research_requests WHERE user_id=? AND tags != '[]'",
            (user["user_id"],),
        ).fetchall()
    finally:
        db.close()
    all_tags: set[str] = set()
    for r in rows:
        try:
            for t in _json.loads(r["tags"] or "[]"):
                if t:
                    all_tags.add(t)
        except Exception:
            pass
    return sorted(all_tags)


@router.get("/research/{request_id}", summary="調査の詳細（進捗 + 結果）")
async def get_research(request_id: str, request: Request):
    user = require_user(request)
    req = get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="リクエストが見つかりません")
    _ensure_request_access(req, user["user_id"])
    res = get_result(request_id)
    # 古い annotation を最新 provenance 集約ロジックで再生成（保存済みデータは触らない）
    if res and isinstance(res.get("result"), dict):
        try:
            from core.provenance import annotate_facts, summarize_sources, count_unknowns
            ud = res["result"].get("university_data")
            if isinstance(ud, dict):
                refreshed = annotate_facts(ud)
                res["result"]["university_data"] = refreshed
                res["source_summary"] = summarize_sources(refreshed)
                res["unknown_count"] = count_unknowns(refreshed)
        except Exception:
            pass
    return {
        "request": req,
        "result":  res,
    }


@router.get("/share/{request_id}", summary="公開シェア用サマリー（認証不要）")
@limiter.limit("60/minute")
async def get_share(request_id: str, request: Request):
    """ログイン不要の公開エンドポイント。SEO・SNSシェア用に基本情報のみ返す。"""
    req = get_request(request_id)
    if not req or req["status"] != "done":
        raise HTTPException(status_code=404, detail="Not found")
    res = get_result(request_id)
    if not res:
        raise HTTPException(status_code=404, detail="Not found")

    result = res.get("result") or {}
    ud     = result.get("university_data") or {}
    univs  = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
    u0     = univs[0] if univs else {}
    ann    = u0.get("_annotated") or {}

    def _v(field):
        return (ann.get(field) or {}).get("value") if ann else u0.get(field)

    rh = u0.get("ratio_history") or {}
    def _rh(y):
        e = rh.get(y)
        if e is None: return None
        if isinstance(e, dict):
            v = e.get("value") or "不明"
            unit = e.get("unit") or ""
            return f"{v}（{unit}）" if unit and v != "不明" else str(v)
        return str(e)
    ratio_history = {y: _rh(y) for y in ["2026", "2025", "2024"] if _rh(y)}

    return {
        "university":        req["university"],
        "faculty":           req["faculty"],
        "department":        req["department"],
        "admission_method":  req["admission_method"],
        # 公開フィールド（基本情報のみ）
        "application_period": _v("application_period") or "不明",
        "selection_methods":  _v("selection_methods") or [],
        "quota":              _v("quota") or "不明",
        "ratio_history":      ratio_history,
        # ロック済みフィールド（ログイン後に閲覧可能）
        "locked": ["eligibility", "gpa_requirement", "english_requirement",
                   "selection_detail", "documents_required"],
        "updated_at": req["updated_at"],
    }


@router.post("/compare", summary="複数調査を比較")
async def compare(body: CompareRequest, request: Request):
    user = require_user(request)
    plan = get_active_plan(user["user_id"])
    schools = []
    for rid in body.request_ids:
        req = get_request(rid)
        if not req or req["status"] != "done":
            raise HTTPException(status_code=400, detail=f"リクエスト {rid} が完了していません")
        _ensure_request_access(req, user["user_id"])
        res = get_result(rid)
        if not res:
            continue
        result = res["result"]
        ud     = result.get("university_data") or {}
        univs  = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
        u0     = univs[0] if univs else {}
        ann    = u0.get("_annotated") or {}
        rh     = (u0.get("ratio_history") or {})
        def _rh_val(y):
            entry = rh.get(y)
            if entry is None:
                return "?"
            if isinstance(entry, dict):
                val = entry.get("value") or "不明"
                unit = entry.get("unit") or ""
                return f"{val}（{unit}）" if unit and val != "不明" else str(val)
            return str(entry)
        ratio  = " / ".join(_rh_val(y) for y in ["2026", "2025", "2024"])

        def _v(field):
            return (ann.get(field) or {}).get("value") if ann else u0.get(field)

        schools.append({
            "request_id": rid,
            "university": result.get("university") or u0.get("university") or "",
            "faculty":    result.get("faculty")    or u0.get("faculty")    or "",
            "department": result.get("department") or u0.get("department") or "",
            "application_period": _v("application_period") or "不明",
            "selection_methods":  _v("selection_methods")  or [],
            "selection_detail":   _v("selection_detail")   or "",
            "quota":              _v("quota") or "不明",
            "ratio":              ratio,
            "eligibility":        _v("eligibility") or "不明",
            "gpa_requirement":    _v("gpa_requirement") or "不明",
            "source_summary":     res.get("source_summary"),
            "unknown_count":      res.get("unknown_count"),
        })

    return {
        "schools": schools,
        "compare_mode": "light" if plan["features"].get("compare") == "light" else "full",
    }


@router.get("/research/filter", summary="条件フィルタ検索")
def filter_research(
    request: Request,
    interview:      Optional[bool]  = Query(None),
    essay:          Optional[bool]  = Query(None),
    no_presentation:Optional[bool]  = Query(None),
    english_required:Optional[bool] = Query(None),
    activity_required:Optional[bool]= Query(None),
    gpa_max:        Optional[float] = Query(None),
    ratio_max:      Optional[float] = Query(None),
    ratio_min:      Optional[float] = Query(None),
    keyword:        Optional[str]   = Query(None, max_length=100),
    tag:            Optional[str]   = Query(None, max_length=50),
    limit:          int             = Query(100, le=200),
):
    """完了リサーチ結果を条件フィルタして返す。ナレッジ蓄積データも含む。"""
    user = require_user(request)
    from database import get_db
    import json as _json
    db = get_db()
    try:
        # ユーザー自身 + チームのリサーチを対象にする
        rows = db.execute(
            """SELECT rr.id, rr.university, rr.faculty, rr.department, rr.admission_method,
                      rr.created_at, rr.updated_at, rr.tags,
                      res.flags_json, res.unknown_count, res.source_summary
                 FROM research_requests rr
                 JOIN research_results res ON res.request_id = rr.id
                WHERE rr.status = 'done'
                  AND (rr.user_id = ?
                       OR rr.team_id IN (
                           SELECT team_id FROM team_members WHERE user_id = ?
                       ))
                ORDER BY rr.updated_at DESC
                LIMIT 500""",
            (user["user_id"], user["user_id"]),
        ).fetchall()
    finally:
        db.close()

    results = []
    for r in rows:
        try:
            flags = _json.loads(r["flags_json"] or "{}")
        except Exception:
            flags = {}
        # フィルタ適用
        if interview is not None and flags.get("interview") != interview:
            continue
        if essay is not None and flags.get("essay") != essay:
            continue
        if no_presentation is not None and flags.get("no_presentation") != no_presentation:
            continue
        if english_required is not None and flags.get("english_required") != english_required:
            continue
        if activity_required is not None and flags.get("activity_required") != activity_required:
            continue
        if gpa_max is not None:
            gpa = flags.get("gpa_min")
            if gpa is not None and gpa > gpa_max:
                continue
        if ratio_max is not None or ratio_min is not None:
            ratio_val = flags.get("ratio_latest")
            if ratio_val is not None:
                if ratio_max is not None and ratio_val > ratio_max:
                    continue
                if ratio_min is not None and ratio_val < ratio_min:
                    continue
        if keyword:
            q = keyword.lower()
            target = " ".join([r["university"] or "", r["faculty"] or "", r["department"] or ""]).lower()
            if q not in target:
                continue
        try:
            row_tags = _json.loads(r["tags"] or "[]")
        except Exception:
            row_tags = []
        if tag and tag not in row_tags:
            continue
        try:
            ss = _json.loads(r["source_summary"] or "{}")
        except Exception:
            ss = {}
        results.append({
            "request_id":      r["id"],
            "university":      r["university"],
            "faculty":         r["faculty"],
            "department":      r["department"],
            "admission_method":r["admission_method"],
            "flags":           flags,
            "tags":            row_tags,
            "unknown_count":   r["unknown_count"],
            "source_summary":  ss,
            "updated_at":      r["updated_at"],
        })
        if len(results) >= limit:
            break

    return {"results": results, "total": len(results)}


@router.get("/knowledge/filter", summary="共有知識ベースからフィルタ検索（Standard以上で全件）")
@limiter.limit("30/minute")
async def filter_knowledge(
    request: Request,
    interview:        Optional[bool]  = Query(None),
    essay:            Optional[bool]  = Query(None),
    no_presentation:  Optional[bool]  = Query(None),
    english_required: Optional[bool]  = Query(None),
    activity_required:Optional[bool]  = Query(None),
    gpa_max:          Optional[float] = Query(None),
    ratio_max:        Optional[float] = Query(None),
    ratio_min:        Optional[float] = Query(None),
    keyword:          Optional[str]   = Query(None, max_length=100),
):
    """全ユーザーの蓄積データから条件フィルタ。Free は先頭3件のみ、Standard以上は全件。"""
    import re as _re
    user = require_user(request)
    plan = get_active_plan(user["user_id"])
    is_paid = plan["plan_code"] != "free"

    from database import get_db
    db = get_db()
    try:
        rows = db.execute(
            """SELECT university, faculty, department, admission_method,
                      fields_json, run_count, updated_at
                 FROM university_knowledge
                ORDER BY run_count DESC, updated_at DESC
                LIMIT 1000"""
        ).fetchall()
    finally:
        db.close()

    def _field_val(fields, key):
        entry = fields.get(key)
        if not entry: return None
        v = entry.get("value") if isinstance(entry, dict) else entry
        return v if v and str(v).strip() not in ("不明", "情報なし", "要確認", "") else None

    results = []
    for r in rows:
        try:
            fields = _json.loads(r["fields_json"] or "{}")
        except Exception:
            continue

        # フラグを動的計算
        sel = str(_field_val(fields, "selection_methods") or "")
        has_interview = any(w in sel for w in ["面接", "口頭", "プレゼン"])
        has_essay     = any(w in sel for w in ["小論文", "作文", "論述"])
        has_pres      = any(w in sel for w in ["プレゼン", "発表"])
        eng_val       = _field_val(fields, "external_exam_requirements")
        has_english   = bool(eng_val) and "不要" not in str(eng_val) and "なし" not in str(eng_val)
        act_val       = _field_val(fields, "eligibility")
        has_activity  = bool(act_val) and any(w in str(act_val) for w in ["実績", "活動", "部活", "受賞"])

        gpa_val = None
        gpa_raw = str(_field_val(fields, "gpa_requirement") or "")
        m = _re.search(r"(\d(?:\.\d+)?)\s*以上", gpa_raw)
        if m:
            try: gpa_val = float(m.group(1))
            except Exception: pass

        ratio_val = None
        rh = _field_val(fields, "ratio_history")
        if isinstance(rh, dict):
            for yr in ["2026", "2025", "2024"]:
                entry = rh.get(yr)
                v = entry.get("value") if isinstance(entry, dict) else entry
                if v:
                    try: ratio_val = float(_re.sub(r"[^\d.]", "", str(v)))
                    except Exception: pass
                if ratio_val and ratio_val > 0:
                    break

        # フィルタ適用
        if interview is not None and has_interview != interview: continue
        if essay is not None and has_essay != essay: continue
        if no_presentation is not None and (not has_pres) != no_presentation: continue
        if english_required is not None and has_english != english_required: continue
        if activity_required is not None and has_activity != activity_required: continue
        if gpa_max is not None and gpa_val is not None and gpa_val > gpa_max: continue
        if ratio_max is not None and ratio_val is not None and ratio_val > ratio_max: continue
        if ratio_min is not None and ratio_val is not None and ratio_val < ratio_min: continue
        if keyword:
            target = " ".join(filter(None, [r["university"], r["faculty"], r["department"]])).lower()
            if keyword.lower() not in target: continue

        results.append({
            "university":      r["university"],
            "faculty":         r["faculty"],
            "department":      r["department"],
            "admission_method":r["admission_method"],
            "run_count":       r["run_count"],
            "updated_at":      r["updated_at"],
            "flags": {
                "interview":        has_interview,
                "essay":            has_essay,
                "presentation":     has_pres,
                "english_required": has_english,
                "gpa_min":          gpa_val,
                "ratio_latest":     ratio_val,
            },
        })

    total = len(results)
    # Free は先頭3件のみ
    visible = results if is_paid else results[:3]
    return {"results": visible, "total": total, "locked": not is_paid and total > 3}


@router.get("/me", summary="ログインユーザー情報")
async def get_me(request: Request):
    user = require_user(request)
    plan = get_active_plan(user["user_id"])
    from database import get_db
    from datetime import datetime, timezone
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, name, email, picture FROM users WHERE id = ?",
            (user["user_id"],),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
        # used_this_period: free は累計、有料は当月
        if plan["plan_code"] == "free":
            cnt = db.execute(
                "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action='research'",
                (user["user_id"],),
            ).fetchone()["c"]
        else:
            now = datetime.now(timezone.utc)
            start = f"{now.year}-{now.month:02d}-01T00:00:00"
            cnt = db.execute(
                "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action='research' AND created_at>=?",
                (user["user_id"], start),
            ).fetchone()["c"]
        d = dict(row)
        d["plan"] = {**plan, "used_this_period": int(cnt)}
        return d
    finally:
        db.close()


@router.get("/health", summary="ヘルスチェック")
async def health():
    from core.llm_router import status as llm_status
    return {"status": "ok", "llm": llm_status()}
