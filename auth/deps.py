"""
認証依存関係 — JWT Cookie からユーザー情報を取得 + プラン制御
"""
import os
import json
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from jose import JWTError, jwt

COOKIE_KEY  = "ao_session"
ALGORITHM   = "HS256"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
JWT_SECRET  = os.environ.get("JWT_SECRET_KEY", "")
if not JWT_SECRET:
    if ENVIRONMENT == "production":
        raise RuntimeError("JWT_SECRET_KEY must be set in production")
    import secrets as _secrets
    JWT_SECRET = _secrets.token_hex(32)
    import warnings
    warnings.warn("JWT_SECRET_KEY not set — using random ephemeral key (dev only)")

PLAN_RANK = {"free": 0, "standard": 1, "student": 1, "premium": 2, "tutor": 2, "school": 3}


def get_current_user(request: Request) -> dict | None:
    """Bearer ヘッダー OR Cookie の JWT を検証してユーザー情報を返す。未認証なら None。"""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else request.cookies.get(COOKIE_KEY)
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return {
            "user_id": int(payload["sub"]),
            "email":   payload.get("email", ""),
            "plan":    payload.get("plan", "free"),
            "name":    payload.get("name", ""),
        }
    except (JWTError, ValueError, KeyError):
        return None


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="認証が必要です")
    return user


def get_active_plan(user_id: int) -> dict:
    """subscriptions から有効プランを取得。無ければ free を返す。"""
    from database import get_db
    db = get_db()
    try:
        row = db.execute(
            """SELECT s.plan_code, s.status, s.period_end,
                      p.name, p.price_monthly, p.monthly_quota, p.features_json
               FROM subscriptions s
               JOIN plans p ON p.code = s.plan_code
               WHERE s.user_id = ? AND s.status = 'active'
               ORDER BY s.created_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if row:
            d = dict(row)
            # period_end が設定されている場合は有効期限を確認
            period_end = d.get("period_end")
            if period_end:
                try:
                    pe = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
                    if pe.tzinfo is None:
                        pe = pe.replace(tzinfo=timezone.utc)
                    if pe < datetime.now(timezone.utc):
                        # 有効期限切れ → Freeにフォールスルー
                        row = None
                except (ValueError, TypeError):
                    pass
        if row:
            d = dict(row)
            try:
                d["features"] = json.loads(d.pop("features_json") or "{}")
            except json.JSONDecodeError:
                d["features"] = {}
            return d
        # フォールバック: Free プランを返す
        free = db.execute(
            "SELECT name, price_monthly, monthly_quota, features_json FROM plans WHERE code='free'"
        ).fetchone()
        if free:
            d = dict(free)
            d["plan_code"] = "free"
            d["status"] = "active"
            d["period_end"] = None
            try:
                d["features"] = json.loads(d.pop("features_json") or "{}")
            except json.JSONDecodeError:
                d["features"] = {}
            return d
        return {"plan_code": "free", "name": "Free", "monthly_quota": 3, "features": {}}
    finally:
        db.close()


def require_plan(min_plan: str):
    """FastAPI Depends 用ファクトリ。指定プラン以上でなければ 403。"""
    def _checker(request: Request) -> dict:
        user = require_user(request)
        plan = get_active_plan(user["user_id"])
        if PLAN_RANK.get(plan["plan_code"], 0) < PLAN_RANK.get(min_plan, 0):
            raise HTTPException(
                status_code=403,
                detail=f"このリクエストは {min_plan.capitalize()} 以上のプランで利用できます",
            )
        return {"user": user, "plan": plan}
    return _checker


def ensure_subscription(user_id: int, plan_code: str = "free") -> None:
    """新規ユーザーに購読を作成（既に active があればno-op）。"""
    from database import get_db
    db = get_db()
    try:
        row = db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=? AND status='active' LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            return
        db.execute(
            "INSERT INTO subscriptions (user_id, plan_code, status) VALUES (?, ?, 'active')",
            (user_id, plan_code),
        )
        db.commit()
    finally:
        db.close()


def log_usage(user_id: int, action: str, ref_id: str | None = None, meta: dict | None = None) -> None:
    # クレジット消費（残高があれば先に使う）
    if action == "research":
        plan = get_active_plan(user_id)
        _use_credit = False
        if plan["plan_code"] == "free":
            _use_credit = get_credit_balance(user_id) > 0
        else:
            # サブスク枠を超えていたらクレジットを使う
            from database import get_db as _gdb
            _db = _gdb()
            try:
                from datetime import datetime as _dt, timezone as _tz
                _now = _dt.now(_tz.utc)
                _start = f"{_now.year}-{_now.month:02d}-01T00:00:00"
                _row = _db.execute(
                    "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action=? AND created_at >= ?",
                    (user_id, action, _start),
                ).fetchone()
                _quota = int(plan.get("monthly_quota", 0))
                _use_credit = int(_row["c"]) >= _quota
            finally:
                _db.close()
        if _use_credit:
            deduct_credit(user_id)

    from database import get_db
    db = get_db()
    try:
        db.execute(
            "INSERT INTO usage_logs (user_id, action, ref_id, meta_json) VALUES (?, ?, ?, ?)",
            (user_id, action, ref_id, json.dumps(meta or {}, ensure_ascii=False)),
        )
        db.commit()
    finally:
        db.close()


def get_credit_balance(user_id: int) -> int:
    """ユーザーのクレジット残高を返す。"""
    from database import get_db
    db = get_db()
    try:
        row = db.execute("SELECT credit_balance FROM users WHERE id=?", (user_id,)).fetchone()
        return int(row["credit_balance"]) if row else 0
    finally:
        db.close()


def deduct_credit(user_id: int) -> bool:
    """クレジットを1消費。残高不足なら False。"""
    from database import get_db
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE users SET credit_balance = credit_balance - 1 WHERE id=? AND credit_balance > 0",
            (user_id,),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def check_quota(user_id: int, action: str = "research") -> None:
    """優先順位: サブスク → クレジット残高 → Free累計。超過時 429。"""
    plan = get_active_plan(user_id)
    quota = int(plan.get("monthly_quota", 0))

    # past_due / paused はクレジットがなければ即 429
    if plan.get("status") in ("past_due", "paused", "unpaid"):
        if get_credit_balance(user_id) > 0:
            return
        raise HTTPException(
            status_code=429,
            detail="お支払いの問題によりご利用を一時停止しています。プランの支払い情報をご確認ください。",
        )

    # サブスク（有料プラン）の月次クォータ
    if plan["plan_code"] != "free" and quota != -1:
        from database import get_db
        db = get_db()
        try:
            now = datetime.now(timezone.utc)
            start = f"{now.year}-{now.month:02d}-01T00:00:00"
            # ログ済み + まだキューに積まれているリクエストを合算（TOCTOU対策）
            logged = db.execute(
                "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action=? AND created_at >= ?",
                (user_id, action, start),
            ).fetchone()["c"]
            in_queue = db.execute(
                "SELECT COUNT(*) AS c FROM research_requests WHERE user_id=? AND status IN ('pending','running') AND created_at >= ?",
                (user_id, start),
            ).fetchone()["c"]
            count = int(logged) + int(in_queue)
        finally:
            db.close()
        if count < quota:
            return  # サブスク枠内 → OK
        # サブスク枠を超えた場合でもクレジットがあれば使える
        if get_credit_balance(user_id) > 0:
            return
        raise HTTPException(
            status_code=429,
            detail=f"今月の利用上限（{quota}回）に達しました。クレジットパックを購入するか、プランをアップグレードしてください。",
        )

    if quota == -1:
        return

    # クレジット残高チェック（Free ユーザーもクレジットがあれば使える）
    if get_credit_balance(user_id) > 0:
        return

    # Free 累計チェック
    from database import get_db
    db = get_db()
    try:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action=?",
            (user_id, action),
        ).fetchone()
        count = int(row["c"])

        # 同一登録IPからのアカウント横断利用チェック（IP上限 = quota * 2）
        ip_row = db.execute("SELECT signup_ip FROM users WHERE id=?", (user_id,)).fetchone()
        signup_ip = (ip_row["signup_ip"] if ip_row else "") or ""
        if signup_ip and signup_ip != "unknown":
            ip_count_row = db.execute(
                """SELECT COUNT(*) AS c FROM usage_logs ul
                   JOIN users u ON u.id = ul.user_id
                   WHERE u.signup_ip = ? AND ul.action = ?""",
                (signup_ip, action),
            ).fetchone()
            ip_total = int(ip_count_row["c"])
            ip_limit = quota * 2  # 同一IPから最大6回（2アカウント分）
            if ip_total >= ip_limit:
                raise HTTPException(
                    status_code=429,
                    detail="同一環境からの無料枠が上限に達しました。プランに登録してください。",
                )
    finally:
        db.close()
    if count >= quota:
        raise HTTPException(
            status_code=429,
            detail=f"無料枠（累計{quota}回）を使い切りました。クレジットパックを購入するか、プランに登録してください。",
        )


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
