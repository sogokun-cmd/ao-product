#!/usr/bin/env python3
"""
E2Eテスト結果とエラーログを受け取り、Claudeに修正案を依頼して適用する。
Usage: python3 auto_fix.py '<issues_json>'
"""
import sys
import json
import os
import subprocess
import re
import urllib.request
import urllib.error

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE_FILES = [
    "main.py",
    "api/research.py",
    "api/billing.py",
    "api/admin.py",
    "auth/routes.py",
    "auth/email_auth.py",
    "auth/deps.py",
    "core/llm_router.py",
    "worker.py",
]


def read_source_files():
    parts = []
    for rel_path in SOURCE_FILES:
        full_path = os.path.join(REPO_ROOT, rel_path)
        if os.path.exists(full_path):
            content = open(full_path, encoding="utf-8").read()
            parts.append(f"### {rel_path}\n```python\n{content}\n```")
    return "\n\n".join(parts)


def call_claude(prompt: str) -> str:
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["content"][0]["text"]


def apply_fix(file_path: str, new_content: str):
    full_path = os.path.join(REPO_ROOT, file_path)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"Applied fix to: {file_path}")


def parse_fixes(response: str) -> list[dict]:
    """Claudeのレスポンスからファイル修正を抽出する"""
    fixes = []
    # FIX_FILE: path/to/file.py の後にコードブロックがある形式
    pattern = r"FIX_FILE:\s*(\S+)\n```(?:python)?\n(.*?)```"
    matches = re.findall(pattern, response, re.DOTALL)
    for file_path, content in matches:
        fixes.append({"file": file_path.strip(), "content": content})
    return fixes


def main():
    if len(sys.argv) < 2:
        print("Usage: auto_fix.py '<issues_json>'")
        sys.exit(1)

    issues = json.loads(sys.argv[1])
    print(f"Issues to fix: {json.dumps(issues, ensure_ascii=False, indent=2)}")

    source_code = read_source_files()

    prompt = f"""あなたはAOリサーチ（FastAPI Webアプリ）の自動修正エージェントです。

## 検出された問題
{json.dumps(issues, ensure_ascii=False, indent=2)}

## ソースコード
{source_code}

## 指示
上記の問題を分析し、**1ファイル以内**で修正できる軽微なバグのみ修正してください。

修正できる場合は以下の形式で返答してください：
FIX_FILE: path/to/file.py
```python
（修正後のファイル全体のコード）
```

COMMIT_MESSAGE: fix: （修正内容の1行説明）

修正できない場合（複数ファイルにまたがる・ロジックの大幅変更が必要）は：
CANNOT_FIX: （理由）

重要な制約:
- セキュリティを下げる変更は絶対にしない
- テストデータやハードコードされた値を本番コードに入れない
- 修正は最小限に留める
"""

    print("Calling Claude API...")
    response = call_claude(prompt)
    print(f"Claude response:\n{response[:500]}...")

    if "CANNOT_FIX:" in response:
        reason = re.search(r"CANNOT_FIX:\s*(.+)", response)
        print(f"Cannot fix automatically: {reason.group(1) if reason else 'unknown'}")
        print(f"CANNOT_FIX:{reason.group(1) if reason else 'requires manual review'}")
        sys.exit(0)

    fixes = parse_fixes(response)
    if not fixes:
        print("No fixes found in response")
        sys.exit(0)

    commit_msg_match = re.search(r"COMMIT_MESSAGE:\s*(.+)", response)
    commit_msg = commit_msg_match.group(1).strip() if commit_msg_match else "fix: 自動修正"

    for fix in fixes:
        apply_fix(fix["file"], fix["content"])

    # git commit & push
    subprocess.run(["git", "config", "user.email", "noreply@ao.helphero.jp"], cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "config", "user.name", "ao-daily-monitor"], cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        print("Nothing to commit")
        sys.exit(0)

    subprocess.run(["git", "push", "origin", "main"], cwd=REPO_ROOT, check=True)
    print(f"AUTO_FIXED:{commit_msg}")


if __name__ == "__main__":
    main()
