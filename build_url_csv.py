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
        return r.read()


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


def collect_urls(code):
    """국가 index.xml부터 사이트맵을 재귀로 펼쳐 모든 page URL을 수집."""
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

    walk(start, 0)
    return all_urls


def build_csv(code):
    code = code.strip().lower()
    urls = collect_urls(code)
    # 같은 국가 경로만 — 타국 hreflang 오염 / 외부 도메인 제거
    pat = re.compile(rf"^https?://www\.lg\.com/{re.escape(code)}(/|$)")
    filtered = sorted(u for u in urls if pat.match(u))
    if not filtered:
        print(f"[build_url_csv] FATAL: {code} 수집 URL 0건 — index.xml 확인 필요", file=sys.stderr)
        return 1

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, f"lg_urls_{code}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url"])
        for u in filtered:
            w.writerow([u])

    support = sum(1 for u in filtered if "/support" in u or "help-library" in u)
    print(f"[build_url_csv] {code}: 수집 {len(urls)} → 필터 {len(filtered)} (support류 {support}) → {out_path}")
    return 0


def main():
    codes = sys.argv[1:] or ["us"]
    rc = 0
    for code in codes:
        rc |= build_csv(code)
    return rc


if __name__ == "__main__":
    sys.exit(main())
