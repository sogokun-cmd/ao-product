"""ナレッジ管理API（蓄積された大学情報の閲覧・削除）"""
from __future__ import annotations

import os
from fastapi import APIRouter, HTTPException, Request

from core import knowledge as kn
from database import get_db
import json

router = APIRouter(prefix="/api/admin/knowledge", tags=["knowledge"])

_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _require_admin(request: Request) -> None:
    token = request.headers.get("X-Admin-Token", "")
    if not _ADMIN_TOKEN or token != _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("", summary="蓄積ナレッジ一覧")
def list_knowledge(request: Request, limit: int = 200):
    _require_admin(request)
    rows = kn.list_knowledge_summary(limit=limit)
    return {"items": rows, "total": len(rows)}


@router.get("/stats", summary="ナレッジ統計")
def knowledge_stats(request: Request):
    _require_admin(request)
    db = get_db()
    try:
        total = db.execute("SELECT COUNT(*) FROM university_knowledge").fetchone()[0]
        total_runs = db.execute("SELECT SUM(run_count) FROM university_knowledge").fetchone()[0] or 0
        universities = db.execute("SELECT COUNT(DISTINCT university) FROM university_knowledge").fetchone()[0]
        recent = db.execute(
            """SELECT university, faculty, department, admission_method, run_count, updated_at
                 FROM university_knowledge ORDER BY updated_at DESC LIMIT 10"""
        ).fetchall()
        # フィールド別充填率
        rows = db.execute("SELECT fields_json FROM university_knowledge").fetchall()
        field_counts: dict[str, int] = {}
        for r in rows:
            fields = json.loads(r[0] or "{}")
            for k, v in fields.items():
                if not kn._is_unknown(v.get("value") if isinstance(v, dict) else v):
                    field_counts[k] = field_counts.get(k, 0) + 1
        top_fields = sorted(field_counts.items(), key=lambda x: -x[1])[:20]
        return {
            "total_entries": total,
            "total_research_runs": total_runs,
            "unique_universities": universities,
            "recent_updates": [dict(r) for r in recent],
            "field_coverage": [{"field": f, "count": c} for f, c in top_fields],
        }
    finally:
        db.close()


@router.get("/{entry_id}", summary="ナレッジ詳細")
def get_knowledge_entry(request: Request, entry_id: int):
    _require_admin(request)
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM university_knowledge WHERE id=?", (entry_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        fields = json.loads(row["fields_json"] or "{}")
        stale_fields = [k for k, v in fields.items() if isinstance(v, dict) and kn._is_stale(v)]
        return {
            "id":               row["id"],
            "university":       row["university"],
            "faculty":          row["faculty"],
            "department":       row["department"],
            "admission_method": row["admission_method"],
            "fields":           fields,
            "run_count":        row["run_count"],
            "contributor_ids":  json.loads(row["contributor_ids"] or "[]"),
            "last_request_id":  row["last_request_id"],
            "created_at":       row["created_at"],
            "updated_at":       row["updated_at"],
            "stale_fields":     stale_fields,
        }
    finally:
        db.close()


@router.delete("/{entry_id}", summary="ナレッジ削除")
def delete_knowledge_entry(request: Request, entry_id: int):
    _require_admin(request)
    db = get_db()
    try:
        r = db.execute("DELETE FROM university_knowledge WHERE id=?", (entry_id,))
        db.commit()
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True, "id": entry_id}
    finally:
        db.close()


@router.delete("", summary="全ナレッジ削除（慎重に）")
def delete_all_knowledge(request: Request):
    _require_admin(request)
    db = get_db()
    try:
        r = db.execute("DELETE FROM university_knowledge")
        db.commit()
        return {"deleted": r.rowcount}
    finally:
        db.close()
