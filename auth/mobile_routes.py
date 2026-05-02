"""
モバイルアプリ用 JSON 認証 API: /auth/mobile/*
- Cookie ではなく JSON で access_token を返す
- Flutter (google_sign_in) から id_token を受け取り Google 認証を行う
"""
import os
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.email_auth import create_token, login_user, register_user
from auth.deps import get_active_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/mobile", tags=["auth-mobile"])
limiter = Limiter(key_func=get_remote_address)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


class RegisterRequest(BaseModel):
    name:     str = Field(..., min_length=1, max_length=80)
    email:    str = Field(..., min_length=5, max_length=254)
    password: str = Field(..., min_length=10)


class LoginRequest(BaseModel):
    email:    str = Field(..., min_length=5, max_length=254)
    password: str


class GoogleTokenRequest(BaseModel):
    id_token: str = Field(..., min_length=10)


def _user_response(user: dict) -> dict:
    return {
        "access_token": create_token(user["id"], user["email"], user.get("plan", "free"), user.get("name", "")),
        "token_type": "bearer",
        "user": {
            "id":      user["id"],
            "name":    user.get("name", ""),
            "email":   user["email"],
            "picture": user.get("picture", ""),
            "plan":    user.get("plan", "free"),
        },
    }


@router.post("/register", summary="モバイル新規登録")
@limiter.limit("5/minute")
async def mobile_register(body: RegisterRequest, request: Request):
    try:
        user = register_user(body.name.strip(), body.email.lower(), body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _user_response(user)


@router.post("/login", summary="モバイルログイン")
@limiter.limit("10/minute")
async def mobile_login(body: LoginRequest, request: Request):
    try:
        user = login_user(body.email.lower(), body.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return _user_response(user)


@router.post("/google", summary="Google ID トークンでログイン（Flutter用）")
@limiter.limit("10/minute")
async def mobile_google(body: GoogleTokenRequest, request: Request):
    """
    Flutter の google_sign_in が返す id_token をサーバーサイドで検証し、
    ユーザーを作成または取得して access_token を返す。
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "GOOGLE_CLIENT_ID が未設定です")

    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        id_info = google_id_token.verify_oauth2_token(
            body.id_token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10,
        )
    except Exception as e:
        logger.warning("Google ID token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Google 認証トークンが無効です")

    google_id = id_info.get("sub")
    email     = id_info.get("email", "")
    name      = id_info.get("name", email)
    picture   = id_info.get("picture", "")

    if not google_id or not email:
        raise HTTPException(400, "Google トークンに必要な情報がありません")

    from auth.google import get_or_create_google_user
    user = get_or_create_google_user(google_id, email, name, picture)
    return _user_response(user)
