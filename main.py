"""GEO Audit Tool — AI Readability Analytics

모델 사용 가이드:
  - 개발(Development): Claude Opus (claude-opus-4-6) — 코드 작성, 리팩토링, 디버깅
  - 운영(Production):  Claude Sonnet (claude-sonnet-4-6) — 코드 리뷰, 모니터링, 경량 작업
"""

import asyncio
import hmac
import ipaddress
import json as _json
import os
import re
import socket
from contextlib import asynccontextmanager
from typing import List
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from analyzer import analyze_url, get_scoring_config, save_scoring_config, get_default_config, load_scoring_config
from rule_engine import RULE_TYPES



async def _install_chromium_on_startup():
    """서버 시작 시 Playwright Chromium 자동 설치."""
    import subprocess, sys
    python = sys.executable or "python"
    try:
        # 이미 설치되어 있는지 확인
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if os.path.exists(path):
                print(f"[startup] Chromium already installed: {path}")
                return
    except Exception:
        pass
    print("[startup] Installing Chromium...")
    for cmd in [
        [python, "-m", "playwright", "install", "chromium", "--with-deps"],
        [python, "-m", "playwright", "install", "chromium"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                print(f"[startup] Chromium installed successfully")
                return
        except Exception as e:
            print(f"[startup] Install attempt failed: {e}")
    print("[startup] WARNING: Chromium installation failed — CSR analysis will be unavailable")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan 이벤트 핸들러."""
    await _install_chromium_on_startup()
    # 정기 Audit 스케줄러 시작
    try:
        from scheduler import start_scheduler, shutdown_scheduler
        start_scheduler()
    except Exception as e:
        print(f"[startup] scheduler 시작 실패: {e}")
        shutdown_scheduler = None  # type: ignore[assignment]
    yield
    if shutdown_scheduler:
        try:
            shutdown_scheduler()
        except Exception:
            pass


app = FastAPI(title="GEO Audit Tool", version="2.23.0", lifespan=lifespan)

# Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "서버 내부 오류가 발생했습니다. 잠시 후 다시 시도해주세요."},
    )

# CORS — 기본값은 자기 자신만 허용
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
        "https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
        "https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-src https://www.googletagmanager.com"
    )
    return response


URL_PATTERN = re.compile(r"^(https?://)?[\w\-.]+(\.[\w\-]+)+([\w\-._~:/?#\[\]@!$&'()*+,;=%]*)?$")


def _is_private_url(url: str) -> bool:
    """SSRF 방지: 내부/프라이빗 IP 주소 접근을 차단합니다."""
    try:
        parsed = urlparse(url if url.startswith(("http://", "https://")) else f"https://{url}")
        hostname = parsed.hostname
        if not hostname:
            return True
        # localhost 차단
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        # IP 주소인 경우 private 범위 체크
        try:
            addr = ipaddress.ip_address(hostname)
            return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        except ValueError:
            pass
        # 도메인인 경우 DNS resolve 후 체크
        try:
            resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for _, _, _, _, sockaddr in resolved:
                addr = ipaddress.ip_address(sockaddr[0])
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    return True
        except (socket.gaierror, ValueError, OSError):
            return True  # DNS 실패 시 안전하게 차단
        return False
    except Exception:
        return True


VALID_SCOPES = {"all", "schema", "seo", "faq"}


class AnalyzeRequest(BaseModel):
    url: str
    scope: str = "all"


class AnalyzeBulkRequest(BaseModel):
    urls: List[str]
    scope: str = "all"


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return FileResponse("static/index.html")


@app.post("/analyze")
@limiter.limit("30/minute")
async def analyze(request: Request, body: AnalyzeRequest):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL을 입력해주세요.")
    if not URL_PATTERN.match(url):
        raise HTTPException(status_code=400, detail="유효하지 않은 URL입니다.")
    if _is_private_url(url):
        raise HTTPException(status_code=400, detail="내부 네트워크 주소는 분석할 수 없습니다.")
    scope = body.scope if body.scope in VALID_SCOPES else "all"
    try:
        result = await analyze_url(url, scope=scope)
        return result
    except Exception:
        raise HTTPException(status_code=500, detail="분석 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


@app.post("/analyze-bulk")
@limiter.limit("5/minute")
async def analyze_bulk(request: Request, body: AnalyzeBulkRequest):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="URL을 하나 이상 입력해주세요.")
    if len(urls) > 1000:
        raise HTTPException(status_code=400, detail="한 번에 최대 1000개 URL까지 분석할 수 있습니다.")

    invalid = [u for u in urls if not URL_PATTERN.match(u)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"유효하지 않은 URL: {invalid[0]}")

    private = [u for u in urls if _is_private_url(u)]
    if private:
        raise HTTPException(status_code=400, detail=f"내부 네트워크 주소는 분석할 수 없습니다: {private[0]}")

    BATCH_SIZE = 10  # 한 번에 10개씩 처리 (메모리 300MB 이하 유지)

    scope = body.scope if body.scope in VALID_SCOPES else "all"

    async def safe_analyze(url: str):
        try:
            return {"url": url, "result": await analyze_url(url, lightweight=True, scope=scope), "error": None}
        except Exception:
            return {"url": url, "result": None, "error": "분석 중 오류가 발생했습니다."}

    # 배치 단위로 순차 처리 — 메모리 누적 방지
    items = []
    for i in range(0, len(urls), BATCH_SIZE):
        batch = urls[i:i + BATCH_SIZE]
        batch_results = await asyncio.gather(*[safe_analyze(u) for u in batch])
        items.extend(batch_results)

    success_count = sum(1 for i in items if i["result"])

    if scope == "all":
        scores = [i["result"]["score"]["total"] for i in items if i["result"]]
        average = round(sum(scores) / len(scores), 1) if scores else 0
    else:
        average = None

    return {"items": items, "average": average, "total": len(items), "success": success_count, "scope": scope}


# ── Admin API ────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def _verify_admin(request: Request) -> bool:
    """Authorization 헤더에서 Bearer 토큰을 검증합니다."""
    if not ADMIN_PASSWORD:
        return False
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    return hmac.compare_digest(token, ADMIN_PASSWORD)


@app.api_route("/admin", methods=["GET", "HEAD"])
async def admin_page():
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=404, detail="어드민이 비활성화되어 있습니다.")
    return FileResponse("static/admin.html")


@app.post("/admin/login")
async def admin_login(request: Request):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=404, detail="어드민이 비활성화되어 있습니다.")
    body = await request.json()
    password = body.get("password", "")
    if not password:
        raise HTTPException(status_code=401, detail="비밀번호를 입력해주세요.")
    if hmac.compare_digest(password, ADMIN_PASSWORD):
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")


@app.get("/admin/config")
async def get_config(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return get_scoring_config()


@app.put("/admin/config")
async def update_config(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    body = await request.json()
    save_scoring_config(body)
    return {"status": "ok", "config": get_scoring_config()}


@app.post("/admin/config/reset")
async def reset_config(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    default = get_default_config()
    save_scoring_config(default)
    return {"status": "ok", "config": default}


@app.get("/admin/rule-types")
async def get_rule_types(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return RULE_TYPES


_AUDIT_CRITERIA_DOC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "audit-criteria.md")


@app.get("/admin/audit-criteria-doc")
async def get_audit_criteria_doc(request: Request):
    """audit-criteria.md를 어드민에 노출 (룰 기준 reference)."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        with open(_AUDIT_CRITERIA_DOC_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return {"status": "ok", "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="audit-criteria.md 파일이 없습니다.")


# ── Audit Groups & Schedules ─────────────────────────────────────────────────

_AUDIT_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_data.json")
_audit_data_lock = asyncio.Lock()


def _load_audit_data() -> dict:
    try:
        with open(_AUDIT_DATA_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return {"groups": [], "schedules": []}


async def _save_audit_data(data: dict):
    async with _audit_data_lock:
        with open(_AUDIT_DATA_PATH, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)


@app.get("/admin/audit-data")
async def get_audit_data(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return _load_audit_data()


@app.put("/admin/audit-data")
async def update_audit_data(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    body = await request.json()
    await _save_audit_data(body)
    # 스케줄 변경이 있을 수 있으므로 스케줄러 재로드
    try:
        from scheduler import reload_schedules
        n = reload_schedules()
    except Exception as e:
        print(f"[admin] reload_schedules 실패: {e}")
        n = 0
    return {"status": "ok", "data": body, "active_schedules": n}


@app.post("/admin/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, request: Request):
    """스케줄을 즉시 1회 실행."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        from scheduler import trigger_now
        result = await trigger_now(schedule_id)
        return {"status": "ok", "run": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"실행 실패: {e}")


@app.get("/admin/schedules/runs")
async def get_schedule_runs(request: Request, schedule_id: str = None, limit: int = 20):
    """최근 정기 Audit 실행 결과 조회."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        from scheduler import get_recent_runs
        runs = get_recent_runs(schedule_id=schedule_id, limit=limit)
        return {"runs": runs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {e}")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
