#!/usr/bin/env python3
"""PLP 가 실제로 쓰는 상품 API(Coveo)에서 활성 PDP 목록을 수집한다.

배경: 사이트맵만으로는 PDP 목록이 최신이 아니다. 2026-08-28 US 실측 기준
      사이트맵에 없는 활성 제품이 211개, 반대로 사이트맵에만 남은 비활성
      제품이 3,253개였다.

왜 API 인가: lg.com PLP 는 CSR 이라 HTML 에 상품 그리드가 없다. 브라우저로
      렌더해도 초기 노출은 16개뿐이고 스크롤로 늘지 않는다. PLP 가 상품을
      받아오는 실제 소스가 Coveo 이고 여기서 카테고리 전체를 한 번에 받는다.
      (US 냉장고: DOM 16 vs API 164)

Coveo 조직이 US 와 그 외로 나뉜다 — 토큰 URL·필터 문법·경로 필드가 전부 다르다:
  US    : lgelectronicsusaproduction… · cq + @ec_store_code · ec_uri_link
  그 외 : lgcorporationproduction…    · aq + @ec_locale_code · ec_model_url_path
  (US 는 비-US org 에 없고, 비-US 는 US org 에 없다)

페이징을 쓰지 않는다: Coveo 기본 정렬이 relevancy 라 요청 간 순서가 흔들려
firstResult 페이지 경계에서 누락된다 (US 실측: 같은 조건 3회에 2361/2060/1831).
sortCriteria 고정도 인덱스에 sortable 필드가 없어 0건. 그래서 카테고리 facet 으로
쪼개 각 카테고리를 1회 질의로 받고, 카테고리가 없는 제품만 전역 스윕으로 보완한다.

사용:
  python3 plp_discover.py --country us            # 수집 + CSV diff 리포트
  python3 plp_discover.py --country uk --merge    # 누락 활성 PDP 를 CSV 에 추가
  python3 plp_discover.py --all                   # 전략 10국 전체
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STRATEGIC = ["us", "uk", "de", "es", "ca", "au", "br", "mx", "in", "vn"]
MAX_RESULTS = 2000          # Coveo 1회 응답 상한(실측)

US = {
    "org": "lgelectronicsusaproductionh9cyypz4",
    "host": "platform.cloud.coveo.com",
    "token_url": "https://www.lg.com/us/plp/api/coveo/v1/getAccessToken",
    "filter_key": "cq",
    "filter": '@ec_store_code="OBS" @ec_model_status_code===ACTIVE',
    "part_field": "ec_category_code",
    "part_expr": '@ec_category_code=="{v}"',
    "path_field": "ec_uri_link",
}
INTL = {
    "org": "lgcorporationproduction0fxcu0qx",
    "host": "lgcorporationproduction0fxcu0qx.org.coveo.com",
    "token_url": "https://www.lg.com/ncms/eu/api/v1/coveo/token",
    "filter_key": "aq",
    "filter": '@ec_locale_code=="{locale}" AND @ec_model_status_code=="ACTIVE"',
    "part_field": "ec_sub_category_id",
    "part_expr": '@ec_sub_category_id=="{v}"',
    "path_field": "ec_model_url_path",
}
# 국가 → Coveo locale 코드. CA 는 영/불 2개 locale 로 나뉜다.
LOCALES = {"uk": ["UK"], "de": ["DE"], "es": ["ES"], "ca": ["CA_EN"],   # 불어(CA_FR)는 감사 대상에서 제외 — 한 국가에 두 언어가 섞이면 표본이 오염된다
           "au": ["AU"], "br": ["BR"], "mx": ["MX"], "in": ["IN"], "vn": ["VN"]}

# 채널 스토어 경로 — 딜러/교육/파트너 전용 스토어프런트다. 같은 제품의 채널별
# 사본이라 소비자 사이트맵에 없는 게 정상이고 GEO 감사 대상도 아니다.
# (DE 실측: 수집 4,110개 중 3,015개(73%)가 이쪽. 걸러내지 않으면 감사 목록이 오염된다)
CHANNEL_SEGMENTS = {
    "dealers", "edustore", "partnerstore", "partners", "partnershop",
    "small-medium-business", "smb", "b2b-store", "epp", "employee",
}


def is_channel_store(path):
    """URL 경로가 채널 스토어면 True. path 는 /<country>/<seg>/... 형태."""
    parts = path.strip("/").split("/")
    return len(parts) > 1 and parts[1].lower() in CHANNEL_SEGMENTS


def _headers():
    from analyzer import build_request_headers
    h = build_request_headers()
    h.pop("Accept-Encoding", None)   # urllib 은 자동 해제하지 않는다
    return h


def _req(url, data=None, extra=None, timeout=120):
    h = _headers()
    h.update(extra or {})
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _top_category(raw, code):
    """제품의 최상위 카테고리명(영문). 국가 간 비교가 되도록 통일한다."""
    if code == "us":
        fc = raw.get("ec_full_category_code")
        try:
            arr = json.loads(fc) if isinstance(fc, str) else fc
            arr = [json.loads(x) if isinstance(x, str) else x
                   for x in (arr if isinstance(arr, list) else [arr])]
            lv1 = [x for x in arr if isinstance(x, dict) and x.get("level") == 1]
            if lv1 and lv1[0].get("categoryName"):
                return lv1[0]["categoryName"]
        except Exception:
            pass
        cat = raw.get("ec_category")
        if isinstance(cat, list) and cat:
            return cat[-1]
        return cat or "(미분류)"
    for f in ("ec_super_category_eng_name", "ec_category_eng_name"):
        v = raw.get(f)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return v
    return "(미분류)"


def _provider(code):
    return US if code == "us" else INTL


def _search(cfg, token, body):
    url = f"https://{cfg['host']}/rest/search/v2?organizationId={cfg['org']}"
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return _req(url, json.dumps(body).encode(), auth)


def _collect_locale(cfg, token, base_filter, path_field, verbose, code):
    """한 필터 조건(국가/locale)의 활성 PDP 경로 집합."""
    fk = cfg["filter_key"]

    # 1) 카테고리 facet 으로 분할 기준 확보
    d = _search(cfg, token, {
        "q": "", fk: base_filter, "numberOfResults": 0,
        "facets": [{"facetId": "p", "field": cfg["part_field"], "type": "specific",
                    "numberOfValues": 500, "sortCriteria": "occurrences"}],
    })
    total = d.get("totalCount", 0)
    facets = d.get("facets") or []
    cats = [(v["value"], v["numberOfResults"]) for v in (facets[0].get("values", []) if facets else [])]
    if verbose:
        print(f"[plp]   카테고리 {len(cats)}개 · 활성 {total}건", flush=True)

    found = {}
    for i, (cat, n) in enumerate(cats, 1):
        if n > MAX_RESULTS and verbose:
            print(f"[plp]   WARN {cat}: {n}건 > 상한 {MAX_RESULTS} — 일부 누락 가능", flush=True)
        body = {"q": "", fk: f'{base_filter} {"AND " if fk == "aq" else ""}'
                             f'{cfg["part_expr"].format(v=cat)}',
                "numberOfResults": min(max(n + 50, 100), MAX_RESULTS), "firstResult": 0,
                "fieldsToInclude": [path_field, "ec_full_category_code", "ec_category",
                                    "ec_super_category_eng_name", "ec_category_eng_name"]}
        try:
            r = _search(cfg, token, body)
        except Exception as e:
            print(f"[plp]   WARN {cat}: {type(e).__name__} {str(e)[:50]}", flush=True)
            continue
        for x in r.get("results", []):
            raw = x.get("raw") or {}
            p = raw.get(path_field)
            if p and not is_channel_store(p):
                found.setdefault(p, _top_category(raw, code))
        if verbose and i % 40 == 0:
            print(f"[plp]   {i}/{len(cats)} · 누적 {len(found)}", flush=True)
        time.sleep(0.2)

    # 2) 보완 스윕 — 카테고리 코드가 없는 제품은 facet 으로 닿지 않는다.
    #    정렬 드리프트가 있지만 '추가만' 하므로 손해가 없다.
    if len(found) < total:
        try:
            r = _search(cfg, token, {"q": "", fk: base_filter,
                                     "numberOfResults": MAX_RESULTS, "firstResult": 0,
                                     "fieldsToInclude": [path_field, "ec_full_category_code",
                                                         "ec_category", "ec_super_category_eng_name",
                                                         "ec_category_eng_name"]})
            add = 0
            for x in r.get("results", []):
                raw = x.get("raw") or {}
                p = raw.get(path_field)
                if p and p not in found and not is_channel_store(p):
                    found[p] = _top_category(raw, code)
                    add += 1
            if verbose and add:
                print(f"[plp]   보완 스윕 +{add}건 (미분류)", flush=True)
        except Exception as e:
            print(f"[plp]   WARN 보완 스윕: {type(e).__name__}", flush=True)
    return found, total


def fetch_active_pdps(code, verbose=True):
    """국가의 활성 PDP URL 집합과 Coveo totalCount 합."""
    cfg = _provider(code)
    token = _req(cfg["token_url"])["token"]
    urls, total = {}, 0
    filters = ([cfg["filter"]] if code == "us"
               else [cfg["filter"].format(locale=l) for l in LOCALES[code]])
    for f in filters:
        if verbose and len(filters) > 1:
            print(f"[plp]  filter: {f}", flush=True)
        got, t = _collect_locale(cfg, token, f, cfg["path_field"], verbose, code)
        total += t
        for p, cat in got.items():
            urls["https://www.lg.com" + p if p.startswith("/") else p] = cat
    return urls, total


def process(code, merge=False, out=None, verbose=True):
    print(f"\n=== {code.upper()} ===", flush=True)
    urls, total = fetch_active_pdps(code, verbose)      # {url: category}
    print(f"[plp] 활성 PDP {len(urls)}개 (Coveo SKU {total})", flush=True)
    if out:
        open(out, "w", encoding="utf-8").write("\n".join(sorted(urls)))
    # PDP 판정 근거로 쓰는 정본 목록. detect_page_type 이 이 파일을 참조한다 —
    # URL 패턴으로는 모델명 형식이 국가마다 달라 오분류가 크다
    # (AU /au/fridge-freezers/french-door/gf-l500mwh/ 242건이 unknown 이었다).
    os.makedirs(os.path.join(HERE, "reports", "plp"), exist_ok=True)
    with open(os.path.join(HERE, "reports", "plp", f"{code}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(urls)))

    csv_path = os.path.join(HERE, "reports", f"lg_urls_{code}.csv")
    if not os.path.exists(csv_path):
        print(f"[plp] {csv_path} 없음 — diff 생략")
        return {"code": code, "active": len(urls), "new": 0, "csv": 0, "by_cat": {}}

    have = {r[0] for r in csv.reader(open(csv_path)) if r and r[0] != "url"}
    missing = sorted(set(urls) - have)

    by_cat = {}
    for u, cat in urls.items():
        d = by_cat.setdefault(cat, {"active": 0, "new": 0})
        d["active"] += 1
    for u in missing:
        by_cat[urls[u]]["new"] += 1

    csv_after = len(have)
    if merge and missing:
        # 비활성 URL 은 지우지 않는다 — 단종 페이지도 #41/#42 감사 대상이다.
        merged = sorted(have | set(missing))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["url"])
            w.writerows([[u] for u in merged])
        csv_after = len(merged)
        print(f"[plp] CSV 갱신: {len(have)} → {csv_after} (+{len(missing)})", flush=True)
    else:
        print(f"[plp] CSV {len(have)}개 · 신규 {len(missing)}개", flush=True)

    return {"code": code, "active": len(urls), "new": len(missing),
            "csv": csv_after, "by_cat": by_cat}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="us")
    ap.add_argument("--all", action="store_true", help="전략 10국 전체")
    ap.add_argument("--merge", action="store_true", help="누락 활성 PDP 를 CSV 에 추가")
    ap.add_argument("--out", help="수집 결과를 이 파일로 저장(단일 국가만)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, HERE)

    codes = STRATEGIC if args.all else [args.country.lower()]
    rows = []
    for c in codes:
        try:
            rows.append(process(c, args.merge, args.out if len(codes) == 1 else None,
                                not args.quiet))
        except Exception as e:
            print(f"[plp] {c.upper()} 실패: {type(e).__name__}: {str(e)[:90]}")

    print("\n=== 국가별 요약 ===")
    print(f"{'국가':<6}{'활성 PDP':>10}{'신규':>8}{'CSV':>9}")
    for r in rows:
        print(f"{r['code'].upper():<6}{r['active']:>10}{r['new']:>8}{r['csv']:>9}")
    print(f"{'합계':<6}{sum(r['active'] for r in rows):>10}"
          f"{sum(r['new'] for r in rows):>8}{sum(r['csv'] for r in rows):>9}")

    # 카테고리 × 국가 매트릭스 (활성 / 신규)
    cats = sorted({c for r in rows for c in r["by_cat"]},
                  key=lambda c: -sum(r["by_cat"].get(c, {}).get("active", 0) for r in rows))
    print("\n=== 카테고리 × 국가 (활성 PDP / 신규) ===")
    hdr = f"{'카테고리':<26}" + "".join(f"{r['code'].upper():>13}" for r in rows) + f"{'합계':>13}"
    print(hdr)
    for cat in cats:
        line = f"{str(cat)[:25]:<26}"
        tot_a = tot_n = 0
        for r in rows:
            d = r["by_cat"].get(cat, {})
            a, n = d.get("active", 0), d.get("new", 0)
            tot_a += a; tot_n += n
            line += f"{(f'{a}/{n}' if a else '-'):>13}"
        line += f"{f'{tot_a}/{tot_n}':>13}"
        print(line)

    out = os.path.join(HERE, "reports", "plp_active_products.json")
    json.dump({"generated_for": codes, "countries": rows},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[plp] 상세 → {out}")


if __name__ == "__main__":
    main()
