"""
メール + パスワード認証
- bcrypt によるパスワードハッシュ化
- JWT トークン生成 / 検証
"""
import os
from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
from jose import jwt

from auth.deps import COOKIE_KEY, JWT_SECRET, ALGORITHM

TOKEN_EXPIRE_DAYS = 7  # Cookieのmax_age（7日）と統一


# ── Password (bcrypt 直接使用 — passlib 互換性問題を回避) ──────

def hash_password(pw: str) -> str:
    return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────────────

def create_token(user_id: int, email: str, plan: str, name: str = "") -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "plan": plan, "name": name, "exp": expire},
        JWT_SECRET,
        algorithm=ALGORITHM,
    )


# ── Register / Login ──────────────────────────────────────────


_DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "throwam.com", "yopmail.com", "sharklasers.com", "guerrillamailblock.com",
    "grr.la", "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
    "guerrillamail.net", "guerrillamail.org", "spam4.me", "trashmail.com",
    "trashmail.me", "trashmail.net", "dispostable.com", "spamgourmet.com",
    "mailnull.com", "maildrop.cc", "mailnesia.com", "fakeinbox.com",
    "tempinbox.com", "tempr.email", "discard.email", "spamex.com",
    "mailexpire.com", "spamfree24.org", "spammotel.com", "spamslicer.com",
    "spamspot.com", "spamthisplease.com", "tempemail.net",
}


def register_user(name: str, email: str, password: str, signup_ip: str = "") -> dict:
    from database import get_db
    from auth.deps import ensure_subscription
    from core.email import generate_verification_token, send_verification_email
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain in _DISPOSABLE_DOMAINS:
        raise ValueError("使い捨てメールアドレスは使用できません")
    if len(password) < 10:
        raise ValueError("パスワードは10文字以上で設定してください")
    if not any(c.isupper() for c in password):
        raise ValueError("パスワードに大文字を1つ以上含めてください")
    if not any(c.isdigit() for c in password):
        raise ValueError("パスワードに数字を1つ以上含めてください")
    if not any(c in "!@#$%^&*()-_=+[]{}|;:',.<>?/~`" for c in password):
        raise ValueError("パスワードに記号を1つ以上含めてください")

    _is_prod = os.environ.get("ENVIRONMENT", "development") == "production"

    db = get_db()
    user_id = None
    try:
        if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            raise ValueError("このメールアドレスは既に登録されています")
        token = generate_verification_token()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        cur = db.execute(
            """INSERT INTO users (name, email, password_hash, email_verified,
               verification_token, verification_expires_at, signup_ip)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (name, email, hash_password(password), token, expires, signup_ip),
        )
        db.commit()
        user_id = cur.lastrowid
    finally:
        db.close()

    ensure_subscription(user_id, "free")

    sent = send_verification_email(email, token)
    if not sent and _is_prod:
        # メール送信失敗時はユーザーを削除してロールバック
        _rollback_user(user_id)
        raise ValueError("メール送信に失敗しました。しばらく後でもう一度お試しください。")

    return {"id": user_id, "name": name, "email": email, "plan": "free"}


def _rollback_user(user_id: int) -> None:
    """登録直後のメール送信失敗時にユーザーをDBから削除する。"""
    try:
        from database import get_db
        db = get_db()
        try:
            db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def verify_email(token: str) -> dict:
    """メール認証トークンを検証してユーザーを有効化"""
    from database import get_db
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, name, email, verification_expires_at FROM users WHERE verification_token = ?",
            (token,),
        ).fetchone()
        if not row:
            raise ValueError("無効または期限切れの認証リンクです")
        if row["verification_expires_at"] and row["verification_expires_at"] < datetime.now(timezone.utc).isoformat():
            raise ValueError("無効または期限切れの認証リンクです")
        db.execute(
            "UPDATE users SET email_verified = 1, verification_token = NULL WHERE id = ?",
            (row["id"],),
        )
        db.commit()
        return {"id": row["id"], "name": row["name"], "email": row["email"]}
    finally:
        db.close()


def login_user(email: str, password: str) -> dict:
    from database import get_db
    from auth.deps import get_active_plan, ensure_subscription
    db = get_db()
    try:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not row["password_hash"]:
            raise ValueError("メールアドレスまたはパスワードが正しくありません")
        if not verify_password(password, row["password_hash"]):
            raise ValueError("メールアドレスまたはパスワードが正しくありません")
        if not row["email_verified"]:
            raise ValueError("メールアドレスが確認されていません。登録時に届いたメールのリンクをクリックしてください。")
        d = dict(row)
    finally:
        db.close()
    ensure_subscription(d["id"], "free")
    d["plan"] = get_active_plan(d["id"])["plan_code"]
    return d
