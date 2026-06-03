"""
Pydantic モデル — リクエスト / レスポンス スキーマ
"""
from pydantic import BaseModel, EmailStr, Field
from typing import Optional


class UserCreate(BaseModel):
    name:     str        = Field(..., min_length=1, max_length=100)
    email:    str        = Field(..., description="メールアドレス")
    password: str        = Field(..., min_length=8, description="パスワード（8文字以上）")


class UserLogin(BaseModel):
    email:    str
    password: str


class UserInfo(BaseModel):
    id:          int
    name:        str
    email:       str
    plan:        str   # 'free' | 'standard'
    usage_count: int

    class Config:
        from_attributes = True


class TokenPayload(BaseModel):
    sub:   str          # user_id (str)
    email: str
    plan:  str
    name:  str = ""
