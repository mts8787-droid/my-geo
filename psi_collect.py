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

resume: 캐시에 성공 기록이 있는 URL만 건너뛴다. 중단 후 재실행하면 이어서 수집하고,
        이전 실행에서 실패한 URL 은 다시 시도한다 (PSI 500 은 대개 일시적).

사용:
  PSI_API_KEY=... python3 psi_collect.py --run data/run_results/us_2026-08-26_run_*.json
  PSI_API_KEY=... python3 psi_collect.py --country us --date 2026-08-26
  PSI_API_KEY=... python3 psi_collect.py --url https://www.lg.com/us/
"""
import argparse
import collections
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


# ── 전역 서킷 브레이커 + 속도 제한 ────────────────────────────────────────────

class Gate:
    """모든 워커가 공유하는 호출 게이트.

    PSI 는 짧은 버스트는 14건/분까지 받아주지만 지속 부하에는 페널티 창을 건다.
    창이 열리면 모든 호출이 '즉시 500' 으로 거부되는데, 여기서 계속 때리면
    Google 네트워크 단위 차단(429 "automated queries", 수 시간 지속)으로 승격된다.
    2026-08-26 실제로 이 차단을 맞았다 — 워커별 백오프를 없앤 설계가
    페널티 창 동안 분당 270건을 쏟아부었기 때문이다.

    그래서 두 겹으로 막는다:
      1. 속도 제한  — 전역으로 초당 호출 간격을 강제 (워커 수와 무관)
      2. 브레이커   — 연속 실패가 임계를 넘으면 '모든' 워커를 쿨다운 동안 정지
    """

    def __init__(self, rate_per_min=6.0, trip_after=5, cooldown=900):
        self.min_interval = 60.0 / max(rate_per_min, 0.1)
        self.trip_after = trip_after
        self.cooldown = cooldown
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.open_until = 0.0
        self.consec_fail = 0
        self.trips = 0

    def acquire(self):
        """호출 직전 호출. 브레이커가 열려 있으면 닫힐 때까지 대기 후 속도 제한 적용."""
        while True:
            with self.lock:
                now = time.time()
                if now >= self.open_until:
                    slot = max(now, self.next_slot)
                    self.next_slot = slot + self.min_interval
                    delay = slot - now
                    break
                remain = self.open_until - now
            time.sleep(min(10.0, remain))
        if delay > 0:
            time.sleep(delay)

    def report(self, ok):
        """호출 결과 보고. 브레이커가 새로 열렸으면 True."""
        with self.lock:
            if ok:
                self.consec_fail = 0
                return False
            self.consec_fail += 1
            if self.consec_fail >= self.trip_after and time.time() >= self.open_until:
                self.open_until = time.time() + self.cooldown
                self.consec_fail = 0
                self.trips += 1
                return True
        return False


# ── 수집 ──────────────────────────────────────────────────────────────────────

def collect(urls, key, strategy="mobile", concurrency=20, sweeps=4,
            rate_per_min=6.0, max_minutes=0, save_every=25):
    """수집. 실패는 인라인 재시도 대신 '스윕 반복'으로 회수한다.

    인라인 재시도(worker 안에서 sleep)는 워커 슬롯을 붙잡아 풀을 굶긴다 —
    동시성 10에서 8개가 백오프에 들어가면 실질 동시성이 2로 떨어져, 느려짐이
    다시 실패를 부르는 악순환이 된다. 그래서 워커는 1회만 시도하고 즉시 반환하고,
    실패분은 다음 스윕에서 쿨다운 후 통째로 재시도한다.
    """
    cache = load_cache()
    gate = Gate(rate_per_min=rate_per_min)
    # 시간 예산: 야간 분할 실행용. 예산이 끝나면 남은 URL 은 다음 실행이 이어받는다.
    deadline = time.time() + max_minutes * 60 if max_minutes else 0
    rates = {2: 2.14, 5: 4.62, 10: 5.42, 20: 13.88, 30: 14.06}
    rate = min(rates[min(rates, key=lambda k: abs(k - concurrency))], rate_per_min)

    for sweep in range(1, sweeps + 1):
        todo = [u for u in urls if u not in cache or cache[u].get("error")]
        if not todo:
            break
        retry_n = sum(1 for u in todo if u in cache)
        eta = len(todo) / rate
        print(f"[psi] 스윕 {sweep}/{sweeps} — 대상 {len(urls)} / 수집 {len(todo)}"
              f"{f' (이전 실패 재시도 {retry_n})' if retry_n else ''}  동시성={concurrency}"
              f"  예상 {eta/60:.1f}시간", flush=True)

        done = {"n": 0, "ok": 0, "fail": 0, "skipped": 0}
        t0 = time.time()

        def worker(u):
            if deadline and time.time() > deadline:
                with _lock:
                    done["skipped"] += 1
                return
            gate.acquire()
            try:
                rec = extract(fetch(u, key, strategy))
                err = None
            except urllib.error.HTTPError as e:
                rec, err = None, f"HTTP {e.code}"
            except Exception as e:
                rec, err = None, f"{type(e).__name__}: {e}"
            if gate.report(err is None):
                print(f"[psi] ⚠ 연속 실패 — 브레이커 작동, 전체 {gate.cooldown//60}분 정지 "
                      f"(누적 {gate.trips}회)", flush=True)
            with _lock:
                if rec is not None:
                    cache[u] = rec
                    done["ok"] += 1
                else:
                    cache[u] = {"error": err,
                                "fetched_at": datetime.now(timezone.utc).isoformat()}
                    done["fail"] += 1
                done["n"] += 1
                if done["n"] % save_every == 0:
                    save_cache(cache)
                _progress(done, len(todo), t0)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(worker, todo))
        save_cache(cache)

        el = time.time() - t0
        held = f" · 시간예산 초과로 보류 {done['skipped']}" if done["skipped"] else ""
        print(f"[psi] 스윕 {sweep} 완료: 성공 {done['ok']} · 실패 {done['fail']}{held}"
              f" · {el/60:.1f}분 ({done['ok']/el*60:.2f}건/분)", flush=True)
        if deadline and time.time() > deadline:
            print(f"[psi] 시간 예산 {max_minutes}분 소진 — 남은 분량은 다음 실행이 이어받습니다.",
                  flush=True)
            break
        if done["fail"] == 0 or sweep == sweeps:
            break
        print(f"[psi] 실패 {done['fail']}건 — 5분 쿨다운 후 재스윕", flush=True)
        time.sleep(300)

    ok = sum(1 for v in cache.values() if not v.get("error"))
    print(f"\n[psi] 완료: 캐시 {len(cache)}건 (성공 {ok} · 실패 {len(cache)-ok}) → {CACHE}")
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

def urls_from_run(pattern: str, exclude_page_types=frozenset(), per_group=0, seed=42):
    """run_results 에서 감사 성공한 URL 추출. 제외 page_type 은 건너뛴다.

    대시보드 집계에서 빠지는 페이지에 호출당 60초를 쓸 이유가 없다 —
    gen_dashboard_data 와 동일하게 B2B/프로모션/unknown/home 과 비-200 을 제외한다.
    """
    out, skipped, meta = [], 0, {}
    for path in sorted(glob.glob(pattern)):
        code = os.path.basename(path).split("_")[0]
        doc = json.load(open(path))
        for x in doc.get("summary", []):
            r = x.get("result") or {}
            if not (r.get("score") and r.get("url")):
                continue
            if (r.get("page_type") or {}).get("id") in exclude_page_types:
                skipped += 1
                continue
            # 비-200 페이지는 대시보드 집계에서 빠진다 — PSI 를 쓸 이유가 없다.
            if r.get("page_error"):
                skipped += 1
                continue
            st = None
            for b in (r.get("score", {}).get("breakdown") or {}).values():
                st = (b.get("items") or {}).get("ai_status_200") or st
            if st and st.get("pass") is False:
                skipped += 1
                continue
            meta[r["url"]] = (code, (r.get("page_type") or {}).get("id"))
            out.append(r["url"])
    uniq = list(dict.fromkeys(out))  # 순서 유지 중복 제거
    if skipped:
        print(f"[psi] page_type 제외 {skipped}건 ({', '.join(sorted(exclude_page_types))})")
    if per_group:
        # 전수 대신 (국가, page_type) 그룹별 무작위 N개만 측정한다.
        # 나머지 URL 은 gen_dashboard_data 가 그룹 중앙값으로 추정 보정한다.
        # 그룹 내 편차보다 그룹 간 편차가 커서(같은 국가에서 타입별 55~391ms)
        # 그룹 대표값으로 근사하는 편이 전수 대비 비용 대비 효과가 크다.
        import random
        rnd = random.Random(seed)
        buckets = collections.defaultdict(list)
        for u in uniq:
            buckets[meta.get(u, ("?", "?"))].append(u)
        picked = []
        for key in sorted(buckets):
            g = buckets[key][:]
            rnd.shuffle(g)
            picked.extend(g[:per_group])
        print(f"[psi] 그룹 샘플링: {len(buckets)}개 그룹 × 최대 {per_group}개 "
              f"→ {len(picked)}건 (전수 {len(uniq)})")
        return picked
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run_results 파일 glob (예: 'data/run_results/us_2026-08-26_run_*.json')")
    ap.add_argument("--country", help="국가 코드 (--date 와 함께 사용)")
    ap.add_argument("--date", help="감사 날짜 YYYY-MM-DD")
    ap.add_argument("--url", action="append", help="단일 URL (반복 지정 가능)")
    ap.add_argument("--strategy", default="mobile", choices=["mobile", "desktop"])
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--max-minutes", type=int, default=0,
                    help="이 시간(분)이 지나면 새 호출을 멈추고 종료. 야간 분할 실행용. 0=무제한")
    ap.add_argument("--rate", type=float, default=6.0,
                    help="전역 호출 속도 상한(건/분). PSI 지속 부하 차단 회피용. 기본 6")
    ap.add_argument("--limit", type=int, default=0, help="처리 수 제한(시험용)")
    ap.add_argument("--per-group", type=int, default=0,
                    help="(국가,page_type) 그룹별 무작위 N개만 측정. 0=전수. "
                         "나머지는 대시보드가 그룹 중앙값으로 추정 보정한다")
    ap.add_argument("--exclude-page-types", default="business,promotion,unknown,home,about",
                    help="제외할 page_type (쉼표 구분). gen_dashboard_data 의 제외 대상과 "
                         "동일한 기본값. 빈 문자열이면 제외 없음")
    args = ap.parse_args()

    key = os.environ.get("PSI_API_KEY")
    if not key:
        sys.exit("PSI_API_KEY 환경변수가 필요합니다.")

    excl = frozenset(t.strip() for t in args.exclude_page_types.split(",") if t.strip())
    if args.url:
        urls = args.url
    elif args.run:
        urls = urls_from_run(args.run, excl, args.per_group)
    elif args.country and args.date:
        urls = urls_from_run(os.path.join(
            HERE, "data", "run_results", f"{args.country}_{args.date}_run_*.json"), excl,
            args.per_group)
    else:
        sys.exit("--run / --country+--date / --url 중 하나가 필요합니다.")

    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        sys.exit("수집할 URL이 없습니다.")

    collect(urls, key, args.strategy, args.concurrency,
            rate_per_min=args.rate, max_minutes=args.max_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
