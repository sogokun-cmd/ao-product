"""
/api/saved — 保存済み調査の管理（生徒別の任意紐付け対応）
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.deps import require_user, get_active_plan
from database import get_db

router = APIRouter(prefix="/api/saved", tags=["saved"])
limiter = Limiter(key_func=get_remote_address)


class SavedCreate(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=64)
    student_id: int | None = None
    memo:       str = Field(default="", max_length=2000)


class SavedUpdate(BaseModel):
    memo: str = Field(..., max_length=2000)


def _check_save_allowed(user_id: int) -> None:
    plan = get_active_plan(user_id)
    if not plan["features"].get("save"):
        raise HTTPException(status_code=403, detail="保存機能は Student 以上のプランで利用できます")


@router.get("", summary="自分の保存一覧")
async def list_saved(request: Request, student_id: int | None = None):
    user = require_user(request)
    _check_save_allowed(user["user_id"])
    db = get_db()
    try:
        if student_id is not None:
            rows = db.execute(
                """SELECT s.*, r.university, r.faculty, r.department, r.status,
                          res.flags_json, res.unknown_count
                   FROM saved_items s
                   JOIN research_requests r ON r.id = s.request_id
                   LEFT JOIN research_results res ON res.request_id = r.id
                   WHERE s.user_id = ? AND s.student_id = ?
                   ORDER BY s.created_at DESC""",
                (user["user_id"], student_id),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT s.*, r.university, r.faculty, r.department, r.status,
                          res.flags_json, res.unknown_count
                   FROM saved_items s
                   JOIN research_requests r ON r.id = s.request_id
                   LEFT JOIN research_results res ON res.request_id = r.id
                   WHERE s.user_id = ?
                   ORDER BY s.created_at DESC""",
                (user["user_id"],),
            ).fetchall()
        import json as _json
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["flags"] = _json.loads(d.pop("flags_json") or "{}")
            except (_json.JSONDecodeError, TypeError):
                d["flags"] = {}
            out.append(d)
        return out
    finally:
        db.close()


@router.post("", summary="保存を追加（重複時はメモ更新）")
@limiter.limit("30/minute")
async def create_saved(body: SavedCreate, request: Request):
    user = require_user(request)
    _check_save_allowed(user["user_id"])
    db = get_db()
    try:
        # request の所有確認
        req = db.execute(
            "SELECT user_id FROM research_requests WHERE id=?", (body.request_id,)
        ).fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="調査が見つかりません")
        if req["user_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="この調査の保存権限がありません")
        # student の所有確認
        if body.student_id is not None:
            s = db.execute(
                "SELECT user_id FROM students WHERE id=?", (body.student_id,)
            ).fetchone()
            if not s or s["user_id"] != user["user_id"]:
                raise HTTPException(status_code=404, detail="生徒が見つかりません")
        try:
            cur = db.execute(
                """INSERT INTO saved_items (user_id, request_id, student_id, memo)
                   VALUES (?, ?, ?, ?)""",
                (user["user_id"], body.request_id, body.student_id, body.memo),
            )
            db.commit()
            new_id = cur.lastrowid
        except Exception:
            # UNIQUE 衝突: メモ更新
            db.execute(
                """UPDATE saved_items SET memo=?, updated_at=datetime('now')
                   WHERE user_id=? AND request_id=? AND IFNULL(student_id,0)=IFNULL(?,0)""",
                (body.memo, user["user_id"], body.request_id, body.student_id),
            )
            db.commit()
            row = db.execute(
                "SELECT id FROM saved_items WHERE user_id=? AND request_id=? AND IFNULL(student_id,0)=IFNULL(?,0)",
                (user["user_id"], body.request_id, body.student_id),
            ).fetchone()
            new_id = row["id"] if row else None
        return {"id": new_id}
    finally:
        db.close()


@router.patch("/{saved_id}", summary="メモ更新")
async def update_saved(saved_id: int, body: SavedUpdate, request: Request):
    user = require_user(request)
    db = get_db()
    try:
        cur = db.execute(
            """UPDATE saved_items SET memo=?, updated_at=datetime('now')
               WHERE id=? AND user_id=?""",
            (body.memo, saved_id, user["user_id"]),
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="保存が見つかりません")
        return {"updated": True}
    finally:
        db.close()


@router.delete("/{saved_id}", summary="保存を削除")
async def delete_saved(saved_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    try:
        cur = db.execute(
            "DELETE FROM saved_items WHERE id=? AND user_id=?",
            (saved_id, user["user_id"]),
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="保存が見つかりません")
        return {"deleted": True}
    finally:
        db.close()
