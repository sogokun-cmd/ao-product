"""PDF直接アップロードエンドポイント"""
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from slowapi import Limiter
from slowapi.util import get_remote_address
from auth.deps import require_user

router = APIRouter(prefix="/api", tags=["upload"])
limiter = Limiter(key_func=get_remote_address)

_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20MB


@router.post("/upload/pdf", summary="PDFをアップロードしてテキスト抽出")
@limiter.limit("20/minute")
async def upload_pdf(request: Request, file: UploadFile = File(...)):
    require_user(request)

    if file.content_type not in ("application/pdf", "application/octet-stream"):
        # ファイル名でも判定
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="PDFファイルのみアップロード可能です")

    raw = await file.read()
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDFは20MB以下にしてください")

    try:
        import pdfplumber  # type: ignore
        import io
        text_parts = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages[:80]:  # 最大80ページ
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        text = "\n".join(text_parts)
    except ImportError:
        raise HTTPException(status_code=500, detail="PDF処理ライブラリが未インストールです")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"PDF解析失敗: {e}")

    if not text.strip():
        raise HTTPException(status_code=422, detail="テキストを抽出できませんでした（画像PDFまたは暗号化PDFの可能性があります）")

    return {
        "text": text[:100000],
        "char_count": len(text),
        "pages": len(text_parts),
        "truncated": len(text) > 100000,
    }
