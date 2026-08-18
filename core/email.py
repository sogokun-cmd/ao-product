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


def send_outage_notice(email: str, name: str, errors: int) -> bool:
    """障害のお詫び・復旧のお知らせ。errors=0 なら未体験ユーザー向けの文面。"""
    name = _sanitize(name) or "ご利用者"
    if errors > 0:
        subject = "【お詫び】リサーチが失敗していた不具合について（復旧済み・回数は返却しました）"
        lead = (
            "<p>このたび、リサーチの実行が失敗する不具合が発生しており、"
            "{n}様のリサーチも正常に完了しておりませんでした。"
            "ご迷惑をおかけし、誠に申し訳ございません。</p>"
        ).format(n=name)
        quota = (
            "<h3 style=\"color:#1a2b4a;font-size:1rem\">リサーチ回数について</h3>"
            "<p>失敗した分の利用回数は、すべてお戻ししました。"
            "ご確認いただかなくても、すでに残高に反映されています。"
            "今後も、エラーで結果が出なかった場合は回数を消費しない仕様に変更しました。</p>"
        )
        closing = (
            "<p>総合型選抜の出願が本格化する時期に、貴重なお時間を無駄にさせてしまいました。"
            "改めてお試しいただけますと幸いです。</p>"
        )
    else:
        subject = "【AOリサーチ】不具合の修正が完了しました（ご利用可能です）"
        lead = (
            "<p>{n}様にご登録いただいたあと、リサーチの実行が失敗する不具合が発生しておりました。"
            "{n}様が影響を受けられたかは分かりかねますが、"
            "もしお試しの際にうまく動かなかったようでしたら、それが原因です。</p>"
        ).format(n=name)
        quota = (
            "<h3 style=\"color:#1a2b4a;font-size:1rem\">リサーチ回数について</h3>"
            "<p>エラーで結果が出なかった場合は回数を消費しない仕様に変更しました。"
            "失敗しても無料枠が減ることはありません。</p>"
        )
        closing = (
            "<p>総合型選抜の出願が本格化する時期です。"
            "まだお使いでなければ、ぜひ一度お試しください。</p>"
        )

    html = """
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px;line-height:1.7">
      <h2 style="color:#1a2b4a">AOリサーチ</h2>
      <p>{name} 様</p>
      <p>AOリサーチをご利用いただきありがとうございます。運営の奥山です。</p>
      {lead}
      <h3 style="color:#1a2b4a;font-size:1rem">不具合の内容</h3>
      <p>8月上旬に行ったシステム更新に不備があり、リサーチの処理が途中で停止する状態になっておりました。
         8月18日に原因を特定し、修正を完了しています。現在は正常に動作することを確認済みです。</p>
      {quota}
      {closing}
      <a href="{base}/app/research"
         style="display:inline-block;background:#2563eb;color:#fff;padding:12px 24px;
                border-radius:6px;text-decoration:none;font-weight:bold;margin:16px 0">
        リサーチを試す
      </a>
      <p>もし出願締切が迫っているなど、お急ぎの事情がございましたら、
         このメールにご返信ください。個別に対応いたします。</p>
      <p style="margin-top:24px">AOリサーチ<br>奥山 樹生</p>
    </div>
    """.format(name=name, lead=lead, quota=quota, closing=closing, base=BASE_URL)
    return _send(email, subject, html)
