#!/usr/bin/env python3
"""
総合型選抜 SNS リサーチ・コンテンツ分析ツール  v3.0

使い方:
  python ao_research.py                         # 全分析を実行（Notion保存はOFF）
  python ao_research.py --mode competitor       # 競合分析のみ
  python ao_research.py --mode buzz             # バズ分析のみ
  python ao_research.py --mode trends           # トレンドのみ
  python ao_research.py --mode googletrends     # Googleトレンド取得のみ（要 pip install pytrends）
  python ao_research.py --mode note             # note.com 人気記事分析のみ
  python ao_research.py --mode university       # 大学 総合型選抜 募集要項取得 → 完了後に自動でNotion階層保存（--no-notion でスキップ）
  python ao_research.py --mode university --pdf-url "https://xxx.ac.jp/admission.pdf"  # PDFを直接指定して分析精度向上
  python ao_research.py --mode news             # Yahoo!ニュースRSS + DuckDuckGo ニュース分析
  python ao_research.py --mode amazon           # Amazon 参考書・書籍分析
  python ao_research.py --mode tiktok           # TikTok ハッシュタグ・動画トレンド分析
  python ao_research.py --mode xtrends          # X/Twitter トレンド分析
  python ao_research.py --mode instagram        # Instagram ハッシュタグ・投稿分析
  python ao_research.py --mode threads          # Threads トレンド分析
  python ao_research.py --mode youtube          # YouTube 動画トレンド分析
  python ao_research.py --mode proposals        # コンテンツ案のみ
  python ao_research.py --keyword "私立大学 総合型選抜"  # キーワード指定
  python ao_research.py --notion                # Notionへ直接保存する（通常はNotion秘書エージェントに依頼）

v2.0 強化点:
  --workers N         並列検索ワーカー数（デフォルト: 4）
  --cache / --no-cache 検索結果キャッシュ（TTL: 24時間）
  --dry-run           Claudeを呼ばず検索データの収集のみ実行
  --output-format     出力形式を json / html / markdown から選択（デフォルト: 両方）
  --verbose           詳細ログ出力

v3.0 強化点:
  --model fast|smart|deep  分析モデル選択（fast=Haiku / smart=Sonnet / deep=Opus）
  --compare           前回の結果と差分を表示する
  --history N         過去N日分のトレンド推移を表示する
  --youtube-key KEY   YouTube Data API v3 キーを指定
  --mode threads      Threads トレンド分析（新規）
  --mode youtube      YouTube 動画トレンド分析（新規）
  SQLite 時系列トラッキング（自動保存・差分比較）
  URL 並列スクレイピング（universityモード高速化）
"""

import argparse
import concurrent.futures
import gc
import hashlib
import html as _html_module
import json
import os
import re
import socket
import sqlite3
import sys
import time


def _mem_rss_mb() -> float:
    """現在プロセスの RSS を MB で返す。psutil が無ければ 0 を返す。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        # psutil が無ければ /proc から読む
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024
        except Exception:
            pass
        return 0.0


def _log_mem(label: str) -> None:
    rss = _mem_rss_mb()
    if rss > 0:
        print(f"[mem] {label}: {rss:.0f}MB", flush=True)
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

# urllib の urlopen の timeout は接続/初回バイトにのみ効き、read() 全体には効かない。
# slow-dribble な相手で無限待ちを避けるため、ソケット全体のデフォルト timeout を設定する。
socket.setdefaulttimeout(float(os.environ.get("RESEARCH_SOCKET_TIMEOUT", "30")))
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import anthropic
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
try:
    from pytrends.request import TrendReq
    PYTRENDS_AVAILABLE = True
except ImportError:
    PYTRENDS_AVAILABLE = False
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

# ─────────────────────────────────────────
# カスタム例外
# ─────────────────────────────────────────
class AoResearchError(Exception):
    """ao_research 基底例外"""

class SearchError(AoResearchError):
    """Web 検索失敗"""

class AnalysisError(AoResearchError):
    """Claude 分析失敗"""

class NotionError(AoResearchError):
    """Notion API 失敗"""


# ─────────────────────────────────────────
# 検索結果キャッシュ（TTL ベース JSON キャッシュ）
# ─────────────────────────────────────────
_CACHE_DIR  = Path("ao_research_cache")
_CACHE_TTL  = timedelta(days=int(os.environ.get("DDGS_CACHE_DAYS", "7")))
_USE_CACHE  = True   # --no-cache で False に切り替え
_VERBOSE    = False  # --verbose で True に
_NUM_WORKERS = 4     # --workers で上書き


def _cache_key(query: str) -> str:
    """クエリ文字列からキャッシュファイル名を生成する。"""
    return hashlib.md5(query.encode("utf-8")).hexdigest()


def _load_cache(query: str) -> Optional[list[dict]]:
    """キャッシュが存在し TTL 内であれば結果リストを返す。それ以外は None。"""
    if not _USE_CACHE:
        return None
    cache_file = _CACHE_DIR / f"{_cache_key(query)}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at > _CACHE_TTL:
            cache_file.unlink(missing_ok=True)
            return None
        if _VERBOSE:
            console.print(f"  [dim]キャッシュHIT: {query[:50]}[/dim]")
        return data["results"]
    except Exception:
        return None


def _save_cache(query: str, results: list[dict]) -> None:
    """検索結果をキャッシュファイルに保存する。"""
    if not _USE_CACHE:
        return
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        cache_file = _CACHE_DIR / f"{_cache_key(query)}.json"
        cache_file.write_text(
            json.dumps({"cached_at": datetime.now().isoformat(), "results": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        if _VERBOSE:
            console.print(f"  [dim yellow]キャッシュ書き込み失敗: {e}[/dim yellow]")


# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
DEFAULT_KEYWORD = "大学 総合型選抜"
SEARCH_REGION    = "jp-jp"
SEARCH_LANG      = "jp"
MAX_RESULTS      = 12   # 検索結果の最大取得件数
OUTPUT_DIR       = Path("ao_research_results")
DB_PATH          = Path("ao_research_history.db")

# モデル tier システム（v3.0）
MODEL_FAST  = "claude-haiku-4-5-20251001"     # --model fast
MODEL_SMART = "claude-sonnet-4-6"               # --model smart（デフォルト）
MODEL_DEEP  = "claude-opus-4-6"                 # --model deep
MODEL       = MODEL_SMART
MODEL_EXTRACT = MODEL_FAST  # 検索結果整理用（smart時はHaiku、fast時もHaiku）

# Step C で別系統モデル（GPT-4o / Gemini Pro）に事実検証を投げる重要フィールド
CRITICAL_FIELDS_C = [
    "quota", "application_period", "selection_methods",
    "eligibility", "gpa_requirement",
    "external_exam_requirements", "ratio_history",
    "selection_phase_1", "selection_phase_2",
    "evaluation_criteria", "submitted_documents",
]

# APIコスト追跡
PRICING = {
    MODEL_FAST:  {"input": 0.80 / 1_000_000, "output": 4.00 / 1_000_000},
    MODEL_SMART: {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
    MODEL_DEEP:  {"input": 15.00 / 1_000_000, "output": 75.00 / 1_000_000},
}
_cost_log: list[dict] = []


def _track_cost(model: str, usage, step: str = "") -> None:
    _cost_log.append({
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "step": step,
    })


def display_cost_summary() -> None:
    if not _cost_log:
        return
    totals: dict[str, dict] = {}
    for entry in _cost_log:
        m = entry["model"]
        if m not in totals:
            totals[m] = {"calls": 0, "input": 0, "output": 0, "cost": 0.0}
        totals[m]["calls"] += 1
        totals[m]["input"] += entry["input_tokens"]
        totals[m]["output"] += entry["output_tokens"]
        pricing = PRICING.get(m, PRICING[MODEL_SMART])
        totals[m]["cost"] += (
            entry["input_tokens"] * pricing["input"]
            + entry["output_tokens"] * pricing["output"]
        )
    parts = []
    grand_total = 0.0
    model_labels = {MODEL_FAST: "Haiku", MODEL_SMART: "Sonnet", MODEL_DEEP: "Opus"}
    for m, t in totals.items():
        label = model_labels.get(m, m)
        parts.append(f"{label}×{t['calls']}回=${t['cost']:.3f}")
        grand_total += t["cost"]
    console.print(f"\n[dim]推定コスト: {' + '.join(parts)} = 合計${grand_total:.3f}[/dim]")

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

COMPETITOR_QUERIES = [
    "{kw} SNS Instagram Twitter note 合格体験",
    "{kw} 大学公式 Instagram フォロワー バズ 投稿",
    "{kw} 高校生 受験生 人気 アカウント site:twitter.com OR site:instagram.com",
    "{kw} YouTube 受験対策 再生数 人気",
    "大学 AO入試 SNS マーケティング 成功事例 2024 2025",
]

BUZZ_QUERIES = [
    "{kw} バズ 人気投稿 まとめ 2024 2025",
    "{kw} 志望理由書 書き方 Twitter Instagram 人気",
    "{kw} 合格体験記 note ヒット 読まれた",
    "{kw} 面接 対策 動画 YouTube 再生 人気",
    "大学 受験生向け SNS コンテンツ 反響 事例",
]

TREND_QUERIES = [
    "{kw} 最新 トレンド 2025",
    "{kw} 受験生 関心 キーワード 検索",
    "総合型選抜 AO 変更点 新情報 2026 2027",
    "大学入試 受験生 SNS 話題 今年",
    "{kw} 不安 悩み よくある質問 掲示板",
]

# 大学募集要項 検索クエリ（汎用）
UNIVERSITY_QUERIES = [
    "(総合型選抜 OR 学校推薦型選抜) 募集要項 2026 2027 大学 出願期間 選考方法 定員",
    "大学 (総合型選抜 OR 学校推薦型選抜) AO入試 出願資格 選考内容 倍率 2026 2027",
    "私立大学 (総合型選抜 OR 学校推薦型選抜) 募集要項 書類選考 面接 小論文 2027",
    "国立大学 (総合型選抜 OR 学校推薦型選抜) 募集要項 出願条件 2026 2027",
    "(総合型選抜 OR 学校推薦型選抜) 大学一覧 募集人数 倍率 難易度 2027",
    "(総合型選抜 OR 学校推薦型選抜) おすすめ大学 受かりやすい 出願資格 特徴 2027",
    "{kw} 総合型選抜 2027年度 募集要項 出願資格 選考方法 最新",
    "{kw} アドミッション 入学者選抜 総合型 2026 2027 詳細",
]

UNIVERSITY_DETAIL_QUERIES = [
    "{kw} アドミッションポリシー 入学者受け入れ方針 求める学生像 総合型選抜 OR 学校推薦型選抜",
    "{kw} 教育方針 教育理念 カリキュラム 授業の特徴 総合型選抜 OR 学校推薦型選抜",
    "{kw} 研究室 教授 准教授 専門分野 研究テーマ 総合型選抜 OR 学校推薦型選抜",
    "{kw} 就職先 卒業生 進路実績 就職率 業界 総合型選抜 OR 学校推薦型選抜",
    "{kw} 学部 研究内容 特色 強み 学べること 総合型選抜 OR 学校推薦型選抜",
]

# 学部・学科レベルの詳細取得クエリ（universityモードの強化用）
UNIVERSITY_FACULTY_QUERIES = [
    "{kw} 学部 学科 一覧 (総合型選抜 OR 学校推薦型選抜) 募集 2026 2027",
    "{kw} 学部別 (総合型選抜 OR 学校推薦型選抜) 募集人数 倍率 選考方法 学科",
    "{kw} 学部 学科 アドミッションポリシー 求める学生像 (総合型選抜 OR 学校推薦型選抜) 2026 2027",
    "{kw} 学科 専攻 コース (総合型選抜 OR 学校推薦型選抜) 出願資格 選考内容",
    "{kw} 学部 学科 カリキュラム 研究分野 専門科目 特色 総合型選抜 OR 学校推薦型選抜",
]

# ── サイト別ターゲット検索クエリ（横断収集用）──
# ※ DuckDuckGo は site: 演算子が機能しないため自然言語クエリを使用
# パスナビ
UNIVERSITY_PASSNAVI_QUERIES = [
    "パスナビ obunsha {kw} (総合型選抜 OR 学校推薦型選抜) 倍率 募集人員",
    "パスナビ {kw} AO入試 募集人員 倍率 選考方法 総合型選抜 OR 学校推薦型選抜",
    "パスナビ {kw} (総合型選抜 OR 学校推薦型選抜) 出願資格 選考日程 2026 2027",
    "{kw} (総合型選抜 OR 学校推薦型選抜) 2026 2027 passnavi",
]

# みんなの大学情報
UNIVERSITY_MINKOU_QUERIES = [
    "みんなの大学情報 {kw} (総合型選抜 OR 学校推薦型選抜) 倍率",
    "みんなの大学情報 minkou {kw} AO入試 倍率 口コミ 総合型選抜 OR 学校推薦型選抜",
    "minkou {kw} (総合型選抜 OR 学校推薦型選抜) 募集要項 評判 2025",
]

# リクナビ進学
UNIVERSITY_RIKUNABI_QUERIES = [
    "リクナビ進学 {kw} (総合型選抜 OR 学校推薦型選抜) 倍率",
    "リクナビ進学 shingakunet {kw} (総合型選抜 OR 学校推薦型選抜) 募集要項",
    "リクナビ {kw} AO入試 選考方法 出願期間 定員 総合型選抜 OR 学校推薦型選抜",
]

# 各大学公式サイト
UNIVERSITY_OFFICIAL_QUERIES = [
    "{kw} site:.ac.jp (総合型選抜 OR 学校推薦型選抜) 募集要項 出願期間 2026 2027",
    "{kw} site:.ac.jp アドミッションポリシー 入学者選抜要項 総合型選抜 OR 学校推薦型選抜",
    "{kw} 大学公式 (総合型選抜 OR 学校推薦型選抜) 選考日程 評定 出願資格 2026 2027",
    "{kw} 入学者選抜 (総合型選抜 OR 学校推薦型選抜) 募集要項 PDF 2027",
    "{kw} site:.ac.jp 入学者選抜要項 2027 PDF 総合型選抜",
    "{kw} site:.ac.jp 総合型選抜 出願資格 評定平均 選考内容 2027",
]

# 倍率・合格実績専用クエリ（過去3年分）
UNIVERSITY_RATIO_QUERIES = [
    "{kw} (総合型選抜 OR 学校推薦型選抜) 倍率 2024 2025 2026 過去 推移",
    "{kw} AO入試 志願者数 合格者数 競争率 実績 総合型選抜 OR 学校推薦型選抜",
    "{kw} (総合型選抜 OR 学校推薦型選抜) 倍率 難易度 合格最低点 データ",
    "パスナビ {kw} 倍率 (総合型選抜 OR 学校推薦型選抜) 過去問 合格者",
    "{kw} (総合型選抜 OR 学校推薦型選抜) 受験者数 合格者数 2025 2026",
    "{kw} (総合型選抜 OR 学校推薦型選抜) 倍率 2027年度 最新",
    "site:keinet.ne.jp {kw} 総合型選抜 入試結果 倍率",
    "keinet {kw} 総合型選抜 入試結果 倍率 志願者 合格者 2025 2026",
    "site:niad.ac.jp {kw} 入学定員 倍率 入学者",
    "site:manabi.benesse.ne.jp {kw} 総合型選抜 倍率 入試情報",
    "benesse manabi {kw} 総合型選抜 倍率 入試情報 2026 2027",
]

# 学科レベル詳細収集クエリ（--keyword "大学名 学部名" 指定時に追加実行）
UNIVERSITY_DEPT_DETAIL_QUERIES = [
    "{kw} 学科 専攻 (総合型選抜 OR 学校推薦型選抜) 募集人員 定員 学科別 2026 2027",
    "{kw} 学科別 倍率 志願者数 合格者数 2024 2025 2026 推移 総合型選抜 OR 学校推薦型選抜",
    "{kw} 学科 選考方法 書類審査 面接 小論文 プレゼン 実技 (総合型選抜 OR 学校推薦型選抜) 2026 2027",
    "{kw} 学科 出願資格 評定平均 基準 条件 (総合型選抜 OR 学校推薦型選抜) 2026 2027",
    "{kw} 学科 出願期間 選考日程 一次選考 二次選考 合格発表日 (総合型選抜 OR 学校推薦型選抜) 2026 2027",
    "{kw} 学科 アドミッションポリシー 求める学生像 入学者選抜方針 (総合型選抜 OR 学校推薦型選抜) 2026 2027",
    "{kw} 学科 (総合型選抜 OR 学校推薦型選抜) 募集要項 2026 2027",
    "パスナビ obunsha {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 募集人員 選考方法",
    "河合塾 keinet {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 入試日程 募集人員 倍率",
    "ベネッセ {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 選考内容 日程 倍率",
    "東進 {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 募集人員 選考日程",
    "スタディサプリ進路 {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 選考方法 出願条件 日程",
    "マイナビ進学 {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 募集人員",
]

# 学科レベル倍率専用クエリ（過去3年分）
UNIVERSITY_DEPT_RATIO_QUERIES = [
    "{kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 2024年度 2025年度 2026年度 推移 比較",
    "{kw} 学科別 AO入試 志願者数 受験者数 合格者数 2024 2025 2026 総合型選抜 OR 学校推薦型選抜",
    "{kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 志願者数 合格者数 最新年度",
    "nyushi mynavi {kw} 学科 総合型選抜 倍率 募集人員 2026 2027",
    "パスナビ obunsha {kw} 学科 倍率 合格者 (総合型選抜 OR 学校推薦型選抜) 過去3年",
    "河合塾 keinet {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 入試結果 倍率 合格者 募集人員",
    "河合塾 {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 入試日程 募集人員 倍率 合格者数",
    "河合塾 keinet {kw} 学科 AO入試 倍率データ 入試結果 2024 2025 2026 総合型選抜 OR 学校推薦型選抜",
    "ベネッセ {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 入試結果",
    "東進 {kw} 学科 AO入試 倍率 入試結果 合格者数 2024 2025 2026 総合型選抜 OR 学校推薦型選抜",
    "スタディサプリ進路 {kw} 学科 (総合型選抜 OR 学校推薦型選抜) 倍率 志願者数 合格者数",
    "{kw} 学科 穴場 競争率 受かりやすい (総合型選抜 OR 学校推薦型選抜) 倍率 低い 2026",
]

# 東進（toshin.com）
UNIVERSITY_TOSHIN_QUERIES = [
    "東進 {kw} 総合型選抜 倍率 募集人員 選考方法",
    "東進 toshin {kw} AO入試 入試結果 倍率 2024 2025 2026",
    "東進 {kw} 総合型選抜 合格実績 募集要項 選考日程 2026 2027",
    "東進 {kw} 学部 学科 総合型選抜 入試データ 募集定員",
]

# スタディサプリ進路・受験サプリ（shingakunet.com / juku.st）
UNIVERSITY_STUDYSAPURI_QUERIES = [
    "スタディサプリ進路 {kw} 総合型選抜 倍率 募集人員 選考方法",
    "スタディサプリ進路 shingakunet {kw} AO入試 入試情報 2026 2027",
    "スタディサプリ {kw} 総合型選抜 入試結果 倍率 選考内容 2027",
    "スタディサプリ {kw} 学部 学科 総合型選抜 募集定員 出願条件 2026 2027",
]

# 大学受験ナビ（nyushi.mynavi.jp / jyuken-lab.com）
UNIVERSITY_JUKEN_NAVI_QUERIES = [
    "マイナビ進学 {kw} 総合型選抜 倍率 募集人員 選考方法",
    "マイナビ進学 nyushi {kw} AO入試 入試情報 選考内容 2026 2027",
    "受験ラボ jyuken-lab {kw} 総合型選抜 倍率 募集人員 合格者数",
    "大学受験ナビ {kw} 総合型選抜 倍率 募集人員 選考方法 2026",
]

# 過去3年データ専用クエリ（年度指定・複数ソース横断）
# 研究室・教員・研究テーマ（志望理由書の素材として最重要）
UNIVERSITY_RESEARCH_QUERIES = [
    # 大学公式の研究ディレクトリ
    "{kw} 研究室一覧 site:ac.jp",
    "{kw} ゼミ 研究会 一覧 site:ac.jp",
    "{kw} 教員紹介 教授 准教授 site:ac.jp",
    "{kw} 研究分野 専門分野 研究テーマ site:ac.jp",
    "{kw} 研究プロジェクト 研究成果",
    # 研究者データベース（researchmap / KAKEN）
    "site:researchmap.jp {kw}",
    "site:kaken.nii.ac.jp {kw}",
    # カリキュラム・看板科目
    "{kw} カリキュラム 看板科目 特徴的科目 site:ac.jp",
    "{kw} 教員 論文 書籍 著書",
    # 研究室の公式サイト（lab.サブドメイン等）
    "{kw} 研究室 公式サイト",
]

UNIVERSITY_HISTORY_QUERIES = [
    # パスナビ 年度指定
    "パスナビ {kw} (総合型選抜 OR 学校推薦型選抜) 倍率 2025 2026 過去",
    "パスナビ obunsha {kw} (総合型選抜 OR 学校推薦型選抜) 志願者数 合格者数 2024 2025",
    # 東進データネット 年度指定
    "東進データネット {kw} (総合型選抜 OR 学校推薦型選抜) 入試結果 2025 2026",
    "東進 toshin {kw} AO入試 入試結果 志願者数 合格者数 2024 2025",
    # みんなの大学情報 年度指定
    "みんなの大学情報 {kw} (総合型選抜 OR 学校推薦型選抜) 倍率 2024 2025",
    "minkou {kw} AO入試 (総合型選抜 OR 学校推薦型選抜) 過去倍率 2025 2026 推移",
    # 大学受験ナビ・マイナビ 年度指定
    "大学受験ナビ マイナビ {kw} (総合型選抜 OR 学校推薦型選抜) 倍率 2024 2025 2026",
    "受験ラボ jyuken-lab {kw} (総合型選抜 OR 学校推薦型選抜) 入試結果 過去データ 2024 2025",
    # 週刊朝日・サンデー毎日
    "週刊朝日 大学入試 {kw} (総合型選抜 OR 学校推薦型選抜) 倍率 2025 2026",
    "サンデー毎日 {kw} 大学入試 (総合型選抜 OR 学校推薦型選抜) 倍率 志願者数 2024 2025",
]

# ニュース検索クエリ
NEWS_QUERIES = [
    "総合型選抜 最新ニュース 2026 2027",
    "AO入試 制度変更 最新情報 文部科学省 2026",
    "総合型選抜 大学 入試変更 新情報",
    "高校生 大学受験 総合型選抜 話題 注目",
    "総合型選抜 合格 受験生 ニュース",
]

# Amazon 参考書検索クエリ
AMAZON_QUERIES = [
    "amazon.co.jp 総合型選抜 参考書 対策 評価",
    "amazon 志望理由書 書き方 本 おすすめ レビュー 評判",
    "amazon 総合型選抜 面接 小論文 参考書 ランキング",
    "総合型選抜 AO入試 参考書 口コミ 評判 2024 2025",
    "志望理由書 対策本 amazon カスタマーレビュー 高校生",
]

# TikTok 検索クエリ
TIKTOK_QUERIES = [
    "TikTok 総合型選抜 人気動画 ハッシュタグ 2024 2025",
    "site:tiktok.com 総合型選抜 AO入試",
    "TikTok #総合型選抜 バズ 再生数 人気クリエイター",
    "TikTok 受験生 志望理由書 面接対策 人気動画",
    "TikTok 塾 受験 総合型選抜 フォロワー 人気アカウント",
]

# X/Twitter トレンド検索クエリ
XTRENDS_QUERIES = [
    "X Twitter 総合型選抜 トレンド 話題 2024 2025",
    "Twitterトレンド AO入試 総合型選抜 バズ 拡散",
    "X 受験生 総合型選抜 つぶやき 反応 傾向",
    "X 志望理由書 面接 総合型選抜 人気ツイート",
    "Xトレンド 総合型選抜 合否 受験生 話題",
]

# Instagram 検索クエリ
INSTAGRAM_QUERIES = [
    "Instagram 総合型選抜 人気投稿 ハッシュタグ 2024 2025",
    "site:instagram.com 総合型選抜 AO入試",
    "#総合型選抜 Instagram 投稿数 人気 フォロワー",
    "Instagram 受験生 志望理由書 合格 人気アカウント",
    "Instagram 塾 予備校 総合型選抜 フォロワー 人気投稿",
]

# note.com スクレイピング補助クエリ（API失敗時のフォールバック用）
NOTE_FALLBACK_QUERIES = [
    "{kw} note.com 人気記事 まとめ",
    "{kw} note 合格体験記 スキ 人気",
    "{kw} 志望理由書 note 読まれた ランキング",
    "site:note.com {kw} 総合型選抜",
]

# Googleトレンド取得対象キーワード（pytrends は最大5件）
GOOGLE_TRENDS_KEYWORDS = [
    "総合型選抜",
    "AO入試",
    "志望理由書",
    "総合型選抜 対策",
    "大学入試",
]

# Threads 検索クエリ（v3.0 新規）
THREADS_QUERIES = [
    "{kw} Threads threads.net 人気投稿 バズ 2025",
    "Threads {kw} 総合型選抜 受験生 拡散 フォロワー",
    "threads.net {kw} AO入試 話題 人気アカウント",
    "Threads {kw} 志望理由書 面接 合格体験 バズ",
    "threads app {kw} 受験 コンテンツ トレンド 2025",
]

# YouTube 検索クエリ（v3.0 新規）
YOUTUBE_QUERIES = [
    "YouTube {kw} 総合型選抜 再生数 人気 2024 2025",
    "YouTube {kw} AO入試 受験生 チャンネル フォロワー",
    "YouTube {kw} 志望理由書 面接 対策 動画 再生数",
    "YouTube {kw} 合格体験記 解説 人気チャンネル",
    "site:youtube.com {kw} 総合型選抜 受験",
]


# ─────────────────────────────────────────
# SQLite 時系列トラッキング（v3.0）
# ─────────────────────────────────────────
def _db_connect() -> sqlite3.Connection:
    """履歴DBに接続（なければ初期化）"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword   TEXT NOT NULL,
            mode      TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            data_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kw_mode
        ON snapshots (keyword, mode, captured_at)
    """)
    conn.commit()
    return conn


def save_snapshot(keyword: str, mode: str, data: dict) -> None:
    """分析結果をDBに保存"""
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT INTO snapshots (keyword, mode, captured_at, data_json) VALUES (?,?,?,?)",
            (keyword, mode, datetime.now().isoformat(), json.dumps(data, ensure_ascii=False))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        if _VERBOSE:
            console.print(f"[dim yellow]  DB保存失敗: {e}[/dim yellow]")


def get_history(keyword: str, mode: str, days: int = 30) -> list[dict]:
    """過去N日分のスナップショットを取得"""
    try:
        conn = _db_connect()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT captured_at, data_json FROM snapshots "
            "WHERE keyword=? AND mode=? AND captured_at>=? ORDER BY captured_at DESC",
            (keyword, mode, cutoff)
        ).fetchall()
        conn.close()
        return [{"captured_at": r[0], "data": json.loads(r[1])} for r in rows]
    except Exception:
        return []


def get_last_snapshot(keyword: str, mode: str) -> dict | None:
    """最新1件のスナップショットを取得（差分比較用）"""
    history = get_history(keyword, mode, days=365)
    return history[0] if history else None


# ─────────────────────────────────────────
# Web検索
# ─────────────────────────────────────────
def web_search(query: str, max_results: int = MAX_RESULTS, timeout: int = 15) -> list[dict]:
    """DuckDuckGo で検索して結果リストを返す。キャッシュがあればそれを使用する。
    429 / 空結果 / ネットワークエラーは指数バックオフで最大3回リトライ。"""
    cached = _load_cache(query)
    if cached is not None:
        return cached

    _max_retries = int(os.environ.get("DDGS_MAX_RETRIES", "3"))
    _base_wait = float(os.environ.get("DDGS_BACKOFF_BASE_SEC", "2"))
    last_err: Exception | None = None
    for _attempt in range(_max_retries):
        try:
            with DDGS(timeout=timeout) as ddgs:
                results = list(ddgs.text(
                    query,
                    region=SEARCH_REGION,
                    safesearch="moderate",
                    max_results=max_results,
                ))
            if results:
                _save_cache(query, results)
                return results
            # 空結果 → 次の試行（レート制限の初期兆候のことがある）
            if _attempt < _max_retries - 1:
                time.sleep(_base_wait * (2 ** _attempt))
                continue
            # 全試行で空 → 空のまま保存しない（次回は別タイミングで再取得）
            return []
        except Exception as e:
            last_err = e
            err_str = str(e)[:200]
            is_rate = any(kw in err_str.lower() for kw in ("429", "rate", "too many", "ratelimit"))
            if _attempt < _max_retries - 1:
                wait = _base_wait * (2 ** _attempt)
                if is_rate:
                    wait *= 2  # レート制限は長めに待つ
                if _VERBOSE:
                    console.print(f"[yellow]  検索リトライ ({query[:40]}) attempt {_attempt+1}: {err_str[:80]} / wait {wait}s[/yellow]")
                time.sleep(wait)
                continue
            break
    if last_err:
        console.print(f"[yellow]  検索失敗（リトライ {_max_retries}回後）: {str(last_err)[:120]}[/yellow]")
    return []


def news_search(query: str, max_results: int = 8, timeout: int = 15) -> list[dict]:
    """DuckDuckGo ニュース検索。キャッシュがあればそれを使用する。"""
    cache_key_str = f"__news__{query}"
    cached = _load_cache(cache_key_str)
    if cached is not None:
        return cached
    try:
        with DDGS(timeout=timeout) as ddgs:
            results = list(ddgs.news(
                query,
                region=SEARCH_REGION,
                max_results=max_results,
            ))
        _save_cache(cache_key_str, results)
        return results
    except Exception as e:
        console.print(f"[yellow]  ニュース検索エラー: {e}[/yellow]")
        return []


def collect_search_data(queries: list[str], keyword: str, label: str) -> list[dict]:
    """複数クエリを並列実行して結果を統合する。

    _NUM_WORKERS スレッドで同時検索し、レート制限対策のスリープを維持しながら
    全体の待ち時間を短縮する。
    """
    all_results: list[dict] = []
    seen_urls: set[str] = set()
    formatted_queries = [q.format(kw=keyword) for q in queries]
    lock = concurrent.futures.ThreadPoolExecutor._lock if hasattr(
        concurrent.futures.ThreadPoolExecutor, "_lock") else None

    results_map: dict[str, list[dict]] = {}

    def _search_one(query: str) -> tuple[str, list[dict]]:
        """1クエリを実行してタプルで返す（スレッド安全）。"""
        res = web_search(query)
        time.sleep(0.05)  # レート制限対策（並列でも個別にスリープ）
        return query, res

    # Rich Progress は Railway の非 TTY 環境で問題を起こす可能性があるため
    # TTY のときのみ使う（本番は plain print に）
    _use_progress = sys.stdout.isatty()
    if _use_progress:
        progress_ctx = Progress(
            SpinnerColumn(),
            TextColumn(f"[cyan]{label} 検索中...[/cyan] {{task.description}}"),
            BarColumn(bar_width=30),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
    else:
        progress_ctx = None

    def _advance(desc: str):
        if progress_ctx is not None:
            progress_ctx.update(_task_id, description=desc, advance=1)

    n_workers = min(_NUM_WORKERS, len(formatted_queries))
    print(f"[search] {label}: {len(formatted_queries)} queries × {n_workers} workers", flush=True)

    _ctx_enter = progress_ctx.__enter__() if progress_ctx else None
    _task_id = progress_ctx.add_task("", total=len(formatted_queries)) if progress_ctx else None
    try:
        # with ブロックを使うとスタックスレッドで shutdown が無限待ちになる。
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
        try:
            futures = {executor.submit(_search_one, q): q for q in formatted_queries}
            _TOTAL_TIMEOUT = 90
            done, not_done = concurrent.futures.wait(
                futures.keys(), timeout=_TOTAL_TIMEOUT,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            print(f"[search] {label}: done={len(done)} pending={len(not_done)}", flush=True)
            for future in done:
                try:
                    query, res = future.result(timeout=1)
                except Exception as e:
                    query = futures[future]
                    if _VERBOSE:
                        print(f"[search]  failure ({query[:40]}): {e}", flush=True)
                    res = []
                results_map[query] = res
                _advance(f'"{query[:40]}..."')
            for future in not_done:
                query = futures[future]
                print(f"[search]  timeout abandoned ({query[:40]})", flush=True)
                results_map.setdefault(query, [])
                future.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    finally:
        if progress_ctx:
            progress_ctx.__exit__(None, None, None)

    # クエリ順序を維持して重複除去
    for query in formatted_queries:
        for r in results_map.get(query, []):
            url = r.get("href", r.get("url", ""))
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)

    return all_results


def _call_with_timeout(fn, *args, timeout_sec: float = 120.0, default=None, label: str = ""):
    """任意の関数をハードタイムアウト付きで呼ぶ。超過時は default を返し、
    スレッドは放棄（daemon なのでプロセス終了で消える）。"""
    import threading
    holder: list = []
    err_holder: list = []

    def _runner():
        try:
            holder.append(fn(*args))
        except Exception as e:
            err_holder.append(e)

    t = threading.Thread(target=_runner, name=f"ct-{label[:20]}", daemon=True)
    print(f"[timeout-wrap] enter {label} (limit={timeout_sec}s)", flush=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        print(f"[timeout-wrap] EXPIRED {label} after {timeout_sec}s (abandoned)", flush=True)
        return default
    if err_holder:
        print(f"[timeout-wrap] error {label}: {err_holder[0]}", flush=True)
        raise err_holder[0]
    print(f"[timeout-wrap] done {label}", flush=True)
    return holder[0] if holder else default


# ─────────────────────────────────────────
# Claude 分析
# ─────────────────────────────────────────
def _parse_json_robust(raw: str) -> dict:
    """JSON抽出を複数の方法で試みる。fence/前後テキストが混ざっていても堅牢に処理。"""
    text = raw.strip()

    # 1) ```json ... ``` フェンス内を最大一致で抽出して試す
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    if fence_match:
        inner = fence_match.group(1)
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass

    # 2) 既存の単純なフェンス除去（バックワード互換）
    stripped = re.sub(r'^```(?:json)?\s*', '', text)
    stripped = re.sub(r'\s*```\s*$', '', stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 3) 最初の { から最後の } を抽出
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass

    # 4) 末尾が切れている場合の修復試行（ネスト順を正しく追跡して閉じる）
    if start != -1:
        partial = text[start:]
        # 未完了の文字列を閉じる
        attempt_base = partial.rstrip()
        # トレイリングカンマ除去
        attempt_base = re.sub(r",\s*$", "", attempt_base)
        # スキャンして { / [ のスタックを正しい順で閉じる
        stack: list[str] = []
        in_string = False
        escape = False
        for _ch in attempt_base:
            if escape:
                escape = False
                continue
            if in_string:
                if _ch == "\\":
                    escape = True
                elif _ch == '"':
                    in_string = False
                continue
            if _ch == '"':
                in_string = True
            elif _ch == "{":
                stack.append("}")
            elif _ch == "[":
                stack.append("]")
            elif _ch == "}" and stack and stack[-1] == "}":
                stack.pop()
            elif _ch == "]" and stack and stack[-1] == "]":
                stack.pop()
        # 未閉じ文字列の閉じ引用符
        closings = ""
        if in_string:
            closings += '"'
        # スタック末尾から逆順に閉じる
        closings += "".join(reversed(stack))
        # 不完全な key:value の末尾を整形（例: `"key": ` → `"key": null`）
        tail_fix = attempt_base.rstrip()
        if tail_fix.endswith(":"):
            attempt_base = tail_fix + " null"
        elif re.search(r'[,{\[]\s*$', tail_fix):
            # 末尾カンマや開き括弧直後 — 何も追加しない
            pass
        for candidate in [attempt_base + closings, attempt_base + closings + closings]:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # 5) 完全失敗: 診断情報を出す
    console.print(f"[yellow]  JSON解析失敗（raw {len(raw)} 字）先頭: {raw[:200]}[/yellow]")
    console.print(f"[yellow]  末尾: {raw[-300:]}[/yellow]")
    return {"raw": raw}


def _model_to_task(model: str | None) -> str:
    """Claude モデル名から llm_router のタスク名へマップ。
    Step の優先度に応じたタスク名を返す:
      MODEL_FAST  → summarization（軽量）
      MODEL_SMART → step_ab（Step A/B 用: 高速モデル優先）
      MODEL_DEEP  → step_c（Step C 用: 最高品質モデル優先）
    """
    if model == MODEL_FAST:
        return "summarization"
    if model == MODEL_DEEP:
        return "step_c"
    return "step_ab"


def _extract_json_via_router(step: str, prompt: str, max_tokens: int = 3000) -> dict:
    """ニュース/Amazon/TikTok/X/Instagram 分析で使う共通のJSON抽出。
    client.messages.create 直叩きを llm_router.call_json に置き換えるための薄いラッパ。
    `client=None` でワーカーから呼ばれるコードパスを安全化する。"""
    from core import llm_router
    parsed, resp = llm_router.call_json(
        task="extraction",
        system="",
        user=prompt,
        max_tokens=max_tokens,
    )
    if resp.usage:
        class _U:
            input_tokens = resp.usage.get("input_tokens", 0) or 0
            output_tokens = resp.usage.get("output_tokens", 0) or 0
        _track_cost(resp.model, _U(), step)
    if "_raw" in parsed:
        retry = _parse_json_robust(parsed["_raw"])
        if retry:
            return retry
        return {"error": "JSON parse failed", "raw": parsed["_raw"][:500]}
    return parsed


def analyze_with_claude(
    client=None,
    system_prompt: str = "",
    user_content: str = "",
    max_tokens: int = 4096,
    max_retries: int = 4,
    model: str | None = None,
) -> str:
    """LLM を呼び出してテキスト分析を実行する。

    `core.llm_router.call()` 経由で複数プロバイダ（Anthropic / OpenAI / Google）に
    フォールバックする。`client` 引数は後方互換のため残しているが使用しない。
    `model` は Claude のモデル名を受け取り、タスクカテゴリに変換されて
    llm_router のルーティング設定に従って処理される。
    """
    from core import llm_router

    task = _model_to_task(model)
    _BASE_WAIT = 30

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = llm_router.call(
                task, system_prompt, user_content,
                max_tokens=max_tokens,
            )
            if resp.usage:
                class _U:
                    input_tokens = resp.usage.get("input_tokens", 0) or 0
                    output_tokens = resp.usage.get("output_tokens", 0) or 0
                _track_cost(resp.model, _U())
            return resp.text

        except llm_router.NoProviderAvailable as e:
            raise AnalysisError(str(e)) from e

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = _BASE_WAIT * (2 ** attempt)
                console.print(
                    f"  [yellow]⚠ LLM エラー ({attempt + 1}/{max_retries}) "
                    f"— {wait}秒後にリトライ...[/yellow]"
                )
                time.sleep(wait)
                continue
            raise AnalysisError(
                f"LLM 呼び出しが {max_retries} 回失敗しました: {e}"
            ) from e

    raise AnalysisError("analyze_with_claude: 予期しないリトライループ終了")


def format_search_results(results: list[dict]) -> str:
    """検索結果を Claude に渡すテキスト形式に変換"""
    lines = []
    for i, r in enumerate(results[:20], 1):
        title = r.get("title", "")
        body  = r.get("body", r.get("snippet", ""))
        url   = r.get("href", r.get("url", ""))
        date  = r.get("date", r.get("published", ""))
        lines.append(f"[{i}] {title}")
        if date:
            lines.append(f"    日付: {date}")
        lines.append(f"    {body[:300]}")
        lines.append(f"    URL: {url}")
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────
# 分析モジュール①: 競合アカウント分析
# ─────────────────────────────────────────
def run_competitor_analysis(client: anthropic.Anthropic, keyword: str) -> dict:
    console.print(Rule("[bold cyan]競合アカウント分析[/bold cyan]"))

    raw = collect_search_data(COMPETITOR_QUERIES, keyword, "競合")
    # ニュースも追加
    news = news_search(f"{keyword} 大学 SNS 広報 2024 2025")
    raw += news

    console.print(f"  収集件数: [bold]{len(raw)}[/bold] 件")

    system = """あなたは大学SNSマーケティングの専門家です。
総合型選抜（AO入試）に特化したSNS戦略を分析・提案します。
必ず日本語で回答してください。"""

    user = f"""以下はWeb検索で収集した「{keyword}」に関する競合大学・受験情報アカウントの情報です。

=== 収集データ ===
{format_search_results(raw)}

上記データを分析して、以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "top_accounts": [
    {{
      "name": "アカウント名または大学名",
      "platform": "Instagram/Twitter/YouTube/note",
      "estimated_followers": "推定フォロワー数",
      "content_style": "コンテンツの特徴・スタイル",
      "strong_points": ["強み1", "強み2"],
      "weak_points": ["弱み1"],
      "ao_focus": "総合型選抜関連コンテンツへの注力度（高/中/低）"
    }}
  ],
  "market_overview": "市場全体の状況を2〜3文で",
  "competitive_gaps": ["競合が手薄なポイント1", "競合が手薄なポイント2", "競合が手薄なポイント3"],
  "benchmark_metrics": {{
    "avg_engagement_rate": "業界平均エンゲージメント率",
    "top_content_types": ["コンテンツ種類1", "コンテンツ種類2"],
    "posting_frequency": "一般的な投稿頻度"
  }},
  "insights": ["インサイト1", "インサイト2", "インサイト3"]
}}"""

    with console.status("[cyan]Claude が競合データを分析中...[/cyan]"):
        raw_response = analyze_with_claude(client, system, user, model=MODEL_EXTRACT)

    result = _parse_json_robust(raw_response)
    _print_competitor_result(result)
    return result


def _print_competitor_result(data: dict):
    accounts = data.get("top_accounts", [])
    if accounts:
        t = Table(title="競合アカウント", box=box.ROUNDED, show_lines=True)
        t.add_column("名前", style="bold white", max_width=20)
        t.add_column("媒体", style="cyan")
        t.add_column("フォロワー", justify="right")
        t.add_column("スタイル", max_width=30)
        t.add_column("AO注力", justify="center")
        for a in accounts:
            ao = a.get("ao_focus","")
            color = "green" if ao=="高" else "yellow" if ao=="中" else "red"
            t.add_row(
                a.get("name",""),
                a.get("platform",""),
                a.get("estimated_followers",""),
                a.get("content_style",""),
                f"[{color}]{ao}[/{color}]",
            )
        console.print(t)

    if data.get("competitive_gaps"):
        console.print(Panel(
            "\n".join(f"  • {g}" for g in data["competitive_gaps"]),
            title="[green]競合の手薄エリア（チャンス）[/green]",
            border_style="green",
        ))

    if data.get("insights"):
        console.print(Panel(
            "\n".join(f"  {i+1}. {ins}" for i, ins in enumerate(data["insights"])),
            title="[yellow]主要インサイト[/yellow]",
            border_style="yellow",
        ))


# ─────────────────────────────────────────
# 分析モジュール②: バズコンテンツ分析
# ─────────────────────────────────────────
def run_buzz_analysis(client: anthropic.Anthropic, keyword: str) -> dict:
    console.print(Rule("[bold magenta]バズコンテンツ分析[/bold magenta]"))

    raw = collect_search_data(BUZZ_QUERIES, keyword, "バズ")
    news = news_search(f"{keyword} バズ 話題 SNS 受験生 2025")
    raw += news

    console.print(f"  収集件数: [bold]{len(raw)}[/bold] 件")

    system = """あなたは大学受験・総合型選抜に特化したSNSコンテンツ戦略家です。
どのようなコンテンツが受験生に刺さり、拡散されるかを深く分析します。
必ず日本語で回答してください。"""

    user = f"""以下はWeb検索で収集した「{keyword}」関連のバズコンテンツ情報です。

=== 収集データ ===
{format_search_results(raw)}

上記データを分析して、以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "buzz_patterns": [
    {{
      "pattern_name": "パターン名",
      "description": "どんなコンテンツか",
      "why_it_works": "なぜ受験生に刺さるか",
      "platform": "最も効果的な媒体",
      "estimated_reach": "推定リーチ規模",
      "examples": ["具体例1", "具体例2"],
      "replicability": "再現しやすさ（高/中/低）"
    }}
  ],
  "emotional_triggers": ["感情的トリガー1", "感情的トリガー2", "感情的トリガー3"],
  "timing_insights": {{
    "best_days": ["最適曜日1", "最適曜日2"],
    "best_times": "最適投稿時間帯",
    "seasonal_peaks": ["シーズナルピーク1", "シーズナルピーク2"]
  }},
  "format_ranking": [
    {{"rank": 1, "format": "フォーマット名", "reason": "理由"}},
    {{"rank": 2, "format": "フォーマット名", "reason": "理由"}},
    {{"rank": 3, "format": "フォーマット名", "reason": "理由"}}
  ],
  "anti_patterns": ["避けるべきパターン1", "避けるべきパターン2"],
  "summary": "バズコンテンツの全体傾向を3文で"
}}"""

    with console.status("[magenta]Claude がバズパターンを分析中...[/magenta]"):
        raw_response = analyze_with_claude(client, system, user, model=MODEL_EXTRACT)

    result = _parse_json_robust(raw_response)
    _print_buzz_result(result)
    return result


def _print_buzz_result(data: dict):
    patterns = data.get("buzz_patterns", [])
    if patterns:
        t = Table(title="バズコンテンツパターン", box=box.ROUNDED, show_lines=True)
        t.add_column("パターン", style="bold magenta", max_width=18)
        t.add_column("説明", max_width=28)
        t.add_column("なぜ刺さる？", max_width=28)
        t.add_column("媒体", style="cyan")
        t.add_column("再現性", justify="center")
        for p in patterns:
            rep = p.get("replicability","")
            color = "green" if rep=="高" else "yellow" if rep=="中" else "red"
            t.add_row(
                p.get("pattern_name",""),
                p.get("description",""),
                p.get("why_it_works",""),
                p.get("platform",""),
                f"[{color}]{rep}[/{color}]",
            )
        console.print(t)

    if data.get("format_ranking"):
        t2 = Table(title="フォーマット効果ランキング", box=box.SIMPLE)
        t2.add_column("順位", justify="center", style="bold")
        t2.add_column("フォーマット", style="yellow")
        t2.add_column("理由")
        for f in data["format_ranking"]:
            medals = {1:"[gold1]1位[/gold1]", 2:"[silver]2位[/silver]", 3:"[orange3]3位[/orange3]"}
            t2.add_row(medals.get(f.get("rank",0), str(f.get("rank",""))), f.get("format",""), f.get("reason",""))
        console.print(t2)

    if data.get("emotional_triggers"):
        console.print(Panel(
            "  " + "  /  ".join(data["emotional_triggers"]),
            title="[red]感情的トリガー[/red]",
            border_style="red",
        ))

    if data.get("timing_insights"):
        ti = data["timing_insights"]
        console.print(Panel(
            f"  最適曜日: {', '.join(ti.get('best_days',[]))}\n"
            f"  最適時間帯: {ti.get('best_times','')}\n"
            f"  シーズナルピーク: {', '.join(ti.get('seasonal_peaks',[]))}",
            title="[cyan]投稿タイミング[/cyan]",
            border_style="cyan",
        ))


# ─────────────────────────────────────────
# 分析モジュール③: トレンドキーワード収集
# ─────────────────────────────────────────
def run_trend_analysis(client: anthropic.Anthropic, keyword: str) -> dict:
    console.print(Rule("[bold green]トレンドキーワード収集[/bold green]"))

    raw = collect_search_data(TREND_QUERIES, keyword, "トレンド")
    # ニュース追加
    news1 = news_search(f"{keyword} 最新情報 2025")
    news2 = news_search("大学入試 総合型選抜 変更 新制度 2026 2027")
    raw += news1 + news2

    console.print(f"  収集件数: [bold]{len(raw)}[/bold] 件")

    system = """あなたは大学受験トレンドとSEO・SNSキーワード戦略の専門家です。
受験生が今まさに検索・関心を持っているキーワードを精密に分析します。
必ず日本語で回答してください。"""

    user = f"""以下はWeb検索で収集した「{keyword}」のトレンド情報です（2024〜2025年）。

=== 収集データ ===
{format_search_results(raw)}

上記データを分析して、以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "hot_keywords": [
    {{
      "keyword": "キーワード",
      "trend": "上昇/安定/下降",
      "search_intent": "情報収集/比較/対策/不安解消",
      "target_audience": "高3/高2/保護者/全般",
      "content_opportunity": "このキーワードでどんなコンテンツが作れるか",
      "priority": "高/中/低"
    }}
  ],
  "emerging_topics": ["新興トピック1", "新興トピック2", "新興トピック3"],
  "declining_topics": ["衰退トピック1", "衰退トピック2"],
  "hashtag_suggestions": {{
    "x_twitter": ["#ハッシュタグ1", "#ハッシュタグ2", "#ハッシュタグ3", "#ハッシュタグ4", "#ハッシュタグ5"],
    "instagram": ["#ハッシュタグ1", "#ハッシュタグ2", "#ハッシュタグ3"],
    "note": ["タグ1", "タグ2", "タグ3"]
  }},
  "seasonal_calendar": [
    {{"month": "4月", "keywords": ["キーワード1", "キーワード2"], "action": "推奨アクション"}},
    {{"month": "5月", "keywords": ["キーワード1", "キーワード2"], "action": "推奨アクション"}},
    {{"month": "6月", "keywords": ["キーワード1", "キーワード2"], "action": "推奨アクション"}},
    {{"month": "7月", "keywords": ["キーワード1", "キーワード2"], "action": "推奨アクション"}},
    {{"month": "8月", "keywords": ["キーワード1", "キーワード2"], "action": "推奨アクション"}},
    {{"month": "9月", "keywords": ["キーワード1", "キーワード2"], "action": "推奨アクション"}}
  ],
  "trend_summary": "トレンド全体の傾向を3文で"
}}"""

    with console.status("[green]Claude がトレンドを分析中...[/green]"):
        raw_response = analyze_with_claude(client, system, user, model=MODEL_EXTRACT)

    result = _parse_json_robust(raw_response)
    _print_trend_result(result)
    return result


def _print_trend_result(data: dict):
    keywords = data.get("hot_keywords", [])
    if keywords:
        t = Table(title="ホットキーワード", box=box.ROUNDED, show_lines=True)
        t.add_column("キーワード", style="bold green", max_width=20)
        t.add_column("トレンド", justify="center")
        t.add_column("検索意図", style="dim")
        t.add_column("ターゲット", style="dim")
        t.add_column("コンテンツ機会", max_width=35)
        t.add_column("優先度", justify="center")
        for kw in keywords:
            trend = kw.get("trend","")
            tc = "red" if trend=="上昇" else "yellow" if trend=="安定" else "dim"
            arrow = "▲" if trend=="上昇" else "→" if trend=="安定" else "▼"
            pri = kw.get("priority","")
            pc = "bright_green" if pri=="高" else "yellow" if pri=="中" else "dim"
            t.add_row(
                kw.get("keyword",""),
                f"[{tc}]{arrow} {trend}[/{tc}]",
                kw.get("search_intent",""),
                kw.get("target_audience",""),
                kw.get("content_opportunity",""),
                f"[{pc}]{pri}[/{pc}]",
            )
        console.print(t)

    hashtags = data.get("hashtag_suggestions", {})
    if hashtags:
        lines = []
        for platform, tags in hashtags.items():
            lines.append(f"  [bold]{platform}[/bold]: " + "  ".join(f"[cyan]{t}[/cyan]" for t in tags))
        console.print(Panel("\n".join(lines), title="[cyan]推奨ハッシュタグ[/cyan]", border_style="cyan"))

    cal = data.get("seasonal_calendar", [])
    if cal:
        t2 = Table(title="月別キーワードカレンダー", box=box.SIMPLE)
        t2.add_column("月", style="bold", justify="center")
        t2.add_column("注力キーワード", style="green")
        t2.add_column("推奨アクション")
        for month in cal:
            t2.add_row(
                month.get("month",""),
                " / ".join(month.get("keywords",[])),
                month.get("action",""),
            )
        console.print(t2)


# ─────────────────────────────────────────
# 分析モジュール③-b: Googleトレンド取得
# ─────────────────────────────────────────
def run_google_trends(keyword: str) -> dict:
    console.print(Rule("[bold blue]Googleトレンド取得[/bold blue]"))

    if not PYTRENDS_AVAILABLE:
        console.print("[red]  pytrends がインストールされていません。[/red]")
        console.print("[dim]  pip install pytrends を実行してください[/dim]")
        return {"error": "pytrends not installed"}

    # キーワードリストに引数キーワードが含まれていなければ先頭に追加（最大5件）
    kw_list = list(GOOGLE_TRENDS_KEYWORDS)
    if keyword not in kw_list:
        kw_list = [keyword] + kw_list[:4]

    console.print(f"  対象キーワード: {', '.join(f'[cyan]{k}[/cyan]' for k in kw_list)}")

    try:
        pytrends = TrendReq(hl="ja", tz=540, timeout=(10, 25), retries=2, backoff_factor=0.5)
    except Exception as e:
        console.print(f"[red]  TrendReq 初期化失敗: {e}[/red]")
        return {"error": str(e)}

    result: dict = {
        "keywords_analyzed": kw_list,
        "timeframe": "today 3-m",
        "geo": "JP",
        "interest_over_time": [],
        "averages": {},
        "peaks": {},
        "related_queries": {},
        "related_topics": {},
    }

    # ── 時系列トレンド取得
    with console.status("[blue]Googleトレンド: 時系列データ取得中...[/blue]"):
        try:
            pytrends.build_payload(kw_list, cat=0, timeframe="today 3-m", geo="JP", gprop="")
            df = pytrends.interest_over_time()
            if not df.empty:
                if "isPartial" in df.columns:
                    df = df.drop(columns=["isPartial"])
                for col in df.columns:
                    result["averages"][col] = round(float(df[col].mean()), 1)
                    peak_date = df[col].idxmax()
                    result["peaks"][col] = str(peak_date.date()) if hasattr(peak_date, "date") else str(peak_date)
                # 週次に間引いて保存（全行だとNotionブロック上限に当たる）
                sampled = df.resample("W").mean().tail(12)
                for dt, row in sampled.iterrows():
                    entry = {"date": str(dt.date())}
                    for col in row.index:
                        entry[col] = round(float(row[col]), 1)
                    result["interest_over_time"].append(entry)
            console.print(f"  [green]✓[/green] 時系列: {len(result['interest_over_time'])} 週分取得")
        except Exception as e:
            console.print(f"  [yellow]  時系列取得失敗: {e}[/yellow]")

    time.sleep(0.2)  # レート制限対策

    # ── 関連クエリ取得
    with console.status("[blue]Googleトレンド: 関連クエリ取得中...[/blue]"):
        try:
            related = pytrends.related_queries()
            for kw, data in related.items():
                entry: dict = {}
                if data.get("top") is not None and not data["top"].empty:
                    entry["top"] = data["top"].head(10).to_dict(orient="records")
                if data.get("rising") is not None and not data["rising"].empty:
                    entry["rising"] = data["rising"].head(10).to_dict(orient="records")
                if entry:
                    result["related_queries"][kw] = entry
            console.print(f"  [green]✓[/green] 関連クエリ: {len(result['related_queries'])} キーワード分取得")
        except Exception as e:
            console.print(f"  [yellow]  関連クエリ取得失敗: {e}[/yellow]")

    time.sleep(0.2)

    # ── 関連トピック取得
    with console.status("[blue]Googleトレンド: 関連トピック取得中...[/blue]"):
        try:
            topics = pytrends.related_topics()
            for kw, data in topics.items():
                entry = {}
                if data.get("top") is not None and not data["top"].empty:
                    cols = [c for c in ["topic_title", "topic_type", "value"] if c in data["top"].columns]
                    entry["top"] = data["top"][cols].head(5).to_dict(orient="records")
                if data.get("rising") is not None and not data["rising"].empty:
                    cols = [c for c in ["topic_title", "topic_type", "value"] if c in data["rising"].columns]
                    entry["rising"] = data["rising"][cols].head(5).to_dict(orient="records")
                if entry:
                    result["related_topics"][kw] = entry
            console.print(f"  [green]✓[/green] 関連トピック: {len(result['related_topics'])} キーワード分取得")
        except Exception as e:
            console.print(f"  [yellow]  関連トピック取得失敗: {e}[/yellow]")

    _print_google_trends_result(result)
    return result


def _print_google_trends_result(data: dict):
    if data.get("error"):
        console.print(f"[red]Googleトレンド取得エラー: {data['error']}[/red]")
        return

    # 平均スコア表
    averages = data.get("averages", {})
    peaks    = data.get("peaks", {})
    if averages:
        t = Table(title="Googleトレンド スコア（直近3ヶ月・日本）", box=box.ROUNDED, show_lines=True)
        t.add_column("キーワード", style="bold blue", max_width=25)
        t.add_column("平均スコア", justify="right", style="cyan")
        t.add_column("ピーク日", style="dim")
        for kw, avg in sorted(averages.items(), key=lambda x: -x[1]):
            bar = "█" * int(avg / 10) + "░" * (10 - int(avg / 10))
            t.add_row(kw, f"{avg:>5.1f}  [dim]{bar}[/dim]", peaks.get(kw, ""))
        console.print(t)

    # 関連クエリ（上昇中のみ）
    related = data.get("related_queries", {})
    if related:
        for kw, entry in list(related.items())[:3]:  # 上位3キーワードのみ表示
            rising = entry.get("rising", [])
            if not rising:
                continue
            t2 = Table(title=f"「{kw}」急上昇クエリ", box=box.SIMPLE, show_header=True)
            t2.add_column("クエリ", style="yellow")
            t2.add_column("上昇率", justify="right", style="bright_green")
            for row in rising[:8]:
                val = str(row.get("value", ""))
                t2.add_row(str(row.get("query", "")), val if val else "Breakout")
            console.print(t2)


# ─────────────────────────────────────────
# 分析モジュール④: コンテンツ案自動生成
# ─────────────────────────────────────────
def run_content_proposals(
    client: anthropic.Anthropic,
    keyword: str,
    competitor_data: dict | None = None,
    buzz_data: dict | None = None,
    trend_data: dict | None = None,
) -> dict:
    console.print(Rule("[bold yellow]コンテンツ案 自動生成[/bold yellow]"))

    # 前段の分析結果を要点のみ圧縮して渡す
    context_parts = []
    if competitor_data:
        gaps = competitor_data.get("competitive_gaps", [])
        insights = competitor_data.get("insights", [])
        context_parts.append(
            "=== 競合の手薄エリア ===\n" + "\n".join(f"・{g}" for g in gaps) +
            "\n=== 競合インサイト ===\n" + "\n".join(f"・{i}" for i in insights)
        )
    if buzz_data:
        patterns = [f"・{p.get('pattern_name')}: {p.get('why_it_works')}" for p in buzz_data.get("buzz_patterns", [])]
        triggers = buzz_data.get("emotional_triggers", [])
        fr = [f"{f.get('rank')}位:{f.get('format')}" for f in buzz_data.get("format_ranking", [])]
        context_parts.append(
            "=== バズパターン ===\n" + "\n".join(patterns) +
            "\n感情的トリガー: " + " / ".join(triggers) +
            "\nフォーマットランキング: " + " / ".join(fr)
        )
    if trend_data:
        hot = [f"・{k.get('keyword')}({k.get('trend')}): {k.get('content_opportunity')}" for k in trend_data.get("hot_keywords", [])[:8]]
        emerging = trend_data.get("emerging_topics", [])
        context_parts.append(
            "=== 上昇トレンドキーワード ===\n" + "\n".join(hot) +
            "\n新興トピック: " + " / ".join(emerging)
        )

    context = "\n\n".join(context_parts) if context_parts else "（前段分析データなし）"

    system = """【受かるノウハウ10原則（必ずこの番号・名称で紐づけること）】

原則1: 志望校は偏差値ではなくマッチ度で決める
原則2: 自己分析は年表と感情から掘る
原則3: 実績は多さより意味づけが大事
原則4: 他人と競うのではなく自分の背景で差別化する
原則5: 経験を社会課題につなげる
原則6: 社会課題を大学での学びにつなげる
原則7: 学びを将来実現したいことにつなげる
原則8: 書類は羅列ではなく論理で書く
原則9: 面接は書類の一貫性を口頭で証明する場だと考える
原則10: 情報は集めるより選んで使う

knowhow_principle フィールドは必ず上記10原則の中から最も近いものを1つ選んで「原則N「原則名」」の形式で出力すること。
複数該当する場合は最も核心に近いものを1つに絞ること。上記以外の独自原則名は絶対に使わないこと。

あなたは大学の総合型選抜（AO入試）専門のSNSコンテンツストラテジストです。
受験生（主に高校2〜3年生）の心理・行動を深く理解し、エンゲージメントと問い合わせ転換率を最大化するコンテンツを設計します。
必ず日本語で回答してください。JSONのみを出力し、説明文や前置きは一切不要です。"""

    user = f"""ターゲットキーワード: 「{keyword}」
分析データ:
{context}

上記の分析結果を踏まえ、即実行可能な具体的コンテンツ案を生成してください。
コンテンツ案は最低8件、できれば12件以上生成してください。
以下のJSON形式のみで回答してください（コードブロック不要、説明不要、純粋なJSONのみ）:

{{
  "proposals": [
    {{
      "id": 1,
      "title": "コンテンツタイトル（具体的な投稿タイトル案）",
      "platform": "Instagram/X/YouTube/note/Threads",
      "format": "リール/カルーセル/スレッド/動画/記事 など",
      "category": "合格体験/対策情報/キャンパスライフ/Q&A/ニュース など",
      "hook": "冒頭フック文（最初の1〜2文、受験生の注目を引く）",
      "content_outline": [
        "本文構成1",
        "本文構成2",
        "本文構成3"
      ],
      "hashtags": ["#ハッシュタグ1", "#ハッシュタグ2"],
      "cta": "コール・トゥ・アクション（例：プロフィールのリンクから詳細を確認）",
      "estimated_reach": "推定リーチ",
      "priority": "S/A/B",
      "best_timing": "投稿最適タイミング",
      "production_effort": "低/中/高",
      "why_effective": "なぜこのコンテンツが効くか（1〜2文）",
      "outline": "この投稿の構成概要（冒頭・本文・締めの3点を各1行で）",
      "draft_hook": "実際に使えるフック文言の下書き（冒頭1〜2文、キャラ属性入り。「私」=偏差値40台から早慶上理に逆転合格、「僕」=実績ありでMARCHに落ちた塾講師）",
      "draft_body": "本文の下書き（箇条書き3点 or 短文2〜3文）",
      "draft_cta": "締め・CTAの下書き（コメント誘導またはプロフィール誘導）",
      "knowhow_principle": "根拠となる受かるノウハウ10原則の番号と原則名（例: 原則4「他人と競うのではなく自分の背景で差別化する」）",
      "knowhow_angle": "このコンテンツにどのノウハウ視点を加えると強くなるか（A=塾講師視点・B=逆転合格者視点・AB共通のどれかを明記して1〜2文で）",
      "ab_character": "「私」（逆転合格者）か「僕」（塾講師）どちらの視点で語るべきか・その理由",
      "weakness": "このコンテンツ案の弱点・競合と差別化できていない部分（正直に1文で）"
    }}
  ],
  "30day_plan": {{
    "week1": ["コンテンツ案ID番号や内容の概要"],
    "week2": ["コンテンツ案ID番号や内容の概要"],
    "week3": ["コンテンツ案ID番号や内容の概要"],
    "week4": ["コンテンツ案ID番号や内容の概要"]
  }},
  "quick_wins": ["今すぐできる施策1（低コスト高リターン）", "今すぐできる施策2", "今すぐできる施策3"],
  "kpi_targets": {{
    "follower_growth": "目標フォロワー増加数/月",
    "engagement_rate": "目標エンゲージメント率",
    "inquiry_conversion": "目標問い合わせ転換率",
    "reach": "目標月間リーチ"
  }}
}}

各案は本当に使える具体性にしてください。"""

    with console.status("[yellow]Claude がコンテンツ案を生成中... (少し時間がかかります)[/yellow]"):
        raw_response = analyze_with_claude(client, system, user, max_tokens=8192)

    result = _parse_json_robust(raw_response)

    _print_proposals_result(result)
    return result


def _print_proposals_result(data: dict):
    proposals = data.get("proposals", [])
    if proposals:
        priority_colors = {"S": "bright_red", "A": "yellow", "B": "green"}
        effort_colors   = {"低": "green", "中": "yellow", "高": "red"}

        t = Table(title=f"コンテンツ提案 ({len(proposals)}件)", box=box.ROUNDED, show_lines=True)
        t.add_column("No.", justify="center", style="dim", width=4)
        t.add_column("タイトル", style="bold", max_width=26)
        t.add_column("媒体", style="cyan", width=10)
        t.add_column("形式", width=10)
        t.add_column("フック", max_width=32)
        t.add_column("下書きフック", max_width=36)
        t.add_column("優先度", justify="center", width=5)
        t.add_column("制作工数", justify="center", width=6)
        t.add_column("タイミング", max_width=14)

        for p in proposals:
            pri = p.get("priority","")
            eff = p.get("production_effort","")
            t.add_row(
                str(p.get("id","")),
                p.get("title",""),
                p.get("platform",""),
                p.get("format",""),
                p.get("hook","")[:60],
                p.get("draft_hook","")[:70],
                f"[{priority_colors.get(pri,'white')}]{pri}[/{priority_colors.get(pri,'white')}]",
                f"[{effort_colors.get(eff,'white')}]{eff}[/{effort_colors.get(eff,'white')}]",
                p.get("best_timing",""),
            )
        console.print(t)

        # 詳細パネル（優先度Sのみ）
        s_proposals = [p for p in proposals if p.get("priority") == "S"]
        if s_proposals:
            console.print(f"\n[bold bright_red]優先度S — 詳細[/bold bright_red]")
            for p in s_proposals[:3]:
                outline = "\n".join(f"    {i+1}. {line}" for i, line in enumerate(p.get("content_outline", [])))
                hashtags = " ".join(p.get("hashtags", []))
                draft_body = p.get('draft_body', '')
                draft_cta = p.get('draft_cta', '')
                content = (
                    f"[bold]{p.get('hook','')}[/bold]\n\n"
                    f"[dim]構成:[/dim]\n{outline}\n\n"
                    f"[dim]下書きフック:[/dim] {p.get('draft_hook','')}\n"
                    f"[dim]下書き本文:[/dim] {draft_body}\n"
                    f"[dim]下書きCTA:[/dim] {draft_cta}\n\n"
                    f"[dim]ノウハウ原則:[/dim] {p.get('knowhow_principle','')}\n"
                    f"[dim]強化アングル:[/dim] {p.get('knowhow_angle','')}\n"
                    f"[dim]キャラクター:[/dim] {p.get('ab_character','')}\n"
                    f"[dim]弱点:[/dim] {p.get('weakness','')}\n\n"
                    f"[dim]CTA:[/dim] {p.get('cta','')}\n"
                    f"[dim]タグ:[/dim] [cyan]{hashtags}[/cyan]\n"
                    f"[dim]なぜ効く:[/dim] {p.get('why_effective','')}\n"
                    f"[dim]推定リーチ:[/dim] {p.get('estimated_reach','')}"
                )
                console.print(Panel(content, title=f"[bright_red]S: {p.get('title','')}[/bright_red] ({p.get('platform','')} / {p.get('format','')})", border_style="bright_red"))

    # 30日プラン
    plan = data.get("30day_plan", {})
    if plan:
        t2 = Table(title="30日コンテンツプラン", box=box.SIMPLE)
        t2.add_column("期間", style="bold", width=8)
        t2.add_column("投稿予定", style="dim")
        for week, items in plan.items():
            label = week.replace("week","第") + "週"
            t2.add_row(label, " → ".join(str(i) for i in items))
        console.print(t2)

    # KPI
    kpi = data.get("kpi_targets", {})
    if kpi:
        kpi_text = "  ".join(f"[bold]{k}[/bold]: {v}" for k, v in kpi.items())
        console.print(Panel(kpi_text, title="[green]KPI目標[/green]", border_style="green"))

    # Quick wins
    qw = data.get("quick_wins", [])
    if qw:
        console.print(Panel(
            "\n".join(f"  ✓ {w}" for w in qw),
            title="[bright_green]今すぐできる施策（Quick Wins）[/bright_green]",
            border_style="bright_green",
        ))


# ─────────────────────────────────────────
# 分析モジュール⑤: note.com 人気記事分析
# ─────────────────────────────────────────
NOTE_API_BASE = "https://note.com/api/v2/searches"
NOTE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _fetch_note_api(keyword: str, order: str = "like", size: int = 20) -> list[dict]:
    """note.com 公開検索 API から記事リストを取得する。失敗時は空リストを返す。"""
    params = urllib.parse.urlencode({
        "context": "note",
        "q": keyword,
        "size": size,
        "order": order,
    })
    url = f"{NOTE_API_BASE}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": NOTE_UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        contents = (
            data.get("data", {})
                .get("notes", {})
                .get("contents", [])
        )
        articles = []
        for c in contents:
            articles.append({
                "title":     c.get("name", ""),
                "excerpt":   c.get("description", "")[:200],
                "author":    c.get("user", {}).get("nickname", ""),
                "username":  c.get("user", {}).get("urlname", ""),
                "likes":     c.get("likeCount", 0),
                "comments":  c.get("commentCount", 0),
                "published": c.get("publishAt", "")[:10],
                "url":       c.get("noteUrl", ""),
            })
        return articles
    except Exception as e:
        console.print(f"  [yellow]  note API ({order}): {e}[/yellow]")
        return []


def _fetch_note_fallback(keyword: str) -> list[dict]:
    """DuckDuckGo で note.com 記事を補完取得する。"""
    raw = collect_search_data(NOTE_FALLBACK_QUERIES, keyword, "note補完")
    articles = []
    for r in raw:
        url = r.get("href", r.get("url", ""))
        if "note.com" not in url:
            continue
        articles.append({
            "title":    r.get("title", ""),
            "excerpt":  r.get("body", r.get("snippet", ""))[:200],
            "author":   "",
            "username": "",
            "likes":    None,
            "comments": None,
            "published": r.get("date", ""),
            "url":      url,
        })
    return articles


def run_note_analysis(client: anthropic.Anthropic, keyword: str) -> dict:
    console.print(Rule("[bold green]note.com 人気記事分析[/bold green]"))

    # ── 人気順・新着順の両方を取得
    with console.status("[green]note.com API: 人気順を取得中...[/green]"):
        liked   = _fetch_note_api(keyword, order="like",  size=20)
    time.sleep(0.2)
    with console.status("[green]note.com API: 新着順を取得中...[/green]"):
        newest  = _fetch_note_api(keyword, order="new",   size=10)

    # API が空ならフォールバック
    if not liked and not newest:
        console.print("  [yellow]note API から取得できませんでした。DuckDuckGo で補完します。[/yellow]")
        fallback = _fetch_note_fallback(keyword)
    else:
        fallback = []

    all_articles = liked + newest + fallback

    # URL で重複除去
    seen_urls: set[str] = set()
    unique: list[dict] = []
    for a in all_articles:
        if a["url"] and a["url"] not in seen_urls:
            seen_urls.add(a["url"])
            unique.append(a)

    console.print(f"  取得記事数: [bold]{len(unique)}[/bold] 件（人気順:{len(liked)} 新着:{len(newest)} 補完:{len(fallback)}）")

    if not unique:
        console.print("[yellow]  記事を取得できませんでした。[/yellow]")
        return {"articles": [], "error": "no articles fetched"}

    # Claude に渡すテキスト
    articles_text = "\n".join(
        f"[{i+1}] {a['title']}\n"
        f"    著者: {a['author'] or '不明'}  スキ: {a['likes'] if a['likes'] is not None else '?'}  "
        f"公開: {a['published']}\n"
        f"    {a['excerpt']}\n"
        f"    URL: {a['url']}"
        for i, a in enumerate(unique[:30])
    )

    system = """あなたは大学総合型選抜（AO入試）のSNSコンテンツ戦略家です。
note.comの人気記事データを分析し、受験生に刺さるnoteコンテンツの法則を抽出します。
必ず日本語で回答してください。JSONのみを出力してください。"""

    user = f"""以下はnote.comで「{keyword}」を検索して取得した記事一覧です。

=== 取得記事 ===
{articles_text}

上記を分析し、以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "top_articles": [
    {{
      "rank": 1,
      "title": "記事タイトル",
      "author": "著者名",
      "likes": 数値または null,
      "url": "URL",
      "why_popular": "なぜ人気か（1〜2文）",
      "content_type": "合格体験記/対策解説/体験談/Q&A/まとめ/その他"
    }}
  ],
  "popular_patterns": [
    {{
      "pattern": "パターン名",
      "description": "どんな記事か",
      "why_effective": "なぜ受験生に刺さるか",
      "examples": ["タイトル例1", "タイトル例2"],
      "replicability": "高/中/低"
    }}
  ],
  "title_formulas": ["タイトルの法則1", "タイトルの法則2", "タイトルの法則3"],
  "content_gaps": ["競合が書いていない空白テーマ1", "空白テーマ2", "空白テーマ3"],
  "top_authors": [
    {{
      "name": "著者名",
      "style": "投稿スタイルの特徴",
      "strength": "強み"
    }}
  ],
  "recommended_topics": [
    {{
      "topic": "推奨トピック",
      "reason": "なぜ今書くべきか",
      "title_draft": "具体的なタイトル案",
      "priority": "S/A/B"
    }}
  ],
  "summary": "note全体の傾向を3文で"
}}"""

    with console.status("[green]Claude が note 記事を分析中...[/green]"):
        raw_response = analyze_with_claude(client, system, user, max_tokens=4096)

    result = _parse_json_robust(raw_response)
    result["_raw_articles"] = unique[:30]   # 生データも保持
    _print_note_result(result)
    return result


def _print_note_result(data: dict):
    if data.get("error"):
        console.print(f"[red]note分析エラー: {data['error']}[/red]")
        return

    # 人気記事 TOP
    top = data.get("top_articles", [])
    if top:
        t = Table(title="note 人気記事 TOP", box=box.ROUNDED, show_lines=True)
        t.add_column("順位", justify="center", width=4)
        t.add_column("タイトル", style="bold green", max_width=32)
        t.add_column("著者", style="cyan", max_width=14)
        t.add_column("スキ", justify="right")
        t.add_column("種別", style="dim", max_width=12)
        t.add_column("なぜ人気？", max_width=30)
        for a in top[:8]:
            likes = str(a.get("likes", "?")) if a.get("likes") is not None else "?"
            t.add_row(
                str(a.get("rank", "")),
                a.get("title", ""),
                a.get("author", ""),
                likes,
                a.get("content_type", ""),
                a.get("why_popular", ""),
            )
        console.print(t)

    # 人気パターン
    patterns = data.get("popular_patterns", [])
    if patterns:
        t2 = Table(title="人気コンテンツパターン", box=box.ROUNDED, show_lines=True)
        t2.add_column("パターン", style="bold", max_width=18)
        t2.add_column("説明", max_width=28)
        t2.add_column("なぜ刺さる", max_width=28)
        t2.add_column("再現性", justify="center", width=6)
        for p in patterns:
            rep = p.get("replicability", "")
            color = "green" if rep == "高" else "yellow" if rep == "中" else "red"
            t2.add_row(
                p.get("pattern", ""),
                p.get("description", ""),
                p.get("why_effective", ""),
                f"[{color}]{rep}[/{color}]",
            )
        console.print(t2)

    # 推奨トピック
    topics = data.get("recommended_topics", [])
    if topics:
        priority_colors = {"S": "bright_red", "A": "yellow", "B": "green"}
        t3 = Table(title="推奨トピック", box=box.SIMPLE)
        t3.add_column("優先度", justify="center", width=5)
        t3.add_column("トピック", style="bold", max_width=20)
        t3.add_column("タイトル案", style="cyan", max_width=30)
        t3.add_column("理由", max_width=30)
        for tp in topics:
            pri = tp.get("priority", "")
            t3.add_row(
                f"[{priority_colors.get(pri, 'white')}]{pri}[/{priority_colors.get(pri, 'white')}]",
                tp.get("topic", ""),
                tp.get("title_draft", ""),
                tp.get("reason", ""),
            )
        console.print(t3)

    # タイトルの法則
    formulas = data.get("title_formulas", [])
    if formulas:
        console.print(Panel(
            "\n".join(f"  {i+1}. {f}" for i, f in enumerate(formulas)),
            title="[yellow]タイトルの法則[/yellow]",
            border_style="yellow",
        ))

    # 空白テーマ
    gaps = data.get("content_gaps", [])
    if gaps:
        console.print(Panel(
            "\n".join(f"  • {g}" for g in gaps),
            title="[bright_green]競合の空白テーマ（チャンス）[/bright_green]",
            border_style="bright_green",
        ))


# ─────────────────────────────────────────
# 分析モジュール⑥: 大学 総合型選抜 募集要項取得
# ─────────────────────────────────────────
def _extract_text_from_html(html_bytes: bytes) -> str:
    """標準ライブラリの html.parser でテキストを抽出する。HTML エンティティも処理する。"""
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self._skip = False
            self._depth = 0
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "header", "footer", "aside", "noscript"):
                self._skip = True
                self._depth += 1
            # テーブルセルの区切りを明示（倍率テーブル解析に必要）
            if tag in ("td", "th", "br", "li", "tr") and not self._skip:
                self.parts.append("\t")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "header", "footer", "aside", "noscript"):
                self._depth -= 1
                if self._depth <= 0:
                    self._skip = False
                    self._depth = 0
            if tag in ("tr", "p", "div", "h1", "h2", "h3", "h4", "li") and not self._skip:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self._skip:
                t = data.strip()
                if t:
                    self.parts.append(t)

        def handle_entityref(self, name):
            """&nbsp; 等の名前付きエンティティを変換する。"""
            if not self._skip:
                char = _html_module.unescape(f"&{name};")
                if char and char.strip():
                    self.parts.append(char.strip())
                elif name == "nbsp":
                    self.parts.append(" ")

        def handle_charref(self, name):
            """&#160; 等の数値参照を変換する。"""
            if not self._skip:
                char = _html_module.unescape(f"&#{name};")
                if char and char.strip():
                    self.parts.append(char.strip())

    parser = _Extractor()
    try:
        html_str = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html_str = html_bytes.decode("latin-1", errors="replace")
    # &nbsp; を先に変換（パーサーに渡す前）
    html_str = _html_module.unescape(html_str)
    parser.feed(html_str)
    raw = "\n".join(parser.parts)
    # 連続タブ・空白を整理しつつ数値データの区切りを保持
    raw = re.sub(r"\t+", "\t", raw)
    raw = re.sub(r" {2,}", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw


_page_cache: dict[str, str] = {}
_PAGE_CACHE_MAX = 200  # 最大200 URL分キャッシュ

def _fetch_raw_html(url: str, max_bytes: int = 200_000, timeout: int = 12) -> str:
    """URL から生 HTML を取得（meta タグ抽出用）。失敗時は空文字列。"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": NOTE_UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
        # charset 推定
        charset = "utf-8"
        ct = resp.headers.get("Content-Type", "") if hasattr(resp, "headers") else ""
        m = re.search(r"charset=([\w-]+)", ct, re.I)
        if m:
            charset = m.group(1)
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _fetch_researchmap_profiles(
    university: str, faculty: str, max_profiles: int = 10
) -> list[dict]:
    """researchmap.jp から (大学 × 学部) に所属する研究者プロファイルを取得。
    各プロファイルから氏名・職位・研究キーワード・研究分野・プロファイルURLを抽出。
    戻り値: [{name, professor, theme, url, position, keywords}, ...]
    """
    if not university:
        return []

    # researchmap.jp の研究者検索ページを直接叩く。
    # DDGS 経由だと個人プロファイル URL が上位に出にくいため、公式検索結果ページを優先。
    # Query: q に「大学名 学部名」、affiliation_text に大学名を入れる
    candidate_urls: list[str] = []
    seen: set[str] = set()

    def _collect_from_search_html(html: str):
        """researchmap 検索結果HTMLから /<slug> プロファイルリンクを抽出"""
        # researchmap の検索結果には <a href="/<slug>"> または <a href="https://researchmap.jp/<slug>">
        # 形式で個人リンクが含まれる
        hrefs = re.findall(r'href=["\'](?:https?://researchmap\.jp)?/([A-Za-z0-9_.\-]+)/?["\']', html)
        exclude = {
            "researchers", "search", "tags", "api", "about", "help", "login",
            "signup", "terms", "privacy", "news", "feedback", "developers",
            "contact", "toppage", "guide", "ja", "en", "", "mypage",
        }
        for slug in hrefs:
            if slug in exclude or "/" in slug or slug.startswith("_"):
                continue
            url = f"https://researchmap.jp/{slug}"
            if url in seen:
                continue
            seen.add(url)
            candidate_urls.append(url)

    search_queries = [
        f'{university} {faculty}' if faculty else university,
        university,
    ]
    for q in search_queries:
        if len(candidate_urls) >= max_profiles * 3:
            break
        try:
            search_url = (
                f"https://researchmap.jp/researchers?"
                f"q={urllib.parse.quote(q)}"
                f"&affiliation_text={urllib.parse.quote(university)}"
            )
            html = _fetch_raw_html(search_url, max_bytes=250_000)
            if html:
                _collect_from_search_html(html)
        except Exception:
            continue

    # フォールバック: DDGS 経由でも検索
    if len(candidate_urls) < 3:
        fac_token = (faculty or "").replace("学部", "").replace("学科", "").strip()
        fallback_queries = [
            f'site:researchmap.jp "{university}" "{faculty}"' if faculty else f'site:researchmap.jp "{university}"',
        ]
        if fac_token and fac_token != faculty:
            fallback_queries.append(f'site:researchmap.jp "{university}" "{fac_token}"')
        for q in fallback_queries:
            for r in web_search(q, max_results=15, timeout=15):
                url = r.get("href") or r.get("link") or ""
                m = re.match(r"^https?://researchmap\.jp/([A-Za-z0-9_.\-]+)/?(?:\?|$)", url)
                if not m:
                    continue
                slug = m.group(1)
                if slug in ("researchers", "search", "tags", "api", "about", "help", "login"):
                    continue
                clean_url = url.split("?")[0].rstrip("/")
                if clean_url in seen:
                    continue
                seen.add(clean_url)
                candidate_urls.append(clean_url)
                if len(candidate_urls) >= max_profiles * 2:
                    break
            if len(candidate_urls) >= max_profiles * 2:
                break

    profiles: list[dict] = []
    position_kws = ("教授", "准教授", "専任講師", "講師", "助教", "特任教授", "特任准教授", "名誉教授")

    for url in candidate_urls:
        html = _fetch_raw_html(url, max_bytes=120_000)
        if not html:
            continue

        # og:title から氏名抽出
        name = ""
        m_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m_title:
            raw_title = _html_module.unescape(m_title.group(1))
            # 例: "山田 太郎 - マイポータル - researchmap"
            name = re.split(r"\s*[-–—]\s*", raw_title)[0].strip()
        if not name:
            m_h1 = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
            if m_h1:
                name = _html_module.unescape(m_h1.group(1)).strip()
        if not name:
            continue

        # 職位
        position = ""
        head = html[:20_000]
        for kw in position_kws:
            if kw in head:
                position = kw
                break

        # 研究キーワード（og:description or 本文）
        keywords: list[str] = []
        m_desc = re.search(r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m_desc:
            desc = _html_module.unescape(m_desc.group(1))
            # 「研究キーワード:」「研究分野:」から拾う
            m_kw = re.search(r"研究キーワード[：:]\s*([^\|]+?)(?:\||\s*研究分野|$)", desc)
            if m_kw:
                keywords = [k.strip() for k in re.split(r"[、,/／]", m_kw.group(1)) if k.strip()][:6]

        # 研究テーマ要約（og:description の先頭 ~150字）
        theme = ""
        if m_desc:
            theme = _html_module.unescape(m_desc.group(1))[:150]

        profiles.append({
            "name": f"{name}{('（' + position + '）') if position else ''}",
            "professor": name,
            "position": position,
            "theme": theme,
            "keywords": keywords,
            "url": url,
        })
        if len(profiles) >= max_profiles:
            break

    return profiles


def _extract_faculty_from_page(url: str, max_profiles: int = 15) -> list[dict]:
    """大学公式の教員紹介ページから教員リストを抽出する。
    複数の URL パターン（/faculty/, /professor/, /staff/, /teachers/, /members/ 等）を試行。
    戻り値: [{name, professor, theme, url}, ...]
    """
    if not url:
        return []

    base = url.rstrip("/")
    # よくあるサブパスを試行（試行数を絞って時間削減）
    candidates = [
        base,
        base + "/faculty/",
        base + "/teachers/",
    ]
    seen: set[str] = set()

    profiles: list[dict] = []
    # 日本人氏名 + 肩書きのパターン
    # 姓名は漢字2〜6字 + スペース/全角スペース + 漢字2〜6字
    # 肩書きは 教授/准教授/特任教授/講師/助教/名誉教授
    name_title_pat = re.compile(
        r"([一-龥々〆ヵヶ]{1,4}[\s　]*[一-龥々〆ヵヶ]{1,6})[\s　]*"
        r"(教授|准教授|特任教授|特任准教授|特任講師|専任講師|講師|助教|名誉教授)"
    )
    # link from name to profile
    link_name_pat = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([一-龥々〆ヵヶ]{1,4}[\s　]+[一-龥々〆ヵヶ]{1,6})\s*</a>',
        re.I,
    )

    for cand_url in candidates:
        if cand_url in seen:
            continue
        seen.add(cand_url)
        try:
            html = _fetch_raw_html(cand_url, max_bytes=300_000, timeout=6)
        except Exception:
            continue
        if not html or len(html) < 500:
            continue

        # Text から氏名+肩書きを抽出
        txt = _extract_text_from_html(html.encode("utf-8"))
        matches_names: dict[str, str] = {}  # name -> title
        for m in name_title_pat.finditer(txt):
            nm = re.sub(r"[\s　]+", " ", m.group(1).strip())
            ttl = m.group(2)
            if len(nm) >= 2 and nm not in matches_names:
                matches_names[nm] = ttl

        # HTML 内の <a> で氏名にリンクが張られているパターンも拾う
        link_by_name: dict[str, str] = {}
        for m in link_name_pat.finditer(html):
            href, nm = m.group(1), re.sub(r"[\s　]+", " ", m.group(2).strip())
            if not href.startswith("http"):
                href = urllib.parse.urljoin(cand_url, href)
            if nm not in link_by_name:
                link_by_name[nm] = href

        for nm, ttl in matches_names.items():
            if len(profiles) >= max_profiles:
                break
            profiles.append({
                "name": f"{nm}（{ttl}）",
                "professor": nm,
                "position": ttl,
                "theme": "",
                "url": link_by_name.get(nm, cand_url),
            })
        if len(profiles) >= 5:
            console.print(f"  [green]✓ 教員ページから {len(profiles)} 名抽出: {cand_url[:70]}[/green]")
            return profiles[:max_profiles]

    return profiles[:max_profiles]


def _extract_urls_from_text(text: str) -> list[str]:
    """テキスト内の http/https URL を抽出。末尾の ）」。, 等を除去。"""
    if not text:
        return []
    urls = re.findall(r'https?://[^\s<>"\'\)\]】）」』、。]+', text)
    # 末尾の句読点を除去
    cleaned = []
    for u in urls:
        u = u.rstrip(".,;:)]」）】")
        cleaned.append(u)
    return cleaned


def _fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """URL からページ本文テキストを取得する。失敗時は空文字列を返す。キャッシュあり。"""
    cache_key = url
    if cache_key in _page_cache:
        return _page_cache[cache_key][:max_chars]
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": NOTE_UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
        text = _extract_text_from_html(raw)
        # 連続する空行を圧縮
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(_page_cache) < _PAGE_CACHE_MAX:
            _page_cache[cache_key] = text
        return text[:max_chars]
    except urllib.error.HTTPError as e:
        if _VERBOSE:
            console.print(f"  [dim yellow]HTTP {e.code}: {url[:60]}[/dim yellow]")
        return ""
    except urllib.error.URLError as e:
        if _VERBOSE:
            console.print(f"  [dim yellow]URL取得失敗 ({e.reason}): {url[:60]}[/dim yellow]")
        return ""
    except TimeoutError:
        if _VERBOSE:
            console.print(f"  [dim yellow]タイムアウト: {url[:60]}[/dim yellow]")
        return ""
    except Exception as e:
        if _VERBOSE:
            console.print(f"  [dim yellow]取得失敗 ({type(e).__name__}): {url[:60]}[/dim yellow]")
        return ""


def _extract_pdf_from_viewer_url(url: str) -> str:
    """viewer ページの URL から生 PDF の URL を抽出する。
    `pdfviewer?url=...pdf` / `viewer.html?file=...pdf` / `#file=...pdf` などに対応。"""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        # query の各パラメータに .pdf が含まれていれば抽出
        for key, vals in urllib.parse.parse_qs(parsed.query).items():
            for v in vals:
                if ".pdf" in v.lower():
                    decoded = urllib.parse.unquote(v)
                    if decoded.startswith("http"):
                        return decoded
                    # 相対パス: viewer ページと同じドメインに組み立て
                    return urllib.parse.urljoin(url, decoded)
        # fragment (#file=...) にも対応
        if parsed.fragment:
            for part in parsed.fragment.split("&"):
                if "=" in part:
                    _, v = part.split("=", 1)
                    if ".pdf" in v.lower():
                        decoded = urllib.parse.unquote(v)
                        if decoded.startswith("http"):
                            return decoded
                        return urllib.parse.urljoin(url, decoded)
    except Exception:
        pass
    return ""


def _extract_pdf_from_pdfjs_viewer(page_url: str, html_body: str) -> str:
    """PDF.js 系ビューア（52school.com 形式等）の HTML から raw PDF URL を抽出。
    ・HTML に `pdfviewer/viewer.html` or `/viewer.html` iframe があるかチェック
    ・`js/viewerSetting.js` を取得して `VIEW_PATH` と `PDF_PATH` を読む
    ・`{origin}{VIEW_PATH}{PDF_PATH}` を組み立てて返す"""
    try:
        # HTML内に PDF.js ビューア的要素があるかざっくり検出
        if not re.search(r'(pdfviewer|viewer\.html|pdf\.js|viewerSetting\.js)', html_body, re.I):
            return ""
        # viewerSetting.js の URL 候補
        candidates = [
            urllib.parse.urljoin(page_url, "js/viewerSetting.js"),
            urllib.parse.urljoin(page_url, "./js/viewerSetting.js"),
            urllib.parse.urljoin(page_url, "../js/viewerSetting.js"),
        ]
        # HTML 内の script src から直接パス取得も試す
        for m in re.finditer(r'script\s+src=["\']([^"\']*viewerSetting\.js[^"\']*)["\']', html_body, re.I):
            src = m.group(1)
            candidates.insert(0, urllib.parse.urljoin(page_url, src))
        for setting_url in candidates:
            try:
                req = urllib.request.Request(setting_url, headers={"User-Agent": NOTE_UA})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    js_body = resp.read().decode("utf-8", errors="replace")
                m_view = re.search(r'VIEW_PATH\s*=\s*["\']([^"\']+)["\']', js_body)
                m_pdf  = re.search(r'PDF_PATH\s*=\s*["\']([^"\']+)["\']', js_body)
                if m_pdf:
                    pdf_path = m_pdf.group(1)
                    if pdf_path.startswith("http"):
                        return pdf_path
                    # VIEW_PATH を使って絶対URL組み立て
                    view_path = m_view.group(1) if m_view else ""
                    parsed = urllib.parse.urlparse(page_url)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    # VIEW_PATH が絶対なら origin と結合、相対なら page_url 基点
                    if view_path.startswith("/"):
                        return origin + view_path + pdf_path.lstrip("/")
                    if view_path:
                        return urllib.parse.urljoin(page_url, view_path + pdf_path)
                    return urllib.parse.urljoin(page_url, pdf_path)
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _find_pdf_url_from_page(page_url: str, keyword: str) -> str:
    """大学公式ページからPDFリンクを自動発見する。
    募集要項・入学者選抜要項・アドミッション関連のPDFを優先して返す。
    iframe/embed タグ経由でPDFビューアに埋め込まれたPDFも抽出する。
    """
    PDF_PRIORITY_WORDS = [
        "募集要項", "boshu", "youkou", "yoko",
        "入学者選抜", "nyushi", "admission",
        "総合型", "学校推薦", "ao",
    ]
    try:
        # まず viewer URL 形式なら即時抽出
        viewer_extracted = _extract_pdf_from_viewer_url(page_url)
        if viewer_extracted:
            return viewer_extracted

        body = _fetch_page_text(page_url, max_chars=15000)
        parsed = urllib.parse.urlparse(page_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        candidates: list[str] = []

        # (1) 通常の絶対URL .pdf
        candidates.extend(re.findall(
            r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*', body, re.I
        ))
        # (2) href/src=".pdf" の相対パス
        for attr in ("href", "src", "data", "data-src"):
            rels = re.findall(
                rf'{attr}=["\']([^"\']+\.pdf[^"\']*)["\']', body, re.I
            )
            for link in rels:
                candidates.append(link if link.startswith("http") else urllib.parse.urljoin(page_url, link))
        # (3) iframe/embed/object の viewer URL 経由
        iframe_srcs = re.findall(
            r'(?:<iframe|<embed|<object)[^>]+?(?:src|data)=["\']([^"\']+)["\']', body, re.I
        )
        for src in iframe_srcs:
            abs_src = src if src.startswith("http") else urllib.parse.urljoin(page_url, src)
            extracted = _extract_pdf_from_viewer_url(abs_src)
            if extracted:
                candidates.append(extracted)
            elif ".pdf" in abs_src.lower():
                candidates.append(abs_src)

        # 重複除去（順序保持）
        seen: set = set()
        unique: list[str] = []
        for u in candidates:
            if u and u not in seen:
                seen.add(u)
                unique.append(u)

        # 優先ワードを含むPDFを先頭に並べ替え
        def _priority(url):
            url_lower = url.lower()
            for i, word in enumerate(PDF_PRIORITY_WORDS):
                if word in url_lower:
                    return i
            return len(PDF_PRIORITY_WORDS)

        unique.sort(key=_priority)
        return unique[0] if unique else ""
    except Exception:
        return ""


def _fetch_pdf_text(
    pdf_url: str,
    max_chars: int = 30000,
    focus_keywords: list[str] | None = None,
) -> str:
    """PDFのURLからテキストを抽出する（pdfplumber使用）。
    pdfplumber未インストール時は警告を表示して空文字列を返す。

    focus_keywords: 指定すると PDF 全体をまずスキャンし、該当キーワードを含む
    ページとその前後 ±2 頁を優先抽出対象にする。長大な要項PDF（100頁超）で
    対象学部セクションが後半にある場合に文字数上限内で確実に取れる。
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        console.print("[yellow]  pdfplumber 未インストール。以下でインストールしてください:[/yellow]")
        console.print("[dim]  pip install pdfplumber[/dim]")
        return ""

    import tempfile

    # viewer URL が渡されてきた場合は raw PDF に変換
    viewer_extracted = _extract_pdf_from_viewer_url(pdf_url)
    if viewer_extracted:
        console.print(f"[dim]viewer URL → raw PDF: {viewer_extracted[:80]}[/dim]")
        pdf_url = viewer_extracted

    console.print(f"[cyan]PDF取得中: {pdf_url[:80]}...[/cyan]")
    tmp_path = ""
    try:
        req = urllib.request.Request(
            pdf_url,
            headers={
                "User-Agent": NOTE_UA,
                "Accept": "application/pdf,*/*",
                "Accept-Language": "ja,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()
            ctype = (resp.headers.get("Content-Type", "") or "").lower()

        # Content-Type が HTML 系の場合は viewer HTML を掴んだ可能性 → 中身から再探索
        if "text/html" in ctype and not pdf_url.lower().endswith(".pdf"):
            html_body = pdf_bytes.decode("utf-8", errors="replace")
            # まず PDF.js 系ビューア（52school.com 形式等）対応を試みる
            pdfjs_extracted = _extract_pdf_from_pdfjs_viewer(pdf_url, html_body)
            if pdfjs_extracted:
                console.print(f"[green]  ✓ PDF.js ビューア → raw PDF 抽出: {pdfjs_extracted[:80]}[/green]")
            iframe_srcs = re.findall(
                r'(?:<iframe|<embed|<object)[^>]+?(?:src|data)=["\']([^"\']+)["\']',
                html_body, re.I,
            )
            nested_pdf = pdfjs_extracted  # PDF.js 経路が見つかっていれば最優先
            if not nested_pdf:
                for src in iframe_srcs:
                    abs_src = src if src.startswith("http") else urllib.parse.urljoin(pdf_url, src)
                    candidate = _extract_pdf_from_viewer_url(abs_src) or (abs_src if abs_src.lower().endswith(".pdf") else "")
                    if candidate:
                        nested_pdf = candidate
                        break
                    # iframe が更に HTML (PDF.js viewer.html) を指している場合は、その URL で再帰的に PDF.js 抽出を試す
                    if abs_src.endswith((".html", "/")) and "viewer" in abs_src.lower():
                        try:
                            _req_ifr = urllib.request.Request(abs_src, headers={"User-Agent": NOTE_UA})
                            with urllib.request.urlopen(_req_ifr, timeout=10) as _resp_ifr:
                                _ifr_body = _resp_ifr.read().decode("utf-8", errors="replace")
                            _nested_pdfjs = _extract_pdf_from_pdfjs_viewer(abs_src, _ifr_body)
                            if _nested_pdfjs:
                                nested_pdf = _nested_pdfjs
                                console.print(f"[green]  ✓ iframe経由のPDF.jsビューアから抽出: {_nested_pdfjs[:80]}[/green]")
                                break
                        except Exception:
                            pass
            if nested_pdf:
                console.print(f"[dim]HTML → 内包PDF検出: {nested_pdf[:80]}[/dim]")
                req2 = urllib.request.Request(
                    nested_pdf,
                    headers={"User-Agent": NOTE_UA, "Accept": "application/pdf,*/*", "Referer": pdf_url},
                )
                with urllib.request.urlopen(req2, timeout=30) as resp2:
                    pdf_bytes = resp2.read()
            else:
                # PDF が無くても HTML 本文を抽出してテキストとして返す
                # （立教など HTML のみで募集要項を公開している大学対応）
                try:
                    html_text = _extract_text_from_html(html_body.encode("utf-8"))
                    html_text = re.sub(r"\n{3,}", "\n\n", html_text)
                    html_text = html_text[:max_chars]
                except Exception:
                    html_text = ""
                # URL 変形: .pdf 拡張子を試す（ビューアの多くは裏に .pdf を持つ）
                if not html_text or len(html_text) < 200:
                    _alt_candidates = []
                    _p = urllib.parse.urlparse(pdf_url)
                    if not _p.path.lower().endswith(".pdf"):
                        _alt_candidates.append(pdf_url.rstrip("/") + ".pdf")
                        _alt_candidates.append(pdf_url.rstrip("/") + "/index.pdf")
                    for _alt in _alt_candidates:
                        try:
                            _req = urllib.request.Request(
                                _alt,
                                headers={"User-Agent": NOTE_UA, "Accept": "application/pdf,*/*"},
                            )
                            with urllib.request.urlopen(_req, timeout=15) as _resp:
                                _alt_bytes = _resp.read()
                                _alt_ctype = (_resp.headers.get("Content-Type", "") or "").lower()
                            if "pdf" in _alt_ctype or _alt_bytes[:4] == b"%PDF":
                                pdf_bytes = _alt_bytes
                                console.print(f"[green]  ✓ .pdf 拡張子変形で取得成功: {_alt[:60]}[/green]")
                                break
                        except Exception:
                            continue
                    else:
                        # .pdf 変形もダメなら HTML 本文をそのまま返す
                        if html_text and len(html_text) >= 200:
                            console.print(f"[green]  ✓ HTML本文を抽出（PDFなし・{len(html_text):,}字）: {pdf_url[:60]}[/green]")
                            return html_text
                        console.print(f"[yellow]  ✗ PDF ではなく HTML（Content-Type: {ctype}）。本文 {len(html_text)}字しか取れず[/yellow]")
                        return html_text or ""
                elif html_text:
                    console.print(f"[green]  ✓ HTML本文を抽出（PDFなし・{len(html_text):,}字）: {pdf_url[:60]}[/green]")
                    return html_text

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        # 書き込み後は pdf_bytes を解放（巨大PDFの二重保持を避ける）
        del pdf_bytes
        import gc
        gc.collect()

        text_parts: list[str] = []
        accum_chars = 0
        # pdfplumber は暗号化PDF でも password="" で開けるケースが多い
        try:
            pdf_ctx = pdfplumber.open(tmp_path, password="")
        except Exception:
            pdf_ctx = pdfplumber.open(tmp_path)
        with pdf_ctx as pdf:
            total = len(pdf.pages)
            # 長い要項PDF（全学共通部分＋学部別セクション）に対応するため 150 頁まで読む
            max_pages = min(total, int(os.environ.get("RESEARCH_PDF_MAX_PAGES", "150")))

            # Phase 1: 全ページの text をスキャン（テーブル抽出は focus 頁のみ後で実施）
            # focus_keywords があれば該当頁を特定するため先に全走査が必要
            pages_text: list[str] = []
            for i in range(max_pages):
                page = pdf.pages[i]
                try:
                    pt = page.extract_text() or ""
                except Exception:
                    pt = ""
                pages_text.append(pt)
                page.flush_cache()
            pages_data: list[tuple[int, str, str]] = [(i, pt, "") for i, pt in enumerate(pages_text)]

            # Phase 2: focus_keywords で学部/学科固有セクションを特定
            # 「大学名」「入試/選抜」系の語は全頁に出るため除外し、
            # 対象学部・学科名のみで絞り込む
            specific_focus: list[str] = []
            if focus_keywords:
                for kw in focus_keywords:
                    if (kw and len(kw) >= 5
                        and not kw.endswith("大学")
                        and "入試" not in kw
                        and "選抜" not in kw):
                        specific_focus.append(kw)

            matched: set[int] = set()
            if specific_focus:
                for idx, pt, tt in pages_data:
                    blob = pt + "\n" + tt
                    if any(kw in blob for kw in specific_focus):
                        # 該当頁とその前後 ±2 頁を focus 範囲に
                        for j in range(max(0, idx - 1), min(max_pages, idx + 3)):
                            matched.add(j)
                if matched:
                    console.print(
                        f"  [cyan]focus キーワード一致頁: {sorted(matched)[:5]}...（計{len(matched)}/{max_pages}頁）[/cyan]"
                    )

            # 抽出順序: focus 頁を先に処理し残予算で共通セクション（先頭3頁）を埋める
            # これにより学部別セクションが後半にある長大PDFでも target を必ず含める
            head_common = [p for p in range(min(3, max_pages)) if p not in matched]
            middle_rest = [p for p in range(max_pages)
                           if p not in matched and p not in head_common]
            if matched:
                priority = sorted(matched) + head_common + middle_rest
            else:
                priority = list(range(max_pages))

            # focus 頁のみテーブル抽出（重い処理を限定）
            if matched:
                for idx in sorted(matched)[:20]:  # 最大20頁まで
                    try:
                        tt_parts: list[str] = []
                        for tbl in pdf.pages[idx].extract_tables() or []:
                            rows = []
                            for row in tbl:
                                cells = [str(c).strip() if c is not None else "" for c in row]
                                if any(cells):
                                    rows.append(" | ".join(cells))
                            if rows:
                                tt_parts.append("\n".join(rows))
                        if tt_parts:
                            _, pt, _ = pages_data[idx]
                            pages_data[idx] = (idx, pt, "\n\n".join(tt_parts))
                        pdf.pages[idx].flush_cache()
                    except Exception:
                        pass

            # Phase 3: 優先順に抽出して max_chars 内に収める
            for i in priority:
                _, pt, tt = pages_data[i]
                page_blob_parts = []
                if pt.strip():
                    page_blob_parts.append(pt)
                if tt.strip():
                    page_blob_parts.append(f"[表]\n{tt}")
                if not page_blob_parts:
                    continue
                page_blob = "\n".join(page_blob_parts)
                text_parts.append(f"--- ページ {i+1}/{total} ---\n{page_blob}")
                accum_chars += len(page_blob)
                if accum_chars >= max_chars:
                    text_parts.append("（以降のページは文字数上限により省略）")
                    break
            if max_pages < total:
                text_parts.append(f"（{max_pages}ページ目以降はメモリ節約のため省略。全{total}ページ）")
            del pages_data

        full_text = "\n\n".join(text_parts)
        del text_parts
        gc.collect()
        if not full_text.strip():
            console.print(f"  [yellow]✗ PDF は取得できたがテキスト抽出不可（画像PDF or 暗号化の可能性）: {pdf_url[:60]}[/yellow]")
            return ""
        console.print(f"  [green]✓ PDF抽出完了: {len(full_text):,}文字 / {total}ページ[/green]")
        return full_text[:max_chars]

    except urllib.error.HTTPError as e:
        console.print(f"  [red]✗ PDF取得失敗 (HTTP {e.code}): {pdf_url[:60]}[/red]")
        return ""
    except Exception as e:
        console.print(f"  [red]✗ PDF取得・抽出失敗: {e}[/red]")
        return ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _stringify_for_verify(field_name: str, value) -> str | None:
    """Step C 検証用: dict/list 型フィールドを短い人間可読文字列に直す。
    "情報なし" / "要確認" / 空は None を返し、検証対象外にする。
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if s in ("", "情報なし", "不明", "要確認"):
            return None
        if "情報なし" in s or "要確認" in s:
            pass  # 末尾注記付きは一応検証
        return s
    if isinstance(value, (int, float, bool)):
        return str(value)
    if field_name == "selection_methods" and isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
        return " / ".join(items) if items else None
    if field_name == "ratio_history" and isinstance(value, dict):
        parts = [f"{y}: {v}" for y, v in value.items()
                 if v and str(v).strip() not in ("要確認", "情報なし", "")]
        return " / ".join(parts) if parts else None
    if field_name == "external_exam_requirements" and isinstance(value, dict):
        req = value.get("required")
        if req is None:
            return None
        exams = value.get("exams") or []
        exam_parts = []
        for e in exams:
            if not isinstance(e, dict):
                continue
            name = str(e.get("name", "")).strip()
            score = str(e.get("score", "")).strip()
            if name or score:
                exam_parts.append(f"{name} {score}".strip())
        body = " / ".join(p for p in exam_parts if p)
        return f"required={req} | {body}" if body else f"required={req}"
    try:
        return json.dumps(value, ensure_ascii=False)[:400]
    except Exception:
        return None


def _build_verify_source(
    combined: str, faculty_name: str,
    text_dept_detail: str, text_dept_ratio: str,
) -> str:
    """Step C 検証用の原文ソースを学部固有セクション優先で構成。"""
    parts = []
    if text_dept_detail:
        parts.append(f"【学科レベル詳細】\n{text_dept_detail[:6000]}")
    if text_dept_ratio:
        parts.append(f"【学科別倍率】\n{text_dept_ratio[:5000]}")
    parts.append(f"【主要データ抜粋】\n{combined[:8000]}")
    return "\n\n".join(parts)


def _verify_step_c_departments(
    departments: list[dict], source: str, faculty_name: str,
) -> dict:
    """各学科の critical fields を複合キー dict に展開し、1 コールで一括検証。
    戻り値は `{"{faculty}/{department}(#N)": {field: {supported, evidences, verifiers}}}`。
    """
    if not departments:
        return {}

    from core import verification as _verif

    # 複合キー flat dict を構築。学科名重複は #N で分離
    flat: dict = {}
    key_map: dict = {}  # complex_key -> (dept_key, field_name)
    seen: dict = {}
    for u in departments:
        base = f"{u.get('faculty', '')}/{u.get('department', '')}"
        if base in seen:
            seen[base] += 1
            dept_key = f"{base}#{seen[base]}"
        else:
            seen[base] = 0
            dept_key = base
        u["_verify_key"] = dept_key

        for field in CRITICAL_FIELDS_C:
            if field not in u:
                continue
            s = _stringify_for_verify(field, u[field])
            if s is None:
                continue
            complex_key = f"{dept_key}__{field}"
            flat[complex_key] = s
            key_map[complex_key] = (dept_key, field)

    if not flat:
        return {}

    dyn_max = min(8000, 800 + len(flat) * 60)

    try:
        raw = _verif.verify_facts(
            flat, source,
            fields=list(flat.keys()),
            source_max_chars=18000,
            max_tokens=dyn_max,
        )
    except Exception:
        return {}

    out: dict = {}
    for complex_key, entry in raw.items():
        if complex_key not in key_map:
            continue
        dept_key, field = key_map[complex_key]
        out.setdefault(dept_key, {})[field] = entry

    return out


def _filter_general_exam_entries(result: dict) -> dict:
    """一般入試・共通テスト利用のみのエントリをJSONから除外する。
    総合型選抜・学校推薦型選抜の情報が含まれないエントリを削除する後処理フィルタ。
    """
    AO_KEYWORDS = {
        "総合型選抜", "学校推薦型選抜", "AO入試", "AO", "指定校推薦",
        "公募推薦", "推薦入試", "自己推薦", "学校長推薦",
    }
    GENERAL_ONLY_KEYWORDS = {
        "一般選抜", "一般入試", "共通テスト利用", "センター試験",
        "大学入学共通テスト", "一般方式",
    }

    universities = result.get("universities", [])
    filtered = []

    for u in universities:
        ap = u.get("admission_policy", "")
        ap_text = ap.get("summary", "") if isinstance(ap, dict) else ap
        sel_detail = u.get("selection_detail", "") or " ".join(filter(None, [
            u.get("selection_phase_1", ""),
            u.get("selection_phase_2", ""),
        ]))
        check_text = " ".join(filter(None, [
            " ".join(str(m) for m in (u.get("selection_methods") or []) if m),
            sel_detail,
            u.get("features", ""),
            u.get("eligibility", ""),
            ap_text,
        ]))

        has_ao = any(kw in check_text for kw in AO_KEYWORDS)
        has_general_only = (
            not has_ao
            and any(kw in check_text for kw in GENERAL_ONLY_KEYWORDS)
        )

        if has_general_only:
            label = f"{u.get('university','')} {u.get('faculty','')} {u.get('department','')}".strip()
            console.print(f"  [dim yellow]⚠ 除外（一般入試のみ）: {label}[/dim yellow]")
            continue

        filtered.append(u)

    removed = len(universities) - len(filtered)
    if removed > 0:
        console.print(f"  [bold yellow]フィルタ: 一般入試のみのエントリを {removed} 件除外しました[/bold yellow]")

    result["universities"] = filtered
    return result


# 「不明」判定対象のトップレベルフィールド（uni dict の直下）
_DEEP_RESEARCH_TARGET_FIELDS = [
    "campus", "formal_ao_name",
    "quota", "gpa_requirement", "external_exam_requirements",
    "application_period", "selection_schedule", "announcement_date",
    "selection_methods", "selection_phase_1", "selection_phase_2",
    "evaluation_criteria", "submitted_documents",
    "eligibility", "application_type",
    "admission_policy", "features",
    "ratio_history", "quota_history", "applicants_history", "accepted_history",
    "specific_materials", "key_phrases", "avoided_phrases",
    "interview_topics", "match_anchors",
]


def _is_unknown_field_value(v) -> bool:
    """『不明』扱いか判定。文字列/None/空配列/空dictのほか、
    「不明」「情報なし」「要確認」を含む文字列、全値が空の dict も不明扱い。"""
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return True
        return any(t in s for t in ("不明", "情報なし", "要確認", "公式記載なし"))
    if isinstance(v, (list, tuple)):
        if len(v) == 0:
            return True
        return all(_is_unknown_field_value(x) for x in v)
    if isinstance(v, dict):
        if not v:
            return True
        vals = list(v.values())
        if all(_is_unknown_field_value(x) for x in vals):
            return True
    return False


def _detect_unknown_fields(u: dict) -> list[str]:
    """uni dict の「不明」フィールド名を返す。"""
    return [f for f in _DEEP_RESEARCH_TARGET_FIELDS
            if _is_unknown_field_value(u.get(f))]


def _deep_research_gap_fill(
    u: dict, keyword: str, progress_cb=None, max_queries: int = 6, max_urls: int = 10,
) -> dict:
    """Step C 後のギャップ埋めラウンド。
    1. 不明フィールドを検出
    2. LLM に追加クエリを生成させる
    3. 生成クエリで検索・スクレイプ
    4. LLM に不明フィールドだけ再抽出させて u に merge"""
    unknown = _detect_unknown_fields(u)
    if not unknown:
        return u

    uni_label = " ".join(x for x in (u.get("university"), u.get("faculty"), u.get("department")) if x)
    def _notify(m):
        if progress_cb:
            try: progress_cb(m)
            except Exception: pass

    console.print(f"[bold magenta]  🔍 Deep Research: 不明フィールド {len(unknown)} 件を追加検索で埋める[/bold magenta]")
    _notify(f"Deep Research: 不明 {len(unknown)} 項目の追加調査")

    # 1) クエリ生成（軽量モデル）
    field_list = ", ".join(unknown[:20])
    gen_system = "あなたは大学入試情報の検索クエリ生成の専門家です。与えられた不明フィールドを埋めるための日本語検索クエリを生成します。"
    gen_user = f"""対象: {uni_label}

以下のフィールドが初回調査で「不明」でした:
{field_list}

この不明を埋めるための Google 検索クエリを {max_queries} 個生成してください。
- 公式サイト（site:ac.jp）と非公式サイト（パスナビ、東進、みんなの大学情報、note、リセマム等）を使い分ける
- 各クエリは具体的で実用的に（固有名詞・年度・サイト指定を含める）
- JSON 配列のみを出力（コードブロック不要）

例: ["慶應SFC 総合型選抜 倍率 2024 2025 site:passnavi.evidus.com", "慶應義塾大学 総合政策学部 研究会 教員 site:sfc.keio.ac.jp", ...]
"""
    try:
        gen_text = analyze_with_claude(
            None, gen_system, gen_user, max_tokens=1500, model=MODEL_FAST,
        )
    except Exception as e:
        console.print(f"  [yellow]クエリ生成失敗: {e}[/yellow]")
        return u

    try:
        m = re.search(r"\[\s*\".*\"\s*\]", gen_text, re.DOTALL)
        queries = json.loads(m.group(0)) if m else []
        queries = [q for q in queries if isinstance(q, str) and q.strip()][:max_queries]
    except Exception:
        queries = []

    if not queries:
        console.print("  [yellow]追加クエリが生成されなかったためスキップ[/yellow]")
        return u

    console.print(f"  [dim]生成クエリ {len(queries)}: {queries[0][:60]}...[/dim]")
    _notify(f"Deep Research: 追加検索 {len(queries)} 件実行中")

    # 2) 検索実行
    try:
        raw = collect_search_data(queries, keyword, "Deep Research")
    except Exception as e:
        console.print(f"  [yellow]追加検索失敗: {e}[/yellow]")
        return u
    if not raw:
        console.print("  [yellow]追加検索結果が空[/yellow]")
        return u

    # 3) ページ取得（上位 max_urls 件のみ）
    seen: set = set()
    extracts: list[str] = []
    for r in raw[:max_urls * 2]:
        url = r.get("href", r.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        body = _fetch_page_text(url, max_chars=3500)
        if body:
            extracts.append(f"=== {r.get('title','')} ===\nURL: {url}\n{body}")
        if len(extracts) >= max_urls:
            break
    if not extracts:
        console.print("  [yellow]追加ページ取得が0件[/yellow]")
        return u

    extra_text = "\n\n".join(extracts)[:28000]

    # 4) 不明フィールドだけ再抽出
    _notify("Deep Research: 不明項目を再抽出中")
    current_uni = {k: u.get(k) for k in unknown}
    fill_system = """あなたは大学入試情報のアナリストです。初回抽出で「不明」だったフィールドに対し、
追加収集した情報源（公式HP・募集要項PDF・パスナビ・東進・みんなの大学情報・note・リセマム等）を読んで値を埋めます。

【ルール】
- 公式で確認できたら主値を更新する
- 公式で不明なままで非公式に値がある場合、主値に非公式値を入れて reliability: 参考（非公式）とマークし、field_sources に非公式URLを入れる
- 収集データにも本当に情報がない場合は「不明」のまま
- 各フィールドは既存スキーマの形式（dict/list/str）を維持する
- 不明以外のフィールドには手を入れない
- 出力は JSON のみ（コードブロック不要）"""

    fill_user = f"""対象: {uni_label}

=== 初回抽出で不明だったフィールド（現在値）===
{json.dumps(current_uni, ensure_ascii=False, indent=2)[:3000]}

=== 追加収集データ ===
{extra_text}

=== 出力 ===
以下の形式で、不明フィールドに対する更新値だけを返してください。
変更がないフィールドは省略して構いません。各フィールドは元のスキーマ形式を維持してください。

{{
  "updates": {{
    "field_name_1": 更新値（元のスキーマ形式）,
    "field_name_2": 更新値,
    ...
  }},
  "field_sources_updates": {{
    "field_name_1": "採用した出典URL",
    ...
  }},
  "references_updates": {{
    "field_name": [{{"value": "...", "source_label": "...", "source_url": "...", "reliability": "参考（非公式）"}}]
  }}
}}"""

    try:
        raw_fill = analyze_with_claude(
            None, fill_system, fill_user, max_tokens=8000, model=MODEL_DEEP,
        )
    except Exception as e:
        console.print(f"  [yellow]再抽出失敗: {e}[/yellow]")
        return u

    parsed = _parse_json_robust(raw_fill)
    if not parsed:
        console.print("  [yellow]再抽出 JSON パース失敗[/yellow]")
        return u

    # 5) merge
    updates = parsed.get("updates") or {}
    n_updated = 0
    for f, v in updates.items():
        if f in _DEEP_RESEARCH_TARGET_FIELDS and v is not None and not _is_unknown_field_value(v):
            u[f] = v
            n_updated += 1

    fs_updates = parsed.get("field_sources_updates") or {}
    if fs_updates:
        u.setdefault("field_sources", {})
        for f, url in fs_updates.items():
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                u["field_sources"][f] = url

    refs_updates = parsed.get("references_updates") or {}
    if refs_updates:
        u.setdefault("references", {})
        for f, arr in refs_updates.items():
            if isinstance(arr, list) and arr:
                existing = u["references"].get(f) or []
                u["references"][f] = existing + arr

    console.print(f"  [green]✓ Deep Research: {n_updated} フィールド更新[/green]")
    _notify(f"Deep Research: {n_updated} フィールドを補完")
    return u


_QUOTA_PATTERNS = [
    re.compile(r"募集人[員数][はがも:：\s]*(?:方式[ＡＢABあい甲乙一二]?[はがも]?)?[\s]*(約|およそ)?[\s]*(\d{1,4})[\s]*名[程度約]*"),
    re.compile(r"定員[はがも:：\s]*(?:方式[ＡＢABあい甲乙一二]?[はがも]?)?[\s]*(約|およそ)?[\s]*(\d{1,4})[\s]*名[程度約]*"),
    # 「総合型選抜でN名」「AO入試 N名」 表形式・文章
    re.compile(r"(?:総合型選抜|AO入試|学校推薦型選抜)[^\n：:。]{0,40}(約|)([\d]{1,4})\s*名"),
    # 「N名を募集/選考/選抜」（N名が先行するパターン）
    re.compile(r"(?<![0-9])(約|)([\d]{1,4})\s*名(?:程度|以内|前後)?\s*(?:を?(?:募集|選考|選抜|採用))"),
    # 「合格者・内定者N名」
    re.compile(r"(?:合格者|内定者|入学者)[^\n。]{0,20}(約|)([\d]{1,4})\s*名"),
    # 若干名
    re.compile(r"(若干)名"),
]


def _recover_quota_from_text(u: dict) -> None:
    """LLM が quota フィールドに「不明」を返しつつ、本文（features / difficulty_facts / department_detail）に
    「N名程度」「募集人員15名」等の記述がある場合、後処理で quota フィールドに復元する。
    """
    current = str(u.get("quota") or "").strip()
    # 裸の「不明」のほか「不明（記載なし）」等の理由付き不明表記も復元対象にする
    is_unknown = (
        not current
        or current in ("不明", "情報なし", "要確認")
        or current.startswith("不明")
    )
    if not is_unknown:
        return
    df = u.get("difficulty_facts") or {}
    df_text = ""
    if isinstance(df, dict):
        df_text = " ".join(filter(None, [df.get("ratio_observations", ""), df.get("notes", "")]))
    _refs_text = " ".join(
        str(r.get("value", "")) for r in (u.get("references") or []) if isinstance(r, dict)
    )
    candidates_text = " ".join(filter(None, [
        u.get("features", ""),
        df_text,
        u.get("department_detail", ""),
        u.get("selection_phase_1", ""),
        u.get("selection_phase_2", ""),
        u.get("eligibility", ""),
        u.get("selection_detail", ""),
        _refs_text,
    ]))
    if not candidates_text:
        return
    for pat in _QUOTA_PATTERNS:
        m = pat.search(candidates_text)
        if m:
            if pat.pattern.startswith("(若干)"):
                u["quota"] = "若干名"
            else:
                prefix = m.group(1) or ""
                num = m.group(2)
                u["quota"] = f"{prefix}{num}名"
            console.print(f"  [cyan]↻ quota 復元: {u['quota']} ← 本文から抽出[/cyan]")
            return


def _is_unknown_field_value(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        t = v.strip()
        return not t or t.startswith("不明") or t in ("情報なし", "要確認", "—", "-", "n/a", "N/A")
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _recover_quota_from_history(u: dict) -> None:
    """quota が不明で quota_history に値があれば前年度値を合成して表示する。
    例: "2027年度未公表（2026年度: 10名）"
    """
    if not _is_unknown_field_value(u.get("quota")):
        return
    qh = u.get("quota_history") or {}
    if not isinstance(qh, dict):
        return
    for year in ["2026", "2025", "2024"]:
        val = qh.get(year)
        if isinstance(val, dict):
            val = val.get("value")
        if val and not _is_unknown_field_value(val):
            u["quota"] = f"2027年度未公表（{year}年度: {val}）"
            console.print(f"  [cyan]↻ quota 復元: {u['quota']} ← quota_history から合成[/cyan]")
            return


def _ensure_legacy_fields(u: dict) -> None:
    """新スキーマ（admission_policy: object / selection_phase_1,2 / difficulty_facts / ratio_history: object）
    から旧フィールド（admission_policy: str / selection_detail / difficulty / ratio_history: str）を派生させる。
    下流の Notion 整形・検証・DB 保存が旧フィールドを参照しているため、両方を持たせて互換を保つ。
    """
    _recover_quota_from_text(u)
    _recover_quota_from_history(u)
    ap = u.get("admission_policy")
    if isinstance(ap, dict):
        u["admission_policy_obj"] = ap
        u["admission_policy"] = ap.get("summary", "") or "不明"
        u.setdefault("ap_keywords", ap.get("keywords", []) or [])

    if not u.get("selection_detail"):
        phase_1 = u.get("selection_phase_1", "")
        phase_2 = u.get("selection_phase_2", "")
        parts = [p for p in [phase_1, phase_2] if p and p != "不明"]
        u["selection_detail"] = " / ".join(parts) if parts else (phase_1 or "不明")

    if "difficulty" not in u:
        df = u.get("difficulty_facts") or {}
        if isinstance(df, dict):
            parts = [df.get("ratio_observations", ""), df.get("notes", "")]
            u["difficulty"] = " / ".join([p for p in parts if p and p != "不明"]) or "不明"
        else:
            u["difficulty"] = "不明"

    rh = u.get("ratio_history")
    if isinstance(rh, dict):
        flat: dict = {}
        for yr, val in rh.items():
            if isinstance(val, dict):
                v = val.get("value") or "不明"
                unit = val.get("unit") or ""
                flat[yr] = f"{v}（{unit}）" if unit and v != "不明" else v
            else:
                flat[yr] = val
        u["ratio_history"] = flat


_KNOWLEDGE_INJECT_FIELDS = (
    "quota", "quota_history", "ratio_history", "applicants_history", "accepted_history",
    "application_period", "selection_schedule", "announcement_date",
    "gpa_requirement", "external_exam_requirements",
    "eligibility", "selection_methods", "selection_phase_1", "selection_phase_2",
    "evaluation_criteria", "submitted_documents", "application_type",
)


def _format_known_facts_for_prompt(fields: dict, max_chars: int = 3000) -> str:
    """knowledge.fields_json をプロンプト注入用テキストに変換。"""
    from core.knowledge import _is_unknown as _kn_unknown, _is_stale as _kn_stale
    lines = []
    for field in _KNOWLEDGE_INJECT_FIELDS:
        entry = fields.get(field)
        if not entry or _kn_unknown(entry.get("value")):
            continue
        val = entry["value"]
        conf = entry.get("confidence", "?")
        src = entry.get("source_type", "?")
        stale_note = "（⚠古い可能性）" if _kn_stale(entry) else ""
        val_str = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
        if len(val_str) > 250:
            val_str = val_str[:250] + "…"
        lines.append(f"- {field}: {val_str}  [信頼度:{conf}, 出典種別:{src}{stale_note}]")
    if not lines:
        return ""
    header = "【過去リサーチ取得済みの既知情報 — 変化がなければそのまま採用・変化があれば最新値に更新してください】"
    return (header + "\n" + "\n".join(lines))[:max_chars]


def _try_wayback_pdf(domain: str, pdf_focus: list[str] | None = None, year: str = "2026") -> str:
    """Wayback Machine CDX API から前年度PDFスナップショットを取得する。
    レート制限対応のため5秒タイムアウト。失敗しても研究は継続。
    """
    try:
        import json as _json
        cdx_base = "https://web.archive.org/cdx/search/cdx"
        # 2026年度相当のスナップショットを優先（20250101〜20261231）
        params = {
            "url": f"{domain}/*",
            "output": "json",
            "limit": "5",
            "fl": "timestamp,original",
            "filter": ["statuscode:200", "mimetype:application/pdf"],
            "collapse": "urlkey",
            "from": "20250101",
            "to": "20261231",
        }
        # filter は複数値なので手動で構築
        query = (
            f"url={urllib.parse.quote(domain + '/*', safe='')}"
            "&output=json&limit=5"
            "&fl=timestamp,original"
            "&filter=statuscode:200&filter=mimetype:application/pdf"
            "&collapse=urlkey&from=20250101&to=20261231"
        )
        cdx_url = f"{cdx_base}?{query}"
        req = urllib.request.Request(cdx_url, headers={"User-Agent": NOTE_UA})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        if not data or len(data) <= 1:
            return ""
        nyushi_kws = ["nyushi", "boshu", "youkou", "yoko", "admission", "guide", "senbatsu", "ao"]
        for row in data[1:]:
            if len(row) < 2:
                continue
            orig_url = str(row[1])
            if any(kw in orig_url.lower() for kw in nyushi_kws):
                archive_url = f"https://web.archive.org/web/{row[0]}/{orig_url}"
                text = _fetch_pdf_text(archive_url, max_chars=15000, focus_keywords=pdf_focus or [])
                if text:
                    console.print(f"  [cyan]Wayback ✓ {archive_url[:80]}[/cyan]")
                    return text
    except Exception as _e:
        console.print(f"  [dim]Wayback フォールバック失敗: {_e}[/dim]")
    return ""


def run_university_analysis(client: anthropic.Anthropic, keyword: str, pdf_url: str = "", pdf_text: str = "", progress_cb=None, enable_deep_research: bool = False) -> dict:
    def _notify(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass
    console.print(Rule("[bold yellow]大学 総合型選抜 募集要項・大学情報取得（多サイト横断）[/bold yellow]"))
    _notify("大学情報取得を開始")

    # ── PDF抽出の focus キーワード: 長大な要項PDFで対象学部セクションを狙い撃ち
    # 「立教大学 異文化コミュニケーション学部 異文化コミュニケーション学科 自由選抜入試」
    # のような空白区切りキーワードをトークン化。長さ2以上のみ採用。
    _pdf_focus = [tok for tok in (keyword or "").split() if len(tok) >= 2]

    TRUSTED_DOMAINS = (
        ".ac.jp",
        "passnavi.obunsha.co.jp",
        "obunsha.co.jp",
        "minkou.jp",
        "shingakunet.com",
        "juken.mynavi.jp",
        "keinet.ne.jp",
        "benesse.ne.jp",
        "manabi.benesse.ne.jp",
        "studyplus.jp",
        "jyuken-lab.com",
        "nyushi.mynavi.jp",
        "toshin.com",
        "juku.st",
    )

    def _collect_and_scrape(queries, label, max_urls=10, max_chars=3000):
        raw = collect_search_data(queries, keyword, label)
        seen: set[str] = set()
        prio, other = [], []
        for r in raw:
            url     = r.get("href", r.get("url", ""))
            title   = r.get("title", "")
            snippet = r.get("body", r.get("snippet", ""))[:200]
            if not url or url in seen:
                continue
            seen.add(url)
            (prio if any(d in url for d in TRUSTED_DOMAINS) else other).append(
                (url, title, snippet)
            )
        targets = (prio + other)[:max_urls]
        texts = []

        def _fetch_one(args):
            url, title, snippet = args
            body = _fetch_page_text(url, max_chars=max_chars)
            return url, title, snippet, body

        with Progress(SpinnerColumn(),
                      TextColumn(f"[cyan]{label} 取得中...[/cyan] {{task.description}}"),
                      BarColumn(bar_width=20), TimeElapsedColumn(),
                      console=console, transient=True) as progress:
            task = progress.add_task("", total=len(targets))
            n_workers = min(_NUM_WORKERS, len(targets)) if targets else 1
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
                futures = {ex.submit(_fetch_one, t): t for t in targets}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        url, title, snippet, body = future.result()
                        entry = f"=== {title} ===\nURL: {url}\n"
                        entry += body if body else f"（取得失敗）\nスニペット: {snippet}"
                        # .ac.jp ページからPDFリンクを自動発見
                        if ".ac.jp" in url and not url.endswith(".pdf"):
                            found_pdf = _find_pdf_url_from_page(url, keyword)
                            if found_pdf and found_pdf not in seen:
                                seen.add(found_pdf)
                                pdf_text = _fetch_pdf_text(found_pdf, max_chars=15000, focus_keywords=_pdf_focus)
                                if pdf_text:
                                    entry += f"\n\n--- PDF内容 ({found_pdf}) ---\n{pdf_text}"
                        texts.append(entry)
                        progress.update(task, description=f"{url[:45]}...", advance=1)
                    except Exception:
                        progress.update(task, advance=1)
        console.print(f"  {label}: {len(raw)}件検索 / {len(targets)}URL取得")
        return "\n\n".join(texts)

    # ── Step0: PDF募集要項 年度別取得（直接指定 or アップロード or 2023〜2026 自動検索）
    text_pdf = ""
    pdf_texts_by_year: dict[str, str] = {}  # 年度 -> PDFテキスト
    _source_log: dict[str, dict] = {}       # ソース別 取得結果ログ
    _pdf_fetch_log: dict[str, str] = {}     # 年度 -> "取得成功: URL" or "未取得: 理由"
    console.print("[bold cyan]Step0: PDF募集要項 年度別検索・取得（2024〜2027）...[/bold cyan]")
    _notify("Step0: 募集要項PDF 検索中")
    if pdf_text:
        # アップロードされたPDFのテキストを直接使用
        console.print(f"  アップロードPDF使用: {len(pdf_text):,}文字")
        pdf_texts_by_year["アップロード"] = pdf_text[:30000]
        _pdf_fetch_log["アップロード"] = "取得成功: ユーザーアップロード"
    if pdf_url:
        # --pdf-url で直接指定された場合（最新年度として扱う）
        console.print(f"  直接指定URL: {pdf_url[:80]}")
        text_pdf = _fetch_pdf_text(pdf_url, max_chars=30000, focus_keywords=_pdf_focus)
        pdf_texts_by_year["指定"] = text_pdf
    else:
        # 年度別自動PDF検索（最新年度優先、2年分見つかれば早期終了）
        # 時間短縮のため最新2年のみを対象（環境変数で拡張可能）
        _target_years = os.environ.get("RESEARCH_PDF_YEARS", "2027,2026").split(",")
        _target_years = [y.strip() for y in _target_years if y.strip()]
        _MAX_YEARS_TO_FETCH = int(os.environ.get("RESEARCH_PDF_MAX_YEARS", "2"))
        _all_seen_pdf_urls: set[str] = set()
        _preferred_domain = ""  # 最初に見つかった公式PDFのドメイン（以降の年度で優先使用）
        _OFFICIAL_DOMAINS = [
            "waseda.jp", "keio.ac.jp", "sophia.ac.jp", "meiji.ac.jp",
            "chuo.ac.jp", "hosei.ac.jp", "rikkyo.ac.jp", "aoyama.ac.jp", "senshu.ac.jp",
        ]

        def _is_university_pdf(url: str) -> bool:
            """PDFのURLが対象大学のものかを検証する（.ac.jp限定 + preferred_domainチェック）"""
            if not url.lower().endswith(".pdf"):
                return False
            if url in _all_seen_pdf_urls:
                return False
            if ".ac.jp" not in url:
                return False  # 学術ドメイン以外は拒否
            if _preferred_domain:
                # 親ドメイン（例: waseda.ac.jp）で比較 — サブドメイン違いを許容
                _parent = ".".join(_preferred_domain.split(".")[-3:]) if _preferred_domain.count(".") >= 2 else _preferred_domain
                if _parent not in url:
                    return False
            return True

        for _year in _target_years:
            print(f"[step0] year={_year} start", flush=True)
            console.print(f"  [cyan]{_year}年度 PDF検索中...[/cyan]")
            _notify(f"Step0: {_year}年度 PDF 検索中")
            _year_short = _year[2:]  # "26", "25", etc.
            _uni_name = keyword.split()[0]  # "慶應義塾大学" など

            _year_pdf_queries = [
                f"{_uni_name} 総合型選抜 募集要項 {_year} PDF site:ac.jp",
                f"{_uni_name} (総合型選抜 OR 学校推薦型選抜) 募集要項 PDF {_year}",
                f"{_uni_name} 入試要項 総合型選抜 {_year} filetype:pdf",
                f"{_uni_name} 入学試験要項 {_year} 総合型選抜 募集要項",
                f"{_uni_name} {_year} 総合型選抜 出願 募集 site:ac.jp",
                f"{_uni_name} 総合型選抜 選考要項 {_year}年度",
                f"{_uni_name} 総合型選抜 選考内容 {_year} PDF site:ac.jp",
                f"{_uni_name} AO入試 募集要項 {_year} site:ac.jp",
                f"{_uni_name} {_year} 入学者選抜要項 総合型選抜 PDF",
            ]
            print(f"[step0] year={_year} calling collect_search_data", flush=True)
            _year_raw = _call_with_timeout(
                collect_search_data, _year_pdf_queries, _uni_name, f"PDF({_year})",
                timeout_sec=120, default=[], label=f"PDF({_year}) 検索",
            ) or []
            print(f"[step0] year={_year} collect done, {len(_year_raw)} results", flush=True)
            _found_year_url = ""

            # 戦略①：検索結果に直接PDFのURLが含まれていれば使う（大学ドメイン検証付き）
            for _r in _year_raw:
                _u = _r.get("href", _r.get("url", ""))
                if _is_university_pdf(_u):
                    _found_year_url = _u
                    break

            # 戦略①-B：preferred_domain未確定かつ .ac.jp PDFがあれば採用
            if not _found_year_url and not _preferred_domain:
                for _r in _year_raw:
                    _u = _r.get("href", _r.get("url", ""))
                    if _u and _u.lower().endswith(".pdf") and ".ac.jp" in _u and _u not in _all_seen_pdf_urls:
                        _found_year_url = _u
                        break

            # 戦略②：大学公式ページを取得しHTML内のPDFリンクを抽出
            if not _found_year_url:
                _admission_pages: list[str] = []
                for _r in _year_raw:
                    _u = _r.get("href", _r.get("url", ""))
                    _is_preferred = bool(_preferred_domain) and _preferred_domain in _u
                    _is_official = (
                        ".ac.jp" in _u or
                        "admission" in _u.lower() or
                        any(_d in _u for _d in _OFFICIAL_DOMAINS)
                    )
                    if _u and (_is_preferred or _is_official) and not _u.lower().endswith(".pdf"):
                        if _is_preferred:
                            _admission_pages.insert(0, _u)  # preferred_domainページを先頭に
                        else:
                            _admission_pages.append(_u)

                # 戦略②-B：0件の場合、汎用クエリで再検索
                if not _admission_pages:
                    _uni_name_only = keyword.split()[0]
                    _retry_raw = _call_with_timeout(
                        collect_search_data,
                        [
                            f"{_uni_name_only} 入試要項 募集要項 {_year} 入学センター site:ac.jp",
                            f"{_uni_name_only} {_year} 総合型選抜 入試情報 site:ac.jp",
                            f"{_uni_name_only} 入試・入学 総合型選抜 {_year}",
                        ],
                        keyword, f"PDF再検索({_year})",
                        timeout_sec=60, default=[], label=f"PDF再検索({_year})",
                    ) or []
                    for _r in _retry_raw:
                        _u = _r.get("href", _r.get("url", ""))
                        if _u and not _u.lower().endswith(".pdf"):
                            _admission_pages.append(_u)
                    _admission_pages = _admission_pages[:3]

                for _page_url in _admission_pages[:3]:
                    try:
                        _req = urllib.request.Request(
                            _page_url,
                            headers={"User-Agent": NOTE_UA, "Accept-Language": "ja,en;q=0.9"}
                        )
                        with urllib.request.urlopen(_req, timeout=10) as _resp:
                            _html = _resp.read().decode("utf-8", errors="replace")
                        _pdf_links = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', _html, re.I)
                        for _link in _pdf_links:
                            if _link.startswith("http"):
                                _candidate = _link
                            elif _link.startswith("/"):
                                _parsed = urllib.parse.urlparse(_page_url)
                                _candidate = f"{_parsed.scheme}://{_parsed.netloc}{_link}"
                            else:
                                _candidate = ""
                            if _candidate and _candidate not in _all_seen_pdf_urls and any(
                                kw in _candidate.lower() for kw in
                                ["youkou", "yoko", "boshu", "nyushi", "admission", _year, _year_short]
                            ):
                                _found_year_url = _candidate
                                break
                        if _found_year_url:
                            break
                        time.sleep(0.05)
                    except Exception:
                        pass

            if _found_year_url and _found_year_url not in _all_seen_pdf_urls:
                # preferred_domain を確定（最初の成功PDFから）
                if not _preferred_domain:
                    _preferred_domain = urllib.parse.urlparse(_found_year_url).netloc
                    console.print(f"  [dim]基準ドメイン確定: {_preferred_domain}[/dim]")
                elif _preferred_domain not in _found_year_url:
                    console.print(f"  [yellow]⚠ 異なるドメインのPDF (スキップ): {_found_year_url[:60]}[/yellow]")
                    _pdf_fetch_log[_year] = f"未取得: 異なるドメインのPDF（{_found_year_url[:60]}）、基準ドメイン={_preferred_domain}"
                    _found_year_url = ""
                if _found_year_url:
                    console.print(f"  [green]✓ {_year}年度 PDF発見: {_found_year_url[:80]}[/green]")
                    _year_text = _call_with_timeout(
                        _fetch_pdf_text, _found_year_url, 15000, _pdf_focus,
                        timeout_sec=60, default="", label=f"PDF取得({_year})",
                    ) or ""
                    if _year_text:
                        pdf_texts_by_year[_year] = _year_text
                        _all_seen_pdf_urls.add(_found_year_url)
                        _pdf_fetch_log[_year] = f"取得成功: {_found_year_url}"
                    else:
                        _pdf_fetch_log[_year] = f"未取得: PDFのテキスト抽出失敗（pdfplumberエラーまたは空ファイル）、URL={_found_year_url}"
            else:
                _fail_reason = "検索結果にPDFリンクなし。大学公式ページへのアクセスも失敗または募集要項PDFが見つからず"
                _pdf_fetch_log[_year] = f"未取得: {_fail_reason}"
                console.print(f"  [yellow]⚠ {_year}年度 PDF未取得: {_fail_reason}[/yellow]")
            time.sleep(0.1)

            # 早期打ち切り: 目標年度数に達したら残りの年度はスキップ
            if len(pdf_texts_by_year) >= _MAX_YEARS_TO_FETCH:
                _remaining = [y for y in _target_years if y > _year and y not in pdf_texts_by_year] + \
                             [y for y in _target_years if y < _year]
                _skipped = [y for y in _target_years[_target_years.index(_year)+1:]]
                if _skipped:
                    console.print(f"  [dim]✓ {_MAX_YEARS_TO_FETCH}年度分取得完了。残り年度スキップ: {', '.join(_skipped)}[/dim]")
                break

        if pdf_texts_by_year:
            _found_years = [yr for yr in _target_years if yr in pdf_texts_by_year]
            parts = [f"=== {yr}年度 PDF募集要項 ===\n{pdf_texts_by_year[yr]}" for yr in _found_years]
            text_pdf = "\n\n".join(parts)
            console.print(
                f"  [bold green]✓ PDF取得完了: {len(pdf_texts_by_year)}年度分"
                f" ({', '.join(_found_years)})[/bold green]"
            )
        else:
            console.print("  [dim]PDF自動取得: 全年度見つからなかったためスキップ（--pdf-url で直接指定可）[/dim]")
            # Wayback Machine フォールバック: preferred_domain が判明していれば前年度アーカイブを試みる
            if _preferred_domain:
                console.print(f"  [dim]Wayback Machine フォールバック試行: {_preferred_domain}[/dim]")
                _notify("Step0: Wayback Machine から前年度PDFを検索中")
                _wb_text = _call_with_timeout(
                    _try_wayback_pdf, _preferred_domain, _pdf_focus,
                    timeout_sec=10, default="", label="Wayback PDF",
                )
                if _wb_text:
                    pdf_texts_by_year["2026(archive)"] = _wb_text
                    text_pdf = f"=== 2026年度 PDF募集要項（Wayback Machine アーカイブ） ===\n{_wb_text}"
                    _pdf_fetch_log["2026(archive)"] = "Wayback Machine から取得"
                    console.print("  [green]✓ Wayback Machine から2026年度PDF取得[/green]")

    # ── Step1: パスナビ（自然言語クエリ + パスナビURL自動補完）
    console.print("[cyan]Step1: パスナビ 検索...[/cyan]")
    _notify("Step1: パスナビ 検索中")
    _uni_name_for_queries = keyword.split()[0]
    _passnavi_extra_queries = [
        f"site:passnavi.obunsha.co.jp {keyword} 総合型選抜",
        f"{keyword} パスナビ 総合型選抜 倍率 2026 2027",
        f"{_uni_name_for_queries} パスナビ 定員 倍率 総合型選抜",
    ]
    _passnavi_raw = collect_search_data(UNIVERSITY_PASSNAVI_QUERIES + _passnavi_extra_queries, keyword, "パスナビ")
    # パスナビのURLが見つかった場合、/top/ /department/ /nyushi/ を追加取得
    _passnavi_extra: list[dict] = []
    for _r in _passnavi_raw:
        _u = _r.get("href", _r.get("url", ""))
        if "passnavi.obunsha.co.jp/univ/" in _u:
            _m = re.search(r"passnavi\.obunsha\.co\.jp/univ/(\d+)", _u)
            if _m:
                _uid = _m.group(1)
                for _sub in ("/top/", "/department/", "/nyushi/ao/"):
                    _extra_url = f"https://passnavi.obunsha.co.jp/univ/{_uid}{_sub}"
                    if not any(_extra_url == _x.get("href","") for _x in _passnavi_raw + _passnavi_extra):
                        _passnavi_extra.append({"href": _extra_url, "title": f"パスナビ {_sub}", "body": ""})
    _passnavi_all = _passnavi_raw + _passnavi_extra
    seen_p: set[str] = set()
    prio_p, other_p = [], []
    for _r in _passnavi_all:
        _u = _r.get("href", _r.get("url", ""))
        if not _u or _u in seen_p: continue
        seen_p.add(_u)
        (prio_p if any(d in _u for d in TRUSTED_DOMAINS) else other_p).append(
            (_u, _r.get("title",""), _r.get("body",_r.get("snippet",""))[:200])
        )
    _passnavi_targets = (prio_p + other_p)[:8]
    _passnavi_texts = []
    for _u, _t, _s in _passnavi_targets:
        _body = _fetch_page_text(_u, max_chars=4000)
        _entry = f"=== {_t} ===\nURL: {_u}\n"
        _entry += _body if _body else f"（取得失敗）\nスニペット: {_s}"
        _passnavi_texts.append(_entry)
        time.sleep(0.05)
    text_passnavi = "\n\n".join(_passnavi_texts)
    console.print(f"  パスナビ: {len(_passnavi_raw)}件検索 / {len(_passnavi_targets)}URL取得（補完{len(_passnavi_extra)}件）")
    if text_passnavi:
        _source_log["パスナビ"] = {"status": "ok", "chars": len(text_passnavi), "urls": len(_passnavi_targets)}
    else:
        _source_log["パスナビ"] = {"status": "empty", "reason": f"検索{len(_passnavi_raw)}件取得もテキスト抽出0件"}

    # ── Step2-10 + 7: 全サイト横断収集を並列実行（15分化の主要最適化）
    is_faculty_mode = any(s in keyword for s in ["学部", "学院", "学科", "研究科", "学府"])
    _minkou_extra = [f"site:minkou.jp {keyword} 総合型選抜 倍率"]
    _toshin_extra_queries = [
        f"site:toshin.com {keyword} 総合型選抜 倍率",
        f"{_uni_name_for_queries} 東進 総合型選抜 倍率 2026 2027 定員",
    ]
    _history_extra = [
        f"{_uni_name_for_queries} 総合型選抜 倍率 2024 2025 2026 年度別",
        f"{_uni_name_for_queries} AO入試 志願者数 合格者数 定員 推移",
    ]

    _parallel_tasks = [
        ("minkou",     UNIVERSITY_MINKOU_QUERIES + _minkou_extra, "みんなの大学情報",        5, 2500),
        ("rikunabi",   UNIVERSITY_RIKUNABI_QUERIES,               "リクナビ進学",            5, 2500),
        ("official",   UNIVERSITY_OFFICIAL_QUERIES,               "公式サイト",              6, 3000),
        ("ratio",      UNIVERSITY_RATIO_QUERIES,                  "倍率・実績",              5, 2500),
        ("admission",  UNIVERSITY_QUERIES,                        "募集要項",                6, 2500),
        ("detail",     UNIVERSITY_DETAIL_QUERIES,                 "大学詳細情報",            5, 2000),
        ("faculty",    UNIVERSITY_FACULTY_QUERIES,                "学部・学科情報",          5, 2000),
        ("toshin",     UNIVERSITY_TOSHIN_QUERIES + _toshin_extra_queries, "東進",           6, 3000),
        ("sapuri",     UNIVERSITY_STUDYSAPURI_QUERIES,            "スタディサプリ進路",      5, 2500),
        ("juken_navi", UNIVERSITY_JUKEN_NAVI_QUERIES,             "受験ナビ",                5, 2500),
        ("history",    UNIVERSITY_HISTORY_QUERIES + _history_extra, "過去3年データ",          6, 2500),
        ("research_labs", UNIVERSITY_RESEARCH_QUERIES,            "研究室・教員・研究テーマ", 8, 3000),
    ]
    if is_faculty_mode:
        _parallel_tasks.append(("dept_detail", UNIVERSITY_DEPT_DETAIL_QUERIES, "学科詳細（選考・日程・AP・資格）", 10, 8000))
        _parallel_tasks.append(("dept_ratio",  UNIVERSITY_DEPT_RATIO_QUERIES,  "学科別倍率（過去3年）",           8, 6000))

    _log_mem("before_step_2_10_parallel")
    console.print(f"[bold cyan]Step2-10 並列収集開始（{len(_parallel_tasks)}タスク / 最大4並列）[/bold cyan]")
    _notify(f"Step2-10: {len(_parallel_tasks)}サイト横断を並列収集中")

    _results: dict[str, str] = {}
    _parallel_workers = int(os.environ.get("RESEARCH_PARALLEL_SITE_WORKERS", "8"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=_parallel_workers) as _ex:
        _fut_map = {
            _ex.submit(_collect_and_scrape, queries, label, max_urls=mu, max_chars=mc): name
            for (name, queries, label, mu, mc) in _parallel_tasks
        }
        _done_count = 0
        for _fut in concurrent.futures.as_completed(_fut_map):
            _name = _fut_map[_fut]
            try:
                _results[_name] = _fut.result() or ""
            except Exception as _e:
                console.print(f"  [yellow]⚠ {_name} 収集失敗: {_e}[/yellow]")
                _results[_name] = ""
            _done_count += 1
            # 4 タスク完了ごとにメモリログ + GC
            if _done_count % 4 == 0:
                gc.collect()
                _log_mem(f"step_2_10_progress_{_done_count}/{len(_parallel_tasks)}")
    _log_mem("after_step_2_10_parallel")

    text_minkou       = _results.get("minkou", "")
    text_rikunabi     = _results.get("rikunabi", "")
    text_official     = _results.get("official", "")
    text_ratio        = _results.get("ratio", "")
    text_admission    = _results.get("admission", "")
    text_detail       = _results.get("detail", "")
    text_faculty      = _results.get("faculty", "")
    text_toshin       = _results.get("toshin", "")
    text_sapuri       = _results.get("sapuri", "")
    text_juken_navi   = _results.get("juken_navi", "")
    text_history      = _results.get("history", "")
    text_dept_detail  = _results.get("dept_detail", "")
    text_dept_ratio   = _results.get("dept_ratio", "")
    text_research_labs = _results.get("research_labs", "")

    for _name, _label, _key in [
        ("みんなの大学情報", "minkou.jpへのアクセス失敗またはデータなし", "minkou"),
        ("リクナビ進学", "リクナビ進学へのアクセス失敗またはデータなし", "rikunabi"),
        ("大学公式サイト", "大学公式サイト（.ac.jp）へのアクセス失敗またはデータなし", "official"),
        ("倍率・合格実績", "倍率データが検索結果に見つからず", "ratio"),
        ("募集要項", "募集要項ページが見つからず", "admission"),
        ("東進", "東進サイトへのアクセス失敗またはデータなし", "toshin"),
        ("スタディサプリ進路", "スタディサプリ進路へのアクセス失敗またはデータなし", "sapuri"),
        ("過去3年データ", "過去年度データが検索結果に見つからず", "history"),
        ("研究室・教員", "研究室・教員情報が検索結果に見つからず", "research_labs"),
    ]:
        _text = _results.get(_key, "")
        _source_log[_name] = {"status": "ok", "chars": len(_text)} if _text else {"status": "empty", "reason": _label}

    if is_faculty_mode and (text_dept_detail or text_dept_ratio):
        console.print("  [green]✓ 学科レベル詳細収集完了（並列）[/green]")

    # 合算（セクション見出し付き）
    combined = ""
    if text_pdf:
        _pdf_year_summary = ", ".join(pdf_texts_by_year.keys()) if pdf_texts_by_year else "指定"
        combined += (
            f"【★PDF募集要項（年度別・最高信頼度・最優先参照）取得年度: {_pdf_year_summary}】\n"
            f"⚠️ 指示: このPDFから総合型選抜・学校推薦型選抜の情報のみ抽出すること。"
            f"一般選抜（一般入試）・大学入学共通テスト利用入試の情報は無視すること。\n"
            f"複数年度のPDFがある場合は年度ごとに募集人員・倍率・選考方法の変化を整理すること。\n"
            f"{text_pdf[:40000]}\n\n"
        )
    combined += (
        f"【★過去3年データ（年度指定・パスナビ/東進/みんな大/週刊朝日等）】\n{text_history[:6000]}\n\n"
        f"【パスナビ情報（旺文社）】\n{text_passnavi[:8000]}\n\n"
        f"【東進情報（倍率・合格実績）】\n{text_toshin[:6000]}\n\n"
        f"【スタディサプリ進路情報】\n{text_sapuri[:5000]}\n\n"
        f"【受験ナビ情報（マイナビ・受験ラボ）】\n{text_juken_navi[:5000]}\n\n"
        f"【みんなの大学情報】\n{text_minkou[:6000]}\n\n"
        f"【リクナビ進学情報】\n{text_rikunabi[:5000]}\n\n"
        f"【大学公式サイト情報】\n{text_official[:10000]}\n\n"
        f"【倍率・合格実績データ（過去3年分）】\n{text_ratio[:6000]}\n\n"
        f"【募集要項データ】\n{text_admission[:8000]}\n\n"
        f"【教育・研究・進路データ】\n{text_detail[:6000]}\n\n"
        f"【学部・学科レベルデータ】\n{text_faculty[:6000]}"
    )
    if text_research_labs:
        combined += (
            f"\n\n【★研究室・教員・研究テーマ情報 ★志望理由書の素材として最重要 — 必ずここから具体名を抽出すること】\n"
            f"{text_research_labs[:12000]}"
        )

    # ── researchmap.jp 直接クロール（教員名・研究テーマの確実な抽出源）────
    _researchmap_profiles: list[dict] = []
    try:
        _rm_univ = (step_a or {}).get("university_name") or keyword.split()[0] if keyword else ""
        _rm_fac = ""
        if faculties_from_b:
            _rm_fac = faculties_from_b[0].get("faculty", "")
        if _rm_univ:
            console.print(f"  [cyan]researchmap.jp から教員プロファイル取得中: {_rm_univ} {_rm_fac}[/cyan]")
            _researchmap_profiles = _fetch_researchmap_profiles(_rm_univ, _rm_fac, max_profiles=5)
            console.print(f"  [green]✓ researchmap {len(_researchmap_profiles)} 件取得[/green]")
    except Exception as _rm_e:
        console.print(f"  [yellow]researchmap 取得失敗: {_rm_e}[/yellow]")

    if _researchmap_profiles:
        _rm_lines = []
        for p in _researchmap_profiles:
            line = f"- {p['name']}: {p.get('theme','')}"
            if p.get('keywords'):
                line += f"（キーワード: {', '.join(p['keywords'])}）"
            line += f" [URL: {p['url']}]"
            _rm_lines.append(line)
        combined += (
            f"\n\n【★researchmap.jp 教員ディレクトリ（直接取得・最も確実な一次情報）】\n"
            f"以下は researchmap.jp から取得した {_rm_univ} 所属研究者のプロファイル。"
            f"specific_materials.research_labs には必ずここから具体的な教員名・研究テーマ・URLを抽出すること。\n"
            + "\n".join(_rm_lines)
        )
    if is_faculty_mode:
        combined += (
            f"\n\n【★学科レベル詳細データ（選考方法・日程・AP・出願資格）★最優先で参照】\n{text_dept_detail[:12000]}"
            f"\n\n【★学科別倍率データ（過去3年分）★最優先で参照】\n{text_dept_ratio[:10000]}"
        )
    console.print(f"  取得テキスト合計: [bold]{len(combined):,}[/bold] 字")

    # メモリ節約: 結合後は中間 text_XXX を破棄して GC を呼ぶ
    # （各 text_XXX は combined 内にコピーされているため保持不要）
    _log_mem("before_step_a_gc")
    try:
        del text_pdf, text_history, text_passnavi, text_toshin, text_sapuri
        del text_juken_navi, text_minkou, text_rikunabi, text_official
        del text_ratio, text_admission, text_detail, text_faculty
        del text_dept_detail, text_dept_ratio, text_research_labs
    except Exception:
        pass
    # _results 辞書も不要（値は combined にコピー済）
    try:
        _results.clear()
    except Exception:
        pass
    gc.collect()
    _log_mem("after_step_a_gc")

    # ── Step A/B: 大学共通情報・学部共通情報の並列抽出
    system_a = """あなたは大学入試情報の一次情報整理担当者です。
Step A では「大学全体レベル」の基礎情報を最大限詳しく抽出します。

【情報源ポリシー — 2層構造】
- **第1層（優先・確実）**: 大学公式HP（.ac.jp 等）/ 公式募集要項PDF / 公式サイトからリンクされた許可対象ページ
- **第2層（補助・参考）**: パスナビ・東進・みんなの大学情報・河合塾Kei-Net・スタディサプリ・マイナビ進学・リセマム・週刊朝日 等の受験情報サイト
- 公式で情報が取れる項目は公式を採用
- 公式で不明な項目は第2層（非公式）から値を引いて主値に入れる。このとき `field_sources` にはその非公式URLを入れる（UIが自動で「補助」バッジを付ける）
- 第1層にも第2層にも情報がない項目のみ「不明」

【出力品質ルール】
- 各フィールドは「要約」ではなく「具体的な記述」を優先。公式文書の表現をそのまま引用できるなら引用する（鉤括弧「」で囲む）
- 数値・名称・日付は省略せず明記
- 曖昧表現（「おそらく」「一般的に」「例年」）は禁止、主観的評価（「比較的」「難しい」）も禁止
- 推測・一般論・過去知識で埋めてはならない（事実ベースのみ）
- 「情報なし」ではなく「不明」を用いる

【抽出対象（大学全体に共通する情報のみ）】
- 大学全体のアドミッションポリシー / 教育理念
- 総合型選抜の大学全体としての基本方針
- 大学共通の出願資格（全学部・学科に共通する条件）
- 大学の特色・強み

【除外】
- 学部・学科固有の情報
- 学科別の募集人員・倍率・選考日程
- 一般選抜・共通テスト利用の情報

必ず日本語で回答し、JSONのみを出力する（コードブロック不要）。"""

    user_a = f"""以下は「{keyword}」に関して収集したテキストです。収集データには公式・非公式が混在しているため、
公式（.ac.jp もしくは大学公式サイト配下、あるいは大学公式サイトからリンクされている許可対象ページ）の記述のみを採用し、
非公式情報しか根拠がない項目は「不明」としてください。

=== 収集データ ===
{combined[:44000]}

以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "university": "大学名（公式表記）",
  "ap_university": "大学全体のAP・理念（600〜800字。公式記述を具体的に。不明は「不明」）",
  "ao_basic_policy": "総合型選抜の基本方針（400〜600字。公式が述べる方針を引用・要約。不明は「不明」）",
  "common_eligibility": "大学共通の出願資格（400〜600字。条件を箇条書きレベルで具体的に。不明は「不明」）",
  "education_philosophy": "教育方針・理念（300〜500字。不明は「不明」）",
  "university_features": "大学の特色・強み（400〜600字。看板学部・独自制度・留学・研究資源など具体的に。不明は「不明」）",
  "official_sources": [
    {{"label": "何の資料か（例: 大学公式HP 入試案内ページ / 2027年度入試要項PDF）", "url": "フルURL"}}
  ],
  "field_sources": {{
    "ap_university": "出典URL（公式で確認できれば公式URL、非公式で補完した場合は非公式URL。不明時は空文字）",
    "ao_basic_policy": "出典URL",
    "common_eligibility": "出典URL",
    "education_philosophy": "出典URL",
    "university_features": "出典URL"
  }}
}}"""

    # ── Step B: 学部共通情報の抽出（Step A と並列実行）
    system_b = """あなたは大学入試情報の一次情報整理担当者です。
Step B では「学部レベル」の情報を学部ごとに最大限詳しく抽出します。

【情報源ポリシー — 2層構造】
- **第1層（優先・確実）**: 大学公式HP / 公式募集要項PDF / 公式サイトからリンクされた許可対象ページ
- **第2層（補助・参考）**: パスナビ・東進・みんなの大学情報・河合塾Kei-Net・スタディサプリ・マイナビ進学・リセマム 等
- 公式で情報が取れる項目は公式を採用
- 公式で不明な項目は第2層から値を引いて主値に入れる。`field_sources` にその非公式URLを入れる（UIが自動で「補助」バッジを付ける）
- どちらでも情報がない項目のみ「不明」

【出力品質ルール】
- 各フィールドは「要約」ではなく「具体的な記述」を優先。公式文書の表現を引用できれば引用（「」）
- カリキュラム・特色は、科目名・研究室名・独自制度名を具体的に列挙
- 曖昧表現（「おそらく」「一般的に」）・主観的評価（「比較的」「難しい」）は禁止
- 推測・過去知識での補完は禁止（事実ベースのみ）
- 「情報なし」ではなく「不明」を用いる
- 学部別条件は必ず対象学部の該当箇所を直接確認し、他学部の条件を流用しない

【抽出対象（学部ごとに異なる情報）】
- 各学部のアドミッションポリシー・特色（要約＋キーワード3〜5個）
- 学部としての選考方針（学部全体に共通する方針）
- 学部共通の出願条件
- 学べる内容 / カリキュラムの特徴 / 特記事項
- 主な教授陣・研究室
- 卒業後の就職・進路傾向

【除外】
- 大学全体レベルの情報（Step Aで収集済み）
- 学科別の詳細な募集人員・倍率・選考日程（Step Cで処理）
- 一般選抜・共通テスト利用の情報

必ず日本語で回答し、JSONのみを出力する（コードブロック不要）。"""

    user_b = f"""以下は「{keyword}」に関して収集したテキストです。公式（.ac.jp 等）の記述のみ採用し、
非公式のみが根拠の項目は「不明」としてください。

=== 収集データ ===
{combined[:44000]}

【重要】大学全体レベルの情報（AP・教育理念・大学全体の出願資格）は別途処理します。学部固有の情報のみ新たに抽出してください。

以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "faculties": [
    {{
      "faculty": "学部名（公式表記）",
      "ap_faculty": "学部のAP・特色・求める学生像（600〜800字。公式記述を具体的に。不明は「不明」）",
      "ap_keywords": ["APキーワード（3〜5個。公式記述から抽出。不足時は配列を短くする）"],
      "faculty_selection_policy": "学部としての選考方針（400〜600字。書類審査・面接・小論文など評価軸を具体的に。不明は「不明」）",
      "faculty_eligibility": "学部共通の出願条件（400〜600字。評定・英語資格・特定科目履修などを具体的に。不明は「不明」）",
      "learning_content": "学べる内容（400〜600字。看板科目・必修科目・専門領域を具体的に。不明は「不明」）",
      "curriculum_features": "カリキュラムの特徴（400〜600字。学年ごとの流れ、ゼミ制度、留学機会、独自プログラムなど。不明は「不明」）",
      "special_notes": "特記事項（新設・改組・国際プログラム・独自制度など。300〜500字。不明は「不明」）",
      "professors": ["教授名・専門: 研究テーマ（できるだけ具体的に。最大8件）"],
      "career": "卒業後の進路・就職の特徴（300〜500字。業界別割合・大学院進学率・特徴的な就職先を具体的に。不明は「不明」）",
      "career_examples": ["就職先・進学先の具体例（公開情報ベース。最大10件）"],
      "official_sources": [
        {{"label": "資料名", "url": "フルURL"}}
      ],
      "field_sources": {{
        "ap_faculty": "出典URL（公式・非公式どちらでも可。不明時は空文字）",
        "faculty_selection_policy": "出典URL",
        "faculty_eligibility": "出典URL",
        "learning_content": "出典URL",
        "curriculum_features": "出典URL",
        "special_notes": "出典URL",
        "career": "出典URL"
      }}
    }}
  ]
}}

注意: 学部が複数ある場合はすべてを個別エントリにすること。"""

    # Step A/B を並列実行（Step B は combined のみで抽出し step_a を待たない）
    console.print("[bold cyan]  Step A/B: 大学共通・学部共通情報を並列抽出中...[/bold cyan]")
    _notify("StepA/B: 大学・学部情報を並列抽出中")
    from concurrent.futures import ThreadPoolExecutor as _ABPool
    with _ABPool(max_workers=2) as _ab_pool:
        _fut_a = _ab_pool.submit(
            analyze_with_claude, client, system_a, user_a, 4000, 4, MODEL_SMART,
        )
        _fut_b = _ab_pool.submit(
            analyze_with_claude, client, system_b, user_b, 6000, 4, MODEL_SMART,
        )
        raw_a = _fut_a.result()
        raw_b = _fut_b.result()

    step_a = _parse_json_robust(raw_a)
    step_b = _parse_json_robust(raw_b)
    fac_count = len(step_b.get("faculties", []))
    console.print(f"  [green]✓ Step A 完了[/green]: {step_a.get('university', '不明')} — AP・理念・共通資格")
    console.print(f"  [green]✓ Step B 完了[/green]: {fac_count}学部分の情報を抽出")

    # ── Step A 検証: Step C と並列（バックグラウンドスレッド）
    from concurrent.futures import ThreadPoolExecutor as _VerifyPool
    _verify_executor_a = _VerifyPool(max_workers=1)

    def _run_step_a_verification():
        from core import verification as _verif
        _critical_fields_a = [
            "university", "ap_university", "ao_basic_policy",
            "common_eligibility", "education_philosophy",
        ]
        return _verif.verify_facts(
            step_a, combined, fields=_critical_fields_a, source_max_chars=10000,
        )

    _verify_future_a = _verify_executor_a.submit(_run_step_a_verification)
    console.print("  [dim]Step A 検証: バックグラウンドで実行中...[/dim]")

    # ── Step C: 学科個別情報の抽出（学部ごとに分割処理）
    console.print("[bold cyan]  Step C: 学科個別情報 抽出中（学部別分割処理）...[/bold cyan]")
    _notify("StepC: 学科別詳細を抽出中")

    faculties_from_b = step_b.get("faculties", [])
    if not faculties_from_b:
        faculties_from_b = [{"faculty": "（学部情報なし）"}]

    _all_universities: list[dict] = []
    _all_overall_trends: list[str] = []
    _all_selection_ranking: list[dict] = []
    _all_key_insights: list[str] = []
    _ratio_summary = ""
    _data_quality_parts: list[str] = []

    def _process_faculty(_fac_idx, _fac_info):
        """1学部分のStep C処理（並列実行用）"""
        _fac_name = _fac_info.get("faculty", f"学部{_fac_idx + 1}")
        console.print(f"  [{_fac_idx + 1}/{len(faculties_from_b)}] [cyan]{_fac_name}[/cyan] 学科情報抽出中...")

        _fac_mode_instruction = ""
        if is_faculty_mode or len(faculties_from_b) > 1:
            _fac_mode_instruction = f"""
★★★ 対象学部: {_fac_name} ★★★
この学部に属する学科・専攻・コースのみを抽出してください。他の学部の情報は含めないこと。
学科・専攻・コースが複数ある場合は必ず1学科1エントリで分割してください。"""

        _system_c = f"""あなたは総合型選抜・学校推薦型選抜に特化した大学情報アナリストです。
出力は記事本文ではなく、受験生・進路指導者が合格ロジックに基づいて活用できる「素材」として整理します。

【🚨 「不明」を極力避ける — 最重要ルール】
- 「不明」と書くのは**最終手段**。収集データを隅々まで走査してから判断する
- 収集データに「パスナビ」「東進」「みんなの大学情報」「河合塾Kei-Net」「スタディサプリ」「マイナビ進学」「リセマム」「週刊朝日」「サンデー毎日」等の非公式情報源からの記述がある場合、**必ず該当項目の `references` に収録すること**
- **主値のフォールバック規則**: 公式に該当情報がない場合、非公式情報源から最も信頼性が高いものを主値に入れ、同時に references にも同じ値を source 情報付きで登録する
  - 例: 倍率が公式にないが、パスナビに「2.5倍（2024年度）」と記載 → `ratio_history["2024"] = {{"value": "2.5", "unit": "学科単位", "source_type": "公式リンク先"}}` として主値採用＋ references にも登録
- 特に以下は公式が通常出さず、非公式のみに存在するため、非公式値を主値で採用する: 倍率・志願者数・合格者数・最低評定平均・英語スコアの合格者分布
- references 収録の最低条件: value, source_label, source_url の3つを満たす情報があれば収録
- 主値が「不明」で references も空のままの項目は、UI で「情報ゼロ」となり価値が下がる。避けること
- 「不明」とする前に、必ず次を自問: ①収集データ全体を検索したか ②近縁サイト（passnavi.obunsha.co.jp/univ/）や minkou.jp にも無かったか ③該当学科の記載が一部でもあるか
- それでも何も見つからなければ「不明（公式・非公式ともに記載なし）」と記す

【募集人数（quota）と倍率（ratio_history）の特別ルール】
- quota: 本文中に「募集人員 N名」「募集定員 N名」「選抜定員 N名」「定員 N名」「定員：N名」「N名程度」「約N名」「若干名」「各ブロックN名」「合計N名」「計N名」「N名を募集」等があれば**必ず `quota` フィールドに原文表記のまま抽出**。「定員」は大学によって「募集人数」の意味で使われることが多いため必ず確認すること。features / difficulty_facts / selection_phase_1 / selection_phase_2 / eligibility を全て走査してから判断すること（これらの本文に書いてあるのに `quota` が「不明」は禁止）。2027未公表で過年度のみ判明なら「2027年度未公表、2026年度はN名」と明記
- quota が不明・未公表のとき、`quota_history` に値があれば**必ず** quota フィールドを「2027年度未公表（2026年度: N名）」形式で合成すること。quota_history が空でも difficulty_facts・features・references 等の本文全体を必ず再探索してから「不明」とする
- 「地域ブロックごとN名×7ブロック」「各都道府県N名」等の地域割りの場合は合計人数も計算して「N名（7ブロック×各5名程度）」形式で書く
- ratio_history: 収集データに「非公表」等の明示記載があれば value を「公式・非公式ともに非公表」と書く。単に見当たらないだけなら「不明（記載なし）」と書く（裸の「不明」は避ける）
- ratio_history の全年度が不明の場合のみ「不明」可。1年度でもデータが取得できた場合は**必ず**その年度を埋めること。年度不明でも倍率数値があれば `"不明年度"` キーで登録する
- keinet.ne.jp / passnavi.com / toshin.com / manabi.benesse.ne.jp 等の収集データに倍率・志願者・合格者の数値が1つでもあれば必ず ratio_history に収録する。収集データを全走査してもゼロ件は禁止。「記載なし」は「不明（記載なし）」と書き、裸の「不明」は使わない

【合格ロジックの根本命題 — 全出力が従う】
> 総合型選抜は「実績の強さで決まる試験ではなく、自分の経験を言語化し、社会課題と大学での学びと将来像につなげ、
> その大学とのマッチ度を論理的に示せるかで決まる試験」
分析の軸は常に: ①マッチ度 ②必然性 ③独自性 ④一貫性（経験→社会課題→大学→将来）

{_fac_mode_instruction}

【情報源ポリシー — 2層構造】
- **第1層（最優先・確実）**: 大学公式HP（.ac.jp 等）/ 公式募集要項PDF（raw PDF優先）/ 公式PDF資料 / 大学公式サイトからリンクされた許可対象ページ
- **第2層（補助・参考）**: パスナビ・東進・みんなの大学情報・河合塾Kei-Net・スタディサプリ・マイナビ進学・リセマム・週刊朝日・サンデー毎日 等の受験情報サイト
- 公式情報が取れる項目は公式を採用し、`field_sources` に公式URLを記載
- 公式で不明だが第2層に値がある項目は、その値を主フィールドに入れて `field_sources` に非公式URLを記載（UIが自動で「補助」バッジを表示する）
- 第2層の値を採用した際、同じ値と出典を `references` にも残すこと（複数ソースがあれば全部列挙）
- 公式・非公式ともに情報がない項目のみ「不明」

【出力品質ルール】
- 各フィールドは「要約」ではなく「具体的な記述」を優先。公式文書の表現はそのまま引用（「」）
- 数値・名称・日付・科目名・試験名などは省略せず明記
- 推測・一般論・過去知識での補完は禁止（事実ベース）
- 「情報なし」ではなく「不明」を用いる
- 出典URLは可能な限り raw PDF 直接URL（末尾 .pdf 等）を優先
- 入試要項本体PDFを確認できなかった「細目」（英語最低基準点、評定例外規定、併願詳細など）については、公式HTMLで確認できれば公式値を使い、公式HTMLにも記述がなく非公式で見つかれば非公式値＋`field_sources`非公式URL、どちらも無ければ「不明（本体PDF未確認）」
- 他学部の条件を対象学部に流用しない
- 主観的評価（「比較的合格しやすい」「ハードルが高い」等）は禁止
- 曖昧表現（「おそらく」「一般的に」「例年」）は禁止

【志望理由書で使う素材として抽出する際の絶対禁止】
- 偏差値・知名度・就職実績・「伝統がある」「少人数教育」「雰囲気が良い」「グローバル人材になれる」等、他大学にも当てはまる評価表現
- アドミッションポリシーの丸写し（必ず解釈を添える／引用する場合は鉤括弧で明示し、自分の解釈と区別する）
- 抽象ワード単独での記述（「多様性」「国際性」「リーダーシップ」だけで止めない — 必ずその大学の具体要素で裏打ちする）
- 「〇〇先生のもとで学びたい」単独（何を・なぜ学ぶかまで含めて書く）

【「その大学でしかできない」ことを優先抽出】
以下の5レイヤーで独自性を判定し、各レイヤーで「高/中/低」+ 具体的根拠を出す:
  1) 研究テーマの独自性（他大学と比べて固有の研究・分野・アプローチ）
  2) カリキュラム構造の独自性（必修/選択の組み合わせ、学部横断、独自の演習制度）
  3) 制度・施設の独自性（留学・フィールドワーク・プロジェクト・研究資源）
  4) 教育思想の独自性（建学の精神・教育観・学生像）
  5) 卒業後接続の独自性（進路ネットワーク・産学連携・接続の強さ）
他大学にも当てはまる項目は「低」と評価する。

【志望理由書・面接で使える固有名詞を列挙 — 必ず最低件数を満たすこと】
以下を全て具体名で抽出。募集要項PDFに無くても【★研究室・教員・研究テーマ情報】セクションや【学部・学科レベルデータ】【大学公式サイト情報】から拾う。
- 研究室/ゼミ: **最低 5 件、可能なら 10 件**。名称・教員名・研究テーマ・リンク
- 科目/演習/プロジェクト: **最低 5 件**、具体名・概要
- 制度: **最低 3 件**、具体名・概要・リンク
researchmap / KAKEN / 大学公式の研究ディレクトリが収集データに含まれている場合、必ずそこから教員名・研究テーマを拾うこと。
「募集要項に記載がない」という理由で『不明』にしてはならない（募集要項は入試情報、研究情報は別ディレクトリにある）。
収集データにも一切見当たらない場合のみ、当該エントリは「不明（収集データから確認できず）」で短く記す。

【キーフレーズと避けるべき表現】
- key_phrases: 大学公式文書で繰り返される語彙を5-10個列挙
- avoided_phrases: 志望理由書でこの大学相手に使うと弱くなる表現（大学の文脈で軽視・避けられる語彙）を列挙

【面接想定テーマ・接続アンカー】
- 面接で問われやすいテーマをテーマレベルで列挙（具体的質問文ではなくテーマ）
- どんな経験・問題意識がこの大学と強く接続するかのアンカーを列挙（受験生の判断材料）

【年度の基準】
- 2027年度を最新年度として扱い、過去3年分（2026・2025・2024年度）を年度別に整理する
- 最新年度が未公開の場合は「未公開」または「不明」と明記し、過年度値で埋めない
- 倍率・志願者数・合格者数は年度と集計単位（学部単位／学科単位／制度全体）を明記する
- 集計単位が対象学科と一致しない場合はその旨を明記し、断定的比較をしない

【英語資格】
- 要否 / 対象試験名 / 最低基準点 / 有効期間 を分けて記載
- 試験名は確認できても最低基準点が未確認なら、score は「不明（最低基準点は本体PDF未確認）」とする
- 「最低基準点なし」と書くのは公式に明記されている場合のみ

【専願/併願】
- 同一入試方式内での併願 / 同一大学内の他方式との併願 / 他大学との併願 を区別
- 本体PDFまたは公式資料で確認できた範囲のみ記載し、確認できなければ「不明」

【選考方法】
- 一次選考 / 二次選考 を分けて記載（段階が1段階のみなら二次は「該当なし（1段階選考）」）
- 評価観点は公式に明記されている場合のみ記載
- 提出書類は「全員共通で必要な書類」と「条件に応じて追加で必要な書類」を区別

【除外】
- 大学全体・学部レベルのAP・理念・教育方針・カリキュラム・教授陣・就職（Step A/B で取得済み）
- 一般選抜・共通テスト利用の情報
- 記事の導入文案・結論文案・読者向けの語り口

必ず日本語で回答し、JSONのみを出力する（コードブロック不要）。"""

        # ── ナレッジ注入: 過去リサーチで取得済みの情報をプロンプトに注入 ──
        _known_facts_text = ""
        try:
            from core import knowledge as _kn
            _uni_kw = keyword.split()[0]
            _prior = _kn.get_knowledge_hierarchical(_uni_kw, _fac_name)
            if _prior and _prior.get("fields"):
                _known_facts_text = _format_known_facts_for_prompt(_prior["fields"])
        except Exception as _ke:
            console.print(f"  [dim]ナレッジ注入スキップ: {_ke}[/dim]")

        _user_c = f"""対象学部: {_fac_name}
大学キーワード: {keyword}

{_known_facts_text + chr(10) + chr(10) if _known_facts_text else ""}=== Step Aで収集済みの大学共通情報（重複抽出不要） ===
{json.dumps(step_a, ensure_ascii=False)[:2000]}

=== Step Bで収集済みの {_fac_name} 学部情報（重複抽出不要） ===
{json.dumps(_fac_info, ensure_ascii=False)[:1000]}

=== 収集データ（複数サイト横断）===
{combined[:35000]}

【重要】{_fac_name} の学科・専攻・コース別の総合型選抜・学校推薦型選抜情報のみを抽出してください。
一般選抜・共通テスト利用の情報は絶対に含めないこと。

{{
  "universities": [
    {{
      "university": "大学名（公式表記）",
      "faculty": "学部名（公式表記）",
      "department": "学科名・専攻名・コース名（公式表記）",
      "campus": "キャンパス（公式表記。不明は「不明」）",
      "department_detail": "学科・コースの特色（400〜600字。看板分野・独自科目・ゼミ制度・国際性・研究資源など具体的に。不明は「不明」）",
      "formal_ao_name": "総合型選抜の正式名称（公式サイト上の正式名称。確認できない場合は「不明」）",
      "quota": "2027年度募集定員（原文表記そのまま。2027未公表で過年度判明なら「2027年度未公表、2026年度はN名」）",
      "quota_history": {{
        "2026": "募集人員（原文表記）",
        "2025": "募集人員（原文表記）",
        "2024": "募集人員（原文表記）"
      }},
      "ratio_history": {{
        "2026": {{"value": "倍率（数値 or 公式・非公式ともに非公表 or 不明（記載なし））", "unit": "学科単位/学部単位/制度全体", "source_type": "公式/公式リンク先/非公表/不明"}},
        "2025": {{"value": "倍率（同上）", "unit": "学科単位/学部単位/制度全体", "source_type": "公式/公式リンク先/非公表/不明"}},
        "2024": {{"value": "倍率（同上）", "unit": "学科単位/学部単位/制度全体", "source_type": "公式/公式リンク先/非公表/不明"}}
      }},
      "applicants_history": {{
        "2026": "志願者数（不明は「不明」）",
        "2025": "志願者数（不明は「不明」）",
        "2024": "志願者数（不明は「不明」）"
      }},
      "accepted_history": {{
        "2026": "合格者数（不明は「不明」）",
        "2025": "合格者数（不明は「不明」）",
        "2024": "合格者数（不明は「不明」）"
      }},
      "application_period": "出願期間（年月日を明記。複数枠があれば全て。不明は「不明」）",
      "selection_schedule": "選考日程の詳細（一次・二次それぞれの日付、試験日程、面接日程を全部列挙。不明は「不明」）",
      "announcement_date": "合格発表日（一次・二次・最終それぞれ。不明は「不明」）",
      "selection_phase_1": "一次選考の内容（400〜600字。書類審査の評価項目、提出内容、配点や評価比重など具体的に。不明は「不明」）",
      "selection_phase_2": "二次選考の内容（400〜600字。面接の形式・時間・質問傾向、小論文のテーマ・字数・時間、プレゼンの内容など具体的に。1段階選考は「該当なし（1段階選考）」。不明は「不明」）",
      "selection_methods": ["選考方法を具体的に（書類審査/面接/小論文/プレゼンテーション/課題研究/口頭試問 等）"],
      "evaluation_criteria": "評価観点（400〜600字。公式が明示する評価軸を引用。活動実績の種類別の重み、学力要件など。明記なしは「公式記載なし」、不明は「不明」）",
      "submitted_documents": {{
        "common": ["全員共通で必要な書類を具体名で列挙（志願理由書 / 調査書 / 活動報告書 / 推薦状 / 任意提出資料 等）"],
        "conditional": [
          {{"document": "条件付き書類名", "condition": "必要となる条件（例: 海外高校出身者のみ / 英語外部試験で出願する場合のみ）"}}
        ]
      }},
      "eligibility": "出願資格・条件（500〜800字。高校種別・評定平均・既卒可否・英語資格・特定科目履修・国籍・海外在住経験など具体的に全条件を列挙。不明は「不明」）",
      "application_type": {{
        "intra_method": "同一入試方式内での併願可否（専願/併願/不明）",
        "intra_university": "同一大学内の他方式との併願可否（可/不可/不明）",
        "inter_university": "他大学との併願可否（可/不可/不明）",
        "notes": "補足（不明は空文字）"
      }},
      "gpa_requirement": "評定条件（300〜500字。全体の評定平均、特定科目の評定下限、例外規定、免除条件など具体的に。不明は「不明」）",
      "external_exam_requirements": {{
        "required": true,
        "exams": [
          {{
            "name": "試験名（英検CSEスコア/GTEC/IELTS/TEAP/TOEFL iBT/TOEIC等）",
            "score": "最低基準点（確認できない場合は「不明（最低基準点は本体PDF未確認）」）",
            "validity": "有効期間（確認できない場合は「不明」）",
            "notes": "備考"
          }}
        ],
        "notes": "補足（不明の場合は required: null）"
      }},
      "admission_policy": {{
        "summary": "学科別AP要約（500〜800字）。フォールバック順: ①公式HP・PDFの学科別AP → ②公式の「求める学生像」「望む人物像」→ ③リクナビ進学・パスナビの「こんな人に向いている」(source_type=aggregator) → ④公式が「APは制定していない」と明記なら「公式非制定」と記す → ⑤上記すべてなしのときのみ「不明」。APページのURL必須",
        "keywords": ["APキーワード（3〜5個。公式記述から抽出）"]
      }},
      "features": "総合型選抜の特徴（300〜500字。制度の独自性・他方式との違い・過去の変更点などを具体的に。不明は「不明」）",
      "difficulty_facts": {{
        "ratio_observations": "倍率・データの事実記載（年度と集計単位を必ず明記。評価や断定はしない。不明は「不明」）",
        "notes": "難易度に関する事実（公式記載のみ。主観評価は書かない）"
      }},
      "comparison_axes": ["比較素材の軸（例: 自由度 / 国際性 / 研究志向）。評価や仮説は書かず事実ベースの軸のみ列挙。不足時は配列を短くする"],
      "inferred_persona": {{
        "tendency": "一次情報から論理的に読み取れる人物像の傾向（簡潔に）",
        "basis": "その傾向の根拠となる一次情報の記述（引用または要約）"
      }},
      "uniqueness_layers": {{
        "research_theme":           {{"level": "高|中|低", "reasoning": "この大学でしかできない研究テーマ・分野・アプローチの具体（200-400字）"}},
        "curriculum_structure":     {{"level": "高|中|低", "reasoning": "必修/選択の組み合わせ、学部横断性、独自演習など構造的な独自性（200-400字）"}},
        "facility_system":          {{"level": "高|中|低", "reasoning": "留学・フィールドワーク・研究資源・独自制度の独自性（200-400字）"}},
        "educational_philosophy":   {{"level": "高|中|低", "reasoning": "建学の精神・教育観・求める学生像の独自性（200-400字）"}},
        "career_connection":        {{"level": "高|中|低", "reasoning": "進路ネットワーク・産学連携・卒業後接続の独自性（200-400字）"}}
      }},
      "specific_materials": {{
        "research_labs": [
          {{"name": "研究室/ゼミの具体名", "professor": "教員名", "theme": "研究テーマ", "url": "URL（不明なら空文字）"}}
        ],
        "courses": [
          {{"name": "科目/演習/プロジェクトの具体名", "description": "内容の具体（100-200字）"}}
        ],
        "systems": [
          {{"name": "制度の具体名（XX留学プログラム / XX奨学金等）", "description": "内容の具体（100-200字）", "url": "URL（不明なら空文字）"}}
        ]
      }},
      "key_phrases": ["大学公式文書で繰り返される語彙を5-10個。（例: 「実践知」「社会デザイン」「越境」「現場主義」等、この大学固有の表現）"],
      "avoided_phrases": ["志望理由書でこの大学相手に使うと弱くなる表現（例: 「伝統がある」「少人数教育」「偏差値が高い」「グローバル人材になりたい」など、大学の文脈で軽視される一般論）を5-8個"],
      "interview_topics": ["面接で問われやすいテーマ（具体的質問文ではなくテーマレベル。例: 「研究テーマの深掘り」「海外経験との接続」「課題設定の妥当性」「他大学ではなくこの大学を選ぶ必然性」など）を5-8個"],
      "match_anchors": ["どんな経験・問題意識がこの大学と強く接続するかのアンカー（例: 「地方自治体でのフィールドワーク経験」「越境的な学際プロジェクトの推進経験」「当事者性のある社会課題への関与」など）を5-8個"],
      "pdf_confirmed": "raw PDF 本体（入試要項本体PDF）を確認できたか true/false",
      "official_sources": [
        {{"label": "資料名（例: 2027年度入試要項PDF / 大学公式 総合型選抜ページ）", "url": "フルURL（raw PDFがある場合は raw PDF 直接URLを優先）"}}
      ],
      "field_sources": {{
        "quota": "出典URL",
        "application_period": "出典URL",
        "selection_schedule": "出典URL",
        "announcement_date": "出典URL",
        "selection_phase_1": "出典URL",
        "selection_phase_2": "出典URL",
        "evaluation_criteria": "出典URL",
        "submitted_documents": "出典URL",
        "eligibility": "出典URL",
        "gpa_requirement": "出典URL",
        "external_exam_requirements": "出典URL",
        "application_type": "出典URL",
        "admission_policy": "出典URL",
        "ratio_history": "出典URL"
      }},
      "references": {{
        "description": "公式では確認できなかったが、非公式情報源（パスナビ・東進・みんなの大学情報・河合塾Kei-Net等の塾系/受験情報サイト）に記載がある場合のみ、ここに参考値として収録する。公式で確認できた項目は references に入れない。公式・非公式の両方とも情報がなければ空配列/空オブジェクトで良い。",
        "ratio_history": [
          {{"year": "2026", "value": "倍率値", "source_label": "情報源名（例: パスナビ）", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "applicants_history": [
          {{"year": "2026", "value": "志願者数", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "accepted_history": [
          {{"year": "2026", "value": "合格者数", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "quota_history": [
          {{"year": "2026", "value": "募集人員", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "gpa_requirement": [
          {{"value": "参考値（例: 評定平均4.0以上）", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "external_exam_requirements": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "selection_phase_1": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "selection_phase_2": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "application_period": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "selection_schedule": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "submitted_documents": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ],
        "application_type": [
          {{"value": "参考値", "source_label": "情報源名", "source_url": "URL", "reliability": "参考（非公式）"}}
        ]
      }},
      "url": "最も詳細な情報が得られた公式URL（不明は空文字）"
    }}
  ],
  "overall_trends": ["全体的な傾向（公式記述・事実のみ。評価表現禁止）"],
  "selection_method_ranking": [{{"method": "選考方法名", "count": 頻出数, "note": "特記事項"}}],
  "ratio_summary": "倍率の全体的な傾向（事実のみ。主観評価禁止。不明は「不明」）",
  "key_insights": ["一次情報から論理的に読める範囲のインサイトのみ（断定しすぎない）"],
  "data_quality": "情報品質の評価と理由（raw PDF 確認可否・公式サイト到達可否・非公式依存度を明記）"
}}

注意事項:
- universities は最大15件まで
- 不明な値は「不明」と記載する（0・空欄・「要確認」を使わない）
- ratio_history は year → {{value, unit, source_type}} のオブジェクト形式で出す。集計単位が不一致なら unit に明記すること
- quota は本文中に「N名」「若干名」「程度」「約」等の記述があれば必ず抽出する（difficulty_facts の本文だけで触れて quota フィールドが「不明」は禁止）
- inferred_persona は評価ではなく「一次情報からこう読める」レベルにとどめる
- 学科別AP が公式に存在しない場合、keywords は空配列にすること
- raw PDF 未確認時は、英語資格の最低基準点・評定例外・併願詳細・二次選考内容・提出書類を勝手に断定しないこと（募集人数は本文に記述があれば PDF 未確認でも抽出）

【references（参考・非公式情報）の扱い】— 積極的に埋めること
- メインフィールドは「大学公式HP or 公式PDF で確認できた情報」を採用する
- 公式で確認できず、かつ収集データに非公式情報源（パスナビ・東進・みんなの大学情報・河合塾Kei-Net・スタディサプリ・マイナビ進学・受験ラボ・週刊朝日・リセマム・note の予備校アカウント等）からの値が存在する場合、その項目の `references` に必ず収録する
- 公式で不明な項目に非公式情報があるのに references を空にするのは「情報欠落」とみなす。必ず拾うこと
- references に入れる値には必ず source_label（情報源名）と source_url（フルURL）を付ける
- 公式で値が取れている項目は references に入れない（公式で十分）
- 収集データに該当項目の非公式値が一切無ければ、その項目の references 配列は空にする
- references の値は主値を上書きしない。UI 側で「参考（非公式）」バッジ付きで並列表示される
- 特に以下の項目は非公式サイトに記載が多いので必ず references を確認: 倍率（ratio_history）/ 志願者数・合格者数（applicants_history, accepted_history）/ 募集人員（quota_history）/ 評定条件（gpa_requirement）/ 英語資格（external_exam_requirements）/ 選考方法（selection_phase_1/2）"""

        with console.status(f"[yellow]  {_fac_name} 学科情報抽出中...[/yellow]"):
            _raw_c = analyze_with_claude(client, _system_c, _user_c, max_tokens=16000, model=MODEL_DEEP)

        _step_c_fac = _parse_json_robust(_raw_c)
        _step_c_fac = _filter_general_exam_entries(_step_c_fac)

        _unis = _step_c_fac.get("universities", [])

        # ── Deep Research 反復ラウンド（ギャップ埋め）— Premium プランのみ有効
        _deep_rounds = 1 if enable_deep_research else int(os.environ.get("RESEARCH_DEEP_ROUNDS", "0"))
        _deep_max_unis = int(os.environ.get("RESEARCH_DEEP_MAX_UNIS", "1"))
        _deep_max_urls = int(os.environ.get("RESEARCH_DEEP_MAX_URLS", "5"))
        _deep_max_queries = int(os.environ.get("RESEARCH_DEEP_MAX_QUERIES", "4"))
        if _deep_rounds > 0 and _unis:
            for _u in _unis[:_deep_max_unis]:
                for _round_i in range(_deep_rounds):
                    try:
                        _deep_research_gap_fill(
                            _u, keyword, progress_cb=progress_cb,
                            max_queries=_deep_max_queries, max_urls=_deep_max_urls,
                        )
                        import gc as _gc
                        _gc.collect()
                    except Exception as _de:
                        console.print(f"  [yellow]Deep Research 失敗: {_de}[/yellow]")
                        break

        for _u in _unis:
            _ensure_legacy_fields(_u)

        # ── LLM が「公式サイトの教員ページ <URL> に教員一覧あり」等と書いた場合、
        # その URL を直接叩いて教員リストを抽出する（二次救済層）
        for _u in _unis:
            _sm = _u.get("specific_materials") or {}
            _labs = _sm.get("research_labs") or []
            # プレースホルダ判定: name/professor に「不明」「該当」「収集データ」を含む
            _placeholder_entries = [l for l in _labs if isinstance(l, dict) and any(
                t in str(l.get("name", "")) + str(l.get("professor", "")) + str(l.get("theme", ""))
                for t in ("不明", "該当", "収集データから")
            )]
            # プレースホルダから URL を抽出
            _hint_urls: list[str] = []
            for _pl in _placeholder_entries:
                for _f in ("name", "theme", "description", "url"):
                    _hint_urls.extend(_extract_urls_from_text(str(_pl.get(_f, ""))))
            # 全体 text からも（features/department_detail 等に URL が書かれていることも）
            for _f in ("features", "department_detail"):
                _hint_urls.extend(_extract_urls_from_text(str(_u.get(_f, ""))))
            # dedupe、.ac.jp のみ対象
            _hint_urls = list(dict.fromkeys([u for u in _hint_urls if ".ac.jp" in u]))[:3]

            if _hint_urls:
                console.print(f"  [cyan]不明コメント内URLから教員抽出: {_hint_urls}[/cyan]")
                _scraped_profiles: list[dict] = []
                for _u_url in _hint_urls:
                    try:
                        _profs = _extract_faculty_from_page(_u_url, max_profiles=10)
                        _scraped_profiles.extend(_profs)
                    except Exception as _e:
                        console.print(f"  [yellow]教員抽出失敗 ({_u_url[:60]}): {_e}[/yellow]")
                if _scraped_profiles:
                    _sm.setdefault("research_labs", [])
                    # プレースホルダを除去して置換
                    _valid = [l for l in _labs if l not in _placeholder_entries]
                    _existing_profs = {str(l.get("professor", "")).strip() for l in _valid}
                    for p in _scraped_profiles:
                        if p["professor"] and p["professor"] not in _existing_profs:
                            _valid.append({
                                "name":      p["name"],
                                "professor": p["professor"],
                                "theme":     p.get("theme", "") or "研究テーマ詳細は公式プロファイル参照",
                                "url":       p["url"],
                            })
                            _existing_profs.add(p["professor"])
                    _sm["research_labs"] = _valid
                    _u["specific_materials"] = _sm
                    console.print(f"  [cyan]↻ {_u.get('department', '')}: 教員ページから {len(_scraped_profiles)} 名抽出[/cyan]")

        # ── researchmap 直接取得データで research_labs を機械的に強化 ──
        # LLM が「不明（推定）」等を返している場合に、構造化データで上書き
        if _researchmap_profiles:
            for _u in _unis:
                _sm = _u.setdefault("specific_materials", {})
                _existing = _sm.get("research_labs") or []
                # 既存が空 or 全て「不明」or プレースホルダなら置き換え
                def _is_placeholder_lab(lab: dict) -> bool:
                    if not isinstance(lab, dict):
                        return True
                    name = str(lab.get("name", ""))
                    prof = str(lab.get("professor", ""))
                    # 「不明」「推定」「該当」を含めば placeholder
                    return any(t in name + prof for t in ("不明", "推定", "該当", "収集データから"))
                _valid_existing = [l for l in _existing if not _is_placeholder_lab(l)]
                if len(_valid_existing) < 3 and _researchmap_profiles:
                    _sm["research_labs"] = [
                        {
                            "name":      p["name"],
                            "professor": p["professor"],
                            "theme":     p["theme"] or "、".join(p.get("keywords", [])) or "研究テーマ詳細は researchmap プロファイル参照",
                            "url":       p["url"],
                        }
                        for p in _researchmap_profiles
                    ]
                    console.print(f"  [cyan]↻ {_u.get('department', '')}: research_labs を researchmap データで上書き（{len(_researchmap_profiles)}件）[/cyan]")
                else:
                    # 既存に有効値あり。ただし researchmap プロファイル側に
                    # 既存に含まれない教員があれば追加
                    existing_profs = {str(l.get("professor", "")).strip() for l in _valid_existing}
                    for p in _researchmap_profiles:
                        if p["professor"] and p["professor"] not in existing_profs:
                            _valid_existing.append({
                                "name":      p["name"],
                                "professor": p["professor"],
                                "theme":     p["theme"],
                                "url":       p["url"],
                            })
                    _sm["research_labs"] = _valid_existing

        # ── 検証層: 別系統モデル（GPT-4o / Gemini Pro）で critical fields を事実照合
        if _unis:
            try:
                from core import verification as _verif
                _verify_source = combined[:10000]
                _dept_verification = _verify_step_c_departments(
                    _unis, _verify_source, _fac_name,
                )
            except Exception as _verr:
                console.print(f"  [yellow]⚠ {_fac_name}: 検証エラー ({_verr})[/yellow]")
                _dept_verification = {}

            for _u in _unis:
                _dkey = _u.pop("_verify_key", None) or f"{_u.get('faculty', '')}/{_u.get('department', '')}"
                _fc = _dept_verification.get(_dkey) or {}
                _sup = sum(1 for e in _fc.values() if e.get("supported") is True)
                _unsup = sum(1 for e in _fc.values() if e.get("supported") is False)
                _amb = sum(1 for e in _fc.values() if e.get("supported") is None and e.get("verifiers"))
                _u["_verification"] = {
                    "fact_check": _fc,
                    "summary": {"checked": len(_fc), "supported": _sup, "unsupported": _unsup, "ambiguous": _amb, "skipped": 0},
                    "verifiers": [],
                }
            console.print(f"    [cyan]検証完了: {_fac_name}[/cyan]")

        return {
            "unis": _unis,
            "trends": _step_c_fac.get("overall_trends", []),
            "ranking": _step_c_fac.get("selection_method_ranking", []),
            "insights": _step_c_fac.get("key_insights", []),
            "ratio": _step_c_fac.get("ratio_summary", ""),
            "dq": f"{_fac_name}: {_step_c_fac.get('data_quality', '')}" if _step_c_fac.get("data_quality") else "",
            "fac_name": _fac_name,
        }

    # 学部を最大3並列で処理
    from concurrent.futures import ThreadPoolExecutor as _StepCPool
    with _StepCPool(max_workers=2) as _pool:
        _fac_results = list(_pool.map(
            lambda args: _process_faculty(*args),
            enumerate(faculties_from_b),
        ))

    for _fr in _fac_results:
        _all_universities.extend(_fr["unis"])
        _all_overall_trends.extend(_fr["trends"])
        _all_selection_ranking.extend(_fr["ranking"])
        _all_key_insights.extend(_fr["insights"])
        if not _ratio_summary and _fr["ratio"]:
            _ratio_summary = _fr["ratio"]
        if _fr["dq"]:
            _data_quality_parts.append(_fr["dq"])
        console.print(f"    [green]✓ {_fr['fac_name']}: {len(_fr['unis'])}件抽出[/green]")

    # マージ結果でstep_cを構成
    step_c = {
        "universities": _all_universities,
        "overall_trends": list(dict.fromkeys(_all_overall_trends)),  # 重複除去
        "selection_method_ranking": _all_selection_ranking,
        "ratio_summary": _ratio_summary,
        "key_insights": list(dict.fromkeys(_all_key_insights)),
        "data_quality": " / ".join(_data_quality_parts),
    }

    # Step C 全体の検証サマリ
    _verified_depts = [u for u in _all_universities
                       if u.get("_verification", {}).get("summary", {}).get("checked", 0) > 0]
    _field_ratio: dict = {}
    for _field in CRITICAL_FIELDS_C:
        _checked = 0
        _ok = 0
        for u in _verified_depts:
            entry = u.get("_verification", {}).get("fact_check", {}).get(_field)
            if entry is None:
                continue
            _checked += 1
            if entry.get("supported") is True:
                _ok += 1
        _field_ratio[_field] = round(_ok / _checked, 3) if _checked else None
    _all_verifiers: list[dict] = []
    _seen_v: set = set()
    for u in _verified_depts:
        for v in u.get("_verification", {}).get("verifiers", []):
            t = (v.get("provider"), v.get("model"))
            if t not in _seen_v:
                _seen_v.add(t)
                _all_verifiers.append(v)
    step_c["_verification"] = {
        "total_departments": len(_all_universities),
        "verified_departments": len(_verified_depts),
        "field_supported_ratio": _field_ratio,
        "verifiers": _all_verifiers,
    }
    # Step A 検証結果を回収（Step C と並列で実行していた）
    try:
        _fact_check_a = _verify_future_a.result(timeout=60)
        step_a["_verification"] = {
            "fact_check": _fact_check_a,
            "verifiers": list({(v["provider"], v["model"])
                               for entry in _fact_check_a.values()
                               for v in entry.get("verifiers", [])}),
        }
        _supported = sum(1 for e in _fact_check_a.values() if e.get("supported") is True)
        _unsupported = sum(1 for e in _fact_check_a.values() if e.get("supported") is False)
        console.print(f"  [green]✓ Step A 検証完了[/green]: 支持 {_supported} / 非支持 {_unsupported}")
    except Exception as _ve:
        step_a["_verification"] = {"fact_check": {}, "verifiers": [], "error": str(_ve)}
        console.print(f"  [yellow]Step A 検証: タイムアウトまたはエラー ({_ve})[/yellow]")
    _verify_executor_a.shutdown(wait=False)

    _print_university_result(step_c)

    # _source_log に PDF 取得結果を統合
    _source_log["PDF募集要項"] = _pdf_fetch_log if _pdf_fetch_log else {"全年度": "未取得: 自動検索で全年度のPDFが見つからなかった"}

    return {
        "step_a": step_a,
        "step_b": step_b,
        "step_c": step_c,
        # 後方互換性のため既存キーも維持
        "universities": step_c.get("universities", []),
        "overall_trends": step_c.get("overall_trends", []),
        "selection_method_ranking": step_c.get("selection_method_ranking", []),
        "ratio_summary": step_c.get("ratio_summary", ""),
        "key_insights": step_c.get("key_insights", []),
        "data_quality": step_c.get("data_quality", ""),
        "data_collection_log": _source_log,
    }


def _print_university_result(data: dict):
    universities = data.get("universities", [])
    if universities:
        t = Table(
            title=f"総合型選抜 募集要項まとめ ({len(universities)}件)",
            box=box.ROUNDED, show_lines=True,
        )
        t.add_column("大学名", style="bold yellow", max_width=14)
        t.add_column("学部", max_width=12)
        t.add_column("学科", max_width=12)
        t.add_column("定員", justify="center", width=5)
        t.add_column("評定", justify="center", width=6)
        t.add_column("外部試験", max_width=18)
        t.add_column("倍率(26/25/24)", max_width=16)
        t.add_column("出願期間", max_width=14)
        t.add_column("選考方法", max_width=20)
        for u in universities:
            methods = " / ".join(str(m) for m in (u.get("selection_methods") or []) if m)
            rh = u.get("ratio_history", {})
            ratio_str = f"{rh.get('2026','?')} / {rh.get('2025','?')} / {rh.get('2024','?')}"
            ext_req = u.get("external_exam_requirements", {})
            ext_str = ""
            if ext_req and ext_req.get("exams"):
                ext_str = " / ".join(
                    f"{e.get('name','')}{e.get('score','')}" for e in ext_req["exams"][:3]
                )
            elif ext_req and ext_req.get("required") is False:
                ext_str = "不要"
            elif ext_req and ext_req.get("required") is None:
                ext_str = "不明"
            t.add_row(
                u.get("university", ""),
                u.get("faculty", ""),
                u.get("department", ""),
                u.get("quota", ""),
                u.get("gpa_requirement", ""),
                ext_str,
                ratio_str,
                u.get("application_period", ""),
                methods,
            )
        console.print(t)

    # 倍率サマリー
    ratio_summary = data.get("ratio_summary", "")
    if ratio_summary:
        console.print(f"  [bold cyan]倍率傾向:[/bold cyan] {ratio_summary}")

    # 選考方法ランキング
    ranking = data.get("selection_method_ranking", [])
    if ranking:
        t2 = Table(title="選考方法 頻出ランキング", box=box.SIMPLE)
        t2.add_column("選考方法", style="bold")
        t2.add_column("頻出数", justify="right", style="cyan")
        t2.add_column("特記事項", style="dim")
        for r in ranking[:8]:
            t2.add_row(r.get("method", ""), str(r.get("count", "")), r.get("note", ""))
        console.print(t2)

    trends = data.get("overall_trends", [])
    if trends:
        console.print(Panel(
            "\n".join(f"  {i+1}. {tr}" for i, tr in enumerate(trends)),
            title="[cyan]全体傾向[/cyan]", border_style="cyan",
        ))

    insights = data.get("key_insights", [])
    if insights:
        console.print(Panel(
            "\n".join(f"  • {ins}" for ins in insights),
            title="[green]受験生へのインサイト[/green]", border_style="green",
        ))

    # 大学ごとの詳細（上位5件のみ端末に表示）
    if universities:
        for u in universities[:5]:
            name    = u.get("university", "")
            faculty = u.get("faculty", "")
            lines = []
            # 倍率推移
            rh = u.get("ratio_history", {})
            if any(v != "要確認" for v in rh.values()):
                lines.append(f"  [bold]倍率推移:[/bold] 2026:{rh.get('2026','?')} / 2025:{rh.get('2025','?')} / 2024:{rh.get('2024','?')}")
            # 出願資格・評定
            gpa = u.get("gpa_requirement", "")
            if gpa and gpa != "情報なし":
                lines.append(f"  [bold]評定条件:[/bold] {gpa}")
            elig = u.get("eligibility", "")
            if elig and elig != "情報なし":
                lines.append(f"  [bold]出願資格:[/bold] {elig[:100]}")
            ext_req = u.get("external_exam_requirements", {})
            if ext_req and ext_req.get("exams"):
                ext_lines = [f"{e.get('name','')}: {e.get('score','')}" for e in ext_req["exams"]]
                lines.append(f"  [bold]外部試験:[/bold] {' / '.join(ext_lines)}")
            elif ext_req and ext_req.get("required") is False:
                lines.append(f"  [bold]外部試験:[/bold] 不要")
            # 選考日程
            sched = u.get("selection_schedule", "")
            if sched and sched != "情報なし":
                lines.append(f"  [bold]選考日程:[/bold] {sched[:100]}")
            # AP
            ap = u.get("admission_policy", "")
            if ap and ap != "情報なし":
                lines.append(f"  [bold]AP:[/bold] {ap[:100]}")
            # データソース
            sources = u.get("data_sources", [])
            if sources:
                lines.append(f"  [dim]情報源: {' / '.join(sources)}[/dim]")
            if lines:
                console.print(Panel(
                    "\n".join(lines),
                    title=f"[yellow]{name}　{faculty}[/yellow]",
                    border_style="dim",
                ))

    dq = data.get("data_quality", "")
    if dq:
        console.print(f"  [dim]データ品質: {dq}[/dim]")


# ─────────────────────────────────────────
# ニュース分析
# ─────────────────────────────────────────
def _fetch_yahoo_news_rss(keyword: str) -> list[dict]:
    """Yahoo!ニュース RSS からエントリを取得する（複数URLフォールバック付き）"""
    encoded = urllib.parse.quote(keyword)
    # 複数のRSSエンドポイントを順に試す
    urls = [
        f"https://news.yahoo.co.jp/rss/search?p={encoded}&ei=utf-8",
        f"https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NOTE_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    continue
                data = resp.read()
            root = ET.fromstring(data)
            items = []
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                link  = item.findtext("link") or ""
                desc  = item.findtext("description") or ""
                pub   = item.findtext("pubDate") or ""
                source_el = item.find("source")
                source = source_el.text if source_el is not None else "Yahoo!ニュース"
                if title:
                    items.append({"title": title, "url": link, "description": desc,
                                  "published": pub, "source": source})
            if items:
                return items[:30]
        except Exception:
            continue
    return []


def run_news_analysis(client, keyword: str, progress_cb=None) -> dict:
    def _notify(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass
    console.print(Rule(f"[bold]📰 ニュース分析[/bold] — {keyword}"))
    _notify("ニュース分析 開始")

    raw_items: list[dict] = []
    with Progress(console=console, transient=True) as prog:
        t = prog.add_task("Yahoo!ニュース RSS 取得中…", total=None)
        rss = _fetch_yahoo_news_rss(keyword)
        raw_items.extend(rss)
        prog.update(t, description=f"RSS {len(rss)} 件取得")

        # DuckDuckGo フォールバック（RSSが空でも必ず実行）
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            ddgs = DDGS()
            for q in NEWS_QUERIES[:4]:
                try:
                    for r in ddgs.text(q, max_results=6):
                        raw_items.append({"title": r.get("title",""), "url": r.get("href",""),
                                          "description": r.get("body",""), "published": "", "source": "DuckDuckGo"})
                    time.sleep(0.2)
                except Exception:
                    time.sleep(0.5)
                    continue
        except Exception as e:
            console.print(f"[yellow]  DuckDuckGo取得エラー: {e}[/yellow]")
        prog.update(t, description=f"合計 {len(raw_items)} 件")

    # Claude で分析
    text_lines = []
    for i, item in enumerate(raw_items[:40], 1):
        if item.get("error"):
            continue
        text_lines.append(
            f"[{i}] {item.get('title','')} / {item.get('source','')} / {item.get('published','')}\n"
            f"  URL: {item.get('url','')}\n"
            f"  {item.get('description','')[:200]}"
        )
    combined = "\n\n".join(text_lines)[:25000]

    # データが空の場合はClaudeの知識で補完
    if not combined.strip():
        combined = f"※ リアルタイムデータの取得に失敗しました。Claudeの学習データに基づいて「{keyword}」に関するニュース動向を分析してください。"

    prompt = f"""以下は「{keyword}」に関する最新ニュース一覧です。

{combined}

以下のJSON形式で分析してください（コードブロック不要、純粋なJSONのみ）:
{{
  "summary": "全体サマリー（3〜5文）",
  "hot_topics": [
    {{"rank": 1, "topic": "話題のトピック", "description": "概要", "source": "媒体名", "url": "URL"}}
  ],
  "trend_direction": "トレンドの方向性（増加/減少/横ばい/注目など）",
  "key_themes": ["テーマ1", "テーマ2"],
  "policy_updates": ["制度・政策の変更点1", "変更点2"],
  "insights": ["マーケティング示唆1", "示唆2"],
  "data_quality": "データ品質コメント"
}}"""

    try:
        result = _extract_json_via_router("news", prompt, max_tokens=3000)
        if result.get("error") and not result.get("raw_items"):
            result["raw_items"] = raw_items[:20]
    except Exception as e:
        result = {"error": str(e), "raw_items": raw_items[:20]}

    _print_news(result)
    return result


def _print_news(data: dict):
    if data.get("error"):
        console.print(f"[red]エラー: {data['error']}[/red]")
        return
    if data.get("summary"):
        console.print(Panel(data["summary"], title="サマリー", border_style="blue"))
    topics = data.get("hot_topics", [])
    if topics:
        tbl = Table("順位", "トピック", "媒体", "URL", show_header=True, header_style="bold cyan")
        for t in topics[:10]:
            tbl.add_row(str(t.get("rank","")), t.get("topic","")[:40],
                        t.get("source","")[:20], t.get("url","")[:50])
        console.print(tbl)
    if data.get("trend_direction"):
        console.print(f"  トレンド方向: [yellow]{data['trend_direction']}[/yellow]")
    for ins in data.get("insights", []):
        console.print(f"  [dim]→ {ins}[/dim]")


# ─────────────────────────────────────────
# Amazon 参考書分析
# ─────────────────────────────────────────
def run_amazon_analysis(client, keyword: str) -> dict:
    console.print(Rule(f"[bold]📚 Amazon 参考書分析[/bold] — {keyword}"))

    raw_items: list[dict] = []
    with Progress(console=console, transient=True) as prog:
        t = prog.add_task("DuckDuckGo で Amazon 情報取得中…", total=None)
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            ddgs = DDGS()
            for q in AMAZON_QUERIES:
                for r in ddgs.text(q, max_results=6):
                    raw_items.append({"title": r.get("title",""), "url": r.get("href",""),
                                      "description": r.get("body","")})
                time.sleep(0.1)
        except Exception as e:
            raw_items.append({"error": str(e)})
        prog.update(t, description=f"{len(raw_items)} 件取得")

    text_lines = []
    for i, item in enumerate(raw_items[:40], 1):
        if item.get("error"):
            continue
        text_lines.append(
            f"[{i}] {item.get('title','')}\n  URL: {item.get('url','')}\n  {item.get('description','')[:250]}"
        )
    combined = "\n\n".join(text_lines)[:25000]

    prompt = f"""以下は「{keyword}」に関するAmazon参考書・書籍の検索結果です。

{combined}

以下のJSON形式で分析してください（コードブロック不要）:
{{
  "summary": "全体サマリー（3〜5文）",
  "top_books": [
    {{"rank": 1, "title": "書名", "author": "著者", "price": "価格", "rating": "評価",
      "reviews": "レビュー数", "why_popular": "なぜ人気か", "url": "URL"}}
  ],
  "popular_categories": ["カテゴリ1", "カテゴリ2"],
  "reader_concerns": ["読者の悩み1", "悩み2"],
  "content_gaps": ["まだ少ないテーマ1", "テーマ2"],
  "insights": ["マーケティング示唆1", "示唆2"],
  "data_quality": "データ品質コメント"
}}"""

    try:
        result = _extract_json_via_router("amazon", prompt, max_tokens=1200)
    except Exception as e:
        result = {"error": str(e)}

    _print_amazon(result)
    return result


def _print_amazon(data: dict):
    if data.get("error"):
        console.print(f"[red]エラー: {data['error']}[/red]")
        return
    if data.get("summary"):
        console.print(Panel(data["summary"], title="Amazon 参考書サマリー", border_style="blue"))
    books = data.get("top_books", [])
    if books:
        tbl = Table("順位", "書名", "著者", "評価", "なぜ人気", show_header=True, header_style="bold cyan")
        for b in books[:10]:
            tbl.add_row(str(b.get("rank","")), b.get("title","")[:35],
                        b.get("author","")[:20], b.get("rating",""), b.get("why_popular","")[:40])
        console.print(tbl)
    for ins in data.get("insights", []):
        console.print(f"  [dim]→ {ins}[/dim]")


# ─────────────────────────────────────────
# TikTok トレンド分析
# ─────────────────────────────────────────
def run_tiktok_analysis(client, keyword: str) -> dict:
    console.print(Rule(f"[bold]🎵 TikTok トレンド分析[/bold] — {keyword}"))

    raw_items: list[dict] = []
    with Progress(console=console, transient=True) as prog:
        t = prog.add_task("DuckDuckGo で TikTok 情報取得中…", total=None)
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            ddgs = DDGS()
            for q in TIKTOK_QUERIES:
                try:
                    for r in ddgs.text(q, max_results=6):
                        raw_items.append({"title": r.get("title",""), "url": r.get("href",""),
                                          "description": r.get("body","")})
                    time.sleep(0.2)
                except Exception:
                    time.sleep(0.5)
                    continue
        except Exception as e:
            console.print(f"[yellow]  DuckDuckGo取得エラー: {e}[/yellow]")
        prog.update(t, description=f"{len(raw_items)} 件取得")

    text_lines = []
    for i, item in enumerate(raw_items[:40], 1):
        if item.get("error"):
            continue
        text_lines.append(
            f"[{i}] {item.get('title','')}\n  URL: {item.get('url','')}\n  {item.get('description','')[:250]}"
        )
    combined = "\n\n".join(text_lines)[:25000]

    if not combined.strip():
        combined = f"※ リアルタイムデータの取得に失敗しました。Claudeの学習データに基づいて「{keyword}」に関するTikTokトレンドを分析してください。"

    prompt = f"""以下は「{keyword}」に関するTikTok動画・ハッシュタグの検索結果です。

{combined}

以下のJSON形式で分析してください（コードブロック不要、純粋なJSONのみ）:
{{
  "summary": "全体サマリー（3〜5文）",
  "trending_hashtags": [
    {{"rank": 1, "hashtag": "#ハッシュタグ", "view_count": "再生数目安", "description": "内容傾向"}}
  ],
  "popular_creators": [
    {{"name": "クリエイター名", "style": "動画スタイル", "strength": "強み"}}
  ],
  "content_formats": ["人気フォーマット1（例：勉強vlog）", "フォーマット2"],
  "hook_patterns": ["冒頭フックパターン1", "パターン2"],
  "content_gaps": ["まだ少ないテーマ1", "テーマ2"],
  "insights": ["マーケティング示唆1", "示唆2"],
  "data_quality": "データ品質コメント"
}}"""

    try:
        result = _extract_json_via_router("tiktok", prompt, max_tokens=3000)
    except Exception as e:
        result = {"error": str(e)}

    _print_tiktok(result)
    return result


def _print_tiktok(data: dict):
    if data.get("error"):
        console.print(f"[red]エラー: {data['error']}[/red]")
        return
    if data.get("summary"):
        console.print(Panel(data["summary"], title="TikTok サマリー", border_style="blue"))
    tags = data.get("trending_hashtags", [])
    if tags:
        tbl = Table("順位", "ハッシュタグ", "再生数目安", "内容傾向", show_header=True, header_style="bold cyan")
        for tag in tags[:10]:
            tbl.add_row(str(tag.get("rank","")), tag.get("hashtag",""),
                        tag.get("view_count",""), tag.get("description","")[:40])
        console.print(tbl)
    for ins in data.get("insights", []):
        console.print(f"  [dim]→ {ins}[/dim]")


# ─────────────────────────────────────────
# X (Twitter) トレンド分析
# ─────────────────────────────────────────
def run_xtrends_analysis(client, keyword: str) -> dict:
    console.print(Rule(f"[bold]🐦 X/Twitter トレンド分析[/bold] — {keyword}"))

    raw_items: list[dict] = []
    with Progress(console=console, transient=True) as prog:
        t = prog.add_task("DuckDuckGo で X/Twitter 情報取得中…", total=None)
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            ddgs = DDGS()
            for q in XTRENDS_QUERIES:
                try:
                    for r in ddgs.text(q, max_results=6):
                        raw_items.append({"title": r.get("title",""), "url": r.get("href",""),
                                          "description": r.get("body","")})
                    time.sleep(0.2)
                except Exception:
                    time.sleep(0.5)
                    continue
        except Exception as e:
            console.print(f"[yellow]  DuckDuckGo取得エラー: {e}[/yellow]")
        prog.update(t, description=f"{len(raw_items)} 件取得")

    text_lines = []
    for i, item in enumerate(raw_items[:40], 1):
        if item.get("error"):
            continue
        text_lines.append(
            f"[{i}] {item.get('title','')}\n  URL: {item.get('url','')}\n  {item.get('description','')[:250]}"
        )
    combined = "\n\n".join(text_lines)[:25000]

    if not combined.strip():
        combined = f"※ リアルタイムデータの取得に失敗しました。Claudeの学習データに基づいて「{keyword}」に関するX/Twitterトレンドを分析してください。"

    prompt = f"""以下は「{keyword}」に関するX(Twitter)のトレンド・投稿の検索結果です。

{combined}

以下のJSON形式で分析してください（コードブロック不要、純粋なJSONのみ）:
{{
  "summary": "全体サマリー（3〜5文）",
  "trending_topics": [
    {{"rank": 1, "topic": "話題のトピック", "tweet_count": "ツイート数目安",
      "sentiment": "ポジティブ/ネガティブ/中立", "description": "内容傾向"}}
  ],
  "influential_accounts": [
    {{"name": "アカウント名", "type": "受験生/塾/大学/メディア", "influence": "影響力の説明"}}
  ],
  "viral_patterns": ["バズりやすいパターン1", "パターン2"],
  "negative_themes": ["ネガティブな話題1（注意すべき）", "話題2"],
  "hashtags": ["#ハッシュタグ1", "#ハッシュタグ2"],
  "insights": ["マーケティング示唆1", "示唆2"],
  "data_quality": "データ品質コメント"
}}"""

    try:
        result = _extract_json_via_router("xtrends", prompt, max_tokens=3000)
    except Exception as e:
        result = {"error": str(e)}

    _print_xtrends(result)
    return result


def _print_xtrends(data: dict):
    if data.get("error"):
        console.print(f"[red]エラー: {data['error']}[/red]")
        return
    if data.get("summary"):
        console.print(Panel(data["summary"], title="X/Twitter サマリー", border_style="blue"))
    topics = data.get("trending_topics", [])
    if topics:
        tbl = Table("順位", "トピック", "ツイート数", "感情", "内容傾向", show_header=True, header_style="bold cyan")
        for tp in topics[:10]:
            tbl.add_row(str(tp.get("rank","")), tp.get("topic","")[:35],
                        tp.get("tweet_count",""), tp.get("sentiment",""), tp.get("description","")[:35])
        console.print(tbl)
    for ins in data.get("insights", []):
        console.print(f"  [dim]→ {ins}[/dim]")


# ─────────────────────────────────────────
# Instagram 分析
# ─────────────────────────────────────────
def run_instagram_analysis(client, keyword: str) -> dict:
    console.print(Rule(f"[bold]📸 Instagram 分析[/bold] — {keyword}"))

    raw_items: list[dict] = []
    with Progress(console=console, transient=True) as prog:
        t = prog.add_task("DuckDuckGo で Instagram 情報取得中…", total=None)
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            ddgs = DDGS()
            for q in INSTAGRAM_QUERIES:
                for r in ddgs.text(q, max_results=6):
                    raw_items.append({"title": r.get("title",""), "url": r.get("href",""),
                                      "description": r.get("body","")})
                time.sleep(0.1)
        except Exception as e:
            raw_items.append({"error": str(e)})
        prog.update(t, description=f"{len(raw_items)} 件取得")

    text_lines = []
    for i, item in enumerate(raw_items[:40], 1):
        if item.get("error"):
            continue
        text_lines.append(
            f"[{i}] {item.get('title','')}\n  URL: {item.get('url','')}\n  {item.get('description','')[:250]}"
        )
    combined = "\n\n".join(text_lines)[:25000]

    prompt = f"""以下は「{keyword}」に関するInstagramの投稿・ハッシュタグの検索結果です。

{combined}

以下のJSON形式で分析してください（コードブロック不要）:
{{
  "summary": "全体サマリー（3〜5文）",
  "trending_hashtags": [
    {{"rank": 1, "hashtag": "#ハッシュタグ", "post_count": "投稿数目安", "description": "内容傾向"}}
  ],
  "popular_accounts": [
    {{"name": "アカウント名", "type": "受験生/塾/大学/インフルエンサー",
      "style": "投稿スタイル", "followers": "フォロワー数目安"}}
  ],
  "visual_formats": ["人気ビジュアルフォーマット1（例：勉強垢）", "フォーマット2"],
  "caption_patterns": ["キャプションパターン1", "パターン2"],
  "content_gaps": ["まだ少ないテーマ1", "テーマ2"],
  "insights": ["マーケティング示唆1", "示唆2"],
  "data_quality": "データ品質コメント"
}}"""

    try:
        result = _extract_json_via_router("instagram", prompt, max_tokens=1200)
    except Exception as e:
        result = {"error": str(e)}

    _print_instagram(result)
    return result


def _print_instagram(data: dict):
    if data.get("error"):
        console.print(f"[red]エラー: {data['error']}[/red]")
        return
    if data.get("summary"):
        console.print(Panel(data["summary"], title="Instagram サマリー", border_style="blue"))
    tags = data.get("trending_hashtags", [])
    if tags:
        tbl = Table("順位", "ハッシュタグ", "投稿数", "内容傾向", show_header=True, header_style="bold cyan")
        for tag in tags[:10]:
            tbl.add_row(str(tag.get("rank","")), tag.get("hashtag",""),
                        tag.get("post_count",""), tag.get("description","")[:40])
        console.print(tbl)
    for ins in data.get("insights", []):
        console.print(f"  [dim]→ {ins}[/dim]")


# ─────────────────────────────────────────
# Threads トレンド分析（v3.0 新規）
# ─────────────────────────────────────────
def run_threads_analysis(client: anthropic.Anthropic, keyword: str) -> dict:
    """Threads投稿トレンド分析（DuckDuckGo経由）"""
    console.print(Rule("[bold cyan]Threads トレンド分析[/bold cyan]"))

    raw = collect_search_data(THREADS_QUERIES, keyword, "Threads")
    news = news_search(f"{keyword} Threads 受験生 話題 2025")
    raw += news

    console.print(f"  収集件数: [bold]{len(raw)}[/bold] 件")

    system = """あなたは総合型選抜（AO入試）専門のSNSトレンド分析エージェントです。
Threads（Meta社のSNS）における総合型選抜関連のトレンドを分析します。
必ず日本語で回答してください。JSONのみを出力してください。"""

    user = f"""以下はWeb検索で収集した「{keyword}」に関するThreads上のトレンド情報です。

=== 収集データ ===
{format_search_results(raw)}

以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "popular_topics": [
    {{
      "topic": "話題のトピック",
      "engagement_level": "高/中/低",
      "content_type": "体験談/解説/Q&A/まとめ/その他",
      "why_popular": "なぜ刺さるか（1〜2文）",
      "examples": ["投稿例1", "投稿例2"]
    }}
  ],
  "top_accounts": [
    {{
      "name": "アカウント名",
      "estimated_followers": "推定フォロワー数",
      "content_style": "投稿スタイルの特徴",
      "ao_focus": "高/中/低"
    }}
  ],
  "trending_hashtags": ["ハッシュタグ1", "ハッシュタグ2", "ハッシュタグ3"],
  "content_gaps": ["競合が手薄なテーマ1", "空白テーマ2", "空白テーマ3"],
  "best_format": "Threadsで最も効果的な投稿形式",
  "posting_tips": ["投稿のコツ1", "コツ2", "コツ3"],
  "summary": "Threadsトレンドの全体傾向を3文で"
}}"""

    with console.status("[cyan]Claude が Threads データを分析中...[/cyan]"):
        raw_response = analyze_with_claude(client, system, user, model=MODEL_EXTRACT)

    result = _parse_json_robust(raw_response)
    _print_threads_result(result)
    return result


def _print_threads_result(data: dict):
    topics = data.get("popular_topics", [])
    if topics:
        t = Table(title="Threads 人気トピック", box=box.ROUNDED, show_lines=True)
        t.add_column("トピック", style="bold cyan", max_width=22)
        t.add_column("エンゲージメント", justify="center")
        t.add_column("種別", style="dim")
        t.add_column("なぜ刺さる？", max_width=35)
        for tp in topics:
            eng = tp.get("engagement_level", "")
            color = "green" if eng == "高" else "yellow" if eng == "中" else "dim"
            t.add_row(
                tp.get("topic", ""),
                f"[{color}]{eng}[/{color}]",
                tp.get("content_type", ""),
                tp.get("why_popular", ""),
            )
        console.print(t)

    if data.get("trending_hashtags"):
        console.print(Panel(
            "  " + "  ".join(f"[cyan]{h}[/cyan]"
                              for h in data["trending_hashtags"]),
            title="[cyan]トレンドハッシュタグ[/cyan]",
            border_style="cyan",
        ))

    if data.get("content_gaps"):
        console.print(Panel(
            "\n".join(f"  • {g}" for g in data["content_gaps"]),
            title="[green]Threadsの空白テーマ（チャンス）[/green]",
            border_style="green",
        ))


# ─────────────────────────────────────────
# YouTube 動画トレンド分析（v3.0 新規）
# ─────────────────────────────────────────
def _fetch_youtube_api(keyword: str, max_results: int = 20) -> list[dict]:
    """YouTube Data API v3 で動画を取得（APIキーがある場合のみ）"""
    if not YOUTUBE_API_KEY:
        return []
    try:
        params = urllib.parse.urlencode({
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "maxResults": max_results,
            "regionCode": "JP",
            "relevanceLanguage": "ja",
            "order": "viewCount",
            "key": YOUTUBE_API_KEY,
        })
        req = urllib.request.Request(
            f"https://www.googleapis.com/youtube/v3/search?{params}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        videos = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            videos.append({
                "title":        snippet.get("title", ""),
                "channel":      snippet.get("channelTitle", ""),
                "published":    snippet.get("publishedAt", ""),
                "description":  snippet.get("description", "")[:200],
                "video_id":     video_id,
                "url":          f"https://www.youtube.com/watch?v={video_id}",
            })
        console.print(f"  [green]✓[/green] YouTube API: {len(videos)} 件取得")
        return videos
    except Exception as e:
        if _VERBOSE:
            console.print(f"  [yellow]YouTube API失敗: {e}[/yellow]")
        return []


def run_youtube_analysis(client: anthropic.Anthropic, keyword: str) -> dict:
    """YouTube 動画トレンド分析"""
    console.print(Rule("[bold red]YouTube 動画トレンド分析[/bold red]"))

    api_videos = _fetch_youtube_api(f"{keyword} 総合型選抜")
    if not api_videos:
        console.print("  [dim]YouTube API未設定 — DuckDuckGo で補完します[/dim]")

    raw = collect_search_data(YOUTUBE_QUERIES, keyword, "YouTube")
    news = news_search(f"YouTube {keyword} 受験生 人気 2025")
    raw += news

    console.print(f"  収集件数: [bold]{len(raw)}[/bold] 件"
                  + (f" + API {len(api_videos)} 件" if api_videos else ""))

    api_text = ""
    if api_videos:
        api_text = "\n=== YouTube API 取得動画 ===\n"
        for i, v in enumerate(api_videos[:10], 1):
            api_text += (f"[{i}] {v['title']}\n"
                         f"    チャンネル: {v['channel']}  公開: {v['published']}\n"
                         f"    URL: {v['url']}\n\n")

    system = """あなたは総合型選抜（AO入試）専門の動画コンテンツ戦略家です。
YouTube上の総合型選抜関連コンテンツのトレンドと競合状況を分析します。
必ず日本語で回答してください。JSONのみを出力してください。"""

    user = f"""以下はYouTubeの「{keyword}」関連動画の情報です。

{api_text}
=== Web検索収集データ ===
{format_search_results(raw)}

以下のJSON形式で回答してください（コードブロックなし、純粋なJSONのみ）:

{{
  "top_channels": [
    {{
      "name": "チャンネル名",
      "estimated_subscribers": "推定登録者数",
      "content_style": "コンテンツの特徴",
      "strong_points": ["強み1", "強み2"],
      "ao_focus": "高/中/低",
      "upload_frequency": "投稿頻度"
    }}
  ],
  "popular_video_formats": [
    {{
      "format": "動画フォーマット名",
      "avg_views": "推定平均再生数",
      "why_works": "なぜ伸びるか",
      "examples": ["タイトル例1", "タイトル例2"]
    }}
  ],
  "trending_topics": ["トレンドトピック1", "トピック2", "トピック3"],
  "content_gaps": ["競合が手薄な動画テーマ1", "空白テーマ2", "空白テーマ3"],
  "title_patterns": ["タイトルの法則1", "法則2", "法則3"],
  "best_upload_timing": "最適投稿曜日・時間帯",
  "shorts_vs_long": "Shorts vs 長尺の効果比較",
  "summary": "YouTubeトレンドの全体傾向を3文で"
}}"""

    with console.status("[red]Claude が YouTube データを分析中...[/red]"):
        raw_response = analyze_with_claude(client, system, user)

    result = _parse_json_robust(raw_response)
    result["_api_videos"] = api_videos
    _print_youtube_result(result)
    return result


def _print_youtube_result(data: dict):
    channels = data.get("top_channels", [])
    if channels:
        t = Table(title="YouTube 競合チャンネル", box=box.ROUNDED, show_lines=True)
        t.add_column("チャンネル", style="bold red", max_width=20)
        t.add_column("登録者", justify="right")
        t.add_column("スタイル", max_width=28)
        t.add_column("AO注力", justify="center")
        t.add_column("投稿頻度", style="dim")
        for ch in channels:
            ao = ch.get("ao_focus", "")
            color = "green" if ao == "高" else "yellow" if ao == "中" else "dim"
            t.add_row(
                ch.get("name", ""),
                ch.get("estimated_subscribers", ""),
                ch.get("content_style", ""),
                f"[{color}]{ao}[/{color}]",
                ch.get("upload_frequency", ""),
            )
        console.print(t)

    if data.get("content_gaps"):
        console.print(Panel(
            "\n".join(f"  • {g}" for g in data["content_gaps"]),
            title="[green]YouTube 空白テーマ（チャンス）[/green]",
            border_style="green",
        ))

    if data.get("shorts_vs_long"):
        console.print(Panel(
            f"  {data['shorts_vs_long']}",
            title="[yellow]Shorts vs 長尺[/yellow]",
            border_style="yellow",
        ))


# ─────────────────────────────────────────
# 結果の保存
# ─────────────────────────────────────────
def save_results(results: dict, keyword: str):
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_kw = keyword.replace(" ", "_").replace("/", "-")

    # JSON
    json_path = OUTPUT_DIR / f"analysis_{safe_kw}_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    console.print(f"\n[dim]JSON保存: {json_path}[/dim]")

    # HTMLレポート生成
    html_path = OUTPUT_DIR / f"report_{safe_kw}_{ts}.html"
    _save_html_report(results, keyword, html_path)
    console.print(f"[dim]HTMLレポート: {html_path}[/dim]")

    return json_path, html_path


def _save_html_report(results: dict, keyword: str, path: Path):
    proposals = results.get("proposals", {}).get("proposals", [])
    competitor_accounts = results.get("competitor", {}).get("top_accounts", [])
    buzz_patterns = results.get("buzz", {}).get("buzz_patterns", [])
    hot_keywords = results.get("trends", {}).get("hot_keywords", [])
    quick_wins = results.get("proposals", {}).get("quick_wins", [])
    kpi = results.get("proposals", {}).get("kpi_targets", {})

    ts = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    def rows(items, cols):
        return "".join("<tr>" + "".join(f"<td>{r.get(c,'')}</td>" for c in cols) + "</tr>" for r in items)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>総合型選抜 SNSリサーチレポート — {keyword}</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#0f0f1a;color:#e2e8f0;margin:0;padding:0}}
  header{{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:32px 40px;border-bottom:1px solid #2d2d4e}}
  h1{{color:#6c63ff;margin:0 0 4px;font-size:1.6rem}}
  .meta{{color:#94a3b8;font-size:.85rem}}
  main{{padding:32px 40px;max-width:1200px;margin:0 auto}}
  section{{margin-bottom:40px}}
  h2{{color:#6c63ff;font-size:1.1rem;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid #2d2d4e}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  th{{background:#1a1a2e;color:#94a3b8;padding:10px 12px;text-align:left;border-bottom:2px solid #2d2d4e}}
  td{{padding:10px 12px;border-bottom:1px solid #2d2d4e;vertical-align:top}}
  tr:hover td{{background:rgba(255,255,255,.03)}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.7rem;font-weight:700}}
  .s{{background:#ff658430;color:#ff6584}}.a{{background:#f59e0b30;color:#f59e0b}}.b{{background:#43e97b30;color:#43e97b}}
  .panel{{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:10px;padding:20px;margin-bottom:16px}}
  .panel h3{{color:#94a3b8;font-size:.85rem;margin-bottom:8px}}
  ul{{padding-left:20px;line-height:2;color:#94a3b8}}
  .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}}
  .kpi-card{{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:10px;padding:20px;text-align:center}}
  .kpi-val{{font-size:1.4rem;font-weight:800;color:#6c63ff}}
  .kpi-label{{color:#94a3b8;font-size:.8rem;margin-top:4px}}
  @media(max-width:768px){{main{{padding:16px}}.grid{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<header>
  <h1>総合型選抜 SNSリサーチレポート</h1>
  <div class="meta">キーワード: <strong>{keyword}</strong> &nbsp;|&nbsp; 生成日時: {ts}</div>
</header>
<main>

<section>
  <h2>KPI目標</h2>
  <div class="grid">
    {''.join(f'<div class="kpi-card"><div class="kpi-val">{v}</div><div class="kpi-label">{k}</div></div>' for k,v in kpi.items())}
  </div>
</section>

<section>
  <h2>競合アカウント</h2>
  <table>
    <thead><tr><th>名前</th><th>媒体</th><th>フォロワー</th><th>スタイル</th><th>強み</th><th>AO注力</th></tr></thead>
    <tbody>
      {''.join(f'<tr><td>{a.get("name","")}</td><td>{a.get("platform","")}</td><td>{a.get("estimated_followers","")}</td><td>{a.get("content_style","")}</td><td>{"<br>".join(a.get("strong_points",[]))}</td><td>{a.get("ao_focus","")}</td></tr>' for a in competitor_accounts)}
    </tbody>
  </table>
</section>

<section>
  <h2>バズコンテンツパターン</h2>
  <table>
    <thead><tr><th>パターン</th><th>説明</th><th>なぜ刺さる</th><th>媒体</th><th>再現性</th></tr></thead>
    <tbody>
      {''.join(f'<tr><td>{p.get("pattern_name","")}</td><td>{p.get("description","")}</td><td>{p.get("why_it_works","")}</td><td>{p.get("platform","")}</td><td>{p.get("replicability","")}</td></tr>' for p in buzz_patterns)}
    </tbody>
  </table>
</section>

<section>
  <h2>トレンドキーワード</h2>
  <table>
    <thead><tr><th>キーワード</th><th>トレンド</th><th>検索意図</th><th>コンテンツ機会</th><th>優先度</th></tr></thead>
    <tbody>
      {''.join(f'<tr><td>{k.get("keyword","")}</td><td>{k.get("trend","")}</td><td>{k.get("search_intent","")}</td><td>{k.get("content_opportunity","")}</td><td>{k.get("priority","")}</td></tr>' for k in hot_keywords)}
    </tbody>
  </table>
</section>

<section>
  <h2>コンテンツ提案</h2>
  <table>
    <thead><tr><th>No.</th><th>タイトル</th><th>媒体</th><th>形式</th><th>フック</th><th>優先度</th><th>タイミング</th></tr></thead>
    <tbody>
      {''.join(f'<tr><td>{p.get("id","")}</td><td>{p.get("title","")}</td><td>{p.get("platform","")}</td><td>{p.get("format","")}</td><td>{p.get("hook","")}</td><td><span class="badge {p.get("priority","").lower()}">{p.get("priority","")}</span></td><td>{p.get("best_timing","")}</td></tr>' for p in proposals)}
    </tbody>
  </table>
</section>

<section>
  <h2>今すぐできる施策（Quick Wins）</h2>
  <ul>{''.join(f"<li>{w}</li>" for w in quick_wins)}</ul>
</section>

</main>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ─────────────────────────────────────────
# Notion 保存
# ─────────────────────────────────────────
# 情報収集エージェントページID（サブページの親）
NOTION_PARENT_PAGE_ID = "3302c0f3-8d2d-81aa-b102-cca314b0ee0a"
# universityモードの大学個別ページは🎓大学情報配下に保存（Notion秘書が国公立/私立/地方を仕分ける）
NOTION_UNIVERSITY_PARENT_PAGE_ID = "3302c0f3-8d2d-81b6-a7af-cdc2fd1d831b"
NOTION_API_URL = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"

# 大学群 / サブグループ マッピング（notion_secretary.md の6分類19サブグループ準拠）
UNIVERSITY_GROUP_MAP: dict[tuple[str, str], list[str]] = {
    ("国公立系", "旧帝大"):        ["東京大学", "京都大学", "大阪大学", "名古屋大学", "東北大学", "九州大学", "北海道大学"],
    ("国公立系", "金岡千広"):      ["金沢大学", "岡山大学", "千葉大学", "広島大学"],
    ("国公立系", "電農名繊"):      ["電気通信大学", "東京農工大学", "名古屋工業大学", "京都工芸繊維大学"],
    ("最難関私大系", "早慶上理"):  ["早稲田大学", "慶應義塾大学", "上智大学", "東京理科大学"],
    ("首都圏私大系", "GMARCH"):    ["学習院大学", "明治大学", "青山学院大学", "立教大学", "中央大学", "法政大学"],
    ("首都圏私大系", "成成明学獨國武"): ["成蹊大学", "成城大学", "明治学院大学", "獨協大学", "國學院大學", "武蔵大学"],
    ("首都圏私大系", "日東駒専"):  ["日本大学", "東洋大学", "駒澤大学", "専修大学"],
    ("首都圏私大系", "大東亜帝国"): ["大東文化大学", "東海大学", "亜細亜大学", "帝京大学", "国士舘大学"],
    ("関西私大系", "関関同立"):    ["関西大学", "関西学院大学", "同志社大学", "立命館大学"],
    ("関西私大系", "産近甲龍"):    ["京都産業大学", "近畿大学", "甲南大学", "龍谷大学"],
    ("関西私大系", "摂神追桃"):    ["摂南大学", "神戸学院大学", "追手門学院大学", "桃山学院大学"],
    ("東海圏私大系", "愛愛名中"):  ["愛知大学", "愛知学院大学", "名城大学", "中京大学"],
    ("東海圏私大系", "名名中日"):  ["名古屋学院大学", "名古屋外国語大学", "中部大学", "日本福祉大学"],
}


def _classify_university(uni_name: str) -> tuple[str, str]:
    """大学名から (大学群, サブグループ) を返す。未分類は ('地域別', 'その他')。"""
    for (group, subgroup), unis in UNIVERSITY_GROUP_MAP.items():
        for u in unis:
            if u in uni_name or uni_name in u:
                return group, subgroup
    return "地域別", "その他"


def _notion_paragraph(text: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _notion_bullet(text: str) -> dict:
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _notion_h2(text: str) -> dict:
    return {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _notion_h3_block(text: str) -> dict:
    return {"type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _build_yearly_data_blocks_for_faculty(dept_entries: list, ts: str, keyword: str) -> list:
    """学部配下の全学科の年度別データをNotionブロックに変換"""
    blocks = [_notion_paragraph(f"収集日時: {ts}  |  キーワード: {keyword}")]

    for u in dept_entries:
        dept_name = u.get("department", "") or ""
        if dept_name and dept_name not in ("情報なし", ""):
            blocks.append(_notion_h2(dept_name))

        ratio_h = u.get("ratio_history", {})
        quota_h = u.get("quota_history", {})
        appli_h = u.get("applicants_history", {})
        methods = " / ".join(str(m) for m in (u.get("selection_methods") or []) if m) or "情報なし"
        gpa     = u.get("gpa_requirement", "")

        for year in ["2026", "2025", "2024"]:
            ratio      = ratio_h.get(year, "要確認")
            quota      = quota_h.get(year, "要確認")
            applicants = appli_h.get(year, "要確認")
            if all(v in ("要確認", "情報なし") for v in [ratio, quota, applicants]):
                continue
            blocks.append(_notion_h3_block(f"{year}年度"))
            blocks.append(_notion_bullet(f"募集人員: {quota}"))
            blocks.append(_notion_bullet(f"倍率: {ratio}"))
            blocks.append(_notion_bullet(f"志願者数: {applicants}"))
            if year == "2026":
                blocks.append(_notion_bullet(f"選考方法: {methods}"))
                if gpa and gpa not in ("情報なし", ""):
                    blocks.append(_notion_bullet(f"評定条件: {gpa}"))

        blocks.append(_notion_paragraph(""))  # 区切り

    return blocks[:100]


def _build_notion_blocks_competitor(data: dict) -> list:
    blocks = []
    if data.get("market_overview"):
        blocks.append(_notion_paragraph(f"【市場概況】\n{data['market_overview']}"))
    accounts = data.get("top_accounts", [])
    if accounts:
        blocks.append(_notion_paragraph("【競合アカウント】"))
        for a in accounts:
            text = (f"・{a.get('name','')} [{a.get('platform','')}] "
                    f"フォロワー:{a.get('estimated_followers','')} AO注力:{a.get('ao_focus','')}\n"
                    f"  スタイル: {a.get('content_style','')}\n"
                    f"  強み: {', '.join(a.get('strong_points',[]))}")
            blocks.append(_notion_bullet(text))
    gaps = data.get("competitive_gaps", [])
    if gaps:
        blocks.append(_notion_paragraph("【競合の手薄エリア（チャンス）】"))
        for g in gaps:
            blocks.append(_notion_bullet(g))
    insights = data.get("insights", [])
    if insights:
        blocks.append(_notion_paragraph("【主要インサイト】"))
        for ins in insights:
            blocks.append(_notion_bullet(ins))
    bm = data.get("benchmark_metrics", {})
    if bm:
        text = (f"【ベンチマーク】\n"
                f"平均エンゲージメント率: {bm.get('avg_engagement_rate','')}\n"
                f"主要コンテンツタイプ: {', '.join(bm.get('top_content_types',[]))}\n"
                f"投稿頻度: {bm.get('posting_frequency','')}")
        blocks.append(_notion_paragraph(text))
    return blocks


def _build_notion_blocks_buzz(data: dict) -> list:
    blocks = []
    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))
    patterns = data.get("buzz_patterns", [])
    if patterns:
        blocks.append(_notion_paragraph("【バズコンテンツパターン】"))
        for p in patterns:
            text = (f"・{p.get('pattern_name','')} [{p.get('platform','')}] 再現性:{p.get('replicability','')}\n"
                    f"  {p.get('description','')}\n"
                    f"  なぜ刺さる: {p.get('why_it_works','')}")
            blocks.append(_notion_bullet(text))
    triggers = data.get("emotional_triggers", [])
    if triggers:
        blocks.append(_notion_paragraph(f"【感情的トリガー】\n{' / '.join(triggers)}"))
    fr = data.get("format_ranking", [])
    if fr:
        text = "【フォーマットランキング】\n" + "\n".join(
            f"{f.get('rank')}位: {f.get('format','')} — {f.get('reason','')}" for f in fr)
        blocks.append(_notion_paragraph(text))
    ti = data.get("timing_insights", {})
    if ti:
        text = (f"【投稿タイミング】\n"
                f"最適曜日: {', '.join(ti.get('best_days',[]))}\n"
                f"最適時間帯: {ti.get('best_times','')}\n"
                f"シーズナルピーク: {', '.join(ti.get('seasonal_peaks',[]))}")
        blocks.append(_notion_paragraph(text))
    anti = data.get("anti_patterns", [])
    if anti:
        blocks.append(_notion_paragraph("【避けるべきパターン】"))
        for a in anti:
            blocks.append(_notion_bullet(a))
    return blocks


def _build_notion_blocks_trends(data: dict) -> list:
    blocks = []
    if data.get("trend_summary"):
        blocks.append(_notion_paragraph(f"【トレンドサマリー】\n{data['trend_summary']}"))
    keywords = data.get("hot_keywords", [])
    if keywords:
        blocks.append(_notion_paragraph("【ホットキーワード】"))
        for kw in keywords:
            text = (f"・{kw.get('keyword','')} [{kw.get('trend','')}] "
                    f"優先度:{kw.get('priority','')} 対象:{kw.get('target_audience','')}\n"
                    f"  検索意図: {kw.get('search_intent','')}\n"
                    f"  コンテンツ機会: {kw.get('content_opportunity','')}")
            blocks.append(_notion_bullet(text))
    emerging = data.get("emerging_topics", [])
    if emerging:
        blocks.append(_notion_paragraph("【新興トピック】"))
        for e in emerging:
            blocks.append(_notion_bullet(e))
    declining = data.get("declining_topics", [])
    if declining:
        blocks.append(_notion_paragraph("【衰退トピック】"))
        for d in declining:
            blocks.append(_notion_bullet(d))
    hashtags = data.get("hashtag_suggestions", {})
    if hashtags:
        text = "【推奨ハッシュタグ】\n" + "\n".join(
            f"{platform}: {' '.join(tags)}" for platform, tags in hashtags.items())
        blocks.append(_notion_paragraph(text))
    cal = data.get("seasonal_calendar", [])
    if cal:
        blocks.append(_notion_paragraph("【月別キーワードカレンダー】"))
        for month in cal:
            text = (f"・{month.get('month','')} — "
                    f"{' / '.join(month.get('keywords',[]))} → {month.get('action','')}")
            blocks.append(_notion_bullet(text))
    return blocks


def _build_notion_blocks_googletrends(data: dict) -> list:
    blocks = []

    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks

    kws = data.get("keywords_analyzed", [])
    timeframe = data.get("timeframe", "")
    geo = data.get("geo", "")
    if kws:
        blocks.append(_notion_paragraph(
            f"【取得条件】\n"
            f"対象キーワード: {', '.join(kws)}\n"
            f"期間: {timeframe}  /  地域: {geo}"
        ))

    # 平均スコア＆ピーク
    averages = data.get("averages", {})
    peaks    = data.get("peaks", {})
    if averages:
        lines = ["キーワード / 平均スコア / ピーク日"]
        for kw, avg in sorted(averages.items(), key=lambda x: -x[1]):
            lines.append(f"・{kw}: {avg:.1f}pt  ピーク: {peaks.get(kw, '—')}")
        blocks.append(_notion_paragraph("【平均トレンドスコア（直近3ヶ月・日本）】\n" + "\n".join(lines)))

    # 週次時系列（直近12週）
    iot = data.get("interest_over_time", [])
    if iot:
        blocks.append(_notion_paragraph("【週次トレンド推移（直近12週）】"))
        for entry in iot:
            date = entry.get("date", "")
            scores = "  ".join(f"{k}:{v:.0f}" for k, v in entry.items() if k != "date")
            blocks.append(_notion_bullet(f"{date}  |  {scores}"))

    # 関連クエリ
    related = data.get("related_queries", {})
    if related:
        blocks.append(_notion_paragraph("【関連クエリ】"))
        for kw, entry in related.items():
            top_queries    = [str(r.get("query", "")) for r in entry.get("top", [])[:5]]
            rising_queries = [str(r.get("query", "")) for r in entry.get("rising", [])[:5]]
            text = f"「{kw}」\n"
            if top_queries:
                text += f"  TOP: {' / '.join(top_queries)}\n"
            if rising_queries:
                text += f"  急上昇: {' / '.join(rising_queries)}"
            blocks.append(_notion_bullet(text.strip()))

    # 関連トピック
    topics = data.get("related_topics", {})
    if topics:
        blocks.append(_notion_paragraph("【関連トピック】"))
        for kw, entry in topics.items():
            top_topics = [str(r.get("topic_title", "")) for r in entry.get("top", [])[:5]]
            if top_topics:
                blocks.append(_notion_bullet(f"「{kw}」TOP: {' / '.join(top_topics)}"))

    return blocks


def _h2(text: str) -> dict:
    return {"type": "heading_2", "heading_2": {
        "rich_text": [{"type": "text", "text": {"content": text[:200]}}]
    }}


def _h3(text: str) -> dict:
    return {"type": "heading_3", "heading_3": {
        "rich_text": [{"type": "text", "text": {"content": text[:200]}}]
    }}


def _divider() -> dict:
    return {"type": "divider", "divider": {}}


def _build_notion_blocks_single_university(u: dict, ts: str, keyword: str) -> list:
    """大学1件分の詳細ページブロックを構築する。"""
    blocks = []
    name       = u.get("university", "不明")
    faculty    = u.get("faculty", "")
    department = u.get("department", "")

    # メタブロック
    blocks.append(_notion_paragraph(
        f"📅 取得日時: {ts}　🏷️ キーワード: {keyword}\n"
        f"🏫 {name}　{faculty}　{department}".rstrip()
    ))
    blocks.append(_divider())

    # ── 学科・コース情報
    dept_detail = u.get("department_detail", "")
    if department and department != "情報なし":
        blocks.append(_h2("🏛️ 学部・学科情報"))
        blocks.append(_notion_paragraph(
            f"学部: {faculty or '情報なし'}\n"
            f"学科・専攻: {department}"
        ))
        if dept_detail and dept_detail != "情報なし":
            blocks.append(_notion_paragraph(dept_detail))
        blocks.append(_divider())

    # ── 募集要項・選考方法
    blocks.append(_h2("📋 総合型選抜 基本情報"))
    basics = (
        f"定員: {u.get('quota','情報なし')}\n"
        f"評定条件: {u.get('gpa_requirement','情報なし')}\n"
        f"選考方法: {' / '.join(str(m) for m in (u.get('selection_methods') or []) if m) or '情報なし'}\n"
        f"難易度・倍率: {u.get('difficulty','情報なし')}"
    )
    blocks.append(_notion_paragraph(basics))
    if u.get("features") and u["features"] != "情報なし":
        blocks.append(_notion_bullet(f"特徴: {u['features']}"))
    blocks.append(_divider())

    # ── 出願資格・評定条件
    blocks.append(_h2("✅ 出願資格・評定条件"))
    if u.get("eligibility") and u["eligibility"] != "情報なし":
        blocks.append(_notion_paragraph(u["eligibility"]))
    else:
        blocks.append(_notion_paragraph("情報なし"))
    blocks.append(_divider())

    # ── 出願期間・選考日程
    blocks.append(_h2("📅 出願期間・選考日程"))
    schedule_text = (
        f"出願期間: {u.get('application_period','情報なし')}\n"
        f"選考日程: {u.get('selection_schedule','情報なし')}"
    )
    blocks.append(_notion_paragraph(schedule_text))
    blocks.append(_divider())

    # ── 選考内容詳細
    blocks.append(_h2("🔍 選考内容詳細"))
    sel_detail = u.get("selection_detail", "情報なし")
    blocks.append(_notion_paragraph(sel_detail if sel_detail and sel_detail != "情報なし" else "情報なし"))
    blocks.append(_divider())

    # ── 倍率・志願者数・合格者数（過去3年分）
    blocks.append(_h2("📊 倍率・志願者数・合格者数（過去3年）"))
    rh = u.get("ratio_history", {}) or u.get("competition_ratio_history", {})
    ah = u.get("applicants_history", {})
    ch = u.get("accepted_history", {}) or u.get("admitted_history", {})
    qh = u.get("quota_history", {})
    yearly_lines = []
    for yr in ["2026", "2025", "2024"]:
        yearly_lines.append(
            f"{yr}年度: 倍率={rh.get(yr,'要確認')} / 志願者={ah.get(yr,'要確認')} / "
            f"合格者={ch.get(yr,'要確認')} / 定員={qh.get(yr,'要確認')}"
        )
    blocks.append(_notion_paragraph("\n".join(yearly_lines)))
    blocks.append(_divider())

    # ── 教育方針・AP
    blocks.append(_h2("🎓 教育方針・アドミッションポリシー"))
    ep = u.get("education_policy")
    if isinstance(ep, str) and ep and ep not in ("情報なし", "不明"):
        blocks.append(_notion_paragraph(ep))
    ap_raw = u.get("admission_policy")
    ap_text = ap_raw.get("summary", "") if isinstance(ap_raw, dict) else (ap_raw or "")
    ap_keywords = ap_raw.get("keywords", []) if isinstance(ap_raw, dict) else u.get("ap_keywords", [])
    if ap_text and ap_text not in ("情報なし", "不明"):
        blocks.append(_h3("求める学生像（アドミッションポリシー）"))
        blocks.append(_notion_paragraph(ap_text))
        if ap_keywords:
            blocks.append(_notion_paragraph("キーワード: " + " / ".join(str(k) for k in ap_keywords)))
    has_ep = isinstance(ep, str) and ep and ep not in ("情報なし", "不明")
    has_ap = bool(ap_text) and ap_text not in ("情報なし", "不明")
    if not (has_ep or has_ap):
        blocks.append(_notion_paragraph("不明"))
    blocks.append(_divider())

    # ── 研究内容・カリキュラム
    blocks.append(_h2("📚 研究内容・カリキュラムの特徴"))
    blocks.append(_notion_paragraph(u.get("curriculum", "情報なし")))
    blocks.append(_divider())

    # ── 教授陣・研究室
    blocks.append(_h2("👨‍🏫 教授陣・研究室"))
    professors = u.get("professors", [])
    if professors:
        for p in professors:
            blocks.append(_notion_bullet(p))
    else:
        blocks.append(_notion_paragraph("情報なし"))
    blocks.append(_divider())

    # ── 卒業後の進路・就職
    blocks.append(_h2("💼 卒業後の進路・就職"))
    if u.get("career") and u["career"] != "情報なし":
        blocks.append(_notion_paragraph(u["career"]))
    examples = u.get("career_examples", [])
    if examples:
        blocks.append(_h3("就職先・進学先の例"))
        for ex in examples:
            blocks.append(_notion_bullet(ex))
    if not any([u.get("career", "情報なし") != "情報なし", examples]):
        blocks.append(_notion_paragraph("情報なし"))
    blocks.append(_divider())

    # ── 参照URL
    if u.get("url"):
        blocks.append(_notion_paragraph(f"🔗 参照URL: {u['url']}"))

    return blocks[:100]


def _build_notion_blocks_university(data: dict) -> list:
    """全大学のサマリーページ用ブロック（--notion オプション時の一覧ページ）。"""
    blocks = []

    if data.get("data_quality"):
        blocks.append(_notion_paragraph(f"【データ品質】{data['data_quality']}"))

    trends = data.get("overall_trends", [])
    if trends:
        blocks.append(_notion_paragraph("【全体傾向】\n" + "\n".join(f"・{t}" for t in trends)))

    ranking = data.get("selection_method_ranking", [])
    if ranking:
        lines = ["【選考方法 頻出ランキング】"]
        for r in ranking:
            lines.append(f"・{r.get('method','')}（{r.get('count','')}校）  {r.get('note','')}")
        blocks.append(_notion_paragraph("\n".join(lines)))

    insights = data.get("key_insights", [])
    if insights:
        blocks.append(_notion_paragraph("【受験生へのインサイト】"))
        for ins in insights:
            blocks.append(_notion_bullet(ins))

    universities = data.get("universities", [])
    if universities:
        blocks.append(_divider())
        blocks.append(_h2("🏫 大学別 総合型選抜情報"))
        for u in universities:
            if len(blocks) >= 92:
                blocks.append(_notion_paragraph("（ブロック数上限のため以降の大学は省略）"))
                break
            blocks.append(_h3(f"🎓 {u.get('university','')}　{u.get('faculty','')}"))
            blocks.append(_notion_paragraph(
                f"定員: {u.get('quota','情報なし')}　"
                f"出願期間: {u.get('application_period','情報なし')}\n"
                f"選考: {' / '.join(str(m) for m in (u.get('selection_methods') or []) if m)}　"
                f"難易度: {u.get('difficulty','情報なし')}"
            ))
            ap = u.get("admission_policy")
            if isinstance(ap, dict):
                ap = ap.get("summary", "") or ""
            if ap and ap not in ("情報なし", "不明"):
                blocks.append(_notion_bullet(f"AP: {str(ap)[:120]}"))
            career = u.get("career")
            if isinstance(career, str) and career and career not in ("情報なし", "不明"):
                blocks.append(_notion_bullet(f"進路: {career[:120]}"))
            if u.get("url"):
                blocks.append(_notion_bullet(f"URL: {u['url']}"))

    return blocks


def _build_notion_blocks_news(data: dict) -> list:
    blocks = []
    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks
    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))
    if data.get("trend_direction"):
        blocks.append(_notion_paragraph(f"【トレンド方向】 {data['trend_direction']}"))
    topics = data.get("hot_topics", [])
    if topics:
        blocks.append(_notion_paragraph("【注目トピック】"))
        for t in topics:
            blocks.append(_notion_bullet(
                f"[{t.get('rank','')}] {t.get('topic','')} / {t.get('source','')}\n  {t.get('description','')}  {t.get('url','')}"))
    themes = data.get("key_themes", [])
    if themes:
        blocks.append(_notion_paragraph("【主要テーマ】\n" + "\n".join(f"・{th}" for th in themes)))
    policy = data.get("policy_updates", [])
    if policy:
        blocks.append(_notion_paragraph("【制度・政策アップデート】\n" + "\n".join(f"・{p}" for p in policy)))
    for ins in data.get("insights", []):
        blocks.append(_notion_bullet(f"示唆: {ins}"))
    return blocks


def _build_notion_blocks_amazon(data: dict) -> list:
    blocks = []
    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks
    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))
    books = data.get("top_books", [])
    if books:
        blocks.append(_notion_paragraph("【人気参考書 TOP】"))
        for b in books:
            blocks.append(_notion_bullet(
                f"[{b.get('rank','')}] {b.get('title','')} / {b.get('author','')}\n"
                f"  評価: {b.get('rating','')}  レビュー数: {b.get('reviews','')}\n"
                f"  なぜ人気: {b.get('why_popular','')}\n  URL: {b.get('url','')}"))
    cats = data.get("popular_categories", [])
    if cats:
        blocks.append(_notion_paragraph("【人気カテゴリ】\n" + "\n".join(f"・{c}" for c in cats)))
    concerns = data.get("reader_concerns", [])
    if concerns:
        blocks.append(_notion_paragraph("【読者の悩み】\n" + "\n".join(f"・{c}" for c in concerns)))
    gaps = data.get("content_gaps", [])
    if gaps:
        blocks.append(_notion_paragraph("【コンテンツ空白】\n" + "\n".join(f"・{g}" for g in gaps)))
    for ins in data.get("insights", []):
        blocks.append(_notion_bullet(f"示唆: {ins}"))
    return blocks


def _build_notion_blocks_tiktok(data: dict) -> list:
    blocks = []
    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks
    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))
    tags = data.get("trending_hashtags", [])
    if tags:
        blocks.append(_notion_paragraph("【トレンドハッシュタグ】"))
        for tag in tags:
            blocks.append(_notion_bullet(
                f"[{tag.get('rank','')}] {tag.get('hashtag','')}  再生数: {tag.get('view_count','')}\n  {tag.get('description','')}"))
    creators = data.get("popular_creators", [])
    if creators:
        blocks.append(_notion_paragraph("【注目クリエイター】"))
        for c in creators:
            blocks.append(_notion_bullet(f"・{c.get('name','')}  スタイル: {c.get('style','')}  強み: {c.get('strength','')}"))
    formats = data.get("content_formats", [])
    if formats:
        blocks.append(_notion_paragraph("【人気フォーマット】\n" + "\n".join(f"・{f}" for f in formats)))
    hooks = data.get("hook_patterns", [])
    if hooks:
        blocks.append(_notion_paragraph("【冒頭フックパターン】\n" + "\n".join(f"・{h}" for h in hooks)))
    gaps = data.get("content_gaps", [])
    if gaps:
        blocks.append(_notion_paragraph("【コンテンツ空白】\n" + "\n".join(f"・{g}" for g in gaps)))
    for ins in data.get("insights", []):
        blocks.append(_notion_bullet(f"示唆: {ins}"))
    return blocks


def _build_notion_blocks_xtrends(data: dict) -> list:
    blocks = []
    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks
    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))
    topics = data.get("trending_topics", [])
    if topics:
        blocks.append(_notion_paragraph("【X トレンドトピック】"))
        for tp in topics:
            blocks.append(_notion_bullet(
                f"[{tp.get('rank','')}] {tp.get('topic','')}  ツイート数: {tp.get('tweet_count','')}\n"
                f"  感情: {tp.get('sentiment','')}  {tp.get('description','')}"))
    accounts = data.get("influential_accounts", [])
    if accounts:
        blocks.append(_notion_paragraph("【影響力アカウント】"))
        for a in accounts:
            blocks.append(_notion_bullet(f"・{a.get('name','')} ({a.get('type','')})  {a.get('influence','')}"))
    viral = data.get("viral_patterns", [])
    if viral:
        blocks.append(_notion_paragraph("【バズパターン】\n" + "\n".join(f"・{v}" for v in viral)))
    negative = data.get("negative_themes", [])
    if negative:
        blocks.append(_notion_paragraph("【注意すべきネガティブ話題】\n" + "\n".join(f"・{n}" for n in negative)))
    hashtags = data.get("hashtags", [])
    if hashtags:
        blocks.append(_notion_paragraph("【主要ハッシュタグ】\n" + "  ".join(hashtags)))
    for ins in data.get("insights", []):
        blocks.append(_notion_bullet(f"示唆: {ins}"))
    return blocks


def _build_notion_blocks_instagram(data: dict) -> list:
    blocks = []
    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks
    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))
    tags = data.get("trending_hashtags", [])
    if tags:
        blocks.append(_notion_paragraph("【トレンドハッシュタグ】"))
        for tag in tags:
            blocks.append(_notion_bullet(
                f"[{tag.get('rank','')}] {tag.get('hashtag','')}  投稿数: {tag.get('post_count','')}\n  {tag.get('description','')}"))
    accounts = data.get("popular_accounts", [])
    if accounts:
        blocks.append(_notion_paragraph("【人気アカウント】"))
        for a in accounts:
            blocks.append(_notion_bullet(
                f"・{a.get('name','')} ({a.get('type','')})  フォロワー: {a.get('followers','')}\n  スタイル: {a.get('style','')}"))
    formats = data.get("visual_formats", [])
    if formats:
        blocks.append(_notion_paragraph("【人気ビジュアルフォーマット】\n" + "\n".join(f"・{f}" for f in formats)))
    captions = data.get("caption_patterns", [])
    if captions:
        blocks.append(_notion_paragraph("【キャプションパターン】\n" + "\n".join(f"・{c}" for c in captions)))
    gaps = data.get("content_gaps", [])
    if gaps:
        blocks.append(_notion_paragraph("【コンテンツ空白】\n" + "\n".join(f"・{g}" for g in gaps)))
    for ins in data.get("insights", []):
        blocks.append(_notion_bullet(f"示唆: {ins}"))
    return blocks


def _build_notion_blocks_note(data: dict) -> list:
    blocks = []

    if data.get("error"):
        blocks.append(_notion_paragraph(f"【エラー】\n{data['error']}"))
        return blocks

    if data.get("summary"):
        blocks.append(_notion_paragraph(f"【サマリー】\n{data['summary']}"))

    top = data.get("top_articles", [])
    if top:
        blocks.append(_notion_paragraph("【note 人気記事 TOP】"))
        for a in top:
            likes = str(a.get("likes", "?")) if a.get("likes") is not None else "?"
            text = (
                f"[{a.get('rank','')}] {a.get('title','')}\n"
                f"  著者: {a.get('author','')}  スキ: {likes}  種別: {a.get('content_type','')}\n"
                f"  なぜ人気: {a.get('why_popular','')}\n"
                f"  URL: {a.get('url','')}"
            )
            blocks.append(_notion_bullet(text))

    patterns = data.get("popular_patterns", [])
    if patterns:
        blocks.append(_notion_paragraph("【人気コンテンツパターン】"))
        for p in patterns:
            text = (
                f"・{p.get('pattern','')} 再現性:{p.get('replicability','')}\n"
                f"  {p.get('description','')}\n"
                f"  なぜ刺さる: {p.get('why_effective','')}"
            )
            blocks.append(_notion_bullet(text))

    formulas = data.get("title_formulas", [])
    if formulas:
        blocks.append(_notion_paragraph("【タイトルの法則】\n" + "\n".join(f"{i+1}. {f}" for i, f in enumerate(formulas))))

    gaps = data.get("content_gaps", [])
    if gaps:
        blocks.append(_notion_paragraph("【競合の空白テーマ（チャンス）】"))
        for g in gaps:
            blocks.append(_notion_bullet(g))

    topics = data.get("recommended_topics", [])
    if topics:
        blocks.append(_notion_paragraph("【推奨トピック】"))
        for tp in topics:
            text = (
                f"[{tp.get('priority','')}] {tp.get('topic','')}\n"
                f"  タイトル案: {tp.get('title_draft','')}\n"
                f"  理由: {tp.get('reason','')}"
            )
            blocks.append(_notion_bullet(text))

    authors = data.get("top_authors", [])
    if authors:
        blocks.append(_notion_paragraph("【注目著者】"))
        for a in authors:
            blocks.append(_notion_bullet(f"・{a.get('name','')}  スタイル: {a.get('style','')}  強み: {a.get('strength','')}"))

    return blocks


def _build_notion_blocks_proposals(data: dict) -> list:
    blocks = []
    kpi = data.get("kpi_targets", {})
    if kpi:
        text = "【KPI目標】\n" + "\n".join(f"{k}: {v}" for k, v in kpi.items())
        blocks.append(_notion_paragraph(text))
    qw = data.get("quick_wins", [])
    if qw:
        blocks.append(_notion_paragraph("【今すぐできる施策（Quick Wins）】"))
        for w in qw:
            blocks.append(_notion_bullet(w))
    proposals = data.get("proposals", [])
    if proposals:
        blocks.append(_notion_paragraph("【コンテンツ提案】"))
        for p in proposals:
            text = (f"[{p.get('priority','')}] {p.get('title','')}\n"
                    f"  媒体: {p.get('platform','')} / {p.get('format','')}\n"
                    f"  フック: {p.get('hook','')[:120]}\n"
                    f"  タイミング: {p.get('best_timing','')} / 制作工数: {p.get('production_effort','')}\n"
                    f"  なぜ効く: {p.get('why_effective','')}\n"
                    f"  CTA: {p.get('cta','')}")
            blocks.append(_notion_bullet(text))
    plan = data.get("30day_plan", {})
    if plan:
        blocks.append(_notion_paragraph("【30日コンテンツプラン】"))
        for week, items in plan.items():
            label = week.replace("week", "第") + "週"
            text = f"{label}: {' → '.join(str(i) for i in items)}"
            blocks.append(_notion_bullet(text))
    return blocks


def save_to_notion(results: dict, keyword: str):
    """分析結果をNotionの情報収集エージェントページにサブページとして保存"""
    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token:
        console.print("[yellow]  NOTION_TOKEN が未設定のため Notion 保存をスキップ[/yellow]")
        console.print("[dim]  export NOTION_TOKEN='secret_...' を設定してください[/dim]")
        return

    headers_dict = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    ts = datetime.now().strftime("%Y/%m/%d %H:%M")

    mode_configs = [
        ("competitor",   "競合分析",          _build_notion_blocks_competitor),
        ("buzz",         "バズ分析",          _build_notion_blocks_buzz),
        ("trends",       "トレンド",          _build_notion_blocks_trends),
        ("googletrends", "📈 Googleトレンド", _build_notion_blocks_googletrends),
        ("university",   "🏫 大学募集要項",   _build_notion_blocks_university),
        ("note",         "📝 note人気記事",   _build_notion_blocks_note),
        ("news",         "📰 ニュース",        _build_notion_blocks_news),
        ("amazon",       "📚 Amazon参考書",   _build_notion_blocks_amazon),
        ("tiktok",       "🎵 TikTok",         _build_notion_blocks_tiktok),
        ("xtrends",      "🐦 X/Twitter",      _build_notion_blocks_xtrends),
        ("instagram",    "📸 Instagram",      _build_notion_blocks_instagram),
        ("proposals",    "コンテンツ案",      _build_notion_blocks_proposals),
    ]

    console.print(Rule("[bold blue]Notion 保存[/bold blue]"))

    for key, label, block_builder in mode_configs:
        data = results.get(key)
        if data is None:
            continue

        title = f"{label} — {keyword} ({ts})"
        blocks = [_notion_paragraph(f"キーワード: {keyword}\n実行日時: {ts}")] + block_builder(data)
        blocks = blocks[:100]  # Notion API 上限

        payload = {
            "parent": {"page_id": NOTION_PARENT_PAGE_ID},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}
            },
            "children": blocks,
        }

        try:
            req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                NOTION_API_URL, data=req_data, headers=headers_dict, method="POST"
            )
            with urllib.request.urlopen(req) as response:
                result_data = json.loads(response.read().decode("utf-8"))
                page_url = result_data.get("url", "")
                console.print(f"  [green]✓[/green] {label}: {page_url}")
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            console.print(f"  [red]✗[/red] {label}: HTTP {e.code} — {error_body[:200]}")
        except Exception as e:
            console.print(f"  [red]✗[/red] {label}: {e}")

    # ── 大学ごとの個別ページを保存（大学名→学部名→学科名 の3層構造）
    university_data = results.get("university")
    if university_data:
        universities = university_data.get("universities", [])
        if not universities:
            return

        def _notion_create_page(parent_id: str, title: str, blocks: list) -> str:
            """Notionページを作成してページIDを返す。失敗時は空文字。"""
            payload = {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": {"title": [{"type": "text", "text": {"content": title[:100]}}]}
                },
                "children": blocks[:100],
            }
            try:
                req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    NOTION_API_URL, data=req_data, headers=headers_dict, method="POST"
                )
                with urllib.request.urlopen(req) as response:
                    result_data = json.loads(response.read().decode("utf-8"))
                    return result_data.get("id", "")
            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8")
                console.print(f"  [red]✗[/red] Notion API HTTP {e.code} — {error_body[:200]}")
                return ""
            except Exception as e:
                console.print(f"  [red]✗[/red] Notion API エラー: {e}")
                return ""

        # 大学名でグループ化
        uni_grouped: dict[str, list] = {}
        for u in universities:
            uni_name = u.get("university", "不明")
            uni_grouped.setdefault(uni_name, []).append(u)

        console.print(f"  [cyan]大学ページを保存中... ({len(uni_grouped)}大学 / {len(universities)}件)[/cyan]")

        for uni_name, entries in uni_grouped.items():
            # 大学名ページを作成
            uni_blocks = [_notion_paragraph(f"キーワード: {keyword}\n実行日時: {ts}\n大学: {uni_name}")]
            uni_page_id = _notion_create_page(NOTION_UNIVERSITY_PARENT_PAGE_ID, f"🎓 {uni_name}", uni_blocks)
            if not uni_page_id:
                console.print(f"  [red]✗[/red] {uni_name}: 大学ページ作成失敗")
                continue
            console.print(f"  [green]✓[/green] 大学ページ: {uni_name}")
            time.sleep(0.05)

            # 学部でグループ化
            fac_grouped: dict[str, list] = {}
            for u in entries:
                fac_name = u.get("faculty", "情報なし") or "情報なし"
                fac_grouped.setdefault(fac_name, []).append(u)

            for fac_name, dept_entries in fac_grouped.items():
                # 学部ページを作成
                fac_blocks = [_notion_paragraph(f"{uni_name} {fac_name}\n取得日時: {ts}")]
                fac_page_id = _notion_create_page(uni_page_id, fac_name, fac_blocks)
                if not fac_page_id:
                    console.print(f"    [red]✗[/red] {fac_name}: 学部ページ作成失敗")
                    continue
                console.print(f"    [green]✓[/green] 学部ページ: {fac_name}")
                time.sleep(0.05)

                for u in dept_entries:
                    dept = u.get("department", "")
                    if dept and dept != "情報なし":
                        dept_title = f"{dept}（募集要項・AP・選考方法・対策）"
                    else:
                        dept_title = "総合型選抜情報（募集要項・AP・選考方法・対策）"
                    blocks = (
                        [_notion_paragraph(f"キーワード: {keyword}\n実行日時: {ts}")]
                        + _build_notion_blocks_single_university(u, ts, keyword)
                    )
                    dept_page_id = _notion_create_page(fac_page_id, dept_title, blocks)
                    if dept_page_id:
                        console.print(f"      [green]✓[/green] 学科ページ: {dept_title[:40]}")
                    else:
                        console.print(f"      [red]✗[/red] 学科ページ作成失敗: {dept_title[:40]}")
                    time.sleep(0.05)

                # ── 年度別データページを学部配下に作成
                yearly_blocks = _build_yearly_data_blocks_for_faculty(dept_entries, ts, keyword)
                yearly_page_id = _notion_create_page(fac_page_id, "年度別データ", yearly_blocks)
                if yearly_page_id:
                    console.print(f"      [green]✓[/green] 年度別データページ: {fac_name}")
                else:
                    console.print(f"      [red]✗[/red] 年度別データページ作成失敗: {fac_name}")
                time.sleep(0.05)




# ─────────────────────────────────────────
# university モード専用 Notion 階層保存
# ─────────────────────────────────────────
def _build_notion_blocks_step_a(step_a: dict, ts: str, keyword: str) -> list:
    """Step A（大学共通情報）のNotionページブロックを構築する。"""
    blocks: list = []
    blocks.append(_notion_paragraph(f"収集日時: {ts}  |  キーワード: {keyword}"))

    blocks.append(_notion_h2("🏫 大学全体のアドミッションポリシー・理念"))
    blocks.append(_notion_paragraph((step_a.get("ap_university") or "情報なし")[:1900]))

    blocks.append(_notion_h2("🎯 総合型選抜 基本方針"))
    blocks.append(_notion_paragraph((step_a.get("ao_basic_policy") or "情報なし")[:1900]))

    blocks.append(_notion_h2("✅ 大学共通の出願資格"))
    blocks.append(_notion_paragraph((step_a.get("common_eligibility") or "情報なし")[:1900]))

    blocks.append(_notion_h2("💡 教育方針・理念"))
    blocks.append(_notion_paragraph((step_a.get("education_philosophy") or "情報なし")[:1900]))

    blocks.append(_notion_h2("⭐ 大学の特色・強み"))
    blocks.append(_notion_paragraph((step_a.get("university_features") or "情報なし")[:1900]))

    if step_a.get("url"):
        blocks.append(_notion_paragraph(f"🔗 参照URL: {step_a['url'][:500]}"))

    return blocks[:100]


def _build_notion_blocks_step_b(fac: dict, ts: str, keyword: str) -> list:
    """Step B（学部共通情報）のNotionページブロックを構築する。"""
    blocks: list = []
    blocks.append(_notion_paragraph(f"収集日時: {ts}  |  キーワード: {keyword}"))

    blocks.append(_notion_h2("🎓 学部のアドミッションポリシー・特色"))
    blocks.append(_notion_paragraph((fac.get("ap_faculty") or "情報なし")[:1900]))

    blocks.append(_notion_h2("🎯 学部としての選考方針"))
    blocks.append(_notion_paragraph((fac.get("faculty_selection_policy") or "情報なし")[:1900]))

    blocks.append(_notion_h2("✅ 学部共通の出願条件"))
    blocks.append(_notion_paragraph((fac.get("faculty_eligibility") or "情報なし")[:1900]))

    blocks.append(_notion_h2("📚 カリキュラム・教育内容"))
    blocks.append(_notion_paragraph((fac.get("curriculum") or "情報なし")[:1900]))

    blocks.append(_notion_h2("👨‍🏫 教授陣・研究室"))
    professors = fac.get("professors") or []
    if professors:
        for p in professors:
            blocks.append(_notion_bullet(str(p)[:1900]))
    else:
        blocks.append(_notion_paragraph("情報なし"))

    blocks.append(_notion_h2("💼 卒業後の進路・就職"))
    career = fac.get("career") or ""
    if career and career != "情報なし":
        blocks.append(_notion_paragraph(career[:1900]))
    career_examples = fac.get("career_examples") or []
    for ex in career_examples:
        blocks.append(_notion_bullet(str(ex)[:1900]))
    if not career and not career_examples:
        blocks.append(_notion_paragraph("情報なし"))

    return blocks[:100]


def save_university_to_notion_hierarchical(results: dict, keyword: str) -> None:
    """universityモードの結果を
    🎓大学情報 / 大学群 / サブグループ / 大学名 / 学部名 / 学科ページ
    の階層に自動保存する。同名ページが既存なら作成をスキップ（重複チェック）。
    Step A → 大学ページ配下に「大学共通情報」サブページを作成
    Step B → 学部ページ配下に「学部共通情報」サブページを作成
    Step C → 学科ページ配下に「総合型選抜情報」サブページを作成（従来通り）
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token:
        console.print("[yellow]  NOTION_TOKEN が未設定のため Notion 保存をスキップ[/yellow]")
        console.print("[dim]  export NOTION_TOKEN='secret_...' を設定してください[/dim]")
        return

    university_data = results.get("university")
    if not university_data:
        return
    universities = university_data.get("universities", [])
    if not universities:
        console.print("[yellow]  保存対象の大学データなし[/yellow]")
        return
    step_a = university_data.get("step_a", {})
    step_b_faculties = university_data.get("step_b", {}).get("faculties", [])

    ts       = datetime.now().strftime("%Y/%m/%d %H:%M")
    date_str = datetime.now().strftime("%Y-%m-%d")

    _headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    # ── API ヘルパー ────────────────────────────────────────
    def _get_child_pages(page_id: str) -> dict[str, str]:
        """ページの直下子ページを {タイトル: ページID} で返す（最大100件）。"""
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        req = urllib.request.Request(url, headers=_headers, method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                result: dict[str, str] = {}
                for block in data.get("results", []):
                    if block.get("type") == "child_page":
                        t = block["child_page"]["title"]
                        result[t] = block["id"]
                return result
        except Exception as e:
            console.print(f"    [red]✗[/red] 子ページ一覧取得失敗 ({page_id[:8]}...): {e}")
            return {}

    def _create_page(parent_id: str, title: str, blocks: list) -> str:
        """Notionページを作成しページIDを返す。失敗時は空文字。"""
        payload = {
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:100]}}]}},
            "children": blocks[:100],
        }
        try:
            req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(NOTION_API_URL, data=req_data, headers=_headers, method="POST")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8")).get("id", "")
        except urllib.error.HTTPError as e:
            console.print(f"    [red]✗[/red] 作成失敗「{title[:40]}」: HTTP {e.code} — {e.read().decode()[:150]}")
            return ""
        except Exception as e:
            console.print(f"    [red]✗[/red] 作成失敗「{title[:40]}」: {e}")
            return ""

    def _find_or_create(parent_id: str, title: str, intro: str = "") -> str:
        """同名の子ページがあれば既存IDを返す。なければ新規作成してIDを返す。"""
        children = _get_child_pages(parent_id)
        if title in children:
            console.print(f"    [dim]既存使用: {title}[/dim]")
            return children[title]
        time.sleep(0.05)
        init_blocks = [_notion_paragraph(intro)] if intro else []
        new_id = _create_page(parent_id, title, init_blocks)
        if new_id:
            console.print(f"    [green]✓ 作成[/green]: {title}")
        return new_id

    # ── 大学名でグループ化 ──────────────────────────────────
    uni_grouped: dict[str, list] = {}
    for u in universities:
        uni_grouped.setdefault(u.get("university", "不明"), []).append(u)

    console.print(Rule("[bold blue]🎓 大学情報 → Notion 階層保存[/bold blue]"))
    console.print(f"  対象: {len(uni_grouped)}大学 / {len(universities)}件")

    for uni_name, entries in uni_grouped.items():
        group, subgroup = _classify_university(uni_name)
        console.print(f"\n  [cyan]{uni_name}[/cyan]  →  {group} / {subgroup}")

        # ① 大学群ページ（例: 最難関私大系）
        group_id = _find_or_create(NOTION_UNIVERSITY_PARENT_PAGE_ID, group)
        if not group_id:
            continue
        time.sleep(0.05)

        # ② サブグループページ（例: 早慶上理）
        subgroup_id = _find_or_create(group_id, subgroup)
        if not subgroup_id:
            continue
        time.sleep(0.05)

        # ③ 大学名ページ（例: 早稲田大学）
        uni_page_id = _find_or_create(subgroup_id, uni_name)
        if not uni_page_id:
            continue
        time.sleep(0.05)

        # ③-A 大学共通情報ページ（Step A）
        if step_a:
            step_a_title = f"大学共通情報 ({date_str})"
            existing_uni_pages = _get_child_pages(uni_page_id)
            if not any(t.startswith("大学共通情報") for t in existing_uni_pages):
                step_a_blocks = (
                    [_notion_h2(f"🏫 {uni_name}  大学共通情報")]
                    + _build_notion_blocks_step_a(step_a, ts, keyword)
                )
                step_a_pid = _create_page(uni_page_id, step_a_title, step_a_blocks)
                if step_a_pid:
                    console.print(f"    [green]✓[/green] {step_a_title}")
                time.sleep(0.05)
            else:
                console.print(f"    [yellow]⚠ 重複スキップ[/yellow]: {step_a_title}")

        # 学部でグループ化
        fac_grouped: dict[str, list] = {}
        for u in entries:
            fac_name = u.get("faculty") or "情報なし"
            fac_grouped.setdefault(fac_name, []).append(u)

        for fac_name, dept_entries in fac_grouped.items():
            # ④ 学部ページ（例: 政治経済学部）
            fac_intro = f"{uni_name}  {fac_name}\n取得日時: {ts}\nキーワード: {keyword}"
            fac_page_id = _find_or_create(uni_page_id, fac_name, fac_intro)
            if not fac_page_id:
                continue
            time.sleep(0.05)

            # ④-B 学部共通情報ページ（Step B）
            fac_b = next((f for f in step_b_faculties if f.get("faculty") == fac_name), None)
            if fac_b:
                step_b_title = f"学部共通情報 ({date_str})"
                existing_fac_pages = _get_child_pages(fac_page_id)
                if not any(t.startswith("学部共通情報") for t in existing_fac_pages):
                    step_b_blocks = (
                        [_notion_h2(f"📚 {fac_name}  学部共通情報")]
                        + _build_notion_blocks_step_b(fac_b, ts, keyword)
                    )
                    step_b_pid = _create_page(fac_page_id, step_b_title, step_b_blocks)
                    if step_b_pid:
                        console.print(f"      [green]✓[/green] {step_b_title}  [{fac_name}]")
                    time.sleep(0.05)
                else:
                    console.print(f"      [yellow]⚠ 重複スキップ[/yellow]: {step_b_title}")

            for u in dept_entries:
                dept = u.get("department") or ""
                dept_label = dept if dept and dept not in ("情報なし", "") else ""

                if dept_label:
                    # ⑤ 学科名ページ（例: 政治学科）を学部ページ配下に作成
                    dept_page_id = _find_or_create(
                        fac_page_id, dept_label,
                        f"{uni_name}  {fac_name}  {dept_label}\n取得日時: {ts}"
                    )
                    if not dept_page_id:
                        continue
                    time.sleep(0.05)
                    save_parent_id = dept_page_id

                    # ⑥ 総合型選抜情報ページ（学科ページ直下）
                    page_title = f"総合型選抜情報 — {dept_label} ({date_str})"
                else:
                    # 学科名不明 → 学部ページ直下に直接保存
                    save_parent_id = fac_page_id
                    page_title = f"総合型選抜情報 — {fac_name} ({date_str})"

                # 重複チェック（日付違いの同コンテンツもスキップ）
                existing_under_parent = _get_child_pages(save_parent_id)
                prefix = f"総合型選抜情報 — {dept_label}" if dept_label else f"総合型選抜情報 — {fac_name}"
                if any(t.startswith(prefix) for t in existing_under_parent):
                    console.print(f"      [yellow]⚠ 重複スキップ[/yellow]: {page_title[:60]}")
                    continue

                blocks = (
                    [_notion_h2(f"{'　'.join(filter(None, [uni_name, fac_name, dept_label]))}"),
                     _notion_paragraph(f"収集日時: {ts}  |  キーワード: {keyword}")]
                    + _build_notion_blocks_single_university(u, ts, keyword)
                )
                info_page_id = _create_page(save_parent_id, page_title, blocks)
                if info_page_id:
                    _path = f"{fac_name} / {dept_label} / {page_title}" if dept_label else f"{fac_name} / {page_title}"
                    console.print(f"      [green]✓[/green] {_path[:65]}")
                time.sleep(0.05)

            # 年度別データページ（学部ページ直下・重複チェック）
            existing_fac = _get_child_pages(fac_page_id)
            yearly_title = f"年度別データ ({date_str})"
            if yearly_title not in existing_fac:
                yearly_blocks = _build_yearly_data_blocks_for_faculty(dept_entries, ts, keyword)
                yearly_id = _create_page(fac_page_id, yearly_title, yearly_blocks)
                if yearly_id:
                    console.print(f"      [green]✓[/green] {yearly_title}  [{fac_name}]")
                time.sleep(0.05)

    console.print(f"\n  [green bold]✓ Notion 保存完了[/green bold]")


# ─────────────────────────────────────────
# メイン
# ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="総合型選抜 SNS リサーチツール v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--keyword", "-k", default=DEFAULT_KEYWORD, help="分析キーワード")
    parser.add_argument(
        "--mode", "-m",
        choices=["all", "competitor", "buzz", "trends", "googletrends", "note", "university",
                 "news", "amazon", "tiktok", "xtrends", "instagram", "threads", "youtube",
                 "proposals"],
        default="all",
        help="実行モード（デフォルト: all）",
    )
    parser.add_argument("--no-save", action="store_true", help="結果をファイルに保存しない")
    parser.add_argument("--notion", action="store_true", help="Notionへ直接保存する（デフォルトはOFF）")
    parser.add_argument("--no-notion", action="store_true", dest="no_notion",
                        help="universityモードの自動Notion保存をスキップする")
    parser.add_argument("--pdf-url", default="", dest="pdf_url",
                        help="大学の募集要項PDFのURLを直接指定（universityモードで使用）"
                             " 例: --pdf-url 'https://xxx.ac.jp/admission.pdf'")
    # ── v2.0 オプション
    parser.add_argument(
        "--workers", "-w", type=int, default=4, metavar="N",
        help="並列検索ワーカー数（デフォルト: 4）",
    )
    parser.add_argument(
        "--cache", action="store_true", default=True,
        help="検索結果をキャッシュする（デフォルト: ON）",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="キャッシュを使用しない（常に新規検索）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="検索データの収集のみ実行し Claude を呼ばない",
    )
    parser.add_argument(
        "--output-format", choices=["json", "html", "both"], default="both",
        help="出力形式を選択（デフォルト: both）",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="詳細ログを出力する",
    )
    # ── v3.0 新オプション
    parser.add_argument("--model", choices=["fast", "smart", "deep"],
                        default="smart",
                        help="分析モデル: fast=Haiku / smart=Sonnet / deep=Opus")
    parser.add_argument("--compare", action="store_true",
                        help="前回の結果と差分を表示する")
    parser.add_argument("--history", type=int, default=0, metavar="N",
                        help="過去N日分のトレンド推移を表示する")
    parser.add_argument("--youtube-key", default="", dest="youtube_key",
                        help="YouTube Data API v3 キー（省略可）")
    args = parser.parse_args()

    # ── グローバル設定を反映
    global _USE_CACHE, _VERBOSE, _NUM_WORKERS, MODEL, YOUTUBE_API_KEY
    _VERBOSE    = args.verbose
    _NUM_WORKERS = max(1, min(args.workers, 16))  # 1〜16 に制限
    _USE_CACHE  = not args.no_cache

    if args.model == "fast":
        MODEL = MODEL_FAST
        MODEL_EXTRACT = MODEL_FAST
    elif args.model == "deep":
        MODEL = MODEL_DEEP
        MODEL_EXTRACT = MODEL_DEEP
    else:
        MODEL = MODEL_SMART
        MODEL_EXTRACT = MODEL_FAST  # smart: 検索結果の整理はHaikuで十分

    if args.youtube_key:
        YOUTUBE_API_KEY = args.youtube_key

    if args.dry_run:
        console.print(Panel(
            "[bold yellow]--dry-run モード: 検索データの収集のみ実行します（Claude 未使用）[/bold yellow]",
            border_style="yellow",
        ))

    if _USE_CACHE:
        _CACHE_DIR.mkdir(exist_ok=True)

    # ─ API キー確認
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        console.print(Panel(
            "[bold red]ANTHROPIC_API_KEY が設定されていません[/bold red]\n\n"
            "以下を実行してください:\n"
            "  [cyan]export ANTHROPIC_API_KEY='sk-ant-...'[/cyan]\n\n"
            "[dim]検索のみ実行する場合は --dry-run オプションを使用してください[/dim]",
            border_style="red",
        ))
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key) if api_key else None

    # ─ ヘッダー
    gt_available = PYTRENDS_AVAILABLE or args.mode != "googletrends"
    cache_status = "[green]ON[/green]" if _USE_CACHE else "[dim]OFF[/dim]"
    model_label = {"fast": "Haiku", "smart": "Sonnet", "deep": "Opus"}.get(args.model, args.model)
    console.print(Panel(
        f"[bold]総合型選抜 SNS リサーチ・コンテンツ分析ツール v3.0[/bold]\n"
        f"キーワード: [cyan]{args.keyword}[/cyan]  |  モード: [yellow]{args.mode}[/yellow]  |  "
        f"モデル: [dim]{model_label} ({MODEL})[/dim]\n"
        f"ワーカー数: [cyan]{_NUM_WORKERS}[/cyan]  |  キャッシュ: {cache_status}  |  "
        f"dry-run: {'[yellow]ON[/yellow]' if args.dry_run else '[dim]OFF[/dim]'}"
        + ("" if gt_available else "\n[yellow]⚠ pytrends 未インストール: pip install pytrends[/yellow]"),
        border_style="cyan",
    ))

    results = {}
    competitor_data = buzz_data = trend_data = None

    def _run(fn, *fn_args, **fn_kwargs):
        """dry-run 時は Claude 呼び出し関数をスキップするラッパー。"""
        if args.dry_run and client is None:
            console.print(f"[dim]  dry-run: {fn.__name__} をスキップ[/dim]")
            return {}
        return fn(*fn_args, **fn_kwargs)

    def _run_and_save(mode_name, fn, *fn_args, **fn_kwargs):
        """_run() + スナップショット保存"""
        result = _run(fn, *fn_args, **fn_kwargs)
        if result and not args.dry_run:
            save_snapshot(args.keyword, mode_name, result)
        return result

    try:
        if args.mode in ("all", "competitor"):
            competitor_data = _run_and_save("competitor", run_competitor_analysis, client, args.keyword)
            results["competitor"] = competitor_data
            console.print()

        if args.mode in ("all", "buzz"):
            buzz_data = _run_and_save("buzz", run_buzz_analysis, client, args.keyword)
            results["buzz"] = buzz_data
            console.print()

        if args.mode in ("all", "trends"):
            trend_data = _run_and_save("trends", run_trend_analysis, client, args.keyword)
            results["trends"] = trend_data
            console.print()

        if args.mode in ("all", "googletrends"):
            googletrends_data = run_google_trends(args.keyword)
            results["googletrends"] = googletrends_data
            if googletrends_data and not args.dry_run:
                save_snapshot(args.keyword, "googletrends", googletrends_data)
            console.print()

        if args.mode in ("all", "note"):
            note_data = _run_and_save("note", run_note_analysis, client, args.keyword)
            results["note"] = note_data
            console.print()

        if args.mode in ("all", "university"):
            university_data = _run_and_save("university", run_university_analysis, client, args.keyword, pdf_url=args.pdf_url)
            results["university"] = university_data
            console.print()
            # university モードは --no-notion がない限り自動保存
            if not args.no_notion and university_data:
                save_university_to_notion_hierarchical(results, args.keyword)

        if args.mode in ("all", "news"):
            news_data = _run_and_save("news", run_news_analysis, client, args.keyword)
            results["news"] = news_data
            console.print()

        if args.mode in ("all", "amazon"):
            amazon_data = _run_and_save("amazon", run_amazon_analysis, client, args.keyword)
            results["amazon"] = amazon_data
            console.print()

        if args.mode in ("all", "tiktok"):
            tiktok_data = _run_and_save("tiktok", run_tiktok_analysis, client, args.keyword)
            results["tiktok"] = tiktok_data
            console.print()

        if args.mode in ("all", "xtrends"):
            xtrends_data = _run_and_save("xtrends", run_xtrends_analysis, client, args.keyword)
            results["xtrends"] = xtrends_data
            console.print()

        if args.mode in ("all", "instagram"):
            instagram_data = _run_and_save("instagram", run_instagram_analysis, client, args.keyword)
            results["instagram"] = instagram_data
            console.print()

        if args.mode in ("all", "threads"):
            threads_data = _run_and_save("threads", run_threads_analysis, client, args.keyword)
            results["threads"] = threads_data
            console.print()

        if args.mode in ("all", "youtube"):
            youtube_data = _run_and_save("youtube", run_youtube_analysis, client, args.keyword)
            results["youtube"] = youtube_data
            console.print()

        if args.mode in ("all", "proposals"):
            proposals = _run_and_save("proposals", run_content_proposals, client, args.keyword,
                                      competitor_data, buzz_data, trend_data)
            results["proposals"] = proposals
            console.print()

    except AnalysisError as e:
        console.print(Panel(
            f"[bold red]分析エラー[/bold red]\n{e}\n\n"
            "[dim]部分的な結果は保存されます[/dim]",
            border_style="red",
        ))
    except anthropic.APIError as e:
        console.print(f"[red]Claude API エラー: {e}[/red]")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]中断されました[/yellow]")
        sys.exit(0)

    # ─ 前回との差分表示（--compare）
    if args.compare and results:
        console.print(Rule("[yellow]前回との差分[/yellow]"))
        for mode_name, data in results.items():
            if not data:
                continue
            prev = get_last_snapshot(args.keyword, mode_name)
            if prev is None:
                console.print(f"  [dim]{mode_name}: 前回データなし（初回実行）[/dim]")
                continue
            prev_data = prev.get("data", {})
            prev_keys = set(prev_data.keys())
            curr_keys = set(data.keys()) - {"_raw_articles", "_api_videos", "raw"}
            new_keys = curr_keys - prev_keys
            removed_keys = prev_keys - curr_keys
            lines = [f"  [bold]{mode_name}[/bold] (前回: {prev['captured_at'][:16]})"]
            if new_keys:
                lines.append(f"    [green]+新規フィールド: {', '.join(sorted(new_keys))}[/green]")
            if removed_keys:
                lines.append(f"    [red]-削除フィールド: {', '.join(sorted(removed_keys))}[/red]")
            # 主要リストフィールドの件数比較
            for key in sorted(curr_keys & prev_keys):
                if isinstance(data.get(key), list) and isinstance(prev_data.get(key), list):
                    diff = len(data[key]) - len(prev_data[key])
                    if diff != 0:
                        sign = f"[green]+{diff}[/green]" if diff > 0 else f"[red]{diff}[/red]"
                        lines.append(f"    {key}: {len(prev_data[key])} → {len(data[key])} ({sign})")
            if len(lines) > 1:
                console.print("\n".join(lines))

    # ─ 保存
    if not args.no_save and results:
        # output-format に応じて保存
        if args.output_format in ("json", "both"):
            json_path, html_path = save_results(results, args.keyword)
            console.print(Rule("[green]分析完了[/green]"))
            if args.output_format == "both" or args.output_format == "json":
                console.print(f"  JSON: [cyan]{json_path}[/cyan]")
            if args.output_format in ("html", "both"):
                console.print(f"  HTML: [cyan]{html_path}[/cyan]")
        elif args.output_format == "html":
            _, html_path = save_results(results, args.keyword)
            console.print(Rule("[green]分析完了[/green]"))
            console.print(f"  HTML: [cyan]{html_path}[/cyan]")
    else:
        console.print(Rule("[green]分析完了[/green]"))

    # キャッシュ統計を表示
    if _USE_CACHE and _VERBOSE:
        try:
            cache_files = list(_CACHE_DIR.glob("*.json"))
            console.print(f"  [dim]キャッシュ: {len(cache_files)} ファイル ({_CACHE_DIR})[/dim]")
        except Exception:
            pass

    # ─ Notion 保存
    if args.notion and results:
        save_to_notion(results, args.keyword)

    # コスト表示
    display_cost_summary()


if __name__ == "__main__":
    main()
