#!/usr/bin/env python3
"""GEO Audit — 로컬 SSR/CSR 분석 CLI

사용법:
  python csr_local.py https://example.com
  python csr_local.py urls.txt                  # 파일에 URL 한 줄씩
  python csr_local.py https://a.com https://b.com
  python csr_local.py --headless https://example.com   # 창 없이 실행

기본적으로 브라우저 창이 열립니다(headed 모드). 403 차단을 우회하기 위함입니다.
--headless 옵션을 사용하면 창 없이 실행됩니다.

결과는 JSON으로 출력됩니다. 웹 UI에 붙여넣기하여 사용할 수 있습니다.

필요 패키지:
  pip install httpx beautifulsoup4 playwright
  python -m playwright install chromium
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# 보일러플레이트(GNB/Footer/쿠키 배너) 제외 로직은 analyzer.py와 단일 소스 공유.
from analyzer import _BOILERPLATE_JS, _boilerplate_selector, _strip_boilerplate_chars


def _visible_text(soup: BeautifulSoup) -> int:
    """script/style 등 비가시 태그를 제외한 본문 글자수 (soup 변조 없음, #11)."""
    parts = []
    for el in soup.find_all(string=True):
        if el.parent and el.parent.name in ("script", "style", "noscript", "svg", "path"):
            continue
        parts.append(el)
    return len(re.sub(r"\s+", "", "".join(parts)))


async def fetch_ssr_chars(url: str) -> int:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GEOAudit/1.0; +https://geoaudit.dev)",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, max_redirects=10) as client:
        r = await client.get(url, headers=headers)
    if "text/html" not in r.headers.get("content-type", ""):
        return 0
    soup = BeautifulSoup(r.text, "html.parser")
    # 보일러 제외 후 측정 — analyzer.py와 동일 셀렉터.
    return _strip_boilerplate_chars(soup, url)


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
window.chrome = { runtime: {} };
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
  params.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : origQuery(params);
"""


async def fetch_csr_chars(url: str, headless: bool = False) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        browser = await p.chromium.launch(
            headless=headless,
            args=launch_args,
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()

        # networkidle은 광고/추적 스크립트가 계속 폴링하는 사이트에서 영원히 안 옴
        # → domcontentloaded로 안정화하고 명시적 wait_for_timeout으로 렌더 대기
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        http_status = resp.status if resp else None
        bp_selector = _boilerplate_selector(url)

        # 403이어도 본문이 충분하면 정상 파싱 시도
        if http_status and http_status in (403, 406):
            await page.wait_for_timeout(2000)
            body_text = await page.inner_text("body")
            body_chars = len(re.sub(r"\s+", "", body_text))
            if body_chars < 10000:
                await context.close()
                await browser.close()
                return {"status": "blocked", "csr_chars": 0, "http_status": http_status}
            # 본문이 충분하면 계속 진행 (일부 사이트는 403이지만 콘텐츠 정상)

        # JS 프레임워크 렌더링 완료 대기 (analyzer.py와 동일 5초)
        await page.wait_for_timeout(5000)
        try:
            await page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass

        # CSS-aware + 보일러 제외 가시 텍스트
        main_text = await page.evaluate(_BOILERPLATE_JS, bp_selector)
        main_chars = len(re.sub(r"\s+", "", main_text))
        title = await page.title()

        frame_texts = []
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                fc = await frame.content()
                fs = BeautifulSoup(fc, "html.parser")
                frame_texts.append(_visible_text(fs))
            except Exception:
                continue

        await context.close()
        await browser.close()

    iframe_chars = sum(frame_texts)

    return {
        "status": "ok",
        "csr_chars": main_chars + iframe_chars,
        "main_chars": main_chars,
        "iframe_chars": iframe_chars,
        "page_title": title,
        "http_status": http_status,
    }


async def analyze_one(url: str, headless: bool = False) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    print(f"  분석 중: {url}", file=sys.stderr, flush=True)

    ssr_chars, csr_raw = await asyncio.gather(
        fetch_ssr_chars(url),
        fetch_csr_chars(url, headless=headless),
    )

    csr_chars = csr_raw.get("csr_chars", 0)
    status = csr_raw.get("status", "error")

    if status == "ok" and csr_chars > 0:
        ratio = round(ssr_chars / csr_chars, 3)
        ratio = min(ratio, 1.0)
        if ratio >= 0.8:
            tier, score = "excellent", 10
        elif ratio >= 0.5:
            tier, score = "good", 7
        elif ratio >= 0.3:
            tier, score = "partial", 4
        else:
            tier, score = "poor", 0
    else:
        ratio = None
        tier = status
        score = 0

    return {
        "url": url,
        "ssr_chars": ssr_chars,
        "csr_chars": csr_chars,
        "main_chars": csr_raw.get("main_chars"),
        "iframe_chars": csr_raw.get("iframe_chars"),
        "ratio": ratio,
        "tier": tier,
        "score": score,
        "max": 10,
        "status": status,
        "page_title": csr_raw.get("page_title"),
    }


async def main():
    if len(sys.argv) < 2:
        print("사용법: python csr_local.py [--headless] <URL 또는 파일> [URL ...]", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    headless = False
    if "--headless" in args:
        headless = True
        args.remove("--headless")

    urls = []
    for arg in args:
        path = Path(arg)
        if path.is_file():
            urls.extend(line.strip() for line in path.read_text().splitlines() if line.strip())
        else:
            urls.append(arg)

    if not urls:
        print("분석할 URL이 없습니다.", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  GEO Audit — 로컬 SSR/CSR 분석 ({len(urls)}개 URL)", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    results = []
    for url in urls:
        try:
            result = await analyze_one(url, headless=headless)
            results.append(result)

            r = result["ratio"]
            ratio_str = f"{r*100:.1f}%" if r is not None else "N/A"
            tier_icon = {
                "excellent": "🟢", "good": "🟡",
                "partial": "🟠", "poor": "🔴",
            }.get(result["tier"], "⚪")

            mc = result.get("main_chars")
            ic = result.get("iframe_chars")
            breakdown = ""
            if mc is not None and ic is not None:
                breakdown = f"  [main {mc:,} + iframe {ic:,}]"
            print(
                f"  {tier_icon} SSR {result['ssr_chars']:,}자 / "
                f"CSR {result['csr_chars']:,}자 = {ratio_str} "
                f"({result['score']}/{result['max']}점){breakdown}",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"  ❌ 오류: {e}", file=sys.stderr)
            results.append({"url": url, "status": "error", "error": str(e)})

    print(f"\n{'='*60}\n", file=sys.stderr)

    output = results[0] if len(results) == 1 else results
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
