"""
SQLite データベース管理（全面刷新版）
4プラン体制 (free/student/tutor/school) + 一次情報品質管理 + 過去問分析 + チーム共有
"""
import os
import sqlite3
from pathlib import Path

# 本番は Railway Volume (/data)、開発はプロジェクトディレクトリ
_data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DB_PATH = _data_dir / "ao_product.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")   # WAL設定より先に設定
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass  # 他プロセスが既に設定中でも問題なし（WALは永続設定）
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript("""
        -- ── ユーザー & プロフィール ──────────────────────────────────────
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            email         TEXT    UNIQUE NOT NULL,
            password_hash TEXT,
            google_id     TEXT    UNIQUE,
            picture       TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS profiles (
            user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT,
            role         TEXT,
            bio          TEXT,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- ── プラン & 購読 ───────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS plans (
            code          TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            price_monthly INTEGER NOT NULL,
            monthly_quota INTEGER NOT NULL,
            features_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plan_code              TEXT    NOT NULL REFERENCES plans(code),
            status                 TEXT    NOT NULL,
            period_start           TEXT    NOT NULL DEFAULT (datetime('now')),
            period_end             TEXT,
            stripe_subscription_id TEXT,
            created_at             TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id, status);

        CREATE TABLE IF NOT EXISTS usage_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action     TEXT    NOT NULL,
            ref_id     TEXT,
            meta_json  TEXT,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_usage_user_action
            ON usage_logs(user_id, action, created_at DESC);

        -- ── チーム ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS teams (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS team_members (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id   INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role      TEXT    NOT NULL,
            joined_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(team_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS team_invites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id     INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            email       TEXT    NOT NULL,
            token       TEXT    UNIQUE NOT NULL,
            invited_by  INTEGER NOT NULL REFERENCES users(id),
            expires_at  TEXT    NOT NULL,
            accepted_at TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- ── 生徒 ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS students (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            team_id    INTEGER REFERENCES teams(id) ON DELETE SET NULL,
            name       TEXT    NOT NULL,
            note       TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- ── リサーチ（要求 + 結果を分離） ───────────────────────────────
        CREATE TABLE IF NOT EXISTS research_requests (
            id               TEXT    PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            team_id          INTEGER REFERENCES teams(id) ON DELETE SET NULL,
            university       TEXT    NOT NULL,
            faculty          TEXT    NOT NULL DEFAULT '',
            department       TEXT    NOT NULL DEFAULT '',
            admission_method TEXT    NOT NULL DEFAULT '',
            keywords         TEXT    NOT NULL DEFAULT '',
            pdf_url          TEXT    NOT NULL DEFAULT '',
            status           TEXT    NOT NULL,
            progress         TEXT    NOT NULL DEFAULT '[]',
            error            TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_req_user
            ON research_requests(user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS research_results (
            request_id     TEXT    PRIMARY KEY REFERENCES research_requests(id) ON DELETE CASCADE,
            result_json    TEXT    NOT NULL,
            flags_json     TEXT    NOT NULL DEFAULT '{}',
            source_summary TEXT    NOT NULL DEFAULT '{}',
            unknown_count  INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- ── 保存 ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS saved_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            team_id    INTEGER REFERENCES teams(id) ON DELETE SET NULL,
            request_id TEXT    NOT NULL REFERENCES research_requests(id) ON DELETE CASCADE,
            student_id INTEGER REFERENCES students(id) ON DELETE SET NULL,
            memo       TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_saved_user_req
            ON saved_items(user_id, request_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_user_req_student
            ON saved_items(user_id, request_id, IFNULL(student_id, 0));

        -- ── 大学ナレッジ蓄積（全ユーザー横断の共有知識ベース） ──────────
        -- 同一大学×学部×学科×入試方式に対するリサーチ結果をフィールド単位で
        -- 蓄積する。毎回のリサーチで 不明 だったフィールドが次回以降のリサーチで
        -- 埋まっていくほど、既存知識として再利用される。
        CREATE TABLE IF NOT EXISTS university_knowledge (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            university       TEXT    NOT NULL,
            faculty          TEXT    NOT NULL DEFAULT '',
            department       TEXT    NOT NULL DEFAULT '',
            admission_method TEXT    NOT NULL DEFAULT '',
            fields_json      TEXT    NOT NULL DEFAULT '{}',
            run_count        INTEGER NOT NULL DEFAULT 0,
            contributor_ids  TEXT    NOT NULL DEFAULT '[]',
            last_request_id  TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_univ_knowledge
            ON university_knowledge(university, faculty, department, admission_method);

        -- ── 過去問分析 ────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS past_exam_analyses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            team_id         INTEGER REFERENCES teams(id) ON DELETE SET NULL,
            university      TEXT    NOT NULL,
            faculty         TEXT    NOT NULL DEFAULT '',
            source_filename TEXT,
            source_kind     TEXT    NOT NULL DEFAULT 'text',
            extracted_text  TEXT    NOT NULL,
            analysis_json   TEXT,
            practice_json   TEXT,
            notes_text      TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pea_user
            ON past_exam_analyses(user_id, created_at DESC);
    """)
    conn.commit()

    # ── プランマスタ ─────────────────────────────────────────────────────
    conn.executemany(
        """INSERT OR IGNORE INTO plans
           (code, name, price_monthly, monthly_quota, features_json)
           VALUES (?, ?, ?, ?, ?)""",
        [
            ("free",     "Free",         0,    3,
             '{"research":true,"compare":"light","save":false}'),
            ("standard", "スタンダード", 1980,    8,
             '{"research":true,"compare":"light","save":true,"diagnosis":true,"analysis":false}'),
            ("premium",  "プレミアム",  3980,   20,
             '{"research":true,"compare":true,"save":true,"diagnosis":true,"diagnosis_writing":true,"analysis":true}'),
            ("student",  "Student",    1980,   20,
             '{"research":true,"compare":"light","save":true}'),
            ("tutor",    "Tutor",      6980,   80,
             '{"research":true,"compare":true,"save":true,"diagnosis":true,"analysis":true,"students":true}'),
            ("school",   "School",    19800,  300,
             '{"research":true,"compare":true,"save":true,"diagnosis":true,"analysis":true,"students":true,"team":true}'),
        ],
    )
    conn.commit()


    # ── クレジットパックマスタ ────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credit_packs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    NOT NULL UNIQUE,
            name        TEXT    NOT NULL,
            credits     INTEGER NOT NULL,
            price_jpy   INTEGER NOT NULL,
            stripe_price_id TEXT NOT NULL DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.executemany(
        "INSERT OR IGNORE INTO credit_packs (code, name, credits, price_jpy) VALUES (?,?,?,?)",
        [
            ("pack_10", "10回パック",  10, 1980),
            ("pack_20", "20回パック",  20, 3480),
            ("pack_50", "50回パック",  50, 7980),
        ],
    )
    # 環境変数が設定されていれば stripe_price_id を同期（デプロイのたびに最新化）
    for code, envkey in [
        ("pack_10", "STRIPE_PRICE_PACK_10"),
        ("pack_20", "STRIPE_PRICE_PACK_20"),
        ("pack_50", "STRIPE_PRICE_PACK_50"),
    ]:
        price_id = os.environ.get(envkey, "")
        if price_id:
            conn.execute(
                "UPDATE credit_packs SET stripe_price_id=? WHERE code=?",
                (price_id, code),
            )
    # ── クレジット購入履歴 ────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credit_purchases (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL REFERENCES users(id),
            pack_code           TEXT    NOT NULL,
            credits             INTEGER NOT NULL,
            stripe_session_id   TEXT    NOT NULL UNIQUE,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # ── LLM コスト計測テーブル ────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL DEFAULT 0,
            request_id TEXT,
            task       TEXT    NOT NULL,
            provider   TEXT    NOT NULL,
            model      TEXT    NOT NULL,
            tokens_in  INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            cache_read INTEGER NOT NULL DEFAULT 0,
            cost_usd   REAL    NOT NULL DEFAULT 0.0,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage_log(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_usage_req ON api_usage_log(request_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_usage_task ON api_usage_log(task, created_at DESC)"
    )
    conn.commit()

    # ── マイグレーション ──────────────────────────────────────────────────────
    for stmt in [
        "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT",
        "ALTER TABLE subscriptions ADD COLUMN stripe_price_id TEXT",
        "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN verification_token TEXT",
        "ALTER TABLE users ADD COLUMN verification_expires_at TEXT",
        "ALTER TABLE team_invites ADD COLUMN role TEXT NOT NULL DEFAULT 'member'",
        # 受験生プロファイル（Mode 3 マッチ度診断用）
        "ALTER TABLE students ADD COLUMN profile_experience   TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE students ADD COLUMN profile_concerns     TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE students ADD COLUMN profile_future       TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE students ADD COLUMN profile_motivation   TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE research_requests ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE research_requests ADD COLUMN pdf_text TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN credit_balance INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # カラム既存
    conn.commit()

    # ── フィードバック ─────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_id  TEXT    REFERENCES research_requests(id) ON DELETE SET NULL,
            rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment     TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_at DESC)")
    conn.commit()

    # ── ユーザー自身のプロフィール（大学マッチング用） ──────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            gpa            REAL,
            english_type   TEXT NOT NULL DEFAULT '',
            english_score  TEXT NOT NULL DEFAULT '',
            activities     TEXT NOT NULL DEFAULT '',
            future         TEXT NOT NULL DEFAULT '',
            interests      TEXT NOT NULL DEFAULT '',
            concerns       TEXT NOT NULL DEFAULT '',
            updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # ── プランマスタ既存行のアップデート（features_json の差分修正） ────────────
    for code, features in [
        ("standard", '{"research":true,"compare":"light","save":true,"diagnosis":true,"analysis":false}'),
        ("premium",  '{"research":true,"compare":true,"save":true,"diagnosis":true,"diagnosis_writing":true,"analysis":true}'),
        ("tutor",    '{"research":true,"compare":true,"save":true,"diagnosis":true,"diagnosis_writing":true,"analysis":true,"students":true}'),
        ("school",   '{"research":true,"compare":true,"save":true,"diagnosis":true,"diagnosis_writing":true,"analysis":true,"students":true,"team":true}'),
    ]:
        conn.execute(
            "UPDATE plans SET features_json=? WHERE code=?",
            (features, code),
        )
    conn.commit()

    # ── 既存 flags_json に ratio_latest を追加（ワンタイムマイグレーション）──────
    import json as _json, re as _re
    rows = conn.execute(
        "SELECT rres.request_id, rres.flags_json, rres.result_json FROM research_results rres"
    ).fetchall()
    for row in rows:
        try:
            flags = _json.loads(row["flags_json"] or "{}")
            if "ratio_latest" in flags:
                continue  # 既に処理済み
            result = _json.loads(row["result_json"] or "{}")
            ud = result.get("university_data") or {}
            unis = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
            ratio_val = None
            for u in unis:
                rh = u.get("ratio_history") or {}
                if not isinstance(rh, dict):
                    continue
                for yr in ["2026", "2025", "2024"]:
                    entry = rh.get(yr)
                    v = None
                    if isinstance(entry, dict):
                        v = entry.get("value")
                    elif entry:
                        v = entry
                    if v:
                        try:
                            ratio_val = float(_re.sub(r"[^\d.]", "", str(v)))
                        except Exception:
                            pass
                    if ratio_val and ratio_val > 0:
                        break
                if ratio_val:
                    break
            if ratio_val and ratio_val > 0:
                flags["ratio_latest"] = ratio_val
                conn.execute(
                    "UPDATE research_results SET flags_json=? WHERE request_id=?",
                    (_json.dumps(flags, ensure_ascii=False), row["request_id"]),
                )
        except Exception:
            pass
    conn.commit()
    conn.close()
