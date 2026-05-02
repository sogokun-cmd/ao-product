"""
/api/analysis — Tutor 以上向け 過去問分析
"""
import asyncio
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

import uuid

from auth.deps import require_plan, log_usage
from core.analysis import (
    extract_text_from_pdf,
    analyze_past_exam, generate_practice, generate_teaching_notes,
)
from core.llm_router import set_llm_context
from database import get_db

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


class AnalysisCreate(BaseModel):
    university: str = Field(..., min_length=1, max_length=100)
    faculty:    str = Field(default="", max_length=100)
    text:       str = Field(default="", max_length=200000)


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("analysis_json", "practice_json"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except json.JSONDecodeError:
            d[k] = None
    return d


@router.get("", summary="過去問分析一覧")
async def list_analyses(ctx: dict = Depends(require_plan("tutor"))):
    db = get_db()
    try:
        rows = db.execute(
            """SELECT id, university, faculty, source_filename, source_kind,
                      created_at,
                      (analysis_json IS NOT NULL) AS has_analysis,
                      (practice_json IS NOT NULL) AS has_practice,
                      (notes_text IS NOT NULL)    AS has_notes
               FROM past_exam_analyses
               WHERE user_id=?
               ORDER BY created_at DESC""",
            (ctx["user"]["user_id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@router.post("", summary="過去問アップロード（PDF or テキスト）")
async def create_analysis(
    university: str = Form(...),
    faculty:    str = Form(default=""),
    text:       str = Form(default=""),
    file:       UploadFile | None = File(default=None),
    ctx:        dict = Depends(require_plan("tutor")),
):
    extracted = ""
    source_kind = "text"
    source_filename = None
    if file is not None:
        data = await file.read()
        if (file.filename or "").lower().endswith(".pdf"):
            extracted = await asyncio.to_thread(extract_text_from_pdf, data)
            source_kind = "pdf"
        else:
            extracted = data.decode("utf-8", errors="ignore")[:60000]
            source_kind = "text"
        source_filename = file.filename
    if text and not extracted:
        extracted = text[:60000]
        source_kind = "text"
    if not extracted.strip():
        raise HTTPException(status_code=400, detail="過去問のテキスト/PDFを提供してください")
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO past_exam_analyses
               (user_id, university, faculty, source_filename, source_kind, extracted_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ctx["user"]["user_id"], university, faculty,
             source_filename, source_kind, extracted),
        )
        db.commit()
        new_id = cur.lastrowid
        log_usage(ctx["user"]["user_id"], "analysis_upload", ref_id=str(new_id))
        row = db.execute(
            "SELECT * FROM past_exam_analyses WHERE id=?", (new_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        db.close()


def _load_analysis(analysis_id: int, user_id: int) -> dict:
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM past_exam_analyses WHERE id=? AND user_id=?",
            (analysis_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="分析が見つかりません")
        return dict(row)
    finally:
        db.close()


@router.get("/{analysis_id}", summary="分析の詳細")
async def get_analysis(analysis_id: int, ctx: dict = Depends(require_plan("tutor"))):
    return _row_to_dict(_load_analysis(analysis_id, ctx["user"]["user_id"]))


@router.post("/{analysis_id}/run", summary="出題傾向を分析")
async def run_analysis(analysis_id: int, ctx: dict = Depends(require_plan("tutor"))):
    set_llm_context(user_id=ctx["user"]["user_id"], request_id=str(uuid.uuid4()))
    rec = _load_analysis(analysis_id, ctx["user"]["user_id"])
    result = await asyncio.to_thread(analyze_past_exam, rec["extracted_text"])
    db = get_db()
    try:
        db.execute(
            "UPDATE past_exam_analyses SET analysis_json=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), analysis_id),
        )
        db.commit()
        log_usage(ctx["user"]["user_id"], "analysis_run", ref_id=str(analysis_id))
    finally:
        db.close()
    return result


@router.post("/{analysis_id}/practice", summary="類題作成")
async def make_practice(analysis_id: int, ctx: dict = Depends(require_plan("tutor"))):
    set_llm_context(user_id=ctx["user"]["user_id"], request_id=str(uuid.uuid4()))
    rec = _load_analysis(analysis_id, ctx["user"]["user_id"])
    analysis = json.loads(rec["analysis_json"]) if rec.get("analysis_json") else {}
    if not analysis:
        raise HTTPException(status_code=400, detail="先に分析を実行してください")
    result = await asyncio.to_thread(generate_practice, rec["extracted_text"], analysis)
    db = get_db()
    try:
        db.execute(
            "UPDATE past_exam_analyses SET practice_json=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), analysis_id),
        )
        db.commit()
    finally:
        db.close()
    return result


@router.post("/{analysis_id}/notes", summary="指導用メモ")
async def make_notes(analysis_id: int, ctx: dict = Depends(require_plan("tutor"))):
    set_llm_context(user_id=ctx["user"]["user_id"], request_id=str(uuid.uuid4()))
    rec = _load_analysis(analysis_id, ctx["user"]["user_id"])
    analysis = json.loads(rec["analysis_json"]) if rec.get("analysis_json") else {}
    if not analysis:
        raise HTTPException(status_code=400, detail="先に分析を実行してください")
    notes = await asyncio.to_thread(generate_teaching_notes, rec["extracted_text"], analysis)
    db = get_db()
    try:
        db.execute(
            "UPDATE past_exam_analyses SET notes_text=? WHERE id=?",
            (notes, analysis_id),
        )
        db.commit()
    finally:
        db.close()
    return {"notes_text": notes}


@router.delete("/{analysis_id}", summary="削除")
async def delete_analysis(analysis_id: int, ctx: dict = Depends(require_plan("tutor"))):
    db = get_db()
    try:
        cur = db.execute(
            "DELETE FROM past_exam_analyses WHERE id=? AND user_id=?",
            (analysis_id, ctx["user"]["user_id"]),
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="分析が見つかりません")
        return {"deleted": True}
    finally:
        db.close()
