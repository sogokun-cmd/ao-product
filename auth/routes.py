"""
認証ルーター: /auth/*
  POST /auth/register  — メール+パスワード登録
  POST /auth/login     — メール+パスワードログイン
  GET  /auth/google    — Google OAuth2 開始
  GET  /auth/callback  — Google OAuth2 コールバック
  POST /auth/logout    — ログアウト
"""
import os
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.deps import COOKIE_KEY
from auth.email_auth import create_token, login_user, register_user, verify_email

router = APIRouter(prefix="/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)

_MAX_AGE = 7 * 24 * 3600  # 7日（30日から短縮）
_SECURE_COOKIE = os.environ.get("ENVIRONMENT", "development") == "production"


# ── Register ────────────────────────────────────────────────────────────────

@router.post("/register")
@limiter.limit("5/minute")
async def do_register(
    request: Request,
    name:     str = Form(...),
    email:    str = Form(...),
    password: str = Form(...),
):
    try:
        user  = register_user(name.strip(), email.strip().lower(), password)
        token = create_token(user["id"], user["email"], user["plan"], user["name"])
        resp  = RedirectResponse(url="/app?email_pending=1", status_code=303)
        resp.set_cookie(COOKIE_KEY, token, max_age=_MAX_AGE, httponly=True, samesite="lax", secure=_SECURE_COOKIE)
        return resp
    except ValueError as e:
        return RedirectResponse(url=f"/register?error={quote(str(e))}", status_code=303)


# ── Login ───────────────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("5/minute")
async def do_login(
    request: Request,
    email:    str = Form(...),
    password: str = Form(...),
):
    try:
        user  = login_user(email.strip().lower(), password)
        token = create_token(user["id"], user["email"], user["plan"], user["name"])
        resp  = RedirectResponse(url="/app", status_code=303)
        resp.set_cookie(COOKIE_KEY, token, max_age=_MAX_AGE, httponly=True, samesite="lax", secure=_SECURE_COOKIE)
        return resp
    except ValueError as e:
        return RedirectResponse(url=f"/login?error={quote(str(e))}", status_code=303)


# ── Google OAuth2 ────────────────────────────────────────────────────────────

@router.get("/google")
async def google_login():
    from auth.google import get_google_auth_url
    state = secrets.token_urlsafe(16)
    url   = get_google_auth_url(state)
    resp  = RedirectResponse(url=url)
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax")
    return resp


@router.get("/callback")
@limiter.limit("10/minute")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(
            url=f"/login?error={quote('Googleログインがキャンセルされました')}",
            status_code=303,
        )

    # State 検証 (CSRF対策)
    stored = request.cookies.get("oauth_state", "")
    if not stored or not state or stored != state:
        raise HTTPException(400, "Invalid OAuth state — もう一度お試しください")

    from auth.google import exchange_code, get_google_user_info, get_or_create_google_user
    try:
        tokens    = await exchange_code(code)
        user_info = await get_google_user_info(tokens["access_token"])
        user      = get_or_create_google_user(
            google_id = user_info["id"],
            email     = user_info["email"],
            name      = user_info.get("name", user_info["email"]),
            picture   = user_info.get("picture", ""),
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/login?error={quote('Googleログインに失敗しました: ' + str(exc)[:40])}",
            status_code=303,
        )

    from auth.deps import get_active_plan
    current_plan = get_active_plan(user["id"])["plan_code"]
    token = create_token(user["id"], user["email"], current_plan, user.get("name", ""))
    resp  = RedirectResponse(url="/app", status_code=303)
    resp.set_cookie(COOKIE_KEY, token, max_age=_MAX_AGE, httponly=True, samesite="lax", secure=_SECURE_COOKIE)
    resp.delete_cookie("oauth_state")
    return resp


# ── Email Verification ──────────────────────────────────────────────────────

@router.get("/verify")
async def do_verify(token: str = ""):
    if not token:
        return RedirectResponse(url="/login?error=無効な認証リンクです", status_code=303)
    try:
        user = verify_email(token)
        return RedirectResponse(url="/login?msg=メール認証が完了しました。ログインしてください", status_code=303)
    except ValueError as e:
        return RedirectResponse(url=f"/login?error={quote(str(e))}", status_code=303)


# ── Resend Verification ────────────────────────────────────────────────────

@router.post("/resend-verification")
@limiter.limit("1/minute")
async def resend_verification(request: Request, email: str = Form(...)):
    from database import get_db
    from core.email import generate_verification_token, send_verification_email
    from datetime import datetime, timezone, timedelta
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, email_verified FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        # 列挙防止: 存在しなくても同じレスポンス
        if row and not row["email_verified"]:
            token = generate_verification_token()
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            db.execute(
                "UPDATE users SET verification_token = ?, verification_expires_at = ? WHERE id = ?",
                (token, expires, row["id"]),
            )
            db.commit()
            send_verification_email(email.strip().lower(), token)
    finally:
        db.close()
    return RedirectResponse(
        url="/login?msg=確認メールを再送しました。メールをご確認ください",
        status_code=303,
    )


# ── Token Refresh ───────────────────────────────────────────────────────────

@router.post("/refresh")
async def refresh_token(request: Request):
    """JWT を最新プランで再発行する（checkout後に呼ぶ）。"""
    from auth.deps import require_user, get_active_plan
    from fastapi.responses import JSONResponse
    user = require_user(request)
    plan = get_active_plan(user["user_id"])["plan_code"]
    token = create_token(user["user_id"], user["email"], plan, user.get("name", ""))
    resp = JSONResponse({"ok": True, "plan": plan})
    resp.set_cookie(COOKIE_KEY, token, max_age=_MAX_AGE, httponly=True, samesite="lax", secure=_SECURE_COOKIE)
    return resp


# ── Logout ──────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(COOKIE_KEY)
    return resp
