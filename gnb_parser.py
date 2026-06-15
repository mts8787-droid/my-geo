"""GNB(글로벌 네비게이션) 링크 발견.

홈페이지의 SSR HTML에서 헤더/네비 링크를 추출해, sitemap 기반 분류에 없는
URL을 찾아 분류한다. 결과는 read-only로 반환하며, 병합/영속화는 호출 측에서
url-classifications-raw PUT으로 처리한다 (Render 쓰기 로직 최소화).

LG.com GNB는 SSR로 렌더되어 httpx만으로 파싱 가능 (Playwright 불필요).
"""

import asyncio
from urllib.parse import urlparse, urljoin, urldefrag

import httpx
from bs4 import BeautifulSoup

from url_classifier import (
    _CLASSIFY_UA,
    FETCH_TIMEOUT_SEC,
    _EXCLUDE_URL_PATTERN,
    _classify_one,
    CLASSIFY_CONCURRENCY,
    load_classifications_all,
)
from rule_engine import _detect_country_dir

GNB_FETCH_BYTES = 2_000_000  # 헤더+메가메뉴 전체 포함 위해 넉넉히


def _home_url(sitemap_url: str, country: str) -> str:
    p = urlparse(sitemap_url)
    return f"{p.scheme}://{p.netloc}/{country}"


async def _fetch_html(client, url: str):
    async with client.stream("GET", url, headers={
        "User-Agent": _CLASSIFY_UA,
        "Accept": "text/html",
        "Accept-Language": "en;q=0.9",
    }, timeout=FETCH_TIMEOUT_SEC, follow_redirects=True) as r:
        if r.status_code != 200:
            return None, f"http_{r.status_code}"
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) >= GNB_FETCH_BYTES:
                break
        enc = r.charset_encoding or "utf-8"
    return bytes(buf).decode(enc, errors="replace"), None


def _extract(soup, home_url: str, country: str, container):
    """container 태그('header'/'nav') 내부 또는 전체(None)에서 같은 국가 링크 추출."""
    host = urlparse(home_url).netloc
    prefix = f"/{country}/"
    bare = f"/{country}"
    out = set()
    roots = soup.find_all(container) if container else [soup]
    for root in roots:
        for a in root.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href[0] in "#?" or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            absu, _ = urldefrag(urljoin(home_url, href))
            pu = urlparse(absu)
            if pu.netloc != host:
                continue
            path = pu.path.rstrip("/") or "/"
            if not (path == bare or path.startswith(prefix)):
                continue
            clean = f"{pu.scheme}://{pu.netloc}{path}"   # query/fragment 제거
            if _EXCLUDE_URL_PATTERN.search(clean):
                continue
            out.add(clean)
    return out


async def discover_gnb(group_id: str, classify_new: bool = True) -> dict:
    """홈페이지 GNB 링크 발견 + (옵션) 신규 URL 분류. read-only 반환."""
    import audit_store

    data = audit_store.load()
    group = next((g for g in data.get("groups", []) if g.get("id") == group_id), None)
    if not group:
        return {"status": "error", "error": f"group not found: {group_id}"}

    sitemap_url = group.get("sitemap_url") or ""
    country = _detect_country_dir(sitemap_url)
    if not country:
        return {"status": "error", "error": f"country not detected from {sitemap_url!r}"}
    home = _home_url(sitemap_url, country)

    existing = load_classifications_all().get(group_id, {})
    known = set(existing.get("classifications", {}).keys())
    known |= {f.get("url") for f in existing.get("failed_urls", []) if isinstance(f, dict)}

    limits = httpx.Limits(max_connections=CLASSIFY_CONCURRENCY * 2,
                          max_keepalive_connections=CLASSIFY_CONCURRENCY)
    async with httpx.AsyncClient(limits=limits) as client:
        html, err = await _fetch_html(client, home)
        if err:
            return {"status": "error", "error": err, "home": home}
        soup = BeautifulSoup(html, "html.parser")

        breakdown = {}
        for name, cont in (("header", "header"), ("nav", "nav"), ("all", None)):
            links = _extract(soup, home, country, cont)
            breakdown[name] = {"total": len(links), "new": len(links - known)}

        # GNB = header ∪ nav (메가메뉴가 header 밖 nav에 있을 수 있어 합집합)
        gnb_links = _extract(soup, home, country, "header") | _extract(soup, home, country, "nav")
        new_urls = sorted(gnb_links - known)

        classified = []
        if classify_new and new_urls:
            sem = asyncio.Semaphore(CLASSIFY_CONCURRENCY)

            async def _one(u):
                async with sem:
                    return await _classify_one(client, u)

            for res in await asyncio.gather(*[_one(u) for u in new_urls]):
                if res.get("failed"):
                    continue
                classified.append({
                    "url": res["url"],
                    "page_type": res["page_type"],
                    "matched_by": res.get("matched_by", ""),
                    "source": "gnb",
                })

    from collections import Counter
    by_type = dict(Counter(c["page_type"] for c in classified).most_common())
    return {
        "status": "ok",
        "group_id": group_id,
        "home": home,
        "country": country,
        "known_sitemap": len(known),
        "gnb_total": len(gnb_links),
        "new_total": len(new_urls),
        "classified_ok": len(classified),
        "by_container": breakdown,
        "by_type": by_type,
        "classified": classified,
    }
