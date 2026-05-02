"""
過去問テキスト抽出 + 複数LLM 抽象レイヤー経由の分析（断定回避トーン）

モデル依存を排し、core/llm_router.py 経由で呼び出す。
プロバイダ未構成時は使えるものに自動フォールバック。
"""
from __future__ import annotations

import io

from core.llm_router import call, call_json


def extract_text_from_pdf(data: bytes, max_chars: int = 60000) -> str:
    # 1. pdfplumber でテキスト抽出を試みる
    text = _extract_with_pdfplumber(data, max_chars)
    # 2. テキストが少なすぎる場合はスキャンPDFと判断しOCRを試みる
    if len(text.strip()) < 100:
        ocr_text = _extract_with_ocr(data, max_chars)
        if len(ocr_text.strip()) > len(text.strip()):
            return ocr_text
    return text


def _extract_with_pdfplumber(data: bytes, max_chars: int) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""
    out: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t:
                out.append(t)
            if sum(len(x) for x in out) > max_chars:
                break
    return ("\n\n".join(out))[:max_chars]


def _extract_with_ocr(data: bytes, max_chars: int) -> str:
    """pypdfium2 でページを画像化し、pytesseract で OCR"""
    try:
        import pypdfium2 as pdfium
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    out: list[str] = []
    try:
        pdf = pdfium.PdfDocument(data)
        for i in range(min(len(pdf), 30)):
            page = pdf[i]
            bitmap = page.render(scale=2)
            img = bitmap.to_pil()
            text = pytesseract.image_to_string(img, lang="jpn+eng")
            if text.strip():
                out.append(text.strip())
            if sum(len(x) for x in out) > max_chars:
                break
    except Exception:
        return ""
    return ("\n\n".join(out))[:max_chars]


_ANALYSIS_SYSTEM = """あなたは日本の大学入試（総合型選抜・AO入試）の過去問分析者です。
重要原則:
- 与えられた過去問テキストのみから読み取れる事実だけで分析する
- 推測・断定を避け、「〜の傾向が見られる可能性が高い」「〜の形式が多い印象」など慎重な表現を使う
- 一般論や予想は混ぜない
- 確証がない事項は confidence_caveats に列挙する
- 出力は厳密に JSON のみで返す（前後の説明文不要）"""


def analyze_past_exam(text: str) -> dict:
    if not text.strip():
        return {"error": "テキストが空です"}
    user = f"""以下の過去問テキストを分析し、JSON で返してください。

スキーマ:
{{
  "exam_format": [出題形式の特徴を文字列配列],
  "typical_themes": [頻出テーマを文字列配列],
  "skills_required": [問われやすい力を文字列配列],
  "patterns_observed": [傾向を文字列配列・断定回避],
  "confidence_caveats": [このテキストだけでは断定できない注意点]
}}

過去問テキスト（抜粋）:
---
{text[:30000]}
---"""
    data, _resp = call_json("analysis", _ANALYSIS_SYSTEM, user, max_tokens=2500)
    return data


_PRACTICE_SYSTEM = """あなたは過去問の出題傾向を踏まえて練習問題を作るプロです。
原則:
- 過去問の形式に近いが、コピーではない問題を作る
- 想像で大学・学部の固有事情を補完しない
- JSON 出力のみ"""


def generate_practice(text: str, analysis: dict, n: int = 3) -> dict:
    import json
    user = f"""分析結果と過去問を踏まえて、類題を {n} 問作成してください。

スキーマ:
{{
  "practice_questions": [
    {{ "title": "...", "prompt": "...", "estimated_minutes": 30, "tips_for_teaching": "..." }}
  ]
}}

分析結果:
{json.dumps(analysis, ensure_ascii=False)[:4000]}

過去問抜粋:
---
{text[:15000]}
---"""
    data, _resp = call_json("practice_generation", _PRACTICE_SYSTEM, user, max_tokens=3000)
    return data


_NOTES_SYSTEM = """あなたは塾講師向けに「指導用メモ」を作成するプロです。
原則:
- 指導の論点・つまずきやすい点・チェックリスト・声かけ例を列挙
- 断定的な大学評価は避ける
- マークダウン形式で簡潔に"""


def generate_teaching_notes(text: str, analysis: dict) -> str:
    import json
    user = f"""分析結果と過去問を踏まえて、塾講師向けの指導メモをマークダウンで作成してください。

分析結果:
{json.dumps(analysis, ensure_ascii=False)[:4000]}

過去問抜粋:
---
{text[:10000]}
---"""
    resp = call("notes_generation", _NOTES_SYSTEM, user, max_tokens=2500, temperature=0.3)
    return resp.text
