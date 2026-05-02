"""
リサーチのバックグラウンド実行ワーカー（同一プロセス内スレッド版）。

Railway Volume は service ごとで共有できないため、専用 worker service ではなく
gunicorn worker 内で daemon thread を走らせて research_requests を処理する。

- 各 gunicorn worker が起動時に 1 本だけスレッドを立ち上げる（multiprocessing で
  ワーカーが 2 つ動くので合計 2 スレッド）。
- pending 行は atomic UPDATE で running に遷移させて取得、二重実行を防ぐ。
- 長時間実行（15 分超も想定）でも gunicorn のリクエストタイムアウトとは無関係。
"""
from __future__ import annotations

import os
import time
import threading
import traceback
from pathlib import Path

_STOP_EVENT = threading.Event()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()

POLL_INTERVAL_SEC = float(os.environ.get("RESEARCH_WORKER_POLL_SEC", "2.0"))


MAX_CONCURRENT_RESEARCH = int(os.environ.get("RESEARCH_MAX_CONCURRENT", "3"))


def _claim_next_pending() -> dict | None:
    """pending → running に atomic 遷移して 1 件取得。
    メモリ使用量と Railway プランに応じて `RESEARCH_MAX_CONCURRENT` 件まで並列実行を許容。
    既定 3 件（Hobby プラン想定）。Pro プランに上げた場合は env で 5-6 に上げる。"""
    from database import get_db
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        busy = db.execute(
            "SELECT COUNT(*) AS c FROM research_requests WHERE status='running'"
        ).fetchone()
        if busy and (busy["c"] or 0) >= MAX_CONCURRENT_RESEARCH:
            db.rollback()
            return None
        row = db.execute(
            """SELECT id, user_id, university, faculty, department, admission_method, keywords, pdf_url, pdf_text
               FROM research_requests
               WHERE status='pending'
               ORDER BY created_at ASC
               LIMIT 1"""
        ).fetchone()
        if not row:
            db.rollback()
            return None
        db.execute(
            "UPDATE research_requests SET status='running', updated_at=datetime('now') WHERE id=? AND status='pending'",
            (row["id"],),
        )
        # WHERE status='pending' は他ワーカーとの競合時に 0 行更新になる
        if db.total_changes == 0:
            db.rollback()
            return None
        db.commit()
        return dict(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


MAX_RESEARCH_SEC = float(os.environ.get("RESEARCH_MAX_SEC", "1200"))  # 20 分


def _process(job: dict) -> None:
    """1 件のリサーチを実行。ハード watchdog 付き（MAX_RESEARCH_SEC 超で error）。
    処理中は 30 秒ごとに updated_at をリフレッシュするハートビートを並走させ、
    reclaim_orphans が処理中のリクエストを誤って pending に戻すのを防ぐ。"""
    from core.university import _run_sync, _set_error
    from core.llm_router import set_llm_context

    rid = job["id"]
    # コスト計測コンテキストを設定（子スレッドに引き継がれる）
    set_llm_context(user_id=job.get("user_id") or 0, request_id=rid)

    # Deep Research は Premium プランのみ有効化
    enable_deep_research = False
    try:
        from auth.deps import get_active_plan
        _plan = get_active_plan(job.get("user_id") or 0)
        enable_deep_research = _plan.get("plan_code") == "premium"
    except Exception:
        pass
    keyword_parts = [p for p in [
        job["university"], job["faculty"], job["department"],
        job["admission_method"], job["keywords"],
    ] if p]
    keyword = " ".join(keyword_parts)
    if "総合型選抜" not in keyword:
        keyword = f"{keyword} 総合型選抜"

    exc_holder: list = []

    def _run():
        try:
            _run_sync(
                rid,
                job["university"], job["faculty"], job["department"],
                job["admission_method"], keyword, job["pdf_url"],
                pdf_text=job.get("pdf_text") or "",
                enable_deep_research=enable_deep_research,
            )
        except Exception as e:
            exc_holder.append(e)
            traceback.print_exc()

    _heartbeat_stop = threading.Event()

    def _heartbeat():
        from database import get_db
        while not _heartbeat_stop.wait(30):
            try:
                db = get_db()
                try:
                    db.execute(
                        "UPDATE research_requests SET updated_at=datetime('now') WHERE id=? AND status='running'",
                        (rid,),
                    )
                    db.commit()
                finally:
                    db.close()
            except Exception:
                pass

    hb = threading.Thread(target=_heartbeat, name=f"heartbeat-{rid[:8]}", daemon=True)
    hb.start()

    t = threading.Thread(target=_run, name=f"research-{rid[:8]}", daemon=True)
    t.start()
    try:
        t.join(timeout=MAX_RESEARCH_SEC)
    finally:
        _heartbeat_stop.set()

    if t.is_alive():
        _set_error(rid, f"タイムアウト: {int(MAX_RESEARCH_SEC)}秒以内に完了しませんでした")
        print(f"[worker] TIMEOUT request {rid} after {MAX_RESEARCH_SEC}s (thread abandoned)", flush=True)
        return

    if exc_holder:
        exc = exc_holder[0]
        _set_error(rid, f"{type(exc).__name__}: {exc}")


RECLAIM_STALE_SEC = float(os.environ.get("RESEARCH_RECLAIM_STALE_SEC", "900"))  # 15 分


def _reclaim_orphans() -> None:
    """前コンテナで走りっぱなしになった running 行を pending に戻す。
    updated_at が RECLAIM_STALE_SEC 以上更新されてない行のみ対象（実行中ジョブの横取り防止）。"""
    from database import get_db
    db = get_db()
    try:
        cur = db.execute(
            """UPDATE research_requests SET status='pending', updated_at=datetime('now')
               WHERE status='running'
                 AND (strftime('%s','now') - strftime('%s', updated_at)) > ?""",
            (int(RECLAIM_STALE_SEC),),
        )
        n = cur.rowcount or 0
        db.commit()
        if n:
            print(f"[worker] reclaimed {n} orphan running job(s)", flush=True)
    finally:
        db.close()


def _loop() -> None:
    # .env 読み込み（Railway は環境変数から入るが、ローカル開発向け）
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    except Exception:
        pass

    try:
        _reclaim_orphans()
    except Exception:
        traceback.print_exc()

    print(f"[worker] started (pid={os.getpid()}, poll={POLL_INTERVAL_SEC}s)", flush=True)
    _last_reclaim = time.time()
    _RECLAIM_INTERVAL = float(os.environ.get("RESEARCH_RECLAIM_SEC", "300"))  # 5 分毎
    while not _STOP_EVENT.is_set():
        # 定期的に orphan を pending に戻す（前コンテナ・他ワーカーで死んだジョブの復帰）
        if time.time() - _last_reclaim > _RECLAIM_INTERVAL:
            try:
                _reclaim_orphans()
            except Exception:
                traceback.print_exc()
            _last_reclaim = time.time()

        try:
            job = _claim_next_pending()
        except Exception:
            traceback.print_exc()
            job = None

        if job is None:
            if _STOP_EVENT.wait(POLL_INTERVAL_SEC):
                break
            continue

        print(f"[worker] claimed request {job['id']} ({job.get('university')})", flush=True)
        try:
            _process(job)
            print(f"[worker] completed request {job['id']}", flush=True)
        except Exception:
            traceback.print_exc()


def start_background_worker() -> None:
    """gunicorn worker 起動時に 1 回だけ呼ぶ。多重起動防止付き。"""
    global _WORKER_THREAD
    if os.environ.get("DISABLE_RESEARCH_WORKER") == "1":
        print("[worker] disabled via DISABLE_RESEARCH_WORKER=1", flush=True)
        return
    with _WORKER_LOCK:
        if _WORKER_THREAD and _WORKER_THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_loop, name="research-worker", daemon=True,
        )
        _WORKER_THREAD.start()


def stop_background_worker(timeout: float = 5.0) -> None:
    _STOP_EVENT.set()
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        _WORKER_THREAD.join(timeout=timeout)


if __name__ == "__main__":
    # スタンドアローン実行（将来 Postgres 化したら別 service として有効）
    start_background_worker()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop_background_worker()
