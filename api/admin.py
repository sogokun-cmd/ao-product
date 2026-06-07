"""
管理者専用 API — リモート監視エージェント用
GET /api/admin/stats — 利用統計・エラー状況を返す
"""
import hmac
import os
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/admin", tags=["admin"])

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _check_token(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/stats")
def get_stats(request: Request):
    _check_token(request)
    from database import get_db
    db = get_db()
    try:
        total_users = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        new_users_24h = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= datetime('now','-1 day')"
        ).fetchone()["c"]
        new_users_7d = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= datetime('now','-7 days')"
        ).fetchone()["c"]

        total_research = db.execute("SELECT COUNT(*) AS c FROM research_requests").fetchone()["c"]
        research_24h = db.execute(
            "SELECT COUNT(*) AS c FROM research_requests WHERE created_at >= datetime('now','-1 day')"
        ).fetchone()["c"]
        done_24h = db.execute(
            "SELECT COUNT(*) AS c FROM research_requests WHERE status='done' AND created_at >= datetime('now','-1 day')"
        ).fetchone()["c"]
        error_24h = db.execute(
            "SELECT COUNT(*) AS c FROM research_requests WHERE status='error' AND created_at >= datetime('now','-1 day')"
        ).fetchone()["c"]
        pending = db.execute(
            "SELECT COUNT(*) AS c FROM research_requests WHERE status IN ('pending','running')"
        ).fetchone()["c"]

        plan_dist = db.execute("""
            SELECT s.plan_code, COUNT(*) AS c
            FROM subscriptions s
            WHERE s.status = 'active'
            GROUP BY s.plan_code
        """).fetchall()

        recent_errors = db.execute("""
            SELECT rr.error, COUNT(*) AS c
            FROM research_requests rr
            WHERE rr.status = 'error'
              AND rr.created_at >= datetime('now','-1 day')
              AND rr.error IS NOT NULL
            GROUP BY rr.error
            ORDER BY c DESC
            LIMIT 5
        """).fetchall()

        llm_cost_24h = db.execute("""
            SELECT provider, SUM(cost_usd) AS total_cost, COUNT(*) AS calls
            FROM api_usage_log
            WHERE created_at >= datetime('now','-1 day')
            GROUP BY provider
        """).fetchall()

        knowledge_stats = db.execute("""
            SELECT COUNT(*) AS entries,
                   COUNT(DISTINCT university) AS universities,
                   SUM(run_count) AS total_runs,
                   ROUND(AVG(run_count), 1) AS avg_runs
            FROM university_knowledge
        """).fetchone()

    finally:
        db.close()

    return {
        "users": {
            "total": total_users,
            "new_24h": new_users_24h,
            "new_7d": new_users_7d,
        },
        "research": {
            "total": total_research,
            "last_24h": research_24h,
            "done_24h": done_24h,
            "error_24h": error_24h,
            "pending_now": pending,
            "success_rate_24h": round(done_24h / research_24h * 100, 1) if research_24h else None,
        },
        "plans": {row["plan_code"]: row["c"] for row in plan_dist},
        "recent_errors": [{"msg": r["error"][:120], "count": r["c"]} for r in recent_errors],
        "llm_cost_24h": [
            {"provider": r["provider"], "cost_usd": round(r["total_cost"], 4), "calls": r["calls"]}
            for r in llm_cost_24h
        ],
        "knowledge": {
            "entries":      knowledge_stats["entries"] or 0,
            "universities": knowledge_stats["universities"] or 0,
            "total_runs":   knowledge_stats["total_runs"] or 0,
            "avg_runs":     knowledge_stats["avg_runs"] or 0,
        },
    }
