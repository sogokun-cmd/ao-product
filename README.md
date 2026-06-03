# AOリサーチ

一次情報ベースの総合型選抜リサーチツール。受験生にも塾講師にも。

## コア価値

1. 一次情報源（大学公式HP・公式PDF）を主軸にする
2. 推測補完しない — 取得できない項目は「不明」と表示
3. 情報源URL・出典種別・年度を必ず明示
4. プランによって情報の質は変えない（差は回数・保存・分析機能）

## プラン

| Plan   | 月額    | 月リサーチ回数 | 主な機能                       |
|--------|---------|----------------|-------------------------------|
| Free   | ¥0      | 累計 3回       | リサーチのみ                  |
| Student| ¥1,980  | 20回           | リサーチ + 軽量比較 + 保存    |
| Tutor  | ¥6,980  | 80回           | + 過去問分析・類題・生徒管理  |
| School | ¥19,800 | 300回          | + チーム共有・招待            |

## 機能

- **大学・学部・学科・入試方式・キーワード** で調査
- **公式HP / 公式PDF** を優先取得、補助情報源は出典タグで区別
- **構造化結果**: 出願期間・選考方法・募集人数・倍率・出願資格・評定・英語資格・AP・提出書類
- **不明バッジ + 出典サマリ**（公式PDF / 公式HP / 補助 の件数）
- **保存**（生徒紐付け + メモ）/ **横断検索 + フィルタ**
- **過去問分析**: PDF/テキスト → 出題傾向・類題・指導用メモ（Tutor+）
- **チーム共有**: 招待トークン発行（School）

## 技術スタック

- Backend: FastAPI + SQLite (WAL)
- 認証: Google OAuth2 + メール/パスワード + JWT Cookie
- AI: 複数プロバイダ対応（Anthropic / OpenAI / Google）— `core/llm.py` + `core/llm_router.py`
- フロント: Vanilla JS（マルチページ）

### 複数 LLM プロバイダ

`core/llm.py` で各プロバイダを共通インターフェース化。`core/llm_router.py` がタスクごとに優先順位で呼び分け、未構成プロバイダや一時エラーは自動フォールバック。

タスク例:
- `extraction` / `analysis` / `verification`: 高品質モデル中心（Opus / GPT-4o / Gemini Pro）
- `summarization`: 軽量モデル可（Sonnet / mini / flash）
- `practice_generation`, `notes_generation`: 高品質中心

複数プロバイダでの比較が必要な場合は `call_multi(task, ...)` で並列実行できる（合意度・矛盾検出に利用予定）。

UI上はモデル名を出さず、価値を「高品質な一次情報リサーチ」として伝える方針。プランによってモデルを変えない（情報品質は全プラン共通）。

> 注: `ao_research.py`（5000行の外部ライブラリ）は現状 Anthropic を直接利用。今後段階的に `llm_router` 経由に移行予定。

## セットアップ

```bash
pip install -r requirements.txt
cp .env.example .env  # 値を埋める
uvicorn main:app --reload --port 8000
```

### 環境変数

| 変数 | 説明 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API キー（必須） |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth |
| `GOOGLE_REDIRECT_URI` | コールバックURL |
| `JWT_SECRET_KEY` | JWT 署名キー (`openssl rand -hex 32`) |

## ページ構成

| ルート | 認証 | 内容 |
|---|---|---|
| `/` | 公開 | LP |
| `/pricing` | 公開 | 料金プラン |
| `/login`, `/register` | 公開 | 認証 |
| `/app` | 要 | ダッシュボード |
| `/app/research`, `/app/research/{id}` | 要 | リサーチ |
| `/app/saved` | Student+ | 保存一覧 |
| `/app/students` | Tutor+ | 生徒管理 |
| `/app/tutor/analysis` | Tutor+ | 過去問分析 |
| `/app/team` | School | チーム |
| `/app/account` | 要 | アカウント・プラン |
| `/invite/{token}` | 要 | チーム招待受諾 |

## API

```
POST   /api/research                       # 調査開始（プラン制限）
GET    /api/research                       # 履歴
GET    /api/research/{id}                  # 詳細
POST   /api/compare                        # 比較
GET    /api/saved                          # 保存一覧（?student_id=で生徒別）
POST   /api/saved
PATCH  /api/saved/{id}                     # メモ更新
DELETE /api/saved/{id}
GET    /api/students   ...                 # 生徒CRUD（Tutor+）
POST   /api/analysis (multipart)           # 過去問アップロード
POST   /api/analysis/{id}/run              # 出題傾向分析
POST   /api/analysis/{id}/practice         # 類題
POST   /api/analysis/{id}/notes            # 指導メモ
POST   /api/teams                          # チーム作成（School）
POST   /api/teams/{id}/invites             # 招待作成
POST   /api/teams/invites/accept/{token}   # 受諾
GET    /api/plans                          # 全プラン情報
GET    /api/me                             # 自分情報 + 現プラン
GET    /api/me/plan                        # プラン+利用状況
POST   /api/me/plan                        # プラン切替（ダミー）
```

## 既知の制約 / 本番化TODO

- **Stripe 連携**: スキーマ・UIは準備済だが、実決済は未接続（`/api/me/plan` で手動切替）
- **メール送信**: チーム招待は招待URLをUI上に表示するのみ（メール送信なし）
- **PDF抽出**: pdfplumber依存、スキャンPDFはOCR未対応
- **provenance 精度**: ao_research の出力にURLが含まれていない場合は「不明」化される
- **Railway Volume**: SQLite はコンテナFS。本番運用前に Volume か Postgres へ移行
- **チーム共有スコープ**: 現状は read 共有のみ（編集権限境界は将来精緻化）

## ディレクトリ

```
ao-product/
├── main.py
├── database.py
├── ao_research.py            # 既存スクレイピング+LLM抽出
├── api/
│   ├── research.py / saved.py / plans.py
│   ├── students.py / analysis.py / teams.py
├── auth/
│   ├── deps.py / routes.py / email_auth.py / google.py
├── core/
│   ├── university.py         # research_requests/results 管理
│   ├── provenance.py         # 出典分類・「不明」フォールバック
│   └── analysis.py           # 過去問テキスト抽出 + Claude分析
├── static/
│   ├── lp.html / pricing.html
│   ├── login.html / register.html
│   ├── app.html / research.html / saved.html / account.html
│   ├── students.html / tutor.html / team.html / invite.html
│   └── shared/ (tokens.css, header.js)
├── Procfile / requirements.txt / runtime.txt
```
