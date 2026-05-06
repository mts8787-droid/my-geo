import re
import time
import asyncio
import httpx
from bs4 import BeautifulSoup
import json
import os
import copy
from typing import Optional
from urllib.parse import urlparse
from rule_engine import evaluate_rule, evaluate_rule_async, RULE_TYPES

# Playwright 동시 실행 제한 (메모리 보호)
_playwright_sem = asyncio.Semaphore(2)

# 벌크 분석 시 동시 요청 제한
_bulk_sem = asyncio.Semaphore(5)

# ── 채점 설정 관리 ──────────────────────────────────────────────────────────────

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scoring_config.json")
_DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scoring_config.default.json")

_DEFAULT_CONFIG = None  # scoring_config.json에서 로드, 없으면 파일 기본값 사용

def _load_default_config():
    """팩토리 기본 설정 = scoring_config.default.json (리셋용 스냅샷)."""
    for path in (_DEFAULT_CONFIG_PATH, _CONFIG_PATH):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {"grade": {"good": 90, "need_improvement": 70}}

_scoring_config: Optional[dict] = None


def load_scoring_config() -> dict:
    """설정 파일에서 채점 설정을 로드합니다. 없으면 기본값 반환."""
    global _scoring_config
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _scoring_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _scoring_config = _load_default_config()
    return _scoring_config


def save_scoring_config(config: dict) -> None:
    """채점 설정을 파일에 저장합니다."""
    global _scoring_config
    _scoring_config = config
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_scoring_config() -> dict:
    """현재 메모리에 로드된 설정을 반환합니다."""
    if _scoring_config is None:
        return load_scoring_config()
    return _scoring_config


def get_default_config() -> dict:
    """기본 채점 설정을 반환합니다."""
    return _load_default_config()


# 서버 시작 시 설정 로드
load_scoring_config()

AI_BOTS = {
    "GPTBot":          "OpenAI GPT",
    "ChatGPT-User":    "ChatGPT",
    "Google-Extended": "Google Gemini",
    "CCBot":           "Common Crawl (AI 학습)",
    "anthropic-ai":    "Claude (Anthropic)",
    "Claude-Web":      "Claude Web",
    "PerplexityBot":   "Perplexity AI",
    "Bytespider":      "ByteDance AI",
    "cohere-ai":       "Cohere AI",
    "YouBot":          "You.com",
}


def _normalize_url(url: str) -> tuple[str, str]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed   = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return url, base_url


def _safe_visible_text(soup: BeautifulSoup) -> int:
    """soup를 변조하지 않고 보이는 텍스트 글자수를 계산합니다."""
    text_parts = []
    for element in soup.find_all(string=True):
        if element.parent and element.parent.name in ("script", "style", "noscript", "svg", "path"):
            continue
        text_parts.append(element)
    return len(re.sub(r'\s+', '', ''.join(text_parts)))


async def analyze_url(url: str, lightweight: bool = False, scope: str = "all") -> dict:
    """URL 분석.

    lightweight=True: 벌크 분석용 경량 모드 (Playwright CSR 분석 생략, 메모리 절약)
    scope: 'all' | 'schema' | 'seo' | 'faq' — 특정 항목만 분석
    """
    url, base_url = _normalize_url(url)

    # scope별 필요한 분석만 수행
    if scope != "all":
        page_data = await _fetch_page(url)

        if scope == "schema":
            jsonld = _extract_json_ld(page_data)
            page_data["soup"] = None
            return {"url": url, "base_url": base_url, "scope": scope, "json_ld": jsonld}

        if scope == "seo":
            context = {
                "soup": page_data.get("soup"), "page_data": page_data,
                "jsonld_types": set(), "jsonld_raw": [],
                "base_url": base_url, "current_url": page_data.get("final_url") or url,
                "csr_ratio_dict": {"status": "skipped"},
            }
            score = await _calculate_score(context, {"bots": {}}, {"status": "skipped"})
            page_data["soup"] = None
            return {"url": url, "base_url": base_url, "scope": scope, "score": score}

        if scope == "faq":
            jsonld = _extract_json_ld(page_data)
            context = {
                "soup": page_data.get("soup"), "page_data": page_data,
                "jsonld_types": {t.lower() for t in jsonld.get("all_types", [])},
                "jsonld_raw": jsonld.get("raw", []),
                "base_url": base_url, "current_url": page_data.get("final_url") or url,
                "csr_ratio_dict": {"status": "skipped"},
            }
            score = await _calculate_score(context, {"bots": {}}, {"status": "skipped"})
            page_data["soup"] = None
            return {"url": url, "base_url": base_url, "scope": scope, "score": score}

    if lightweight:
        # 벌크: Playwright(CSR) 생략 — httpx만 사용
        async with _bulk_sem:
            robots, llms, page_data = await asyncio.gather(
                _check_robots_txt(base_url),
                _check_llms_txt(base_url),
                _fetch_page(url),
            )
        csr_raw = {"status": "skipped", "csr_chars": 0}
    else:
        robots, llms, page_data, csr_raw = await asyncio.gather(
            _check_robots_txt(base_url),
            _check_llms_txt(base_url),
            _fetch_page(url),
            _check_csr_chars(url),
        )

    jsonld    = _extract_json_ld(page_data)
    pdp       = _detect_pdp(url)

    # SSR 글자수 계산 (soup 변조 없이)
    ssr_chars = 0
    if page_data["status"] == "ok" and page_data["soup"]:
        ssr_chars = _safe_visible_text(page_data["soup"])

    csr_ratio = _calc_csr_ratio(ssr_chars, csr_raw)

    # 룰 엔진 context 구성
    all_types = set(jsonld.get("all_types", []))
    context = {
        "soup":            page_data.get("soup"),
        "page_data":       page_data,
        "jsonld_types":    {t.lower() for t in all_types},
        "jsonld_raw":      jsonld.get("raw", []),
        "base_url":        base_url,
        "current_url":     page_data.get("final_url") or url,
        "csr_ratio_dict":  csr_ratio,
    }

    score = await _calculate_score(context, robots, csr_ratio)

    # 페이지 fetch 실패 시 응답 최상단에 에러 표면화 — 룰들이 모두 "HTML 파싱 실패"로 보이는 혼란 방지
    page_error = None
    if page_data.get("status") != "ok" or page_data.get("soup") is None:
        page_error = {
            "kind":        page_data.get("status", "error"),
            "http_status": page_data.get("http_status"),
            "message":     page_data.get("error") or "페이지 응답에 HTML 본문이 없거나 너무 짧습니다 (500자 미만).",
            "hint":        "봇 차단(Cloudflare/CAPTCHA), 타임아웃, 또는 비-HTML 응답일 수 있습니다. 브라우저에서 해당 URL이 정상 열리는지 확인하세요.",
        }

    # soup 참조 해제 — 메모리 즉시 회수
    page_data["soup"] = None

    return {
        "url":               url,
        "base_url":          base_url,
        "scope":             "all",
        "pdp":               pdp,
        "robots_txt":        robots,
        "json_ld":           jsonld,
        "csr_ratio":         csr_ratio,
        "score":             score,
        "page_error":        page_error,
    }


# ── Page Fetch ────────────────────────────────────────────────────────────────

async def _fetch_page(url: str) -> dict:
    try:
        # 실제 Chrome으로 위장 — Cloudflare/봇 차단 회피.
        # User-Agent를 GEOAudit처럼 명시적으로 두면 LG.com 등이 거부함.
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control":   "no-cache",
            "Pragma":          "no-cache",
            "Sec-Ch-Ua":       '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile":   "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        # http2=True는 'h2' 패키지가 있을 때만 활성. 미설치 환경에서 ImportError 회피
        try:
            httpx.AsyncClient(http2=True).aclose  # 빠른 import 검증
            use_http2 = True
        except ImportError:
            use_http2 = False

        # 봇 보호(Akamai/Cloudflare) 간헐적 403/429 회피용 재시도 (백오프 0.5s, 1.5s)
        BACKOFFS = [0, 0.5, 1.5]
        last_error = None
        r = None
        ttfb_ms = 0
        for attempt, delay in enumerate(BACKOFFS):
            if delay:
                await asyncio.sleep(delay)
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    timeout=15, follow_redirects=True, max_redirects=10, http2=use_http2
                ) as client:
                    r = await client.get(url, headers=headers)
                ttfb_ms = int((time.perf_counter() - t0) * 1000)
                # 403/429는 봇 보호 의심 → 재시도. 그 외는 즉시 사용 (200/3xx/4xx 정상 응답)
                if r.status_code in (403, 429) and attempt < len(BACKOFFS) - 1:
                    last_error = f"HTTP {r.status_code} 후 재시도 #{attempt + 1}"
                    continue
                break
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {str(e)[:100]}"
                if attempt < len(BACKOFFS) - 1:
                    continue
                raise

        if r is None:
            raise RuntimeError(last_error or "fetch 실패")

        redirect_count = len(r.history)
        content_type = r.headers.get("content-type", "")
        resp_headers = {k: v for k, v in r.headers.items()}
        # raw bytes (압축 해제 후) 길이 — utf-8 기준
        try:
            html_bytes = len(r.content)
        except Exception:
            html_bytes = len(r.text.encode("utf-8"))
        http_version = getattr(r, "http_version", "") or ""

        common = {
            "headers":       resp_headers,
            "http_version":  http_version,
            "html_bytes":    html_bytes,
            "ttfb_ms":       ttfb_ms,
            "raw_html":      r.text if "text/html" in content_type else "",
        }

        # HTML 본문이 있으면 상태코드와 관계없이 파싱 (일부 사이트는 404/403이지만 콘텐츠 정상)
        if "text/html" in content_type and len(r.text) > 500:
            soup = BeautifulSoup(r.text, "html.parser")
            return {
                "status":         "ok",
                "soup":           soup,
                "http_status":    r.status_code,
                "final_url":      str(r.url),
                "redirect_count": redirect_count,
                **common,
            }

        if r.status_code != 200:
            return {"status": "error", "http_status": r.status_code,
                    "soup": None, "redirect_count": redirect_count, **common}

        soup = BeautifulSoup(r.text, "html.parser")
        return {
            "status":         "ok",
            "soup":           soup,
            "http_status":    r.status_code,
            "final_url":      str(r.url),
            "redirect_count": redirect_count,
            **common,
        }
    except httpx.TimeoutException:
        return {"status": "error", "error": "요청 시간 초과 (15초)", "soup": None, "redirect_count": 0}
    except httpx.ConnectError:
        return {"status": "error", "error": "서버에 연결할 수 없습니다", "soup": None, "redirect_count": 0}
    except httpx.TooManyRedirects:
        return {"status": "error", "error": "리다이렉트가 너무 많습니다", "soup": None, "redirect_count": 0}
    except Exception as e:
        return {"status": "error", "error": str(e), "soup": None, "redirect_count": 0}


# ── robots.txt ────────────────────────────────────────────────────────────────

async def _check_robots_txt(base_url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(f"{base_url}/robots.txt")
        if r.status_code != 200:
            return {"status": "not_found", "bots": {}, "raw": ""}
        content = r.text
        return {"status": "found", "bots": _parse_robots_for_ai_bots(content), "raw": content[:3000]}
    except Exception as e:
        return {"status": "error", "error": str(e), "bots": {}}


def _parse_robots_for_ai_bots(content: str) -> dict:
    rules: dict[str, list[str]] = {}
    current_agents: list[str]   = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            continue
        if not line:
            current_agents = []
            continue
        lower = line.lower()
        if lower.startswith("user-agent:"):
            agent = line.split(":", 1)[1].strip()
            current_agents.append(agent)
            rules.setdefault(agent, [])
        elif lower.startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            for agent in current_agents:
                rules.setdefault(agent, []).append(path)

    bot_status: dict[str, dict] = {}
    for bot_key, bot_name in AI_BOTS.items():
        blocked      = False
        matched_rule = None
        for agent, disallows in rules.items():
            if agent.lower() in (bot_key.lower(), "*"):
                for disallow in disallows:
                    if disallow in ("/", "/*"):
                        blocked      = True
                        matched_rule = f"User-agent: {agent}  →  Disallow: {disallow}"
                        break
            if blocked:
                break
        bot_status[bot_key] = {"name": bot_name, "blocked": blocked, "rule": matched_rule}

    return bot_status


# ── llms.txt ──────────────────────────────────────────────────────────────────

async def _check_llms_txt(base_url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(f"{base_url}/llms.txt")
        if r.status_code == 200:
            content = r.text
            return {
                "status":          "found",
                "content_preview": content[:1200],
                "size_bytes":      len(content.encode()),
            }
        return {"status": "not_found", "http_status": r.status_code}
    except httpx.TimeoutException:
        return {"status": "error", "error": "요청 시간 초과"}
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"네트워크 오류: {type(e).__name__}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}



# ── JSON-LD ───────────────────────────────────────────────────────────────────

def _extract_json_ld(page_data: dict) -> dict:
    if page_data["status"] != "ok" or not page_data["soup"]:
        return {"status": "error", "schemas": [], "count": 0, "all_types": [], "raw_sources": []}

    soup        = page_data["soup"]
    scripts     = soup.find_all("script", type="application/ld+json")
    schemas     = []
    raw_datas   = []
    raw_sources = []

    for script in scripts:
        try:
            data = json.loads(script.string or "")
            schemas.append(_parse_schema(data))
            raw_datas.append(data)
            raw_sources.append(json.dumps(data, ensure_ascii=False, indent=2)[:5000])
        except Exception:
            pass

    # 1차: 파싱된 스키마에서 타입 수집
    all_types = _get_all_schema_types(schemas)

    # 2차: 원본 JSON을 재귀적으로 스캔하여 중첩된 @type까지 수집
    #      (e.g. LG의 AggregateRating > itemReviewed > @type: "IndividualProduct")
    for raw in raw_datas:
        _collect_raw_types(raw, all_types)

    return {
        "status":      "found" if schemas else "not_found",
        "count":       len(schemas),
        "schemas":     schemas,
        "all_types":   list(all_types),
        "raw_sources": raw_sources,
        "raw":         raw_datas,
    }


def _collect_raw_types(data, types: set):
    """원본 JSON-LD에서 모든 @type 값을 재귀적으로 추출."""
    if isinstance(data, dict):
        t = data.get("@type")
        if t:
            if isinstance(t, list):
                types.update(str(v) for v in t)
            else:
                types.add(str(t))
        for val in data.values():
            _collect_raw_types(val, types)
    elif isinstance(data, list):
        for item in data:
            _collect_raw_types(item, types)


def _parse_schema(data) -> dict:
    """JSON-LD 데이터를 파싱.

    처리 구조:
    - List                     → @graph 취급
    - Dict with @graph key     → { "@context": "...", "@graph": [...] } 패턴
    - Dict with @type          → 일반 스키마 오브젝트
    """
    if isinstance(data, list):
        return {"type": "@graph", "items": [_parse_schema(item) for item in data]}

    if not isinstance(data, dict):
        return {"type": "unknown"}

    if "@graph" in data and "@type" not in data:
        graph = data["@graph"]
        items = [_parse_schema(item) for item in graph] if isinstance(graph, list) else []
        return {"type": "@graph", "items": items}

    return {
        "type":        data.get("@type", "Unknown"),
        "name":        data.get("name", ""),
        "description": str(data.get("description", ""))[:200],
        "keys":        [k for k in data.keys() if not k.startswith("@")],
    }


def _schema_has_type(schema: dict, type_name: str) -> bool:
    t = schema.get("type", "")
    if isinstance(t, list):
        if type_name in t:
            return True
    elif t == type_name:
        return True
    for item in schema.get("items", []):
        if _schema_has_type(item, type_name):
            return True
    return False


def _get_all_schema_types(schemas: list) -> set:
    types: set[str] = set()
    for schema in schemas:
        _collect_types(schema, types)
    return types


def _collect_types(schema: dict, types: set):
    skip = {"@graph", "unknown", "Unknown", ""}
    t = schema.get("type", "")
    if isinstance(t, list):
        types.update(v for v in t if v not in skip)
    elif t not in skip:
        types.add(t)
    for item in schema.get("items", []):
        _collect_types(item, types)



# ── CSR Ratio ─────────────────────────────────────────────────────────────────

async def _ensure_chromium() -> bool:
    """Chromium 바이너리가 없으면 자동 설치. 성공 시 True."""
    import sys
    python = sys.executable or "python"
    for cmd in [
        [python, "-m", "playwright", "install", "chromium", "--with-deps"],
        [python, "-m", "playwright", "install", "chromium"],
    ]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode == 0:
                return True
        except Exception:
            continue
    return False


async def _check_csr_chars(url: str) -> dict:
    """Playwright로 JS 실행 후 텍스트 글자수를 반환."""
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError:
        return {"status": "unavailable", "csr_chars": 0}

    async with _playwright_sem:
        try:
            async with async_playwright() as p:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
                try:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=launch_args,
                    )
                except Exception as launch_err:
                    if "Executable doesn't exist" in str(launch_err):
                        ok = await _ensure_chromium()
                        if not ok:
                            return {"status": "error",
                                    "error": "Chromium 설치 실패 — Render 대시보드에서 Build Command를 확인하세요.",
                                    "csr_chars": 0}
                        browser = await p.chromium.launch(
                            headless=True,
                            args=launch_args,
                        )
                    else:
                        raise

                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    extra_http_headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                page = await context.new_page()
                await stealth_async(page)

                resp = await page.goto(url, wait_until="networkidle", timeout=30000)
                final_url = page.url
                http_status = resp.status if resp else None

                # 봇 차단 감지 (403/406) — 본문이 충분하면 정상 파싱 시도
                if http_status and http_status in (403, 406):
                    await page.wait_for_timeout(2000)
                    quick_text = await page.inner_text("body")
                    body_chars = len(re.sub(r'\s+', '', quick_text))
                    if body_chars < 10000:
                        await context.close()
                        await browser.close()
                        is_bot_block = any(kw in quick_text.lower() for kw in
                                           ["access denied", "robot", "captcha", "blocked",
                                            "not allowed", "permission"])
                        return {
                            "status": "blocked",
                            "csr_chars": 0,
                            "error": f"사이트가 헤드리스 브라우저를 차단합니다 (HTTP {http_status})"
                                     if is_bot_block else
                                     f"HTTP {http_status} 응답",
                            "debug": {
                                "final_url": final_url,
                                "http_status": http_status,
                                "page_title": quick_text[:100],
                                "text_preview": quick_text[:300],
                            },
                        }
                    # 본문이 200자 이상이면 계속 진행 (403이지만 콘텐츠 정상인 경우)

                # JS 프레임워크 렌더링 완료 대기
                await page.wait_for_timeout(3000)

                # 메인 프레임 콘텐츠
                main_html = await page.content()
                title = await page.title()

                # iframe 내부 콘텐츠도 수집
                frame_count = len(page.frames) - 1
                frame_texts = []
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    try:
                        fc = await frame.content()
                        fs = BeautifulSoup(fc, "html.parser")
                        frame_texts.append(_safe_visible_text(fs))
                    except Exception:
                        continue

                await context.close()
                await browser.close()

            csr_soup  = BeautifulSoup(main_html, "html.parser")
            main_chars = _safe_visible_text(csr_soup)
            iframe_chars = sum(frame_texts)
            csr_chars = main_chars + iframe_chars

            return {
                "status": "ok",
                "csr_chars": csr_chars,
                "debug": {
                    "final_url": final_url,
                    "http_status": http_status,
                    "page_title": title,
                    "main_chars": main_chars,
                    "iframe_count": frame_count,
                    "iframe_chars": iframe_chars,
                    "html_length": len(main_html),
                },
            }
        except asyncio.TimeoutError:
            return {"status": "error", "error": "브라우저 렌더링 시간 초과", "csr_chars": 0}
        except Exception as e:
            return {"status": "error", "error": str(e), "csr_chars": 0}


def _calc_csr_ratio(ssr_chars: int, csr_raw: dict) -> dict:
    """SSR 글자수와 CSR 글자수를 비교하여 비율과 상태를 반환."""
    status    = csr_raw.get("status", "unavailable")
    csr_chars = csr_raw.get("csr_chars", 0)
    error     = csr_raw.get("error")
    debug     = csr_raw.get("debug")

    if status != "ok" or csr_chars == 0:
        return {
            "status":    status,
            "ssr_chars": ssr_chars,
            "csr_chars": csr_chars,
            "ratio":     None,
            "error":     error,
            "debug":     debug,
        }

    ratio = round(ssr_chars / csr_chars, 3) if csr_chars > 0 else 1.0
    ratio = min(ratio, 1.0)  # CSR는 항상 SSR 이상
    return {
        "status":    "ok",
        "ssr_chars": ssr_chars,
        "csr_chars": csr_chars,
        "ratio":     ratio,
        "error":     None,
        "debug":     debug,
    }


# ── PDP Detection ─────────────────────────────────────────────────────────────

def _detect_pdp(url: str) -> dict:
    parsed   = urlparse(url)
    path     = parsed.path.strip("/")
    segments = [s for s in path.split("/") if s]
    is_pdp   = len(segments) >= 3
    return {
        "is_pdp":        is_pdp,
        "path_segments": segments,
        "pattern":       "/".join(segments) if segments else "",
        "segment_count": len(segments),
    }


# ── Score (총합 100점) ────────────────────────────────────────────────────────

async def _calculate_score(context: dict, robots: dict, csr_ratio: dict) -> dict:
    """룰 엔진 기반 채점. context에 soup, page_data, jsonld_types, base_url 포함."""
    cfg       = get_scoring_config()
    score     = 0
    breakdown = {}

    # 새 4개 카테고리 + 레거시 카테고리 호환 (설정에 존재하는 것만 평가)
    new_cat_keys = ["performance", "accessibility", "seo", "ai_readiness"]
    legacy_cat_keys = ["seo_tags", "robots_txt", "json_ld", "llms_txt", "faq",
                       "summary_box", "heading_structure", "stats_density", "reviews_ssr", "csr_ratio"]
    cat_keys = [k for k in new_cat_keys + legacy_cat_keys if k in cfg]

    for cat_key in cat_keys:
        c = cfg.get(cat_key, {})
        cat_max  = c.get("max", 0)
        special  = c.get("special")
        criteria = [cr for cr in c.get("criteria", []) if cr.get("enabled", True)]

        # ── 특수 로직: robots_txt (비율 계산) ──
        if special == "robots_ratio":
            bots = robots.get("bots", {})
            if bots:
                allowed   = sum(1 for b in bots.values() if not b["blocked"])
                cat_score = round((allowed / len(bots)) * cat_max)
            else:
                cat_score = cat_max
            score += cat_score
            breakdown[cat_key] = {"points": cat_score, "max": cat_max}
            continue

        # ── 특수 로직: CSR 티어 ──
        if special == "csr_tiers":
            csr_status = csr_ratio.get("status", "unavailable")
            ratio = csr_ratio.get("ratio")
            csr_score = 0
            csr_tier  = "poor"

            if csr_status in ("skipped", "blocked"):
                csr_score = 0; csr_tier = csr_status
            elif ratio is None:
                csr_score = 0; csr_tier = "unavailable"
            else:
                for cr in criteria:
                    min_r = cr.get("rule", {}).get("params", {}).get("min_ratio", 1.0)
                    if ratio >= float(min_r):
                        csr_score = min(cr.get("points", 0), cat_max)
                        csr_tier  = cr.get("id", "unknown")
                        break

            score += csr_score
            breakdown[cat_key] = {
                "points": csr_score, "max": cat_max,
                "ratio": ratio, "tier": csr_tier,
                "ssr_chars": csr_ratio.get("ssr_chars", 0),
                "csr_chars": csr_ratio.get("csr_chars", 0),
                "status": csr_ratio.get("status", "unavailable"),
            }
            continue

        # ── 범용 룰 엔진 평가 ──
        cat_score = 0
        items = {}
        for cr in criteria:
            rule = cr.get("rule")
            if not rule:
                continue
            result = await evaluate_rule_async(rule, context)
            passed = result.get("pass", False)
            if passed:
                cat_score += cr.get("points", 0)
            items[cr["id"]] = {
                "label": cr.get("name", cr["id"]),
                "pass":  passed,
                "value": result.get("value"),
                "hint":  result.get("hint"),
                "rule_type": rule.get("type"),
            }

        cat_score = min(cat_score, cat_max)
        score += cat_score
        passed_count = sum(1 for v in items.values() if v["pass"])
        breakdown[cat_key] = {
            "points": cat_score,
            "max": cat_max,
            "passed": passed_count,
            "total": len(criteria),
            "items": items,
        }

    # 등급
    g = cfg.get("grade", {})
    total_max = sum(cfg.get(k, {}).get("max", 0) for k in cat_keys)
    grade = (
        "Good"             if score >= g.get("good", 90) else
        "Need Improvement" if score >= g.get("need_improvement", 70) else
        "Poor"
    )

    return {"total": score, "max": total_max, "grade": grade, "breakdown": breakdown}
