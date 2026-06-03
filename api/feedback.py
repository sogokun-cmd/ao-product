"""POST /api/feedback — リサーチ結果へのフィードバック"""
import os
import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.deps import require_user
from database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["feedback"])
limiter = Limiter(key_func=get_remote_address)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", os.environ.get("FROM_EMAIL", ""))


class FeedbackBody(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=100)
    rating:     int = Field(..., ge=1, le=5)
    comment:    str = Field(default="", max_length=2000)


@router.post("/feedback", summary="リサーチへのフィードバック送信")
@limiter.limit("10/minute")
async def submit_feedback(body: FeedbackBody, request: Request):
    user = require_user(request)

    db = get_db()
    try:
        # 同一リクエストへの重複投稿を防ぐ
        exists = db.execute(
            "SELECT id FROM feedback WHERE user_id=? AND request_id=?",
            (user["user_id"], body.request_id),
        ).fetchone()
        if exists:
            raise HTTPException(409, "すでにフィードバックを送信済みです")

        # リサーチが自分のものか確認
        req = db.execute(
            "SELECT university, faculty, department FROM research_requests WHERE id=? AND user_id=?",
            (body.request_id, user["user_id"]),
        ).fetchone()
        if not req:
            raise HTTPException(404, "リサーチが見つかりません")

        db.execute(
            "INSERT INTO feedback (user_id, request_id, rating, comment) VALUES (?,?,?,?)",
            (user["user_id"], body.request_id, body.rating, body.comment.strip()),
        )
        db.commit()
        label = " ".join(filter(None, [req["university"], req["faculty"], req["department"]]))
    finally:
        db.close()

    # 管理者にメール通知
    _notify_admin(user["email"], label, body.rating, body.comment.strip())

    return {"ok": True}


@router.get("/admin/feedback", summary="フィードバック一覧（管理者）")
async def list_feedback(request: Request, limit: int = 100):
    from api.admin import _check_token
    _check_token(request)
    db = get_db()
    try:
        rows = db.execute(
            """SELECT f.id, f.rating, f.comment, f.created_at,
                      u.email, rr.university, rr.faculty, rr.department
               FROM feedback f
               JOIN users u ON u.id = f.user_id
               LEFT JOIN research_requests rr ON rr.id = f.request_id
               ORDER BY f.created_at DESC LIMIT ?""",
            (min(limit, 500),),
        ).fetchall()
        avg = db.execute("SELECT AVG(rating) AS a, COUNT(*) AS c FROM feedback").fetchone()
    finally:
        db.close()
    return {
        "average_rating": round(avg["a"], 2) if avg["a"] else None,
        "total":          avg["c"],
        "items": [dict(r) for r in rows],
    }


def _notify_admin(user_email: str, label: str, rating: int, comment: str) -> None:
    if not ADMIN_EMAIL:
        return
    try:
        from core.email import _send
        stars = "★" * rating + "☆" * (5 - rating)
        html = f"""
        <div style="font-family:sans-serif;max-width:480px;padding:24px">
          <h3 style="color:#1a2b4a">新しいフィードバック</h3>
          <p><strong>大学:</strong> {label}</p>
          <p><strong>評価:</strong> {stars} ({rating}/5)</p>
          <p><strong>ユーザー:</strong> {user_email}</p>
          {"<p><strong>コメント:</strong><br>" + comment.replace(chr(10), "<br>") + "</p>" if comment else ""}
        </div>
        """
        _send(ADMIN_EMAIL, f"【AOリサーチ】フィードバック {stars}", html)
    except Exception as e:
        logger.warning("feedback notify failed: %s", e)
