#!/usr/bin/env python3
"""PLP 가 실제로 쓰는 상품 API(Coveo)에서 활성 PDP 목록을 수집한다.

배경: 사이트맵만으로는 PDP 목록이 최신이 아니다. 2026-08-28 US 실측 기준
      사이트맵에 없는 활성 제품이 211개, 반대로 사이트맵에만 남은 비활성
      제품이 3,253개였다.

왜 Coveo 인가: lg.com US PLP 는 CSR 이라 HTML 에 상품 그리드가 없다.
      브라우저로 렌더해도 초기 노출은 16개뿐이고 스크롤로 늘지 않는다.
      PLP 가 상품을 받아오는 실제 소스가 Coveo 검색 API 이고, 여기서
      카테고리 전체를 한 번에 받을 수 있다 (냉장고 예: DOM 16 vs API 164).

동작:
  1. https://www.lg.com/<code>/plp/api/coveo/v1/getAccessToken 에서 토큰 획득
     (공개 엔드포인트 — 브라우저/쿠키 불필요)
  2. Coveo /rest/search/v2 를 페이징하며 활성 제품 전체 수집
  3. raw.ec_uri_link 가 실제 PDP 경로 (clickUri 는 모델코드 URL 이라 쓰면 안 된다)

사용:
  python3 plp_discover.py                      # 수집 후 CSV 와 diff 리포트
  python3 plp_discover.py --merge              # 누락된 활성 PDP 를 CSV 에 추가
  python3 plp_discover.py --out urls.txt       # 수집 결과만 파일로

주의: organizationId 는 국가별로 다르다. US 외 국가는 해당 국가 PLP 의
      네트워크 요청에서 orgId 를 확인한 뒤 ORG_IDS 에 추가해야 한다.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COVEO = "https://platform.cloud.coveo.com/rest/search/v2"
PAGE = 200

# 국가별 Coveo organizationId (PLP 네트워크 요청에서 확인한 값)
ORG_IDS = {"us": "lgelectronicsusaproductionh9cyypz4"}


def _headers():
    from analyzer import build_request_headers
    h = build_request_headers()
    h.pop("Accept-Encoding", None)   # urllib 은 자동 해제하지 않는다 — 압축 끄고 받는다
    return h


def _req(url, data=None, extra=None, timeout=60):
    h = _headers()
    h.update(extra or {})
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_token(code):
    return _req(f"https://www.lg.com/{code}/plp/api/coveo/v1/getAccessToken")["token"]


def _search(url, auth, body):
    return _req(url, json.dumps(body).encode(), auth, timeout=120)


def list_categories(url, auth, cq):
    """facet 으로 카테고리 코드와 각 건수를 얻는다."""
    d = _search(url, auth, {
        "q": "", "cq": cq, "numberOfResults": 0,
        "facets": [{"facetId": "cat", "field": "ec_category_code", "type": "specific",
                    "numberOfValues": 500, "sortCriteria": "occurrences"}],
    })
    facets = d.get("facets") or []
    if not facets:
        return [], d.get("totalCount", 0)
    return [(v["value"], v["numberOfResults"]) for v in facets[0].get("values", [])], \
           d.get("totalCount", 0)


def fetch_active_pdps(code, verbose=True):
    """활성(ACTIVE) 제품의 PDP URL 집합. {url: [카테고리코드]}

    카테고리별로 1회씩 질의한다. 전역 페이징(firstResult)은 쓰지 않는다 —
    Coveo 기본 정렬이 relevancy 라 요청 간 순서가 흔들려 페이지 경계에서
    누락이 생긴다 (실측: 같은 조건 3회 실행에 2361 / 2060 / 1831 건).
    카테고리 최대 건수가 426 이라 한 번에 다 받을 수 있어 드리프트가 없다.
    """
    org = ORG_IDS.get(code)
    if not org:
        sys.exit(f"'{code}' 의 Coveo organizationId 를 모릅니다. ORG_IDS 에 추가하세요.")
    token = get_token(code)
    url = f"{COVEO}?organizationId={org}"
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_cq = '@ec_store_code="OBS" @ec_model_status_code===ACTIVE'

    cats, total = list_categories(url, auth, base_cq)
    if verbose:
        print(f"[plp] 카테고리 {len(cats)}개 · 활성 제품 총 {total}건", flush=True)
    if not cats:
        sys.exit("[plp] 카테고리 facet 을 받지 못했습니다.")

    found = {}
    for i, (cat, n) in enumerate(cats, 1):
        body = {
            "q": "", "cq": f'{base_cq} @ec_category_code=="{cat}"',
            "numberOfResults": max(n + 50, 100), "firstResult": 0,
            "fieldsToInclude": ["ec_uri_link", "ec_category_code", "ec_name"],
        }
        try:
            d = _search(url, auth, body)
        except Exception as e:
            print(f"[plp] WARN {cat}: {type(e).__name__} {str(e)[:60]}", flush=True)
            continue
        got = d.get("results", [])
        for r in got:
            raw = r.get("raw", {})
            link = raw.get("ec_uri_link")
            if link:
                found["https://www.lg.com" + link] = raw.get("ec_category_code") or []
        if len(got) < n:
            print(f"[plp] WARN {cat}: {len(got)}/{n} 만 수신", flush=True)
        if verbose and i % 25 == 0:
            print(f"[plp] {i}/{len(cats)} 카테고리 · 누적 {len(found)}", flush=True)
        time.sleep(0.25)

    # 보완 스윕: 카테고리 코드가 없는 제품은 facet 으로 닿지 않는다.
    # 전역 질의 1회(정렬 드리프트가 있지만 '추가만' 하므로 손해가 없다)로 메운다.
    if len(found) < total:
        try:
            d = _search(url, auth, {"q": "", "cq": base_cq, "numberOfResults": 2000,
                                    "firstResult": 0,
                                    "fieldsToInclude": ["ec_uri_link", "ec_category_code"]})
            added = 0
            for r in d.get("results", []):
                raw = r.get("raw", {})
                link = raw.get("ec_uri_link")
                if link and "https://www.lg.com" + link not in found:
                    found["https://www.lg.com" + link] = raw.get("ec_category_code") or []
                    added += 1
            if verbose and added:
                print(f"[plp] 보완 스윕으로 {added}건 추가 (미분류 제품)", flush=True)
        except Exception as e:
            print(f"[plp] WARN 보완 스윕 실패: {type(e).__name__}", flush=True)

    if verbose:
        print(f"[plp] 수집 {len(found)} / totalCount {total}", flush=True)
    if len(found) < total * 0.98:
        print(f"[plp] ⚠ 수집 {len(found)} < {total} — 카테고리 미분류 제품이 있을 수 있음", flush=True)
    return found, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="us")
    ap.add_argument("--merge", action="store_true",
                    help="사이트맵 CSV 에 없는 활성 PDP 를 CSV 에 추가")
    ap.add_argument("--out", help="수집한 PDP URL 을 이 파일로 저장(줄 단위)")
    args = ap.parse_args()
    code = args.country.lower()

    sys.path.insert(0, HERE)
    found, total = fetch_active_pdps(code)
    print(f"\n[plp] 활성 PDP {len(found)}개 수집 (totalCount {total})")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(found)))
        print(f"[plp] → {args.out}")

    csv_path = os.path.join(HERE, "reports", f"lg_urls_{code}.csv")
    if not os.path.exists(csv_path):
        print(f"[plp] {csv_path} 없음 — diff 생략")
        return

    from page_type import detect_page_type
    existing = [r[0] for r in csv.reader(open(csv_path)) if r and r[0] != "url"]
    have = set(existing)
    missing = sorted(set(found) - have)
    csv_pdp = {u for u in have if detect_page_type(None, u).get("id") == "pdp"}
    stale = csv_pdp - set(found)

    print(f"[plp] CSV PDP {len(csv_pdp)}개")
    print(f"[plp] 사이트맵에 없는 활성 PDP : {len(missing)}")
    print(f"[plp] 사이트맵에만 있는 비활성  : {len(stale)}  (단종 추정 — 제거하지 않음)")
    for u in missing[:5]:
        print("       +", u)

    if args.merge and missing:
        # 비활성 URL 은 지우지 않는다 — 단종 페이지도 감사 대상(#41/#42)이 될 수 있다.
        merged = sorted(have | set(missing))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["url"])
            w.writerows([[u] for u in merged])
        print(f"[plp] CSV 갱신: {len(existing)} → {len(merged)} (+{len(missing)})")


if __name__ == "__main__":
    main()
