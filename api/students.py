"""
/api/students — 生徒CRUD（Tutor 以上）
候補校の保存・紐付けは /api/saved 側で扱う。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.deps import require_plan
from database import get_db

router = APIRouter(prefix="/api/students", tags=["students"])
limiter = Limiter(key_func=get_remote_address)


class StudentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="", max_length=2000)
    profile_experience: str = Field(default="", max_length=4000)
    profile_concerns:   str = Field(default="", max_length=4000)
    profile_future:     str = Field(default="", max_length=4000)
    profile_motivation: str = Field(default="", max_length=4000)


class StudentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=2000)
    profile_experience: str | None = Field(default=None, max_length=4000)
    profile_concerns:   str | None = Field(default=None, max_length=4000)
    profile_future:     str | None = Field(default=None, max_length=4000)
    profile_motivation: str | None = Field(default=None, max_length=4000)


def _require_student(db, student_id: int, user_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM students WHERE id=? AND user_id=?",
        (student_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="生徒が見つかりません")
    return dict(row)


@router.get("", summary="生徒一覧（候補校数付き）")
async def list_students(ctx: dict = Depends(require_plan("premium"))):
    db = get_db()
    try:
        rows = db.execute(
            """SELECT s.*, (
                   SELECT COUNT(*) FROM saved_items si WHERE si.student_id = s.id
               ) AS saved_count
               FROM students s
               WHERE s.user_id = ?
               ORDER BY s.created_at DESC""",
            (ctx["user"]["user_id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@router.post("", summary="生徒を追加")
@limiter.limit("30/minute")
async def create_student(request: Request, body: StudentCreate, ctx: dict = Depends(require_plan("premium"))):
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO students
                 (user_id, name, note, profile_experience, profile_concerns, profile_future, profile_motivation)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ctx["user"]["user_id"], body.name.strip(), body.note,
                body.profile_experience, body.profile_concerns,
                body.profile_future,     body.profile_motivation,
            ),
        )
        db.commit()
        row = db.execute("SELECT * FROM students WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        db.close()


@router.patch("/{student_id}", summary="生徒を更新")
async def update_student(student_id: int, body: StudentUpdate, ctx: dict = Depends(require_plan("premium"))):
    db = get_db()
    try:
        _require_student(db, student_id, ctx["user"]["user_id"])
        sets, vals = [], []
        if body.name is not None:
            sets.append("name=?"); vals.append(body.name.strip())
        if body.note is not None:
            sets.append("note=?"); vals.append(body.note)
        for _field in ("profile_experience", "profile_concerns", "profile_future", "profile_motivation"):
            _v = getattr(body, _field)
            if _v is not None:
                sets.append(f"{_field}=?"); vals.append(_v)
        if not sets:
            return {"updated": False}
        sets.append("updated_at=datetime('now')")
        vals += [student_id]
        db.execute(f"UPDATE students SET {', '.join(sets)} WHERE id=?", vals)
        db.commit()
        row = db.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        return dict(row)
    finally:
        db.close()


@router.get("/{student_id}", summary="生徒1件取得（プロファイル含む）")
async def get_student(student_id: int, ctx: dict = Depends(require_plan("premium"))):
    db = get_db()
    try:
        row = _require_student(db, student_id, ctx["user"]["user_id"])
        return row
    finally:
        db.close()


@router.delete("/{student_id}", summary="生徒を削除")
async def delete_student(student_id: int, ctx: dict = Depends(require_plan("premium"))):
    db = get_db()
    try:
        _require_student(db, student_id, ctx["user"]["user_id"])
        db.execute("DELETE FROM students WHERE id=?", (student_id,))
        db.commit()
        return {"deleted": True}
    finally:
        db.close()
