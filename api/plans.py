"""
/api/plans + /api/me/plan — プラン情報取得
決済は /api/checkout (api/billing.py) 経由。
"""
import json

from fastapi import APIRouter, HTTPException, Request

from auth.deps import require_user, get_active_plan, PLAN_RANK
from database import get_db

router = APIRouter(prefix="/api", tags=["plans"])


@router.get("/plans", summary="全プラン情報（公開）")
async def list_plans():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT code, name, price_monthly, monthly_quota, features_json FROM plans"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["features"] = json.loads(d.pop("features_json") or "{}")
            except json.JSONDecodeError:
                d["features"] = {}
            d["rank"] = PLAN_RANK.get(d["code"], 0)
            out.append(d)
        out.sort(key=lambda x: x["rank"])
        return out
    finally:
        db.close()


@router.get("/me/plan", summary="自分の現在プラン")
async def my_plan(request: Request):
    user = require_user(request)
    plan = get_active_plan(user["user_id"])
    # 利用状況も返す
    db = get_db()
    try:
        if plan["plan_code"] == "free":
            cnt = db.execute(
                "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action='research'",
                (user["user_id"],),
            ).fetchone()["c"]
        else:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            start = f"{now.year}-{now.month:02d}-01T00:00:00"
            cnt = db.execute(
                "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id=? AND action='research' AND created_at>=?",
                (user["user_id"], start),
            ).fetchone()["c"]
    finally:
        db.close()
    return {**plan, "used_this_period": int(cnt)}


@router.post("/me/plan", summary="プラン変更（廃止 → /api/checkout を使用）",
             deprecated=True)
async def change_plan(request: Request):
    raise HTTPException(410, "このエンドポイントは廃止されました。/api/checkout を使用してください。")
