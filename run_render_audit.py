#!/usr/bin/env python3
"""reports/lg_urls_<code>.csv 를 Render의 lightweight bulk 엔드포인트로 감사해
data/run_results/<code>_<date>_run_<hash>.json (캐노니컬 run_results 형식)으로 저장.

배경: 로컬엔 httpx/playwright env가 없고, CSR 렌더링도 불필요하므로 Render의
`/analyze-bulk-async`(analyze_url lightweight=True, csr 베이스라인 주입)에 위임한다.
한 요청 최대 50 URL · 제출 5회/분 제한 → 50개씩 직렬 청크로 제출·폴링·회수한다.
직렬 처리라 Render 512MB 메모리와 rate limit이 자동으로 안전하다.

기본은 전수가 아니라 **page_type별 최대 100개 샘플**만 감사한다(다른 국가 run과 동일).
감사 전에 URL만으로 page_type을 사전 분류(detect_page_type soup=None, url_pattern 1패스 →
실측 분류와 99% 일치)하므로 전수 fetch가 필요 없다. 전수가 꼭 필요하면 --full.

resume: 출력 파일에 이미 들어간 url은 건너뛴다(중간 중단 후 재개 가능).

사용: python3 run_render_audit.py [code] [--per-type 100] [--limit N] [--chunk 50] [--full]
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = os.environ.get("RENDER_BASE", "https://my-geo-89ft.onrender.com")
HERE = os.path.dirname(os.path.abspath(__file__))
MIN_SUBMIT_GAP = 13.0  # 초 — 제출 5회/분 제한 여유 있게 준수
POLL_GAP = 4.0
POLL_MAX = 180         # 청크당 최대 12분 대기


def _req(path, payload=None, retries=6):
    """Render 일시 502/타임아웃/네트워크 오류를 백오프 재시도로 흡수."""
    last = None
    for attempt in range(retries):
        try:
            if payload is None:
                req = Request(BASE + path)
            else:
                req = Request(BASE + path, data=json.dumps(payload).encode(),
                              method="POST", headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"), strict=False)
        except HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (URLError, TimeoutError, OSError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(min(60, 5 * (2 ** attempt)))  # 5,10,20,40,60,60s
    raise RuntimeError(f"{path} 재시도 {retries}회 실패: {last}")


def _post(path, payload):
    return _req(path, payload)


def _get(path):
    return _req(path)


# 발행일 내림차순으로 뽑을 page_type. 계속 새 문서가 나오는 타입은 URL 정렬
# 순서대로 자르면 오래된 문서만 반복 감사하게 된다 (reports/page_dates.json 필요).
RECENCY_TYPES = {"newsroom", "press_media", "support_troubleshoot"}

# 감사 자체를 하지 않는 page_type. 'unknown' 은 분류 실패라 예전부터 제외였고,
# 'about' 은 회사 소개 페이지라 GEO 검수 대상이 아니라고 결정됐다(2026-08-28).
SKIP_TYPES = {"unknown", "about"}



# PDP 샘플에 반드시 포함해야 하는 제품군. 카테고리명이 국가마다 현지어라
# 경로 세그먼트를 다국어 키워드로 매칭한다 (2026-08-28 전 국가 URL 로 검증).
REQUIRED_PDP_CATEGORIES = {
    "tv":           ["tv", "televis", "fernseh", "oled", "qned", "nanocell", "nghe-nhin"],
    "audio":        ["audio", "soundbar", "speaker", "lautsprecher", "altavoz", "colunas",
                     "som", "xboom", "home-cinema", "home-theater", "loa", "am-thanh"],
    "monitor":      ["monitor", "moniteur", "man-hinh"],
    "refrigerator": ["refriger", "fridge", "kuhlschrank", "kuehlschrank", "geladeira",
                     "nevera", "frigorific", "tu-lanh"],
    "washer":       ["wash", "laundry", "wasch", "waesche", "lavadora", "lavanderia",
                     "lava-roupas", "giat", "secadora"],
    "dishwasher":   ["dishwash", "geschirrspul", "geschirrspül", "spuelmaschine",
                     "lavavajilla", "lava-louca", "rua-bat"],
    # 에어컨이 없는 국가(UK 등)는 히트펌프가 같은 공조 제품군을 대표한다.
    "aircon":       ["air-condition", "aircon", "klima", "aire-acondicionado",
                     "ar-condicionado", "dieu-hoa",
                     "heat-pump", "heatpump", "waermepumpe", "wärmepumpe",
                     "bomba-de-calor", "aerotermia", "bom-nhiet", "heating-solution"],
    "aircare":      ["air-care", "air-purif", "purificador", "luftreiniger",
                     "cham-soc-khong-khi", "khong-khi", "air-solution", "air-quality"],
}
# 카테고리 균등 배분을 적용할 page_type. 알파벳순으로 자르면 US 는 76개 카테고리 중
# 3개(에어컨 96개)만 뽑혀 TV·모니터가 통째로 빠졌다.
BALANCED_TYPES = {"pdp", "plp"}


def _category_seg(url):
    """URL 의 국가 다음 첫 세그먼트 = 제품 카테고리."""
    m = re.match(r"https?://www\.lg\.com/[^/]+/([^/?#]+)", url)
    return m.group(1).lower() if m else ""


def _balanced_pick(urls, per_type):
    """카테고리 균등 샘플. 필수 제품군을 먼저 채우고 나머지는 라운드로빈."""
    from collections import defaultdict
    groups = defaultdict(list)
    for u in urls:
        groups[_category_seg(u)].append(u)

    req_segs = defaultdict(list)
    for seg in groups:
        for key, kws in REQUIRED_PDP_CATEGORIES.items():
            if any(w in seg for w in kws):
                req_segs[key].append(seg)
                break

    picked, seen = [], set()

    def take(pool, n):
        for u in pool:
            if len(picked) >= per_type or n <= 0:
                return
            if u not in seen:
                seen.add(u); picked.append(u); n -= 1

    # 1) 필수 제품군 우선 — 존재하는 것만 균등 할당
    present = [k for k in REQUIRED_PDP_CATEGORIES if k in req_segs]
    if present:
        quota = max(1, int(per_type * 0.7) // len(present))
        for k in present:
            pool = [u for seg in sorted(req_segs[k]) for u in groups[seg]]
            take(pool, quota)

    # 2) 남은 자리는 전 카테고리 라운드로빈
    lists = [groups[s][:] for s in sorted(groups)]
    while len(picked) < per_type and any(lists):
        for L in lists:
            if len(picked) >= per_type:
                break
            while L:
                u = L.pop(0)
                if u not in seen:
                    seen.add(u); picked.append(u); break
    return picked[:per_type]


def _load_page_dates():
    path = os.path.join(HERE, "reports", "page_dates.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sample_by_page_type(urls, per_type):
    """감사 전 URL만으로 page_type 사전 분류 후 타입별 per_type개로 축소.

    detect_page_type(soup=None, url) 는 url_pattern 1패스만 타므로 fetch 불필요.
    실측(soup 포함) 분류와 99% 일치 검증됨. SKIP_TYPES 는 감사 자체를 하지 않는다.

    RECENCY_TYPES 는 발행일 내림차순으로 정렬해 최신 문서부터 뽑는다.
    날짜를 모르는 URL 은 뒤로 보낸다(감사 자체는 하되 최신분에 밀린다).
    """
    from collections import defaultdict
    from page_type import detect_page_type
    dates = _load_page_dates()
    buckets = defaultdict(list)
    for u in urls:
        pt = detect_page_type(None, u).get("id")
        if pt and pt not in SKIP_TYPES:
            buckets[pt].append(u)
    out, notes = [], []
    for pt in sorted(buckets):
        group = buckets[pt]
        if pt in RECENCY_TYPES and dates and any(u in dates for u in group):
            group = sorted(group, key=lambda u: dates.get(u, ""), reverse=True)
            sel = group[:per_type]
            notes.append(f"{pt}={len(sel)}(최신순,날짜 {sum(1 for u in sel if u in dates)})")
        elif pt in BALANCED_TYPES:
            sel = _balanced_pick(group, per_type)
            cats = len({_category_seg(u) for u in sel})
            notes.append(f"{pt}={len(sel)}(카테고리 {cats}종)")
        else:
            sel = group[:per_type]
            notes.append(f"{pt}={len(sel)}")
        out.extend(sel)
    print(f"[render-audit] page_type 샘플(≤{per_type}/type): " + " ".join(notes))
    return out


def _load_prior_results(code, today):
    """이전 감사 run 에서 url → result 맵. 최신 run 이 우선.

    URL 목록이 바뀌어도 상당수 URL 은 그대로다. 이미 측정한 페이지를 다시 fetch 할
    이유가 없으므로 재사용한다. 단 page_type 은 현재 분류기로 다시 판정해 덮어쓴다
    — 분류 규칙이 바뀌면(예: newsroom → press_media) 예전 값이 채점 대상 판정을
    틀리게 만든다.
    """
    from page_type import detect_page_type
    pat = re.compile(rf"^{re.escape(code)}_(\d{{4}}-\d{{2}}-\d{{2}})_run_[0-9a-f]+\.json$")
    runs = []
    d = os.path.join(HERE, "data", "run_results")
    for fn in os.listdir(d):
        m = pat.match(fn)
        if m and m.group(1) != today:
            runs.append((m.group(1), fn))
    out = {}
    for date, fn in sorted(runs):            # 오래된 것부터 → 최신이 덮어씀
        try:
            doc = json.load(open(os.path.join(d, fn)))
        except Exception:
            continue
        for x in doc.get("summary", []):
            r = x.get("result") or {}
            if r.get("score") and r.get("url"):
                r = dict(r)
                r["page_type"] = detect_page_type(None, r["url"])
                r["_reused_from"] = date
                out[r["url"]] = {"url": r["url"], "error": None, "result": r}
    return out


def _run_chunk(urls):
    """50개 이하 URL 청크를 제출→완료까지 폴링→items 반환."""
    resp = _post("/analyze-bulk-async", {"urls": urls, "scope": "all"})
    job_id = resp["job_id"]
    for _ in range(POLL_MAX):
        time.sleep(POLL_GAP)
        job = _get(f"/analyze-bulk-status/{job_id}")
        st = job.get("status")
        if st in ("done", "error", "cancelled"):
            return job.get("items", []), st, job.get("error")
    return [], "timeout", f"job {job_id} 폴링 타임아웃"


def _run_chunk_local(urls):
    """Render 대신 로컬 analyze_url(lightweight)로 청크 처리. IP차단 국가(AU/IN) 우회용.
    Render 데이터센터 IP는 Akamai 403을 받지만 Mac Mini 주거용 IP는 통과한다(실측 확인)."""
    import asyncio
    from analyzer import analyze_url  # httpx 필요 — /usr/bin/python3 env 에서만 동작
    conc = int(os.environ.get("LOCAL_AUDIT_CONCURRENCY", "5"))

    async def _run():
        sem = asyncio.Semaphore(conc)

        async def one(u):
            async with sem:
                try:
                    return {"url": u, "result": await analyze_url(u, lightweight=True)}
                except Exception as e:
                    return {"url": u, "result": None, "error": f"{type(e).__name__}: {str(e)[:120]}"}

        return await asyncio.gather(*[one(u) for u in urls])

    return asyncio.run(_run()), "done", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="?", default="us")
    ap.add_argument("--limit", type=int, default=0, help="처리할 URL 수 제한(시험용)")
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--per-type", type=int, default=100,
                    help="page_type별 최대 감사 수 (다른 국가와 동일한 샘플 방식, 기본 100)")
    ap.add_argument("--full", action="store_true",
                    help="전수 감사(비권장). 미지정 시 반드시 page_type별 샘플만 감사.")
    ap.add_argument("--no-reuse", action="store_true",
                    help="이전 run 결과 재사용 없이 전부 새로 측정")
    ap.add_argument("--local", action="store_true",
                    help="Render 대신 로컬 analyze_url(lightweight)로 감사. IP차단 국가(AU/IN) 우회용. /usr/bin/python3 필요.")
    args = ap.parse_args()

    code = args.code.lower()
    csv_path = os.path.join(HERE, "reports", f"lg_urls_{code}.csv")
    urls = [r[0].strip() for r in csv.reader(open(csv_path)) if r and r[0].strip() != "url"]
    # sitemap에 공백/제어문자가 든 잘못된 URL이 섞이면 Render bulk가 청크 전체를
    # 400 처리(50건 동반 거부)한다 → 사전 제거.
    bad = [u for u in urls if (not u.startswith("http")) or re.search(r"\s", u)]
    if bad:
        print(f"[render-audit] invalid URL {len(bad)}건 제외 (공백/형식 오류). 예: {bad[0]}")
        urls = [u for u in urls if u not in set(bad)]
    if not args.full:
        urls = _sample_by_page_type(urls, args.per_type)
    if args.limit:
        urls = urls[: args.limit]

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_hash = uuid.uuid4().hex[:12]
    out_path = os.path.join(HERE, "data", "run_results", f"{code}_{date}_run_{run_hash}.json")

    # resume: 같은 날짜의 기존 출력에서 처리된 url 로드
    done_urls, summary = set(), []
    for fn in os.listdir(os.path.join(HERE, "data", "run_results")):
        if fn.startswith(f"{code}_{date}_run_") and fn.endswith(".json"):
            try:
                prev = json.load(open(os.path.join(HERE, "data", "run_results", fn)))
                if prev.get("status") == "running" or prev.get("_resumable"):
                    for it in prev.get("summary", []):
                        done_urls.add(it["url"])
                        summary.append(it)
                    out_path = os.path.join(HERE, "data", "run_results", fn)
                    run_hash = prev.get("id", run_hash).replace("run_", "")
            except Exception:
                pass

    # 이전 run 에서 이미 측정한 URL 은 재사용 (같은 날짜 resume 과는 별개)
    if not args.no_reuse:
        prior = _load_prior_results(code, date)
        reused = 0
        for u in urls:
            if u not in done_urls and u in prior:
                summary.append(prior[u])
                done_urls.add(u)
                reused += 1
        if reused:
            print(f"[render-audit] 이전 run 재사용 {reused}건 (신규 측정 {len(urls)-len(done_urls)}건)")

    todo = [u for u in urls if u not in done_urls]
    started = datetime.now(timezone.utc).isoformat()
    print(f"[render-audit] {code}: 총 {len(urls)} / 처리완료 {len(done_urls)} / 남은 {len(todo)} → {out_path}")

    def _save(status):
        ok = sum(1 for it in summary if (it.get("result") or {}).get("score"))
        doc = {
            "id": f"run_{run_hash}", "schedule_id": f"render_bulk_{code}",
            "schedule_name": f"Render bulk audit - {code}", "group_id": f"grp_lg_{code}",
            "group_name": f"LG Sitemap - {code}", "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat() if status != "running" else None,
            "status": status, "url_count": len(urls), "success_count": ok,
            "summary": summary, "error": None, "_resumable": True,
        }
        tmp = out_path + ".tmp"
        json.dump(doc, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, out_path)

    last_submit = 0.0
    for i in range(0, len(todo), args.chunk):
        batch = todo[i:i + args.chunk]
        if not args.local:  # 로컬 모드는 Render rate limit 무관 → 제출 간격 불필요
            gap = MIN_SUBMIT_GAP - (time.time() - last_submit)
            if gap > 0:
                time.sleep(gap)
        last_submit = time.time()
        try:
            items, st, err = (_run_chunk_local if args.local else _run_chunk)(batch)
        except Exception as e:
            items, st, err = [], "failed", f"{type(e).__name__}: {e}"
        if st != "done":
            print(f"[render-audit] WARN 청크 {i//args.chunk} status={st} err={err} — 부분 수집 {len(items)}")
        summary.extend(items)
        _save("running")
        ok = sum(1 for it in summary if (it.get('result') or {}).get('score'))
        print(f"[render-audit]   진행 {len(summary)}/{len(urls)} (성공 {ok}) "
              f"청크{i//args.chunk} +{len(items)} [{st}]")

    _save("ok")
    ok = sum(1 for it in summary if (it.get('result') or {}).get('score'))
    print(f"[render-audit] 완료: {len(summary)}건 (성공 {ok}) → {out_path}")
    _refresh_report()
    return 0


def _refresh_report():
    """감사 완료 시 감점 사유 종합 리포트(reports/audit_report.txt)를 자동 갱신.

    리포트 생성 실패는 감사 결과에 영향 주지 않도록 흡수한다."""
    try:
        import gen_audit_report
        gen_audit_report.gen()
    except Exception as e:
        print(f"[render-audit] 리포트 자동 갱신 건너뜀: {type(e).__name__}: {e}")
    try:
        import gen_dashboard_data
        gen_dashboard_data.main()
    except Exception as e:
        print(f"[render-audit] 대시보드 데이터 갱신 건너뜀: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
