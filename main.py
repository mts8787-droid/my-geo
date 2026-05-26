"""GEO Audit Tool — AI Readability Analytics

모델 사용 가이드:
  - 개발(Development): Claude Opus (claude-opus-4-6) — 코드 작성, 리팩토링, 디버깅
  - 운영(Production):  Claude Sonnet (claude-sonnet-4-6) — 코드 리뷰, 모니터링, 경량 작업
"""

import asyncio
import hmac
import httpx
import ipaddress
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import audit_store
import bulk_jobs
import sitemap_agent
from analyzer import analyze_url, get_scoring_config, save_scoring_config, get_default_config, load_scoring_config
from rule_engine import RULE_TYPES

log = logging.getLogger("geo_audit")



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


async def _startup_pull_audit_data() -> None:
    """시작 시 AUDIT_DATA_PEER_URL에서 audit_data + 보조 파일들 pull.
    Render 재배포로 휘발된 데이터를 Mac Mini에서 복구.
    """
    if not _AUDIT_DATA_PEER_URL or not _WORKER_SECRET:
        return
    headers = {"X-Worker-Secret": _WORKER_SECRET}

    # 1) audit_data (groups/schedules) — 핵심 데이터
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(f"{_AUDIT_DATA_PEER_URL}/admin/audit-data", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        await audit_store.save(data)
        log.info("startup pull audit_data 완료 — groups=%d, schedules=%d",
                 len(data.get("groups", [])), len(data.get("schedules", [])))
    except Exception as e:
        log.warning("startup pull audit_data 실패 (peer=%s): %s", _AUDIT_DATA_PEER_URL, e)

    # 2) 보조 파일들 — url_classifications, csr_baseline
    for endpoint, local_path, label in [
        ("/admin/url-classifications-raw", _URL_CLASSIFICATIONS_PATH, "url_classifications"),
        ("/admin/csr-baseline-raw",        _CSR_BASELINE_PATH,        "csr_baseline"),
    ]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(f"{_AUDIT_DATA_PEER_URL}{endpoint}", headers=headers)
                resp.raise_for_status()
                data = resp.json()
            if data:  # 빈 dict 받으면 덮어쓸 필요 없음
                _write_aux_file(local_path, data)
                log.info("startup pull %s 완료 — entries=%d", label, len(data))
        except Exception as e:
            log.warning("startup pull %s 실패: %s", label, e)


async def _peer_push_aux(endpoint: str, data: dict) -> None:
    """보조 파일을 Mac Mini로 즉시 push (변경 시 호출). best-effort, 실패해도 무시."""
    if not _AUDIT_DATA_PEER_URL or not _WORKER_SECRET:
        return
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.put(
                f"{_AUDIT_DATA_PEER_URL}{endpoint}",
                json=data,
                headers={"X-Worker-Secret": _WORKER_SECRET},
            )
            resp.raise_for_status()
            log.info("peer push %s 완료", endpoint)
    except Exception as e:
        log.warning("peer push %s 실패: %s", endpoint, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan 이벤트 핸들러."""
    # Chromium 설치는 백그라운드로 — 헬스체크 timeout 방지 (#5)
    asyncio.create_task(_install_chromium_on_startup())

    # AUDIT_DATA_PEER_URL 설정 시 peer(Mac Mini)에서 audit_data를 1회 pull
    await _startup_pull_audit_data()

    # 프록시 모드(WORKER_URL 설정)에서는 스케줄러를 띄우지 않는다 — 워커가 담당.
    scheduler_started = False
    if not _WORKER_URL:
        try:
            from scheduler import start_scheduler
            start_scheduler()
            scheduler_started = True
        except Exception as e:
            log.exception("scheduler 시작 실패: %s", e)
    else:
        log.info("WORKER_URL 설정됨 — 로컬 스케줄러 비활성 (워커가 담당)")

    # 워커 모드(WORKER_SECRET + HUB_URL): 허브에서 audit_data를 주기 동기.
    sync_task = None
    if _HUB_URL and _WORKER_SECRET and not _WORKER_URL:
        try:
            from worker_sync import start_hub_sync
            sync_task = asyncio.create_task(start_hub_sync(_HUB_URL, _WORKER_SECRET))
            log.info("Hub sync 시작 — HUB_URL=%s", _HUB_URL)
        except Exception as e:
            log.exception("Hub sync 시작 실패: %s", e)

    try:
        yield
    finally:
        if sync_task:
            sync_task.cancel()
        if scheduler_started:
            try:
                from scheduler import shutdown_scheduler
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

# CORS — ALLOWED_ORIGINS 미설정 시 "*"로 폴백 (운영에서는 명시적 origin 권장).
# allow_credentials는 사용하지 않으므로 "*"여도 자격 증명이 노출되진 않음 (#23).
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── 분산 모드 환경변수 ────────────────────────────────────────────────────────
# WORKER_URL  : Render(프록시)에 설정. 무거운 요청을 위임할 Mac Mini Funnel URL.
# WORKER_SECRET: 양쪽에 동일 값 설정. 프록시↔워커 통신에 X-Worker-Secret 헤더로 사용.
# HUB_URL     : Mac Mini(워커)에 설정. audit_data.json을 pull해올 Render URL.
_WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
_WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
_HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
# Render 등에서 시작 시 audit_data를 Mac Mini로부터 1회 pull. /analyze 프록시(WORKER_URL)와는 별개.
_AUDIT_DATA_PEER_URL = os.environ.get("AUDIT_DATA_PEER_URL", "").rstrip("/")
_WORKER_TIMEOUT_SEC = float(os.environ.get("WORKER_TIMEOUT_SEC", "180"))


async def _proxy_to_worker(
    path: str,
    body: Optional[dict] = None,
    method: str = "POST",
    params: Optional[dict] = None,
) -> dict:
    """WORKER_URL로 요청을 전달하고 JSON 응답을 반환.

    호출자(/analyze 등)가 WORKER_URL 설정 여부를 확인한 뒤 사용.
    """
    headers = {"X-Worker-Secret": _WORKER_SECRET} if _WORKER_SECRET else {}
    url = f"{_WORKER_URL}{path}"
    async with httpx.AsyncClient(timeout=_WORKER_TIMEOUT_SEC, follow_redirects=True) as client:
        if method.upper() == "GET":
            resp = await client.get(url, params=params, headers=headers)
        else:
            resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


@app.middleware("http")
async def worker_auth_middleware(request: Request, call_next):
    """X-Worker-Secret 검증 + 워커 모드에선 /analyze 보호.

    - X-Worker-Secret 헤더가 일치하면 request.state.worker_authorized = True
      → _verify_admin 통과 + 워커 모드 /analyze 통과
    - 워커 모드(HUB_URL 설정된 머신 — Mac Mini): /analyze는 시크릿 없이는 401.
      Funnel URL이 공개되어 있어도 외부에서 무단 /analyze DoS 방지.
    """
    if _WORKER_SECRET:
        provided = request.headers.get("x-worker-secret", "")
        if provided and hmac.compare_digest(provided, _WORKER_SECRET):
            request.state.worker_authorized = True

    is_worker_mode = bool(_HUB_URL) and bool(_WORKER_SECRET)
    if is_worker_mode and request.url.path.startswith("/analyze"):
        if not getattr(request.state, "worker_authorized", False):
            return JSONResponse(
                {"detail": "Worker 모드 — X-Worker-Secret 헤더가 필요합니다."},
                status_code=401,
            )

    return await call_next(request)


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


def _is_valid_url(url: str) -> bool:
    """간단한 URL 유효성 검사. 실제 SSRF 차단은 _is_private_url이 담당 (#17).

    - scheme 없으면 https로 보정한 뒤 urlparse가 hostname을 뽑을 수 있는지 확인
    - hostname에 점(.) 이 1개 이상 있어야 함 (TLD 요구)
    - whitespace, control char, scheme이 http/https가 아니면 거절
    """
    if not url or any(c.isspace() for c in url) or "\x00" in url:
        return False
    candidate = url if url.startswith(("http://", "https://")) else f"https://{url}"
    try:
        p = urlparse(candidate)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").strip()
    if not host or "." not in host:
        return False
    return True


async def _is_private_url(url: str) -> bool:
    """SSRF 방지: 내부/프라이빗 IP 주소 접근을 차단합니다.

    DNS resolve는 동기 호출이라 to_thread로 분리해 이벤트 루프 블로킹을 막는다 (#4).
    """
    try:
        parsed = urlparse(url if url.startswith(("http://", "https://")) else f"https://{url}")
        hostname = parsed.hostname
        if not hostname:
            return True
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        try:
            addr = ipaddress.ip_address(hostname)
            return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        except ValueError:
            pass
        try:
            resolved = await asyncio.to_thread(
                socket.getaddrinfo, hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
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
    if not _is_valid_url(url):
        raise HTTPException(status_code=400, detail="유효하지 않은 URL입니다.")
    if await _is_private_url(url):
        raise HTTPException(status_code=400, detail="내부 네트워크 주소는 분석할 수 없습니다.")
    scope = body.scope if body.scope in VALID_SCOPES else "all"

    # 프록시 모드: 워커(Mac Mini)에 위임
    if _WORKER_URL:
        try:
            return await _proxy_to_worker("/analyze", {"url": url, "scope": scope})
        except httpx.HTTPStatusError as e:
            log.warning("worker /analyze HTTP %d: %s", e.response.status_code, e.response.text[:200])
            raise HTTPException(status_code=502, detail="워커 호출 실패")
        except Exception as e:
            log.exception("worker /analyze 연결 실패: %s", e)
            raise HTTPException(status_code=502, detail="워커 연결 실패")

    try:
        result = await analyze_url(url, scope=scope)
        return result
    except Exception as e:
        log.exception("/analyze 실패: url=%s err=%s", url, e)
        raise HTTPException(status_code=500, detail="분석 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


@app.post("/analyze-bulk")
@limiter.limit("5/minute")
async def analyze_bulk(request: Request, body: AnalyzeBulkRequest):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="URL을 하나 이상 입력해주세요.")
    if len(urls) > 50:
        raise HTTPException(status_code=400, detail="한 번에 최대 50개 URL까지 분석할 수 있습니다.")

    invalid = [u for u in urls if not _is_valid_url(u)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"유효하지 않은 URL: {invalid[0]}")

    private_flags = await asyncio.gather(*(_is_private_url(u) for u in urls))
    private = [u for u, p in zip(urls, private_flags) if p]
    if private:
        raise HTTPException(status_code=400, detail=f"내부 네트워크 주소는 분석할 수 없습니다: {private[0]}")

    BATCH_SIZE = 10  # 한 번에 10개씩 처리 (메모리 300MB 이하 유지)

    scope = body.scope if body.scope in VALID_SCOPES else "all"

    async def safe_analyze(url: str):
        try:
            return {"url": url, "result": await analyze_url(url, lightweight=True, scope=scope), "error": None}
        except Exception as e:
            log.warning("/analyze-bulk 실패: url=%s %s: %s", url, type(e).__name__, e)
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


# ── Bulk async job (#18) ─────────────────────────────────────────────────────
# 동기 /analyze-bulk는 1000개 처리 시 클라이언트 timeout 가능성. 이 엔드포인트는 즉시
# job_id를 반환하고 클라이언트가 /analyze-bulk-status/{job_id}로 polling하도록 한다.

@app.post("/analyze-bulk-async")
@limiter.limit("5/minute")
async def analyze_bulk_async(request: Request, body: AnalyzeBulkRequest):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="URL을 하나 이상 입력해주세요.")
    if len(urls) > 50:
        raise HTTPException(status_code=400, detail="한 번에 최대 50개 URL까지 분석할 수 있습니다.")

    invalid = [u for u in urls if not _is_valid_url(u)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"유효하지 않은 URL: {invalid[0]}")

    private_flags = await asyncio.gather(*(_is_private_url(u) for u in urls))
    private = [u for u, p in zip(urls, private_flags) if p]
    if private:
        raise HTTPException(status_code=400, detail=f"내부 네트워크 주소는 분석할 수 없습니다: {private[0]}")

    scope = body.scope if body.scope in VALID_SCOPES else "all"
    job_id = bulk_jobs.submit(urls, scope=scope, lightweight=True)
    return {"job_id": job_id, "status": "submitted", "total": len(urls)}


@app.get("/analyze-bulk-status/{job_id}")
async def analyze_bulk_status(job_id: str):
    job = bulk_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job을 찾을 수 없습니다.")
    return job


@app.post("/analyze-bulk-cancel/{job_id}")
async def analyze_bulk_cancel(job_id: str):
    ok = bulk_jobs.cancel(job_id)
    return {"cancelled": ok}


# ── Admin API ────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_ADMIN_TOKEN_TTL_SEC = 8 * 3600  # 8시간

# 발급된 어드민 토큰 → 만료 epoch. 비밀번호와 분리하여 토큰 노출 면적을 줄인다 (#19).
import secrets
import time as _time
_admin_tokens: dict = {}


def _issue_admin_token() -> str:
    token = secrets.token_urlsafe(32)
    _admin_tokens[token] = _time.time() + _ADMIN_TOKEN_TTL_SEC
    return token


def _verify_admin(request: Request) -> bool:
    """Authorization Bearer 토큰을 검증합니다.

    하위 호환: ADMIN_PASSWORD 그 자체를 토큰으로 보내도 통과 (기존 클라이언트 호환).
    신규 클라이언트는 /admin/login에서 발급받은 short-lived 토큰을 사용.
    워커 통신: X-Worker-Secret 헤더가 일치하면 (worker_auth_middleware가 마킹) 통과.
    """
    if getattr(request.state, "worker_authorized", False):
        return True
    if not ADMIN_PASSWORD:
        return False
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    # 1) 발급된 토큰
    exp = _admin_tokens.get(token)
    if exp is not None:
        if exp >= _time.time():
            return True
        _admin_tokens.pop(token, None)
        return False
    # 2) 하위 호환: 비밀번호 직접 사용 (기존 admin.html이 token=pw로 보냄)
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
        # 신규 토큰 발급 — 클라이언트가 token을 사용하면 비밀번호 노출 면적이 줄어든다
        return {"status": "ok", "token": _issue_admin_token(), "expires_in": _ADMIN_TOKEN_TTL_SEC}
    raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")


@app.post("/admin/logout")
async def admin_logout(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        _admin_tokens.pop(auth[7:], None)
    return {"status": "ok"}


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


# ── Scoring config 스냅샷 (버전 저장/불러오기) ────────────────────────────────

class SnapshotSaveRequest(BaseModel):
    name: str


@app.get("/admin/config/snapshots")
async def list_config_snapshots(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import config_snapshots
    return {"snapshots": config_snapshots.list_snapshots()}


@app.post("/admin/config/snapshots")
async def save_config_snapshot(request: Request, body: SnapshotSaveRequest):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import config_snapshots
    try:
        config_snapshots.save_snapshot(body.name, get_scoring_config())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "snapshots": config_snapshots.list_snapshots()}


@app.get("/admin/config/snapshots/{name}")
async def get_config_snapshot(name: str, request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import config_snapshots
    snap = config_snapshots.get_snapshot(name)
    if snap is None:
        raise HTTPException(status_code=404, detail="스냅샷을 찾을 수 없습니다.")
    return snap


@app.post("/admin/config/snapshots/{name}/restore")
async def restore_config_snapshot(name: str, request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import config_snapshots
    snap = config_snapshots.get_snapshot(name)
    if snap is None:
        raise HTTPException(status_code=404, detail="스냅샷을 찾을 수 없습니다.")
    save_scoring_config(snap)
    return {"status": "ok", "config": get_scoring_config()}


@app.delete("/admin/config/snapshots/{name}")
async def delete_config_snapshot(name: str, request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import config_snapshots
    if not config_snapshots.delete_snapshot(name):
        raise HTTPException(status_code=404, detail="스냅샷을 찾을 수 없습니다.")
    return {"status": "ok"}


# ── page_types.json (페이지 타입 정의 편집) ──────────────────────────────────

_PAGE_TYPES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_types.json")


@app.get("/admin/page-types")
async def get_page_types(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        import json
        with open(_PAGE_TYPES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"읽기 실패: {e}")


@app.put("/admin/page-types")
async def put_page_types(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import json
    body = await request.json()
    if not isinstance(body, dict) or "page_types" not in body or not isinstance(body["page_types"], list):
        raise HTTPException(status_code=400, detail="형식 오류: {\"page_types\": [...]} 필요")
    try:
        with open(_PAGE_TYPES_PATH, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        # page_type 모듈의 캐시 무효화 → 다음 detect 시 새 JSON 적용
        try:
            from page_type import load_page_types
            load_page_types(force=True)
        except Exception:
            pass
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"저장 실패: {e}")


@app.get("/admin/rule-types")
async def get_rule_types(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return RULE_TYPES


_AUDIT_CRITERIA_DOC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "audit-criteria.md")
_EXTENSION_PUBLISH_GUIDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "extension-publish-guide.md")


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


@app.get("/admin/extension-guide-doc")
async def get_extension_publish_guide_doc(request: Request):
    """extension-publish-guide.md를 어드민에 노출."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        with open(_EXTENSION_PUBLISH_GUIDE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return {"status": "ok", "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="extension-publish-guide.md 파일이 없습니다.")

# ── Audit Groups & Schedules ─────────────────────────────────────────────────

class SitemapAgentRequest(BaseModel):
    sitemap_url: str
    email: str
    site_name: str

@app.post("/admin/sitemap-agent")
async def run_sitemap_agent(request: Request, body: SitemapAgentRequest, background_tasks: BackgroundTasks):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    smtp_config = {
        "SMTP_HOST": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "SMTP_PORT": int(os.environ.get("SMTP_PORT", 587)),
        "SMTP_USER": os.environ.get("SMTP_USER", ""),
        "SMTP_PASS": os.environ.get("SMTP_PASS", ""),
        "SMTP_FROM": os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    }

    if not smtp_config["SMTP_USER"] or not smtp_config["SMTP_PASS"]:
        raise HTTPException(status_code=400, detail="서버 환경 변수(SMTP_USER, SMTP_PASS)가 설정되지 않았습니다.")

    background_tasks.add_task(
        sitemap_agent.run_sitemap_audit_task,
        sitemap_url=body.sitemap_url,
        email=body.email,
        site_name=body.site_name,
        smtp_config=smtp_config,
    )
    return {"status": "ok", "message": "사이트맵 자동 감사가 백그라운드에서 시작되었습니다. 완료 시 이메일로 발송됩니다."}

@app.get("/admin/agent-logs")
async def get_agent_logs(request: Request):
    """로컬 시스템 로그 + (있으면) peer 워커 로그 병합 반환.

    Audit는 보통 Render에서 실행돼 Render db에 [audit] 로그가 쌓이고,
    Sitemap sync는 Mac Mini(워커)에서 실행돼 Mac Mini db에 [sitemap-sync] 로그가
    쌓인다. AUDIT_DATA_PEER_URL이 설정된 인스턴스(Render)는 양쪽을 합쳐서 반환.
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    import db
    local_logs = db.get_recent_system_logs(100)

    if _AUDIT_DATA_PEER_URL and _WORKER_SECRET:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    f"{_AUDIT_DATA_PEER_URL}/admin/agent-logs",
                    headers={"X-Worker-Secret": _WORKER_SECRET},
                )
                resp.raise_for_status()
                peer_logs = resp.json().get("logs", [])
                # 중복 제거 + timestamp 기반 정렬 (로그 앞부분이 [YYYY-MM-DD HH:MM:SS]라 lexicographic 정렬 == 시간순)
                merged = sorted(set(local_logs) | set(peer_logs))
                return {"logs": merged[-200:]}  # 너무 많아지지 않게 최근 200건만
        except Exception as e:
            log.warning("agent-logs peer fetch 실패, local만 반환: %s", e)

    return {"logs": local_logs}


@app.post("/admin/sitemap-sync/run")
async def run_sitemap_sync_now(request: Request, background_tasks: BackgroundTasks):
    """사이트맵 동기를 지금 1회 수동 실행 (백그라운드).

    이전엔 Mac Mini로 proxy했지만, Mac Mini IP가 Akamai 차단된 케이스가 있어
    Render에서 직접 실행하도록 변경. Render IP는 Akamai whitelist에 등록돼 있어
    sitemap.xml fetch가 통과한다.
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    from sitemap_sync import run_daily_sync
    background_tasks.add_task(run_daily_sync)
    return {"status": "ok", "message": "사이트맵 동기를 백그라운드에서 시작했습니다. 로그창에서 진행상황 확인."}


@app.post("/admin/csr-baseline/run")
async def run_csr_baseline_now(request: Request, background_tasks: BackgroundTasks):
    """모든 그룹 baseline 갱신을 백그라운드에서 1회 실행. 매우 오래 걸림 (~수 시간).

    그룹별 trigger는 POST /admin/csr-baseline/run/{group_id} 사용.
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    from csr_baseline import regenerate_baseline_all_groups
    background_tasks.add_task(regenerate_baseline_all_groups)
    return {"status": "ok", "message": "모든 그룹 CSR baseline 갱신 시작. 그룹별로 ~30분, 전체는 수 시간 소요."}


@app.post("/admin/csr-baseline/run/{group_id}")
async def run_csr_baseline_for_group(group_id: str, request: Request, background_tasks: BackgroundTasks):
    """지정 그룹 baseline 갱신을 백그라운드에서 1회 실행 (~30분 소요).

    page_type별 10개 URL을 Playwright(lightweight=False)로 실측. Render IP가
    Akamai whitelist라 LG 사이트 차단 없이 통과.
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    from csr_baseline import regenerate_baseline_for_group
    background_tasks.add_task(regenerate_baseline_for_group, group_id)
    return {
        "status": "ok",
        "message": f"그룹 {group_id} CSR baseline 갱신 시작 (~30분 소요). 완료 후 새로고침.",
    }


@app.get("/admin/csr-baseline")
async def get_csr_baseline(request: Request):
    """저장된 모든 그룹·page_type 별 baseline 조회.

    응답: { "<group_id>": { "_meta": {...}, "<page_type_id>": {...}, ... }, ... }
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    from csr_baseline import load_baseline_all
    return load_baseline_all()


@app.post("/admin/classify/run/{group_id}")
async def run_classify_for_group(group_id: str, request: Request, background_tasks: BackgroundTasks):
    """[1단계 + sitemap sync 통합] 그룹의 sitemap fetch + URL 분류 (~15분 백그라운드).

    1) sitemap.xml에서 최신 URL 추출하여 group.urls 갱신 (sync_group)
    2) groups[gid].urls 전체를 httpx fetch + body class/meta template 분류 (classify_group)
    3) 결과 저장 + Mac Mini로 즉시 push

    이전엔 sitemap-sync와 classify를 분리해서 두 번 trigger했지만, 보통 같이
    필요하니 한 번 클릭으로 통합.
    """
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    async def _task():
        try:
            data = audit_store.load()
            group = next((g for g in data.get("groups", []) if g.get("id") == group_id), None)
            if not group:
                log.warning("[sync+classify] group not found: %s", group_id)
                return

            # 1) sitemap-sync (single group)
            sitemap_url = group.get("sitemap_url")
            if sitemap_url:
                try:
                    from sitemap_sync import sync_group
                    log.info("[sync+classify] %s: sitemap sync 시작 (%s)", group_id, sitemap_url)
                    result = await sync_group(group)
                    await audit_store.save(data)
                    # audit_data 변경 → Mac Mini push
                    await _peer_push_aux("/admin/audit-data", data)
                    log.info("[sync+classify] %s: sitemap sync 완료 — %s", group_id, result)
                except Exception as e:
                    log.exception("[sync+classify] %s: sitemap sync 실패 — %s", group_id, e)
                    # sync 실패해도 기존 URLs로 classify는 진행
            else:
                log.info("[sync+classify] %s: sitemap_url 없음 — sync 생략", group_id)

            # 2) classify
            from url_classifier import classify_group
            log.info("[sync+classify] %s: classify 시작", group_id)
            r = await classify_group(group_id)
            log.info("[sync+classify] %s: classify 완료 — %s", group_id, r)
        except Exception as e:
            log.exception("[sync+classify] %s: 전체 실패 — %s", group_id, e)

    background_tasks.add_task(_task)
    return {
        "status":  "ok",
        "message": f"그룹 {group_id} Sitemap sync + URL 분류 시작 (~15분, 백그라운드).",
    }


@app.get("/admin/classify/{group_id}")
async def get_classify_for_group(group_id: str, request: Request):
    """[1단계] 지정 그룹의 분류 결과 조회 (summary + by_type 분포)."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    from url_classifier import get_group_classification
    entry = get_group_classification(group_id)
    if not entry:
        return {"status": "not_classified"}
    # 응답 크기 줄이기 — classifications/failed_urls는 admin이 따로 안 보여줘도 됨
    return {
        "status":     "ok",
        "group_id":   group_id,
        "group_name": entry.get("group_name"),
        "updated_at": entry.get("updated_at"),
        "took_sec":   entry.get("took_sec"),
        "summary":    entry.get("summary"),
    }


@app.get("/admin/classify")
async def get_classify_all(request: Request):
    """전체 그룹의 분류 summary 일괄 조회. 큰 classifications dict는 제외."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    from url_classifier import load_classifications_all
    full = load_classifications_all()
    return {
        gid: {
            "group_name": e.get("group_name"),
            "updated_at": e.get("updated_at"),
            "took_sec":   e.get("took_sec"),
            "summary":    e.get("summary"),
        }
        for gid, e in full.items()
    }


class ParseSitemapRequest(BaseModel):
    sitemap_url: str

@app.post("/admin/parse-sitemap")
async def admin_parse_sitemap(request: Request, body: ParseSitemapRequest):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        urls = await sitemap_agent.parse_sitemap(body.sitemap_url)
        return {"urls": urls}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/admin/sitemap-draft")
async def sitemap_draft(request: Request, body: ParseSitemapRequest):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    try:
        urls = await sitemap_agent.parse_sitemap(body.sitemap_url)
        preview_urls = urls[:3]
        
        from analyzer import analyze_url
        sem = asyncio.Semaphore(3)
        async def _audit(u: str):
            async with sem:
                try:
                    res = await analyze_url(u, lightweight=True)
                    return {"url": u, "result": res}
                except Exception as e:
                    return {"url": u, "error": str(e)}
                    
        tasks = [_audit(u) for u in preview_urls]
        preview_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        cleaned_results = []
        for r in preview_results:
            if isinstance(r, Exception):
                continue
            if "error" in r:
                cleaned_results.append({"url": r["url"], "score": "Error", "grade": "-"})
            else:
                score = r["result"].get("score", {}).get("total", "")
                grade = r["result"].get("score", {}).get("grade", "")
                cleaned_results.append({"url": r["url"], "score": score, "grade": grade})
                
        return {"total_urls": len(urls), "preview": cleaned_results}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/admin/audit-data")
async def get_audit_data(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return audit_store.load()


# ── auxiliary JSON files (url_classifications, csr_baseline) peer sync ──
# Render(ephemeral)에서 만들어진 데이터를 Mac Mini(persistent)에서도 보존하기 위해.
# Render startup pull + Mac Mini 주기 pull + 변경 시 push 모두에서 사용.

import json as _json_mod
_URL_CLASSIFICATIONS_PATH = os.path.join(_DATA_DIR if (_DATA_DIR := os.environ.get("DATA_DIR")) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"), "url_classifications.json")
_CSR_BASELINE_PATH        = os.path.join(_DATA_DIR if (_DATA_DIR := os.environ.get("DATA_DIR")) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"), "csr_baseline.json")


def _read_aux_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return _json_mod.load(f)
    except Exception:
        return {}


def _write_aux_file(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # atomic write
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".aux_", suffix=".json.tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json_mod.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


@app.get("/admin/url-classifications-raw")
async def get_url_classifications_raw(request: Request):
    """url_classifications.json 전체 반환 (sync용). worker_authorized OK."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return _read_aux_file(_URL_CLASSIFICATIONS_PATH)


@app.put("/admin/url-classifications-raw")
async def put_url_classifications_raw(request: Request):
    """url_classifications.json 전체 교체 (peer push 받는 endpoint)."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    body = await request.json()
    _write_aux_file(_URL_CLASSIFICATIONS_PATH, body)
    return {"status": "ok", "entries": len(body)}


@app.get("/admin/csr-baseline-raw")
async def get_csr_baseline_raw(request: Request):
    """csr_baseline.json 전체 반환 (sync용)."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return _read_aux_file(_CSR_BASELINE_PATH)


@app.put("/admin/csr-baseline-raw")
async def put_csr_baseline_raw(request: Request):
    """csr_baseline.json 전체 교체 (peer push 받는 endpoint)."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    body = await request.json()
    _write_aux_file(_CSR_BASELINE_PATH, body)
    return {"status": "ok", "groups": len(body)}


@app.put("/admin/audit-data")
async def update_audit_data(request: Request):
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    body = await request.json()
    await audit_store.save(body)
    try:
        from scheduler import reload_schedules
        n = reload_schedules()
    except Exception as e:
        log.exception("reload_schedules 실패: %s", e)
        n = 0

    # Mac Mini로 즉시 push — Render ephemeral disk 보존용 (best-effort, 실패해도 무시)
    # peer가 보낸 push 자체가 다시 돌아오는 echo는 worker_authorized 헤더 없는 admin
    # 호출만 push하여 회피
    if not getattr(request.state, "worker_authorized", False):
        await _peer_push_aux("/admin/audit-data", body)

    return {"status": "ok", "data": body, "active_schedules": n}


@app.post("/admin/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, request: Request, background_tasks: BackgroundTasks):
    """스케줄을 즉시 1회 실행 (백그라운드)."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    # 프록시 모드: 워커가 실제 실행. 백그라운드 task도 워커 쪽에서 시작됨.
    if _WORKER_URL:
        try:
            return await _proxy_to_worker(f"/admin/schedules/{schedule_id}/run", body={})
        except Exception as e:
            log.exception("worker schedule run 실패: %s", e)
            raise HTTPException(status_code=502, detail="워커 호출 실패")

    try:
        from scheduler import trigger_now
        # Send to background to avoid timeout
        background_tasks.add_task(trigger_now, schedule_id)
        return {"status": "ok", "message": "스케줄 분석이 백그라운드에서 시작되었습니다."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"실행 실패: {e}")


def _aggregate_page_type_stats(runs: list) -> dict:
    """schedule_runs의 summary를 순회하며 page_type별 통계 집계.

    각 run.summary는 [{"url": ..., "result": <full analyzer result>}, {"url": ..., "error": ...}] 형태.
    """
    import json as _json
    stats: dict = {}

    for run in runs:
        summary = run.get("summary") or []
        if isinstance(summary, str):
            try:
                summary = _json.loads(summary)
            except Exception:
                continue
        if not isinstance(summary, list):
            continue

        for entry in summary:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result")
            if not result:
                continue  # error entries skip
            pt = (result.get("page_type") or {})
            pt_id = pt.get("id") or "unknown"

            s = stats.setdefault(pt_id, {
                "label": pt.get("label", pt_id),
                "url_count": 0,
                "scored_count": 0,
                "score_sum": 0.0,
                "schema_check_pass": 0,
                "schema_check_total": 0,
                "cat_score_sum": {},  # cat_key -> sum of pct points
                "cat_score_count": {},
            })
            s["url_count"] += 1

            score_obj = result.get("score") or {}
            total = score_obj.get("total")
            if isinstance(total, (int, float)):
                s["scored_count"] += 1
                s["score_sum"] += float(total)

            # schema_for_page_type 룰 (ai_readiness 카테고리 내 ai_schema_for_page_type item) 통과율
            breakdown = score_obj.get("breakdown") or {}
            ai_items = (breakdown.get("ai_readiness") or {}).get("items") or {}
            sch_item = ai_items.get("ai_schema_for_page_type")
            if sch_item is not None:
                s["schema_check_total"] += 1
                if sch_item.get("pass"):
                    s["schema_check_pass"] += 1

            # 카테고리별 평균 점수 (이미 %)
            for cat_key in ("performance", "accessibility", "seo", "ai_readiness"):
                bd = breakdown.get(cat_key)
                if not bd:
                    continue
                pts = bd.get("points")
                if isinstance(pts, (int, float)):
                    s["cat_score_sum"][cat_key] = s["cat_score_sum"].get(cat_key, 0.0) + float(pts)
                    s["cat_score_count"][cat_key] = s["cat_score_count"].get(cat_key, 0) + 1

    # 정리: 평균/비율 계산
    out = {}
    for pt_id, s in stats.items():
        out[pt_id] = {
            "label":                  s["label"],
            "url_count":              s["url_count"],
            "scored_count":           s["scored_count"],
            "avg_score":              round(s["score_sum"] / s["scored_count"], 1) if s["scored_count"] else None,
            "schema_pass_rate":       round(s["schema_check_pass"] / s["schema_check_total"] * 100, 1) if s["schema_check_total"] else None,
            "schema_pass_count":      s["schema_check_pass"],
            "schema_total":           s["schema_check_total"],
            "category_avg": {
                cat: round(s["cat_score_sum"][cat] / s["cat_score_count"][cat], 1)
                for cat in s["cat_score_sum"]
                if s["cat_score_count"].get(cat)
            },
        }
    return out


@app.get("/admin/stats/page-types")
async def page_type_stats(request: Request, limit: int = 50):
    """최근 schedule_runs를 집계해 페이지 타입별 통계를 반환."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import db
    runs = db.get_recent_schedule_runs(limit=limit)
    return {
        "run_count": len(runs),
        "stats": _aggregate_page_type_stats(runs),
    }


@app.get("/admin/schedules/runs")
async def get_schedule_runs(request: Request, schedule_id: str = None, limit: int = 20):
    """최근 정기 Audit 실행 결과 조회."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    # 프록시 모드: 실행 이력은 워커가 보유 — 워커에서 가져온다.
    if _WORKER_URL:
        try:
            params: dict = {"limit": limit}
            if schedule_id:
                params["schedule_id"] = schedule_id
            return await _proxy_to_worker("/admin/schedules/runs", method="GET", params=params)
        except Exception as e:
            log.warning("worker /admin/schedules/runs 실패, 로컬로 fallback: %s", e)

    try:
        from scheduler import get_recent_runs
        runs = get_recent_runs(schedule_id=schedule_id, limit=limit)
        return {"runs": runs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {e}")


@app.post("/admin/smtp-test")
async def test_smtp(request: Request, body: dict):
    """SMTP 연결 테스트."""
    if not _verify_admin(request):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    import smtplib
    try:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", 587))
        user = os.environ.get("SMTP_USER", "")
        passwd = os.environ.get("SMTP_PASS", "")
        if not user or not passwd:
            raise HTTPException(status_code=400, detail="서버 환경 변수(SMTP_USER, SMTP_PASS)가 설정되지 않았습니다.")
        server = smtplib.SMTP(host, port, timeout=10)
        server.ehlo()
        server.starttls()
        server.login(user, passwd)
        server.quit()
        return {"status": "ok", "message": "SMTP 연결 성공"}
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(status_code=400, detail="인증 실패 — 계정 또는 앱 비밀번호를 확인하세요.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"연결 실패: {str(e)}")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
