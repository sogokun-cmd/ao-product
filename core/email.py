"""
メール送信ユーティリティ — Resend API 経由
"""
import os
import logging
import secrets
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "AOリサーチ <noreply@ao.helphero.jp>")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


def _send(to: str, subject: str, html: str) -> bool:
    """Resend でメール送信。未設定時は警告のみ。"""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping email to %s: %s", to, subject)
        return False
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to, exc)
        return False


def send_verification_email(email: str, token: str) -> bool:
    """メール認証用のリンクを送信"""
    url = f"{BASE_URL}/auth/verify?token={token}"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
      <h2 style="color:#1a2b4a">AOリサーチ — メール確認</h2>
      <p>アカウント登録ありがとうございます。以下のボタンでメールアドレスを確認してください。</p>
      <a href="{url}"
         style="display:inline-block;background:#2563eb;color:#fff;padding:12px 24px;
                border-radius:6px;text-decoration:none;font-weight:bold;margin:16px 0">
        メールアドレスを確認する
      </a>
      <p style="color:#888;font-size:.85rem">このリンクは24時間有効です。</p>
      <p style="color:#888;font-size:.85rem">心当たりがない場合はこのメールを無視してください。</p>
    </div>
    """
    return _send(email, "【AOリサーチ】メールアドレスの確認", html)


def _sanitize(s: str) -> str:
    """メールヘッダーインジェクション防止"""
    import html
    return html.escape(s.replace("\r", "").replace("\n", "").strip())


def send_team_invite_email(email: str, token: str, team_name: str, inviter_name: str) -> bool:
    """チーム招待メールを送信"""
    team_name = _sanitize(team_name)
    inviter_name = _sanitize(inviter_name)
    url = f"{BASE_URL}/invite/{token}"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
      <h2 style="color:#1a2b4a">AOリサーチ — チーム招待</h2>
      <p><strong>{inviter_name}</strong> さんからチーム「<strong>{team_name}</strong>」への招待が届いています。</p>
      <a href="{url}"
         style="display:inline-block;background:#2563eb;color:#fff;padding:12px 24px;
                border-radius:6px;text-decoration:none;font-weight:bold;margin:16px 0">
        招待を受ける
      </a>
      <p style="color:#888;font-size:.85rem">このリンクは14日間有効です。</p>
    </div>
    """
    return _send(email, f"【AOリサーチ】{team_name} への招待", html)


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)
