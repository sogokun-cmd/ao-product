"""
/api/diagnosis — Mode 3（マッチ度診断 + 必然性スコア）
受験生プロファイル × リサーチ結果 を LLM で合格ロジックに照らして診断。
"""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field  # noqa: F401
from slowapi import Limiter
from slowapi.util import get_remote_address

import uuid

from auth.deps import require_plan
from database import get_db
from core import llm_router
from core.llm_router import set_llm_context

router = APIRouter(prefix="/api/diagnosis", tags=["diagnosis"])
limiter = Limiter(key_func=get_remote_address)


class MatchRequest(BaseModel):
    student_id: int
    request_id: str


class StressTestRequest(BaseModel):
    request_id: str
    draft_text: str = Field(..., min_length=50, max_length=8000)
    student_id: int | None = None
    draft_type: str = Field(default="志望理由書", max_length=40)  # "志望理由書" or "面接回答"


class PhaseStrategyRequest(BaseModel):
    current_month: int = Field(..., ge=1, le=12)  # 1〜12 月
    preparation_status: str = Field(default="", max_length=4000)  # 現状メモ（自由記述）
    student_id: int | None = None
    request_id: str | None = None


class DisclosureLevelRequest(BaseModel):
    draft_text: str = Field(..., min_length=50, max_length=8000)
    student_id: int | None = None


class CompetitionMapRequest(BaseModel):
    student_id: int
    request_id: str | None = None


class DensityCheckRequest(BaseModel):
    draft_text: str = Field(..., min_length=50, max_length=8000)
    request_id: str | None = None


class BatchMatchRequest(BaseModel):
    student_id: int
    max_universities: int = Field(default=8, ge=1, le=15)


def _load_student(db, student_id: int, user_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM students WHERE id=? AND user_id=?",
        (student_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "生徒が見つかりません")
    return dict(row)


def _load_research(db, request_id: str, user_id: int) -> dict:
    req = db.execute(
        "SELECT * FROM research_requests WHERE id=?", (request_id,)
    ).fetchone()
    if not req:
        raise HTTPException(404, "リサーチが見つかりません")
    if req["user_id"] != user_id:
        # team 共有は後続課題
        raise HTTPException(403, "このリサーチへのアクセス権がありません")
    if req["status"] != "done":
        raise HTTPException(400, "リサーチが完了していません")
    res = db.execute(
        "SELECT result_json FROM research_results WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if not res:
        raise HTTPException(404, "結果が見つかりません")
    return {"request": dict(req), "result": json.loads(res["result_json"])}


_SYSTEM_PROMPT = """あなたは総合型選抜・学校推薦型選抜に特化した大学情報アナリストです。
受験生プロファイルとリサーチ結果を照合し、合格ロジックに基づいて診断します。

【合格ロジックの根本命題】
総合型選抜は「実績の強さで決まる試験ではなく、自分の経験を言語化し、
社会課題と大学での学びと将来像につなげ、その大学とのマッチ度を論理的に示せるかで決まる試験」。
分析の軸: ①マッチ度 ②必然性 ③独自性 ④一貫性（経験→社会課題→大学→将来）

【出力ポリシー】
- 推測・一般論ではなく、受験生プロファイルとリサーチ結果に根拠付けて診断
- 採点は各小項目で「高（15-20）」「中（8-14）」「低（0-7）」の幅で、合理的な根拠を添える
- 接続が強い/弱いポイントは、受験生のどの経験/関心と、大学のどの具体要素が対応するかを示す
- 「4ステップ接続（経験→社会課題→大学→将来）」の素材を、受験生プロファイルから抜き出して整理
- 絶対に偏差値・知名度・就職実績で評価しない

出力は指定JSONのみ（コードブロック不要）。"""


def _build_user_prompt(student: dict, research: dict) -> str:
    ud = (research["result"].get("university_data") or {})
    unis = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
    u = unis[0] if unis else {}

    # 大学データのコア情報だけ抽出（プロンプト肥大化を避ける）
    uni_summary = {
        "university": u.get("university", ""),
        "faculty":    u.get("faculty", ""),
        "department": u.get("department", ""),
        "formal_ao_name": u.get("formal_ao_name", ""),
        "admission_policy": u.get("admission_policy"),
        "features": u.get("features", ""),
        "uniqueness_layers": u.get("uniqueness_layers", {}),
        "specific_materials": u.get("specific_materials", {}),
        "key_phrases": u.get("key_phrases", []),
        "match_anchors": u.get("match_anchors", []),
        "interview_topics": u.get("interview_topics", []),
        "eligibility": u.get("eligibility", ""),
        "evaluation_criteria": u.get("evaluation_criteria", ""),
        "selection_phase_1": u.get("selection_phase_1", ""),
        "selection_phase_2": u.get("selection_phase_2", ""),
    }
    step_b = (ud.get("step_b") or {}).get("faculties") or []
    if step_b:
        uni_summary["faculty_context"] = step_b[0]

    return f"""=== 受験生プロファイル ===
名前: {student.get('name', '')}
経験・活動実績:
{student.get('profile_experience', '').strip() or '（未入力）'}

問題意識・関心分野:
{student.get('profile_concerns', '').strip() or '（未入力）'}

将来像:
{student.get('profile_future', '').strip() or '（未入力）'}

現時点の志望動機・認識:
{student.get('profile_motivation', '').strip() or '（未入力）'}

=== 対象大学・学科 ===
{json.dumps(uni_summary, ensure_ascii=False, indent=2)[:20000]}

=== 診断出力（JSONのみ）===
{{
  "match_score": {{
    "research_theme":        {{"score": 0-20, "reasoning": "根拠"}},
    "curriculum_structure":  {{"score": 0-20, "reasoning": "根拠"}},
    "admission_policy":      {{"score": 0-15, "reasoning": "根拠"}},
    "facility_system":       {{"score": 0-15, "reasoning": "根拠"}},
    "teaching_support":      {{"score": 0-15, "reasoning": "根拠"}},
    "career_connection":     {{"score": 0-15, "reasoning": "根拠"}},
    "total": "合計（0-100）",
    "interpretation": "80以上=強い志望理由が書ける / 60-79=補強必要 / 40-59=再考推奨 / 40未満=不向き の判定コメント"
  }},
  "necessity_score": {{
    "experience_necessity": {{"score": 0-20, "reasoning": "なぜ他人ではなくこの受験生がこれをやるのかの必然性"}},
    "issue_necessity":      {{"score": 0-20, "reasoning": "課題設定の独自性・当事者性"}},
    "university_necessity": {{"score": 0-20, "reasoning": "なぜ他大学ではなくこの大学かの必然性"}},
    "future_necessity":     {{"score": 0-20, "reasoning": "将来像の現実性・経験と課題からの導出"}},
    "total": "合計（0-80）",
    "interpretation": "64以上=強い必然性 / 48-63=深掘り必要 / 32-47=一般的 / 32未満=誰でも書ける の判定コメント"
  }},
  "map_position": {{
    "type": "完成型 | 補強型 | 必然性先行型 | 再考型",
    "note": "2軸マップ（縦: マッチ度 横: 必然性）での位置づけと次の一手"
  }},
  "strong_connections": [
    {{"student_point": "受験生のこの経験/関心", "university_point": "大学のこの具体要素", "reason": "接続が強い理由"}}
  ],
  "weak_points": [
    {{"description": "志望動機で補強すべき箇所", "how_to_fix": "どう補うか"}}
  ],
  "four_step_materials": {{
    "experience":   "受験生プロファイルから、経験パートに使える素材（具体的に）",
    "social_issue": "課題パートに使える素材（受験生の関心から）",
    "university":   "大学パートに使える素材（大学の具体要素で）",
    "future":       "将来像パートに使える素材"
  }},
  "next_actions": ["今最も効果の大きい行動を3つ、具体的に"]
}}"""


@router.post("/match", summary="Mode 3: マッチ度診断 + 必然性スコア")
@limiter.limit("20/minute")
async def diagnose_match(request: Request, body: MatchRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        student = _load_student(db, body.student_id, user["user_id"])
        research = _load_research(db, body.request_id, user["user_id"])
    finally:
        db.close()

    # プロファイルが何も入っていない場合はエラー
    if not any(student.get(f, "").strip() for f in
               ("profile_experience", "profile_concerns", "profile_future", "profile_motivation")):
        raise HTTPException(400, "受験生プロファイル（経験・関心・将来像など）が未入力です")

    user_prompt = _build_user_prompt(student, research)
    try:
        parsed, resp = llm_router.call_json(
            task="analysis",
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=12000,
        )
    except llm_router.NoProviderAvailable as e:
        raise HTTPException(503, f"LLM プロバイダ利用不可: {e}")

    if "_raw" in parsed:
        # デバッグ用に先頭を返す
        raise HTTPException(
            502,
            f"LLM 応答を JSON としてパースできませんでした。先頭: {parsed['_raw'][:300]}",
        )

    return {
        "student":  {"id": student["id"], "name": student["name"]},
        "request":  {"id": research["request"]["id"],
                     "university": research["request"]["university"],
                     "faculty":    research["request"]["faculty"],
                     "department": research["request"]["department"]},
        "diagnosis": parsed,
        "model": {"provider": resp.provider, "model": resp.model},
    }


# ── Mode 5: 耐圧テスト ──────────────────────────────────────────────────────

_STRESS_SYSTEM_PROMPT = """あなたは総合型選抜・学校推薦型選抜の5つの視点を切り替えて評価できるベテランアナリストです。
受験生の志望理由書（または面接回答）ドラフトを、対象大学の情報と照らして耐圧テストします。

【評価する5つの視点（役割を切り替える）】
1. 面接官 — 論理の穴・必然性の弱さ・根拠不足を容赦なく突く
2. 高校の先生 — 読みやすさ・一貫性・個性が伝わるかを温かく指摘
3. 塾講師 — 強いパターン／弱いパターンのどちらに該当するか、寄せ方を実践的に提示
4. 大学教授 — 分野理解の浅さ・研究テーマの表面性・専門用語の誤用を学術的に正す
5. 同世代受験生 — きれいごと／偽物っぽさ／共感できるかをリアリティで判定

【想定質問カテゴリ（少なくとも各3問ずつ生成）】
- necessity（必然性への突っ込み）: なぜあなたが、なぜこの経験から、なぜ他人ではなく…
- logic（論理の穴への突っ込み）: 飛躍・前提の妥当性・反対意見
- university（大学理解への突っ込み）: 具体的研究室・教員の論文・他大学との比較
- future（将来像への突っ込み）: 10年後・現実性・大学院・想定外への対応
- achievements（実績・活動への突っ込み）: 具体的役割・失敗・個人の貢献

【出力ポリシー】
- 5つの視点それぞれから評価を出す。温度感は視点ごとに異なる
- 質問は具体的でなければならない。「あなたの経験について」のような抽象的な質問は禁止
- ドラフトが耐えられるか／崩れるかを質問別に判定
- 最後に優先補強すべき3点を挙げる
- 絶対に偏差値・就職実績で評価しない

出力は指定JSONのみ（コードブロック不要）。"""


def _build_stress_user_prompt(draft_text: str, research: dict, student: dict | None, draft_type: str) -> str:
    ud = (research["result"].get("university_data") or {})
    unis = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
    u = unis[0] if unis else {}
    uni_summary = {
        "university": u.get("university", ""),
        "faculty":    u.get("faculty", ""),
        "department": u.get("department", ""),
        "admission_policy": u.get("admission_policy"),
        "uniqueness_layers": u.get("uniqueness_layers", {}),
        "specific_materials": u.get("specific_materials", {}),
        "key_phrases": u.get("key_phrases", []),
        "avoided_phrases": u.get("avoided_phrases", []),
        "interview_topics": u.get("interview_topics", []),
        "evaluation_criteria": u.get("evaluation_criteria", ""),
    }
    student_part = ""
    if student:
        student_part = f"""=== 受験生プロファイル（参考） ===
経験: {student.get('profile_experience', '').strip() or '（未入力）'}
問題意識: {student.get('profile_concerns', '').strip() or '（未入力）'}
将来像: {student.get('profile_future', '').strip() or '（未入力）'}
"""

    return f"""{student_part}=== 対象大学・学科 ===
{json.dumps(uni_summary, ensure_ascii=False, indent=2)[:10000]}

=== 耐圧テスト対象（{draft_type} ドラフト） ===
{draft_text[:7000]}

=== 出力（JSONのみ） ===
{{
  "role_evaluations": [
    {{"role": "面接官",         "tone": "厳しく論理的", "strong_points": ["論理が通っている箇所"], "weak_points": ["論理の穴・必然性不足の箇所"], "overall": "総評（2-3文）"}},
    {{"role": "高校の先生",     "tone": "温かく客観", "strong_points": ["..."], "weak_points": ["..."], "overall": "..."}},
    {{"role": "塾講師",         "tone": "実践・経験則", "matched_pattern": "該当する弱い/強いパターン", "recommended_shift": "寄せるための修正案", "overall": "..."}},
    {{"role": "大学教授",       "tone": "学術的", "field_understanding": "分野理解の深さ評価", "errors": ["専門用語の誤用・分野の誤解"], "further_readings": ["追加で読むべき資料"], "overall": "..."}},
    {{"role": "同世代受験生",   "tone": "率直・共感ベース", "feels_real": ["リアルと感じる箇所"], "feels_fake":  ["きれいごとに感じる箇所"], "overall": "..."}}
  ],
  "questions_by_category": {{
    "necessity":    [{{"question": "具体的な質問", "difficulty": "基本|深掘り", "notes": "何を突いているか"}}],
    "logic":        [{{"question": "...", "difficulty": "...", "notes": "..."}}],
    "university":   [{{"question": "...", "difficulty": "...", "notes": "..."}}],
    "future":       [{{"question": "...", "difficulty": "...", "notes": "..."}}],
    "achievements": [{{"question": "...", "difficulty": "...", "notes": "..."}}]
  }},
  "test_summary": {{
    "survives_pct": "耐えられる質問の割合（例: 約60%）",
    "stalls_pct":   "答えに詰まりそうな質問の割合",
    "breaks_pct":   "論理が崩れそうな質問の割合",
    "top_3_reinforcements": ["優先して補強すべき3つ、具体的に", "...", "..."],
    "final_comment": "総評（3-5文）"
  }}
}}"""


@router.post("/stress_test", summary="Mode 5: 耐圧テスト（志望理由書ドラフトの弱点診断）")
@limiter.limit("20/minute")
async def diagnose_stress_test(request: Request, body: StressTestRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        research = _load_research(db, body.request_id, user["user_id"])
        student = _load_student(db, body.student_id, user["user_id"]) if body.student_id else None
    finally:
        db.close()

    user_prompt = _build_stress_user_prompt(body.draft_text, research, student, body.draft_type)
    try:
        parsed, resp = llm_router.call_json(
            task="analysis",
            system=_STRESS_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=12000,
        )
    except llm_router.NoProviderAvailable as e:
        raise HTTPException(503, f"LLM プロバイダ利用不可: {e}")

    if "_raw" in parsed:
        raise HTTPException(
            502,
            f"LLM 応答を JSON としてパースできませんでした。先頭: {parsed['_raw'][:300]}",
        )

    return {
        "request": {"id": research["request"]["id"],
                    "university": research["request"]["university"],
                    "faculty":    research["request"]["faculty"],
                    "department": research["request"]["department"]},
        "student": ({"id": student["id"], "name": student["name"]} if student else None),
        "draft_type": body.draft_type,
        "draft_length": len(body.draft_text),
        "diagnosis": parsed,
        "model": {"provider": resp.provider, "model": resp.model},
    }


# ── Mode 6: 段階別戦略 ──────────────────────────────────────────────────────

_PHASE_SYSTEM_PROMPT = """あなたは総合型選抜・学校推薦型選抜の段階別戦略アドバイザーです。
受験生の現在の月と準備状況を受け取り、今最も効果の大きい行動を提示します。

【時期の区分（日本の受験スケジュールに準拠）】
- 4-6月: 方向性探索期 — 自己分析、志望校候補出し、情報収集の広げ
- 7-8月: 方向性確定期 — マッチ度診断、志望校絞り込み、必然性の言語化（夏休みの活用）
- 9-10月: 完成期 — 志望理由書の論理強化、面接耐圧テスト、情報密度向上
- 11-12月: 出願前最終調整期 — 一貫性チェック、最終添削、誤字脱字
- 1-3月: 次年度準備・反省期 — 浪人の場合は戦略の再構築、現役は二次・共通テスト側

【出力ポリシー】
- 推奨アクションは必ず「具体的な1歩」まで落とす（「自己分析する」ではなく「部活経験の感情の動きを時系列で書き出す」）
- 避けるべき行動も具体的に
- 現状評価は受験生の入力した準備状況から引用して書く
- 絶対に「頑張って」のような空回り励ましをしない
- 偏差値・実績で評価しない

出力は指定JSONのみ（コードブロック不要）。"""


def _build_phase_user_prompt(month: int, status: str, student: dict | None, research: dict | None) -> str:
    ctx_parts = [f"現在の月: {month}月"]
    if status.strip():
        ctx_parts.append(f"現状メモ:\n{status.strip()[:3000]}")
    if student:
        ctx_parts.append(f"""受験生プロファイル:
- 経験: {student.get('profile_experience', '').strip()[:500] or '（未入力）'}
- 問題意識: {student.get('profile_concerns', '').strip()[:500] or '（未入力）'}
- 将来像: {student.get('profile_future', '').strip()[:500] or '（未入力）'}
- 志望動機: {student.get('profile_motivation', '').strip()[:500] or '（未入力）'}""")
    if research:
        ctx_parts.append(f"志望校: {research['request'].get('university', '')} {research['request'].get('faculty', '')} {research['request'].get('department', '')}")
    return "\n\n".join(ctx_parts) + """

=== 出力（JSONのみ） ===
{
  "current_stage": {
    "period_name": "4-6月 方向性探索期 | 7-8月 方向性確定期 | 9-10月 完成期 | 11-12月 出願前最終調整期 | 1-3月 次年度準備・反省期",
    "key_theme": "この時期の最重要テーマ（1文）",
    "days_remaining_to_typical_deadline": "一般的な出願締切まで何日程度かの概算（例: 約60日 / 既に出願期間）"
  },
  "current_assessment": {
    "already_done": ["準備状況メモから読める「できていること」を具体的に"],
    "still_missing": ["準備状況メモから読める「足りていないこと」を具体的に"]
  },
  "top_actions": [
    {"action": "今最も効果の大きい具体的な1歩", "why": "この時期にこれをやる理由", "est_time": "所要時間の目安（例: 1日 / 週末2日間 / 2週間）"},
    {"action": "...", "why": "...", "est_time": "..."},
    {"action": "...", "why": "...", "est_time": "..."}
  ],
  "recommended_modes": [
    {"mode": "Mode 2 独自性抽出 | Mode 3 マッチ度診断 | Mode 5 耐圧テスト | Mode 6 段階別戦略", "why": "なぜこの時期にこのモードが効くか"}
  ],
  "avoid_actions": [
    {"action": "この時期にやると逆効果・浪費になる行動", "why": "理由"}
  ],
  "next_stage_checklist": ["次の段階に進む前にチェックすべき項目を5-8個"],
  "timing_warning": "この時期特有の注意（例: 夏休み明けに危機感が来る受験生が多い、出願期間と試験日の重なりに注意 等）"
}"""


@router.post("/phase_strategy", summary="Mode 6: 段階別戦略（時期 + 現状 → 今やるべき行動）")
@limiter.limit("30/minute")
async def diagnose_phase_strategy(request: Request, body: PhaseStrategyRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        student = _load_student(db, body.student_id, user["user_id"]) if body.student_id else None
        research = _load_research(db, body.request_id, user["user_id"]) if body.request_id else None
    finally:
        db.close()

    user_prompt = _build_phase_user_prompt(body.current_month, body.preparation_status, student, research)
    try:
        parsed, resp = llm_router.call_json(
            task="analysis",
            system=_PHASE_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=3500,
        )
    except llm_router.NoProviderAvailable as e:
        raise HTTPException(503, f"LLM プロバイダ利用不可: {e}")

    if "_raw" in parsed:
        raise HTTPException(502, "LLM 応答を JSON としてパースできませんでした")

    return {
        "current_month": body.current_month,
        "student": ({"id": student["id"], "name": student["name"]} if student else None),
        "request": ({"id": research["request"]["id"],
                     "university": research["request"]["university"]} if research else None),
        "diagnosis": parsed,
        "model": {"provider": resp.provider, "model": resp.model},
    }


# ── 追加 3 診断機能（v2 ノウハウ由来） ─────────────────────────────────────

_DISCLOSURE_SYSTEM_PROMPT = """あなたは総合型選抜の志望理由書評価の専門家です。
提出された文章の「自己開示レベル」を判定します。

【レベル定義】
- レベル1（表層）: 事実ベース（部活・成績・活動実績の客観情報）。差別化への寄与: 低
- レベル2（中層）: 経験＋感情・考察（「この経験で〇〇を痛感した」など）。差別化への寄与: 中
- レベル3（深層）: 弱み・葛藤・個人的背景（家族背景・挫折・内面の葛藤・マイノリティ性）。差別化への寄与: 高
- レベル4（過剰開示）: 私的すぎる情報・政治的主張・他者への攻撃的感情。差別化への寄与: 逆効果

【重要原則】
- レベル3 への開示を「強要」してはならない。踏み込みは受験生本人の判断
- 開示に消極的な場合は「情報密度」での差別化を代替案として示す
- レベル4 は警告する

出力は指定JSONのみ（コードブロック不要）。"""


_COMPETITION_SYSTEM_PROMPT = """あなたは総合型選抜の競合マップ診断の専門家です。
受験生のテーマと大学を照らし、「頻出テーマ」との照合と戦略提案を行います。

【頻出テーマ（同質化しやすい）】
社会貢献・国際貢献 / 地域活性化・地方創生 / 教育格差・教育改革 / 環境問題・SDGs /
AI・テクノロジーで社会変革 / グローバル人材 / 多様性・インクルージョン

【4つのケースと戦略】
- ケースA（実績強＋テーマ頻出）: 同じ土俵で戦う、切り口で差別化
- ケースB（実績弱＋テーマ頻出）: 土俵から降りる、受験生固有の背景から新テーマを発掘
- ケースC（実績問わず＋テーマ稀少）: 現状維持、希少性を磨く
- ケースD（実績強＋テーマ稀少）: 完成度を上げる

【重要】受験生の経験プロファイルと希望テーマを見て、ケース判定 → 戦略 → 具体プロセス の順で出す。
出力は指定JSONのみ（コードブロック不要）。"""


_DENSITY_SYSTEM_PROMPT = """あなたは総合型選抜の志望理由書の「情報密度」評価の専門家です。
提出された文章の調べ込み度（固有名詞・数字・引用・内部情報・専門用語）を測ります。

【密度スコア計算（参考）】
情報密度 = (固有名詞数 × 2 + 数字データ数 × 1 + 引用数 × 3 + 内部情報数 × 2 + 専門用語数 × 1) / (字数 / 100)

【解釈】
- 20以上: 情報で希少性が出ている、強い
- 10-19: 標準的、追加必要
- 10未満: 一般論、同質化

【追加質的チェック】
- 固有名詞が並列か、解釈・接続が入っているか
- 引用が飾りか、主張の根拠として機能しているか
- 専門用語を理解して使っているか丸暗記か

出力は指定JSONのみ（コードブロック不要）。"""


@router.post("/disclosure_level", summary="v2診断3: 自己開示レベル判定")
@limiter.limit("30/minute")
async def diagnose_disclosure(request: Request, body: DisclosureLevelRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        student = _load_student(db, body.student_id, user["user_id"]) if body.student_id else None
    finally:
        db.close()

    student_part = ""
    if student:
        student_part = f"=== 受験生プロファイル（参考） ===\n経験: {student.get('profile_experience','')[:600]}\n\n"

    user_prompt = f"""{student_part}=== 評価対象（志望理由書/活動報告等のドラフト）===
{body.draft_text[:6000]}

=== 出力（JSONのみ）===
{{
  "level": "1|2|3|4",
  "level_label": "表層|中層|深層|過剰開示",
  "differentiation_impact": "低|中|高|逆効果",
  "reasoning": "このレベル判定の根拠（300-500字）",
  "indicators": ["レベル判定の手がかりとなった具体的表現3-5個"],
  "level_4_warnings": ["過剰開示の疑いがある箇所（レベル4のみ。該当なしは空配列）"],
  "advice": "改善/維持のアドバイス。レベル3の強要は避ける",
  "alternative_differentiation": "開示に消極的な場合の代替戦略（情報密度・固有名詞での差別化など）"
}}"""

    try:
        parsed, resp = llm_router.call_json(
            task="analysis", system=_DISCLOSURE_SYSTEM_PROMPT,
            user=user_prompt, max_tokens=3000,
        )
    except llm_router.NoProviderAvailable as e:
        raise HTTPException(503, f"LLM プロバイダ利用不可: {e}")
    if "_raw" in parsed:
        raise HTTPException(502, f"JSON パース失敗: {parsed['_raw'][:300]}")

    return {
        "student": ({"id": student["id"], "name": student["name"]} if student else None),
        "draft_length": len(body.draft_text),
        "diagnosis": parsed,
        "model": {"provider": resp.provider, "model": resp.model},
    }


@router.post("/competition_map", summary="v2診断4: 競合マップ診断")
@limiter.limit("30/minute")
async def diagnose_competition(request: Request, body: CompetitionMapRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        student = _load_student(db, body.student_id, user["user_id"])
        research = _load_research(db, body.request_id, user["user_id"]) if body.request_id else None
    finally:
        db.close()

    if not any(student.get(f, "").strip() for f in
               ("profile_experience", "profile_concerns", "profile_future")):
        raise HTTPException(400, "受験生プロファイル未入力です")

    uni_context = ""
    if research:
        ud = research["result"].get("university_data") or {}
        unis = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
        u = unis[0] if unis else {}
        uni_context = f"""=== 対象大学 ===
{u.get('university','')} {u.get('faculty','')} {u.get('department','')}
特徴: {u.get('features','')}
独自性レイヤー: {json.dumps(u.get('uniqueness_layers', {}), ensure_ascii=False)[:2000]}
"""

    user_prompt = f"""=== 受験生プロファイル ===
経験: {student.get('profile_experience','').strip()[:1500]}

問題意識・関心分野: {student.get('profile_concerns','').strip()[:1500]}

将来像: {student.get('profile_future','').strip()[:800]}

{uni_context}
=== 出力（JSONのみ）===
{{
  "student_theme_summary": "受験生が掲げているテーマ・関心領域を1-2文で要約",
  "frequent_theme_overlap": [
    {{"frequent_theme": "社会貢献・国際貢献|地域活性化|教育格差|環境問題・SDGs|AI|グローバル人材|多様性 等", "overlap_level": "高|中|低", "note": "どの要素が重なるか"}}
  ],
  "achievement_level_assessment": "実績強|実績中|実績弱 （プロファイルから判定）",
  "case": "A: 実績強＋テーマ頻出|B: 実績弱＋テーマ頻出|C: 実績問わず＋テーマ稀少|D: 実績強＋テーマ稀少",
  "recommended_strategy": "同じ土俵で切り口差別化|土俵転換|現状維持・磨き込み|完成度を上げる",
  "concrete_steps": [
    "戦略を具体化する3-5個のアクション"
  ],
  "topic_shift_candidates": [
    {{"theme": "受験生固有の背景から発掘した新テーマ候補", "why_fits_student": "なぜこの受験生に合うか", "why_fits_university": "この大学で扱えるか"}}
  ],
  "warnings": ["ケース別の注意点（複数可）"]
}}"""

    try:
        parsed, resp = llm_router.call_json(
            task="analysis", system=_COMPETITION_SYSTEM_PROMPT,
            user=user_prompt, max_tokens=4500,
        )
    except llm_router.NoProviderAvailable as e:
        raise HTTPException(503, f"LLM プロバイダ利用不可: {e}")
    if "_raw" in parsed:
        raise HTTPException(502, f"JSON パース失敗: {parsed['_raw'][:300]}")

    return {
        "student":  {"id": student["id"], "name": student["name"]},
        "request":  (research["request"] if research else None),
        "diagnosis": parsed,
        "model": {"provider": resp.provider, "model": resp.model},
    }


@router.post("/density_check", summary="v2診断5: 情報密度チェック")
@limiter.limit("30/minute")
async def diagnose_density(request: Request, body: DensityCheckRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        research = _load_research(db, body.request_id, user["user_id"]) if body.request_id else None
    finally:
        db.close()

    uni_context = ""
    if research:
        ud = research["result"].get("university_data") or {}
        unis = (ud.get("step_c") or {}).get("universities") or ud.get("universities") or []
        u = unis[0] if unis else {}
        uni_context = f"""=== 対象大学（密度評価の参考）===
{u.get('university','')} {u.get('faculty','')} {u.get('department','')}
specific_materials: {json.dumps(u.get('specific_materials', {}), ensure_ascii=False)[:2000]}
key_phrases: {u.get('key_phrases', [])}
"""

    user_prompt = f"""{uni_context}
=== 評価対象（志望理由書/面接回答ドラフト）===
{body.draft_text[:6500]}

=== 出力（JSONのみ）===
{{
  "total_chars": "ドラフトの実文字数",
  "counts": {{
    "named_entities":  "固有名詞の数（教員名/科目名/制度名/研究室名など）",
    "numbers_data":    "数字・データ（統計・期間・規模・回数）の数",
    "citations":       "引用の深さ（書名・論文タイトル・発言者特定）の数",
    "internal_info":   "大学内部情報（OC体験談・在学生の声・深い階層の情報）の数",
    "technical_terms": "専門用語の数"
  }},
  "density_score": "計算式に基づくスコア（整数）",
  "interpretation": "20以上=強い / 10-19=標準・追加必要 / 10未満=一般論 の判定コメント",
  "found_entities": {{
    "named_entities":  ["実際に検出できた固有名詞"],
    "numbers_data":    ["実際に検出できた数字・データ"],
    "citations":       ["実際に検出できた引用"],
    "internal_info":   ["実際に検出できた内部情報"],
    "technical_terms": ["実際に検出できた専門用語"]
  }},
  "quality_issues": [
    "固有名詞が列挙だけで解釈が無い|引用が飾りに留まる|専門用語が丸暗記で使われている 等の質的問題"
  ],
  "improvement_suggestions": [
    {{"action": "追加すべき固有名詞/数字/引用などの具体提案", "why": "どのフィールドの密度が上がるか"}}
  ]
}}"""

    try:
        parsed, resp = llm_router.call_json(
            task="analysis", system=_DENSITY_SYSTEM_PROMPT,
            user=user_prompt, max_tokens=4000,
        )
    except llm_router.NoProviderAvailable as e:
        raise HTTPException(503, f"LLM プロバイダ利用不可: {e}")
    if "_raw" in parsed:
        raise HTTPException(502, f"JSON パース失敗: {parsed['_raw'][:300]}")

    return {
        "request":  (research["request"] if research else None),
        "draft_length": len(body.draft_text),
        "diagnosis": parsed,
        "model": {"provider": resp.provider, "model": resp.model},
    }


# ── Feature 3: 複数大学の一括診断ダッシュボード ─────────────────────────────

@router.post("/batch_match", summary="一括診断: 生徒の候補校すべてに Mode 3 を実行")
@limiter.limit("5/minute")
async def diagnose_batch_match(request: Request, body: BatchMatchRequest, ctx: dict = Depends(require_plan("premium"))):
    user = ctx["user"]
    set_llm_context(user_id=user["user_id"], request_id=str(uuid.uuid4()))
    db = get_db()
    try:
        student = _load_student(db, body.student_id, user["user_id"])
        if not any(student.get(f, "").strip() for f in
                   ("profile_experience", "profile_concerns", "profile_future")):
            raise HTTPException(400, "受験生プロファイル未入力です")
        # student に紐づく saved_items + done のリサーチだけ
        rows = db.execute(
            """SELECT si.id AS saved_id, si.request_id, si.memo,
                      rr.university, rr.faculty, rr.department
                 FROM saved_items si
                 JOIN research_requests rr ON rr.id = si.request_id
                WHERE si.student_id = ? AND si.user_id = ? AND rr.status = 'done'
                ORDER BY si.created_at DESC
                LIMIT ?""",
            (body.student_id, user["user_id"], body.max_universities),
        ).fetchall()
        saved_list = [dict(r) for r in rows]
    finally:
        db.close()

    if not saved_list:
        raise HTTPException(400, "この生徒に紐づく「完了」済み候補校がありません。先に /app/research で調査し保存してください")

    results: list[dict] = []
    errors: list[dict] = []
    for item in saved_list:
        try:
            # 各リサーチの結果を取得
            db2 = get_db()
            try:
                research = _load_research(db2, item["request_id"], user["user_id"])
            finally:
                db2.close()

            user_prompt = _build_user_prompt(student, research)
            parsed, resp = llm_router.call_json(
                task="analysis",
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=12000,
            )
            if "_raw" in parsed:
                errors.append({"request_id": item["request_id"], "error": "JSON パース失敗"})
                continue

            ms = parsed.get("match_score", {}) or {}
            ns = parsed.get("necessity_score", {}) or {}
            mp = parsed.get("map_position", {}) or {}
            def _total(obj):
                t = obj.get("total")
                try: return int(str(t).strip())
                except Exception: return None

            results.append({
                "request_id": item["request_id"],
                "university": item["university"],
                "faculty":    item["faculty"],
                "department": item["department"],
                "memo":       item.get("memo") or "",
                "match_total":     _total(ms),
                "necessity_total": _total(ns),
                "map_position_type": mp.get("type"),
                "map_position_note": mp.get("note"),
                "strong_connections": parsed.get("strong_connections") or [],
                "weak_points":        parsed.get("weak_points") or [],
                "next_actions":       parsed.get("next_actions") or [],
                "model": {"provider": resp.provider, "model": resp.model},
            })
        except HTTPException:
            raise
        except Exception as e:
            errors.append({"request_id": item["request_id"], "error": str(e)[:200]})

    # スコア順にソート (match_total + necessity_total)
    def _sort_key(r):
        m = r.get("match_total") or 0
        n = r.get("necessity_total") or 0
        return -(m + n)
    results.sort(key=_sort_key)

    return {
        "student": {"id": student["id"], "name": student["name"]},
        "total_universities": len(saved_list),
        "succeeded": len(results),
        "failed":    len(errors),
        "errors":    errors,
        "results":   results,
    }
