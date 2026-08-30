#!/usr/bin/env python3
"""국가별 lg.com 사이트맵을 펼쳐 audit 입력 CSV(reports/lg_urls_<code>.csv)를 생성.

배경: 기존 입력 CSV는 /<code>/sitemap.xml(main)만으로 만들어져 support/help-library
URL이 거의 누락됐다(US: 5788개 중 support 9개). 국가 index.xml(sitemapindex)에는
main 외에 sitemap-cs.xml(고객지원, ~21000개)·business/sitemap.xml 이 함께 등재돼 있어
이를 모두 펼쳐야 support 페이지가 정상 수집된다.

동작: https://www.lg.com/<code>/index.xml 부터 sitemapindex를 재귀로 펼쳐
모든 urlset의 page URL을 수집 → 같은 국가 경로(^https?://www.lg.com/<code>(/|$))만 필터 →
중복 제거·정렬 후 header가 `url`인 CSV로 저장(batch_audit.read_urls_from_csv 호환).

stdlib만 사용(urllib + ElementTree) — 별도 venv/httpx 불필요. Akamai 403 회피 위해
운영 analyzer와 동일한 AUDIT_USER_AGENT 헤더 사용.

사용: python3 build_url_csv.py us [uk jp ...]
"""

import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

UA = os.environ.get(
    "AUDIT_USER_AGENT",
    "MyGEOAudit/1.0 (Audit agent operated by D2C Digital Marketing Team, LG Electronics)",
)
MAX_DEPTH = int(os.environ.get("SITEMAP_AGENT_MAX_DEPTH", 4))
TIMEOUT = float(os.environ.get("SITEMAP_AGENT_URL_TIMEOUT", 60))
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def _local_tag(tag):
    return tag.split("}")[-1].lower() if "}" in tag else tag.lower()


def _fetch(url):
    # Akamai는 UA만으로는 부족 — Accept 헤더 없으면 stdlib urllib 요청을 403 처리한다.
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    # urllib 은 압축을 자동 해제하지 않는다. 헤더가 없어도 .xml.gz 를 그대로 주는
    # 사이트맵이 있어 매직바이트로도 판별한다 (UK index.xml 이 이 경우였다 —
    # 압축 바이트를 XML 로 파싱하려다 'mismatched tag' 로 죽었다).
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        import zlib
        raw = zlib.decompress(raw)
    elif enc == "br":
        try:
            import brotli
            raw = brotli.decompress(raw)
        except ImportError:
            pass
    return raw


def _parse_one(url):
    """단일 사이트맵 fetch → (child_sitemaps, page_urls)."""
    root = ET.fromstring(_fetch(url))
    is_index = _local_tag(root.tag) == "sitemapindex"
    children, pages = [], []
    for elem in root.iter():
        if _local_tag(elem.tag) != "loc" or not elem.text:
            continue
        u = elem.text.strip()
        if not u.startswith("http"):
            continue
        (children if is_index else pages).append(u)
    return children, pages


# index.xml(sitemapindex)이 없는 국가용 폴백. US 만 index.xml 이 있고 나머지
# 9개국은 HTML 404 를 준다 — 대신 아래 3종이 개별로 존재한다(2026-08-28 확인).
#   sitemap.xml(메인) · sitemap-cs.xml(고객지원) · business/sitemap.xml(B2B)
FALLBACK_SITEMAPS = ["sitemap.xml", "sitemap-cs.xml", "business/sitemap.xml",
                     "hreflang_sitemap.xml"]

# 감사 코드 ≠ 실제 사이트 경로인 국가. CSV 는 코드로, 사이트맵/필터는 경로로 쓴다.
# CA 는 lg.com/ca 가 없고 /ca_en(영문)·/ca_fr(불어)로 나뉜다 — 감사는 영문만 본다.
SITE_PATHS = {"ca": "ca_en"}

# 영구 수집 제외 경로. 감사·집계 대상이 아니므로 URL 목록 단계에서 아예 뺀다.
#   lg-story  — 보도자료가 아닌 브랜드 스토리텔링 콘텐츠
#   lifesgood — 캠페인 페이지
DROP_PATH_PATTERNS = [
    re.compile(r"/lg-story(/|$)"),
    re.compile(r"/lifesgood(/|$)"),
]


def _is_dropped(url):
    return any(p.search(url) for p in DROP_PATH_PATTERNS)


def collect_urls(code):
    """국가 index.xml부터 사이트맵을 재귀로 펼쳐 모든 page URL을 수집.

    index.xml 이 sitemapindex 가 아니면(= 그 국가엔 없음) 알려진 개별 사이트맵을
    각각 펼친다.
    """
    start = f"https://www.lg.com/{code}/index.xml"
    seen_sitemaps, all_urls = set(), set()

    def walk(url, depth):
        if url in seen_sitemaps or depth > MAX_DEPTH:
            return
        seen_sitemaps.add(url)
        try:
            children, pages = _parse_one(url)
        except Exception as e:
            print(f"[build_url_csv] WARN: sitemap 실패 ({url}): {e}", file=sys.stderr)
            if depth == 0:
                raise
            return
        print(f"[build_url_csv]   {url} → children {len(children)} / pages {len(pages)}")
        all_urls.update(pages)
        for c in children:
            walk(c, depth + 1)

    try:
        walk(start, 0)
    except Exception as e:
        print(f"[build_url_csv]   index.xml 사용 불가 ({type(e).__name__}) → 개별 사이트맵으로 전환",
              file=sys.stderr)
    if not all_urls:
        for name in FALLBACK_SITEMAPS:
            walk(f"https://www.lg.com/{code}/{name}", 1)   # depth 1 = 실패해도 raise 안 함
    return all_urls


def build_csv(code):
    code = code.strip().lower()
    site = SITE_PATHS.get(code, code)
    urls = collect_urls(site)
    # 같은 국가 경로만 — 타국 hreflang 오염 / 외부 도메인 제거
    pat = re.compile(rf"^https?://www\.lg\.com/{re.escape(site)}(/|$)")
    filtered = sorted(u for u in urls if pat.match(u) and not _is_dropped(u))
    dropped = sum(1 for u in urls if pat.match(u) and _is_dropped(u))
    if dropped:
        print(f"[build_url_csv]   영구 제외 {dropped}건 (lg-story/lifesgood)")
    if not filtered:
        print(f"[build_url_csv] FATAL: {code}(경로 {site}) 수집 URL 0건 — 사이트맵 확인 필요",
              file=sys.stderr)
        return 1

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, f"lg_urls_{code}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url"])
        for u in filtered:
            w.writerow([u])

    # support 경로는 국가별로 현지어다 (br=suporte, es=soporte, vn=tro-giup...).
    # 영어 문자열로 세면 BR 이 11,237건인데 3건으로 표시된다 — page_type 으로 센다.
    try:
        from page_type import detect_page_type
        support = sum(1 for u in filtered
                      if str(detect_page_type(None, u).get("id", "")).startswith("support"))
    except Exception:
        support = sum(1 for u in filtered if "/support" in u or "help-library" in u)
    print(f"[build_url_csv] {code}: 수집 {len(urls)} → 필터 {len(filtered)} (support류 {support}) → {out_path}")
    return 0


def main():
    codes = sys.argv[1:] or ["us"]
    rc = 0
    for code in codes:
        # 한 국가가 실패해도 나머지는 계속 — 루트 사이트맵 파싱 실패가 전체 실행을
        # 중단시키던 문제(2026-08-28 UK 압축 응답)를 막는다.
        try:
            rc |= build_csv(code)
        except Exception as e:
            print(f"[build_url_csv] ERROR {code}: {type(e).__name__}: {e}", file=sys.stderr)
            rc |= 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
