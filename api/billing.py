"""
Stripe 決済 API
  POST /api/checkout          — Checkout Session 作成
  POST /api/billing/portal    — Customer Portal Session 作成
  POST /api/stripe/webhook    — Stripe Webhook 受信
"""
import os
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth.deps import require_user, get_active_plan, ensure_subscription
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["billing"])
limiter = Limiter(key_func=get_remote_address)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

STRIPE_PRICES = {
    "standard": os.environ.get("STRIPE_PRICE_STANDARD", ""),
    "premium":  os.environ.get("STRIPE_PRICE_PREMIUM", ""),
    "student":  os.environ.get("STRIPE_PRICE_STUDENT", ""),
    "tutor":    os.environ.get("STRIPE_PRICE_TUTOR", ""),
    "school":   os.environ.get("STRIPE_PRICE_SCHOOL", ""),
}
# 逆引き: price_id → plan_code
_PRICE_TO_PLAN = {v: k for k, v in STRIPE_PRICES.items() if v}

# クレジットパック Price ID
PACK_PRICES = {
    "pack_10": os.environ.get("STRIPE_PRICE_PACK_10", ""),
    "pack_20": os.environ.get("STRIPE_PRICE_PACK_20", ""),
    "pack_50": os.environ.get("STRIPE_PRICE_PACK_50", ""),
}
_PRICE_TO_PACK = {v: k for k, v in PACK_PRICES.items() if v}

PACK_CREDITS = {"pack_10": 10, "pack_20": 20, "pack_50": 50}


# ── Checkout ───────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan_code: str = Field(..., pattern=r"^(standard|premium|student|tutor|school)$")


class PackCheckoutRequest(BaseModel):
    pack_code: str = Field(..., pattern=r"^(pack_10|pack_20|pack_50)$")


@router.post("/checkout", summary="Stripe Checkout Session 作成")
@limiter.limit("5/minute")
async def create_checkout(body: CheckoutRequest, request: Request):
    user = require_user(request)
    price_id = STRIPE_PRICES.get(body.plan_code)
    if not price_id:
        raise HTTPException(400, f"プラン {body.plan_code} の Stripe Price が未設定です")

    customer_id = _get_or_create_customer(user["user_id"], user["email"], user.get("name", ""))

    session = stripe.checkout.Session.create(
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{BASE_URL}/app/account?checkout=success",
        cancel_url=f"{BASE_URL}/app/account?checkout=cancel",
        metadata={"user_id": str(user["user_id"]), "plan_code": body.plan_code},
        allow_promotion_codes=True,
    )
    return {"checkout_url": session.url}


@router.post("/checkout/pack", summary="クレジットパック購入（一回払い）")
@limiter.limit("10/minute")
async def create_pack_checkout(body: PackCheckoutRequest, request: Request):
    user = require_user(request)
    price_id = PACK_PRICES.get(body.pack_code)
    if not price_id:
        raise HTTPException(400, f"パック {body.pack_code} の Stripe Price が未設定です。管理者にお問い合わせください。")

    customer_id = _get_or_create_customer(user["user_id"], user["email"], user.get("name", ""))

    session = stripe.checkout.Session.create(
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        mode="payment",
        success_url=f"{BASE_URL}/app/account?checkout=pack_success",
        cancel_url=f"{BASE_URL}/app/account?checkout=cancel",
        metadata={
            "user_id":   str(user["user_id"]),
            "pack_code": body.pack_code,
            "type":      "credit_pack",
        },
        payment_method_types=["card"],
        allow_promotion_codes=True,
    )
    return {"checkout_url": session.url}


@router.get("/credits", summary="クレジット残高と購入履歴")
async def get_credits(request: Request):
    user = require_user(request)
    from auth.deps import get_credit_balance
    db = get_db()
    try:
        rows = db.execute(
            """SELECT pack_code, credits, created_at FROM credit_purchases
               WHERE user_id=? ORDER BY created_at DESC LIMIT 20""",
            (user["user_id"],),
        ).fetchall()
        packs_info = db.execute(
            "SELECT code, name, credits, price_jpy FROM credit_packs WHERE active=1 ORDER BY credits"
        ).fetchall()
    finally:
        db.close()
    return {
        "balance": get_credit_balance(user["user_id"]),
        "history": [dict(r) for r in rows],
        "packs":   [dict(p) for p in packs_info],
    }


# ── Customer Portal ────────────────────────────────────────────────────────

@router.post("/billing/portal", summary="Stripe Customer Portal")
@limiter.limit("5/minute")
async def create_portal(request: Request):
    user = require_user(request)
    db = get_db()
    try:
        row = db.execute(
            "SELECT stripe_customer_id FROM users WHERE id = ?", (user["user_id"],)
        ).fetchone()
    finally:
        db.close()

    if not row or not row["stripe_customer_id"]:
        raise HTTPException(400, "サブスクリプションがありません")

    session = stripe.billing_portal.Session.create(
        customer=row["stripe_customer_id"],
        return_url=f"{BASE_URL}/app/account",
    )
    return {"portal_url": session.url}


# ── Webhook ────────────────────────────────────────────────────────────────

@router.post("/stripe/webhook", summary="Stripe Webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")

    etype = event["type"]
    data = event["data"]["object"]

    if etype == "checkout.session.completed":
        if data.get("metadata", {}).get("type") == "credit_pack":
            _handle_pack_completed(data)
        else:
            _handle_checkout_completed(data)
    elif etype == "customer.subscription.updated":
        _handle_subscription_updated(data)
    elif etype == "customer.subscription.deleted":
        _handle_subscription_deleted(data)
    elif etype == "invoice.payment_failed":
        _handle_payment_failed(data)

    return {"received": True}


# ── Webhook ハンドラ ────────────────────────────────────────────────────────

def _handle_pack_completed(session: dict) -> None:
    """checkout.session.completed (credit_pack): クレジット付与"""
    meta = session.get("metadata", {})
    user_id = meta.get("user_id")
    pack_code = meta.get("pack_code")
    session_id = session.get("id", "")

    if not user_id or not pack_code:
        logger.warning("pack_completed: metadata 不足 %s", meta)
        return

    credits = PACK_CREDITS.get(pack_code, 0)
    if credits <= 0:
        logger.warning("pack_completed: unknown pack_code=%s", pack_code)
        return

    user_id = int(user_id)
    db = get_db()
    try:
        # 冪等性チェック
        existing = db.execute(
            "SELECT id FROM credit_purchases WHERE stripe_session_id=?", (session_id,)
        ).fetchone()
        if existing:
            return

        db.execute(
            "INSERT INTO credit_purchases (user_id, pack_code, credits, stripe_session_id) VALUES (?,?,?,?)",
            (user_id, pack_code, credits, session_id),
        )
        db.execute(
            "UPDATE users SET credit_balance = credit_balance + ? WHERE id=?",
            (credits, user_id),
        )
        db.commit()
    finally:
        db.close()

    logger.info("pack purchased: user=%s pack=%s credits=%s", user_id, pack_code, credits)


def _handle_checkout_completed(session: dict) -> None:
    """checkout.session.completed: 新規サブスクリプション開始"""
    meta = session.get("metadata", {})
    user_id = meta.get("user_id")
    plan_code = meta.get("plan_code")
    stripe_sub_id = session.get("subscription")

    if not user_id or not plan_code or not stripe_sub_id:
        logger.warning("checkout.session.completed: metadata 不足 %s", meta)
        return

    user_id = int(user_id)

    # Stripe Customer の metadata と照合して改ざんを検知
    customer_id = session.get("customer")
    if customer_id:
        customer = stripe.Customer.retrieve(customer_id)
        customer_user_id = customer.get("metadata", {}).get("user_id")
        if customer_user_id and int(customer_user_id) != user_id:
            logger.error(
                "checkout metadata mismatch: session user_id=%s, customer user_id=%s",
                user_id, customer_user_id,
            )
            return

    # Stripe Subscription から period_end を取得
    sub = stripe.Subscription.retrieve(stripe_sub_id)
    period_end = sub.get("current_period_end")
    price_id = ""
    if sub.get("items", {}).get("data"):
        price_id = sub["items"]["data"][0].get("price", {}).get("id", "")

    db = get_db()
    try:
        # 冪等性チェック
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE stripe_subscription_id = ?",
            (stripe_sub_id,),
        ).fetchone()
        if existing:
            return

        # 旧 active を canceled に
        db.execute(
            "UPDATE subscriptions SET status = 'canceled' WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        db.execute(
            """INSERT INTO subscriptions
               (user_id, plan_code, status, period_end, stripe_subscription_id, stripe_price_id)
               VALUES (?, ?, 'active', datetime(?, 'unixepoch'), ?, ?)""",
            (user_id, plan_code, period_end, stripe_sub_id, price_id),
        )
        db.commit()
    finally:
        db.close()

    logger.info("checkout completed: user=%s plan=%s", user_id, plan_code)


def _handle_subscription_updated(sub: dict) -> None:
    """customer.subscription.updated: アップグレード/ダウングレード"""
    stripe_sub_id = sub.get("id")
    if not stripe_sub_id:
        return

    status = sub.get("status", "active")
    period_end = sub.get("current_period_end")
    price_id = ""
    if sub.get("items", {}).get("data"):
        price_id = sub["items"]["data"][0].get("price", {}).get("id", "")

    plan_code = _PRICE_TO_PLAN.get(price_id)
    if not plan_code:
        logger.warning("subscription.updated: unknown price_id=%s", price_id)
        return

    db = get_db()
    try:
        row = db.execute(
            "SELECT id, user_id FROM subscriptions WHERE stripe_subscription_id = ?",
            (stripe_sub_id,),
        ).fetchone()
        if not row:
            logger.warning("subscription.updated: unknown stripe_subscription_id=%s", stripe_sub_id)
            return

        db_status = "active" if status in ("active", "trialing") else status
        db.execute(
            """UPDATE subscriptions
               SET plan_code = ?, status = ?, period_end = datetime(?, 'unixepoch'),
                   stripe_price_id = ?
               WHERE stripe_subscription_id = ?""",
            (plan_code, db_status, period_end, price_id, stripe_sub_id),
        )
        db.commit()
    finally:
        db.close()

    logger.info("subscription updated: sub=%s plan=%s status=%s", stripe_sub_id, plan_code, status)


def _handle_subscription_deleted(sub: dict) -> None:
    """customer.subscription.deleted: キャンセル完了 → Free に戻す"""
    stripe_sub_id = sub.get("id")
    if not stripe_sub_id:
        return

    db = get_db()
    try:
        row = db.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_subscription_id = ?",
            (stripe_sub_id,),
        ).fetchone()
        if not row:
            return

        user_id = row["user_id"]
        db.execute(
            "UPDATE subscriptions SET status = 'canceled' WHERE stripe_subscription_id = ?",
            (stripe_sub_id,),
        )
        db.commit()
    finally:
        db.close()

    # Free プランに戻す（ensure_subscription は内部で独自の DB 接続を使う）
    ensure_subscription(user_id, "free")

    logger.info("subscription deleted: user=%s → free", user_id)


def _handle_payment_failed(invoice: dict) -> None:
    """invoice.payment_failed: 支払い失敗 → past_due"""
    stripe_sub_id = invoice.get("subscription")
    if not stripe_sub_id:
        return

    db = get_db()
    try:
        db.execute(
            "UPDATE subscriptions SET status = 'past_due' WHERE stripe_subscription_id = ?",
            (stripe_sub_id,),
        )
        db.commit()
    finally:
        db.close()

    logger.warning("payment failed: sub=%s", stripe_sub_id)


# ── ヘルパー ───────────────────────────────────────────────────────────────

def _get_or_create_customer(user_id: int, email: str, name: str) -> str:
    """Stripe Customer を取得 or 新規作成し、DBに保存して返す"""
    db = get_db()
    try:
        row = db.execute(
            "SELECT stripe_customer_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row and row["stripe_customer_id"]:
            return row["stripe_customer_id"]

        customer = stripe.Customer.create(
            email=email,
            name=name,
            metadata={"user_id": str(user_id)},
        )
        db.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (customer.id, user_id),
        )
        db.commit()
        return customer.id
    finally:
        db.close()
