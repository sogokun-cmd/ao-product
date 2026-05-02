"""
/api/teams — チーム作成・招待・メンバー管理（School プラン以上）
ロール: owner > admin > editor > member (read-only)
"""
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.deps import require_plan, require_user
from database import get_db

router = APIRouter(prefix="/api/teams", tags=["teams"])
limiter = Limiter(key_func=get_remote_address)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class TeamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class InviteCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    role: str = Field("member", pattern=r"^(admin|editor|member)$")


class RoleUpdate(BaseModel):
    role: str = Field(..., pattern=r"^(admin|editor|member)$")


ROLE_RANK = {"owner": 3, "admin": 2, "editor": 1, "member": 0}


def _is_member(db, team_id: int, user_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM team_members WHERE team_id=? AND user_id=?",
        (team_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _require_team_role(db, team_id: int, user_id: int, roles: tuple[str, ...]) -> dict:
    m = _is_member(db, team_id, user_id)
    if not m or m["role"] not in roles:
        raise HTTPException(status_code=403, detail="このチームでの権限がありません")
    return m


@router.get("", summary="自分の所属チーム一覧")
async def list_my_teams(request: Request):
    user = require_user(request)
    db = get_db()
    try:
        rows = db.execute(
            """SELECT t.id, t.name, t.created_at, tm.role
               FROM teams t
               JOIN team_members tm ON tm.team_id = t.id
               WHERE tm.user_id = ?
               ORDER BY t.created_at DESC""",
            (user["user_id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@router.post("", summary="チーム作成（School プラン）")
async def create_team(body: TeamCreate, ctx: dict = Depends(require_plan("school"))):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO teams (name, owner_user_id) VALUES (?, ?)",
            (body.name.strip(), ctx["user"]["user_id"]),
        )
        team_id = cur.lastrowid
        db.execute(
            "INSERT INTO team_members (team_id, user_id, role) VALUES (?, ?, 'owner')",
            (team_id, ctx["user"]["user_id"]),
        )
        db.commit()
        return {"id": team_id, "name": body.name.strip(), "role": "owner"}
    finally:
        db.close()


@router.get("/{team_id}/members", summary="メンバー一覧")
async def list_members(team_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    try:
        if not _is_member(db, team_id, user["user_id"]):
            raise HTTPException(status_code=403, detail="チームメンバーではありません")
        rows = db.execute(
            """SELECT u.id, u.name, u.email, u.picture, tm.role, tm.joined_at
               FROM team_members tm
               JOIN users u ON u.id = tm.user_id
               WHERE tm.team_id = ?
               ORDER BY tm.joined_at""",
            (team_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@router.delete("/{team_id}/members/{user_id}", summary="メンバー削除（owner/admin）")
async def remove_member(team_id: int, user_id: int, request: Request):
    actor = require_user(request)
    db = get_db()
    try:
        _require_team_role(db, team_id, actor["user_id"], ("owner", "admin"))
        # owner は削除不可
        target = _is_member(db, team_id, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="対象が見つかりません")
        if target["role"] == "owner":
            raise HTTPException(status_code=400, detail="オーナーは削除できません")
        db.execute(
            "DELETE FROM team_members WHERE team_id=? AND user_id=?",
            (team_id, user_id),
        )
        db.commit()
        return {"deleted": True}
    finally:
        db.close()


@router.patch("/{team_id}/members/{user_id}/role", summary="メンバーのロール変更（owner/admin）")
async def update_member_role(team_id: int, user_id: int, body: RoleUpdate, request: Request):
    actor = require_user(request)
    db = get_db()
    try:
        actor_member = _require_team_role(db, team_id, actor["user_id"], ("owner", "admin"))
        target = _is_member(db, team_id, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="対象が見つかりません")
        if target["role"] == "owner":
            raise HTTPException(status_code=400, detail="オーナーのロールは変更できません")
        if ROLE_RANK.get(body.role, 0) >= ROLE_RANK.get(actor_member["role"], 0):
            raise HTTPException(status_code=403, detail="自分以上のロールには変更できません")
        db.execute(
            "UPDATE team_members SET role = ? WHERE team_id = ? AND user_id = ?",
            (body.role, team_id, user_id),
        )
        db.commit()
        return {"user_id": user_id, "role": body.role}
    finally:
        db.close()


@router.get("/{team_id}/invites", summary="招待一覧（owner/admin）")
async def list_invites(team_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    try:
        _require_team_role(db, team_id, user["user_id"], ("owner", "admin"))
        rows = db.execute(
            """SELECT id, email, token, expires_at, accepted_at, created_at
               FROM team_invites WHERE team_id=? ORDER BY created_at DESC""",
            (team_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@router.post("/{team_id}/invites", summary="招待作成")
@limiter.limit("10/minute")
async def create_invite(team_id: int, body: InviteCreate, request: Request):
    user = require_user(request)
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "有効なメールアドレスを入力してください")
    db = get_db()
    try:
        _require_team_role(db, team_id, user["user_id"], ("owner", "admin"))
        # 重複チェック
        existing = db.execute(
            "SELECT id FROM team_invites WHERE team_id=? AND email=? AND accepted_at IS NULL AND expires_at > ?",
            (team_id, email, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if existing:
            raise HTTPException(409, "この宛先には既に有効な招待があります")
        token = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.execute(
            """INSERT INTO team_invites (team_id, email, token, invited_by, expires_at, role)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (team_id, body.email.strip().lower(), token, user["user_id"], expires, body.role),
        )
        # チーム名と招待者名を取得してメール送信
        team_row = db.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
        db.commit()

        from core.email import send_team_invite_email
        send_team_invite_email(
            email=body.email.strip().lower(),
            token=token,
            team_name=team_row["name"] if team_row else "",
            inviter_name=user.get("name", ""),
        )
        return {"token": token, "expires_at": expires, "invite_url": f"/invite/{token}"}
    finally:
        db.close()


@router.post("/invites/accept/{token}", summary="招待を受諾")
@limiter.limit("5/minute")
async def accept_invite(token: str, request: Request):
    user = require_user(request)
    db = get_db()
    try:
        inv = db.execute(
            "SELECT * FROM team_invites WHERE token=?", (token,)
        ).fetchone()
        if not inv:
            raise HTTPException(status_code=404, detail="招待が見つかりません")
        if inv["accepted_at"]:
            raise HTTPException(status_code=409, detail="この招待は既に使用されています")
        if inv["expires_at"] < datetime.now(timezone.utc).isoformat():
            raise HTTPException(status_code=410, detail="招待の期限が切れています")
        invite_role = inv["role"] if "role" in inv.keys() else "member"
        try:
            db.execute(
                "INSERT INTO team_members (team_id, user_id, role) VALUES (?, ?, ?)",
                (inv["team_id"], user["user_id"], invite_role),
            )
        except Exception:
            pass  # 既にメンバー
        db.execute(
            "UPDATE team_invites SET accepted_at=datetime('now') WHERE id=?",
            (inv["id"],),
        )
        db.commit()
        return {"team_id": inv["team_id"]}
    finally:
        db.close()
