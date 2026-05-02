"""
AOリサーチ — 一次情報ベースの総合型選抜リサーチ + 過去問分析 + チーム共有

起動:
    cd /mnt/g/マイドライブ/.claude/scripts/ao-product
    uvicorn main:app --reload --port 8000
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import sentry_sdk
if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
        traces_sample_rate=0.1,
        environment=os.environ.get("ENVIRONMENT", "development"),
    )

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, PlainTextResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from api.research   import router as research_router
from api.saved      import router as saved_router
from api.plans      import router as plans_router
from api.students   import router as students_router
from api.analysis   import router as analysis_router
from api.teams      import router as teams_router
from api.billing    import router as billing_router
from api.diagnosis  import router as diagnosis_router
from api.knowledge  import router as knowledge_router
from api.upload     import router as upload_router
from api.admin      import router as admin_router
from auth.routes        import router as auth_router
from auth.mobile_routes import router as mobile_auth_router
from auth.deps         import get_current_user
from database     import init_db

ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

app = FastAPI(
    title="AOリサーチ",
    description="一次情報ベースで総合型選抜の情報を整理するリサーチツール",
    version="2.0.0",
    docs_url="/docs" if ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if ENVIRONMENT != "production" else None,
)

# ── CORS ───────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

# ── Rate Limiter ───────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── カスタムエラーページ ─────────────────────────────────────────────────────
from fastapi import Request as _Req
from fastapi.responses import HTMLResponse as _HTML
from starlette.exceptions import HTTPException as _HTTPExc

@app.exception_handler(_HTTPExc)
async def http_exception_handler(request: _Req, exc: _HTTPExc):
    if exc.status_code == 404:
        try:
            content = (_STATIC_DIR / "404.html").read_text(encoding="utf-8")
            return _HTML(content=content, status_code=404)
        except Exception:
            return _HTML(content="<h1>404 Not Found</h1>", status_code=404)
    if exc.status_code >= 500:
        try:
            content = (_STATIC_DIR / "500.html").read_text(encoding="utf-8")
            return _HTML(content=content, status_code=exc.status_code)
        except Exception:
            return _HTML(content="<h1>Server Error</h1>", status_code=exc.status_code)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: _Req, exc: Exception):
    import logging, traceback
    logging.getLogger("ao").error("Unhandled: %s\n%s", exc, traceback.format_exc())
    try:
        content = (_STATIC_DIR / "500.html").read_text(encoding="utf-8")
        return _HTML(content=content, status_code=500)
    except Exception:
        return _HTML(content="<h1>Server Error</h1>", status_code=500)


# ── セキュリティヘッダー ──────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.on_event("startup")
async def startup():
    init_db()
    import asyncio, concurrent.futures
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="default")
    )
    from worker import start_background_worker
    start_background_worker()


@app.on_event("shutdown")
async def shutdown():
    from worker import stop_background_worker
    stop_background_worker()


# API ルーター
app.include_router(research_router)
app.include_router(saved_router)
app.include_router(plans_router)
app.include_router(students_router)
app.include_router(analysis_router)
app.include_router(teams_router)
app.include_router(billing_router)
app.include_router(diagnosis_router)
app.include_router(knowledge_router)
app.include_router(upload_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(mobile_auth_router)


_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.mount("/shared", StaticFiles(directory=str(_STATIC_DIR / "shared")), name="shared")


# ── 公開ページ ──────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def lp():
    return FileResponse(str(_STATIC_DIR / "lp.html"))


@app.get("/pricing", include_in_schema=False)
async def pricing():
    return FileResponse(str(_STATIC_DIR / "pricing.html"))


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(str(_STATIC_DIR / "login.html"))


@app.get("/register", include_in_schema=False)
async def register_page():
    return FileResponse(str(_STATIC_DIR / "register.html"))


# ── 招待リンク（要ログイン） ────────────────────────────────────────────────

@app.get("/invite/{token}", include_in_schema=False)
async def invite_page(token: str, request: Request):
    if not get_current_user(request):
        return RedirectResponse(url=f"/login?next=/invite/{token}", status_code=302)
    return FileResponse(str(_STATIC_DIR / "invite.html"))


# ── アプリページ（要ログイン） ──────────────────────────────────────────────

def _gated(filename: str):
    async def handler(request: Request):
        if not get_current_user(request):
            return RedirectResponse(url="/login", status_code=302)
        return FileResponse(str(_STATIC_DIR / filename))
    return handler


app.get("/app", include_in_schema=False)(_gated("app.html"))
app.get("/app/research", include_in_schema=False)(_gated("research.html"))
app.get("/app/research/{rid}", include_in_schema=False)(_gated("research.html"))
app.get("/app/saved", include_in_schema=False)(_gated("saved.html"))
app.get("/app/students", include_in_schema=False)(_gated("students.html"))
app.get("/app/tutor/analysis", include_in_schema=False)(_gated("tutor.html"))
app.get("/app/team", include_in_schema=False)(_gated("team.html"))
app.get("/app/account", include_in_schema=False)(_gated("account.html"))
@app.get("/terms",   include_in_schema=False)
@app.get("/privacy", include_in_schema=False)
@app.get("/law",     include_in_schema=False)
async def legal_pages(request: Request):
    name = request.url.path.lstrip("/")
    return FileResponse(str(_STATIC_DIR / f"{name}.html"))

app.get("/app/compare", include_in_schema=False)(_gated("compare.html"))
app.get("/app/search", include_in_schema=False)(_gated("search.html"))
app.get("/app/admin/knowledge", include_in_schema=False)(_gated("knowledge.html"))


# ── SEO ────────────────────────────────────────────────────────────────────

@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /app/\n"
        "Disallow: /api/\n"
        "Sitemap: https://ao.helphero.jp/sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url><loc>https://ao.helphero.jp/</loc>'
        '<changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
        '  <url><loc>https://ao.helphero.jp/pricing</loc>'
        '<changefreq>monthly</changefreq><priority>0.8</priority></url>\n'
        '  <url><loc>https://ao.helphero.jp/register</loc>'
        '<changefreq>monthly</changefreq><priority>0.7</priority></url>\n'
        '  <url><loc>https://ao.helphero.jp/login</loc>'
        '<changefreq>monthly</changefreq><priority>0.5</priority></url>\n'
        '</urlset>'
    )
    return Response(content=xml, media_type="application/xml")
