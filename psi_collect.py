#!/usr/bin/env python3
"""PageSpeed Insights(Lighthouse) 측정값을 URL별로 수집해 캐시에 적재한다.

감사 본체(analyze_url)와 분리된 별도 패스다. PSI 1회 호출이 약 60초라 감사 안에
넣으면 월간 감사가 3시간 → 17시간이 되기 때문이다. 여기서 캐시를 채워두면
rule_engine 의 psi_* 룰이 호출 없이 캐시만 읽어 판정한다.

수집 지표:
  - performance      : server-response-time(#1 TTFB), LCP/CLS/TBT, CrUX 실측 p75
  - agentic-browsing : Lighthouse 에이전트형 브라우징 카테고리 점수 + 개별 audit
                       (WebMCP audit 3종은 대상 페이지가 Origin Trial 토큰을
                        서빙해야 평가된다. 미배포면 null 로 남는다.)

두 카테고리는 한 번의 호출로 동시 수신된다 — 호출 비용이 2배가 되지 않는다.

실측 처리량(2026-08 캘리브레이션, API 키 사용):
  동시성  2 →  2.14건/분      동시성 20 → 13.88건/분 (성공률 100%)
  동시성  5 →  4.62건/분      동시성 30 → 14.06건/분 (500 발생 — 포화)
  동시성 10 →  5.42건/분
  → 기본 동시성 20. 쿼터는 25,000건/일.

resume: 캐시에 이미 있는 URL은 건너뛴다. 중단 후 재실행하면 이어서 수집한다.

사용:
  PSI_API_KEY=... python3 psi_collect.py --run data/run_results/us_2026-08-26_run_*.json
  PSI_API_KEY=... python3 psi_collect.py --country us --date 2026-08-26
  PSI_API_KEY=... python3 psi_collect.py --url https://www.lg.com/us/
"""
import argparse
import glob
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "psi_cache.json")
ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# 수집할 Lighthouse lab audit — {캐시 키: audit id}
LAB_AUDITS = {
    "server_response_time_ms": "server-response-time",
    "network_server_latency_ms": "network-server-latency",
    "lcp_ms": "largest-contentful-paint",
    "cls": "cumulative-layout-shift",
    "tbt_ms": "total-blocking-time",
    "speed_index_ms": "speed-index",
}

# CrUX 실측(field) 지표 — {캐시 키: PSI metric 키}
FIELD_METRICS = {
    "crux_ttfb_p75_ms": "EXPERIMENTAL_TIME_TO_FIRST_BYTE",
    "crux_lcp_p75_ms": "LARGEST_CONTENTFUL_PAINT_MS",
    "crux_cls_p75": "CUMULATIVE_LAYOUT_SHIFT_SCORE",
    "crux_inp_p75_ms": "INTERACTION_TO_NEXT_PAINT",
    "crux_fcp_p75_ms": "FIRST_CONTENTFUL_PAINT_MS",
}

_lock = threading.Lock()


# ── 캐시 ──────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


# ── PSI 호출 ──────────────────────────────────────────────────────────────────

def fetch(url: str, key: str, strategy: str, timeout: int = 300) -> dict:
    q = urllib.parse.urlencode([
        ("url", url), ("strategy", strategy),
        ("category", "performance"), ("category", "agentic-browsing"),
        ("key", key),
    ])
    with urllib.request.urlopen(f"{ENDPOINT}?{q}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def extract(doc: dict) -> dict:
    """PSI 응답(약 1.7MB)에서 필요한 값만 추린다."""
    lr = doc.get("lighthouseResult") or {}
    audits = lr.get("audits") or {}
    out = {
        "lighthouse_version": lr.get("lighthouseVersion"),
        "final_url": lr.get("finalUrl"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    for key, aid in LAB_AUDITS.items():
        out[key] = (audits.get(aid) or {}).get("numericValue")

    # CrUX: URL 단위 데이터가 없으면 PSI 가 origin 값으로 대체해 내려준다.
    # 어느 쪽인지 구분해야 '이 페이지 실측'인지 '도메인 평균'인지 판별할 수 있다.
    le = doc.get("loadingExperience") or {}
    ole = doc.get("originLoadingExperience") or {}
    out["crux_scope"] = "url" if le.get("id") and le.get("id") != ole.get("id") else "origin"
    for key, mid in FIELD_METRICS.items():
        out[key] = ((le.get("metrics") or {}).get(mid) or {}).get("percentile")

    # 에이전트형 브라우징 — 카테고리 점수(통과 비율) + 개별 audit score
    cat = (lr.get("categories") or {}).get("agentic-browsing") or {}
    ag_audits = {}
    for ref in cat.get("auditRefs", []):
        ag_audits[ref["id"]] = (audits.get(ref["id"]) or {}).get("score")
    out["agentic"] = {"score": cat.get("score"), "audits": ag_audits}
    return out


# ── 수집 ──────────────────────────────────────────────────────────────────────

def collect(urls, key, strategy="mobile", concurrency=20, retries=3, save_every=25):
    cache = load_cache()
    todo = [u for u in urls if u not in cache]
    print(f"[psi] 대상 {len(urls)} / 캐시보유 {len(urls) - len(todo)} / 수집 {len(todo)}"
          f"  동시성={concurrency} strategy={strategy}")
    if not todo:
        return cache

    eta = len(todo) / 13.88
    print(f"[psi] 예상 소요 약 {eta/60:.1f}시간 ({eta:.0f}분)")

    done = {"n": 0, "ok": 0, "fail": 0}
    t0 = time.time()

    def worker(u):
        last = None
        for attempt in range(retries):
            try:
                rec = extract(fetch(u, key, strategy))
                with _lock:
                    cache[u] = rec
                    done["n"] += 1; done["ok"] += 1
                    if done["ok"] % save_every == 0:
                        save_cache(cache)
                    _progress(done, len(todo), t0)
                return
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(min(120, 20 * (2 ** attempt)))  # 20, 40, 80s
        with _lock:
            cache[u] = {"error": last, "fetched_at": datetime.now(timezone.utc).isoformat()}
            done["n"] += 1; done["fail"] += 1
            _progress(done, len(todo), t0)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(worker, todo))

    save_cache(cache)
    el = time.time() - t0
    print(f"\n[psi] 완료: 성공 {done['ok']} · 실패 {done['fail']} · "
          f"{el/60:.1f}분 ({done['ok']/el*60:.2f}건/분) → {CACHE}")
    return cache


def _progress(done, total, t0):
    n = done["n"]
    if n % 10 and n != total:
        return
    el = time.time() - t0
    rate = n / el * 60 if el else 0
    rem = (total - n) / rate if rate else 0
    print(f"[psi]   {n}/{total} (성공 {done['ok']} 실패 {done['fail']}) "
          f"{rate:.1f}건/분 남은시간 {rem:.0f}분", flush=True)


# ── URL 소스 ──────────────────────────────────────────────────────────────────

def urls_from_run(pattern: str, exclude_page_types=frozenset()):
    """run_results 에서 감사 성공한 URL 추출. 제외 page_type 은 건너뛴다.

    대시보드에서 빠지는 page_type(B2B/프로모션)까지 PSI를 돌리면 호출당 60초가
    그대로 낭비된다 — 기본적으로 gen_dashboard_data 와 같은 집합을 제외한다.
    """
    out, skipped = [], 0
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        for x in doc.get("summary", []):
            r = x.get("result") or {}
            if not (r.get("score") and r.get("url")):
                continue
            if (r.get("page_type") or {}).get("id") in exclude_page_types:
                skipped += 1
                continue
            out.append(r["url"])
    uniq = list(dict.fromkeys(out))  # 순서 유지 중복 제거
    if skipped:
        print(f"[psi] page_type 제외 {skipped}건 ({', '.join(sorted(exclude_page_types))})")
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run_results 파일 glob (예: 'data/run_results/us_2026-08-26_run_*.json')")
    ap.add_argument("--country", help="국가 코드 (--date 와 함께 사용)")
    ap.add_argument("--date", help="감사 날짜 YYYY-MM-DD")
    ap.add_argument("--url", action="append", help="단일 URL (반복 지정 가능)")
    ap.add_argument("--strategy", default="mobile", choices=["mobile", "desktop"])
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="처리 수 제한(시험용)")
    ap.add_argument("--exclude-page-types", default="business,promotion",
                    help="제외할 page_type (쉼표 구분). 대시보드 제외 대상과 맞춘 기본값. "
                         "빈 문자열이면 제외 없음")
    args = ap.parse_args()

    key = os.environ.get("PSI_API_KEY")
    if not key:
        sys.exit("PSI_API_KEY 환경변수가 필요합니다.")

    excl = frozenset(t.strip() for t in args.exclude_page_types.split(",") if t.strip())
    if args.url:
        urls = args.url
    elif args.run:
        urls = urls_from_run(args.run, excl)
    elif args.country and args.date:
        urls = urls_from_run(os.path.join(
            HERE, "data", "run_results", f"{args.country}_{args.date}_run_*.json"), excl)
    else:
        sys.exit("--run / --country+--date / --url 중 하나가 필요합니다.")

    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        sys.exit("수집할 URL이 없습니다.")

    collect(urls, key, args.strategy, args.concurrency)
    return 0


if __name__ == "__main__":
    sys.exit(main())
