#!/usr/bin/env python3
"""페이지 발행일을 수집해 감사 샘플링용 인덱스를 만든다.

배경: 감사는 page_type 별 최대 100개 샘플인데, 기존에는 URL 정렬 순서대로 앞의
      100개를 잘랐다. 뉴스룸·트러블슈팅처럼 계속 발행되는 타입은 그러면 오래된
      문서만 반복 감사하게 된다. 발행일 내림차순으로 뽑도록 날짜를 미리 모은다.

왜 사이트맵을 안 쓰나: lastmod 가 US 사이트맵에만 있고(다른 9개국 0%), US
      support 는 31,509개 중 31,476개가 같은 달로 일괄 갱신돼 있어 개별 최신성
      신호로 쓸 수 없다. 그래서 페이지의 datePublished 를 직접 읽는다.

출력: reports/page_dates.json  {url: "YYYY-MM-DD"}

사용:
  python3 page_dates.py                       # 전 국가 · newsroom + support_troubleshoot
  python3 page_dates.py --country us --types newsroom
"""
import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "reports", "page_dates.json")
COUNTRIES = ["us", "uk", "de", "es", "ca", "au", "br", "mx", "in", "vn", "global"]
DEFAULT_TYPES = ["newsroom", "press_media", "support_troubleshoot"]

_DP = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_MDY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")

_lock = threading.Lock()


def norm_date(raw):
    """datePublished 값을 YYYY-MM-DD 로. 형식이 국가별로 다르다(ISO / MM-DD-YYYY)."""
    raw = (raw or "").strip()
    m = _ISO.match(raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _MDY.match(raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def targets(countries, types):
    from page_type import detect_page_type
    out = []
    for cc in countries:
        path = os.path.join(HERE, "reports", f"lg_urls_{cc}.csv")
        if not os.path.exists(path):
            continue
        for r in csv.reader(open(path)):
            if not r or r[0] == "url":
                continue
            if detect_page_type(None, r[0]).get("id") in types:
                out.append(r[0])
    return list(dict.fromkeys(out))


def collect(urls, concurrency=6, save_every=500):
    import build_url_csv as b
    dates = {}
    if os.path.exists(OUT):
        try:
            dates = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            dates = {}
    todo = [u for u in urls if u not in dates]
    print(f"[dates] 대상 {len(urls)} / 보유 {len(urls)-len(todo)} / 수집 {len(todo)}", flush=True)
    if not todo:
        return dates

    stat = {"n": 0, "ok": 0, "nodate": 0, "err": 0}
    t0 = time.time()

    def save():
        tmp = OUT + ".tmp"
        json.dump(dates, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, OUT)

    def work(u):
        d = None
        for attempt in range(3):
            try:
                html = b._fetch(u).decode("utf-8", "replace")
                m = _DP.search(html)
                d = norm_date(m.group(1)) if m else None
                break
            except Exception:
                time.sleep(2 * (attempt + 1))
        with _lock:
            stat["n"] += 1
            if d:
                dates[u] = d
                stat["ok"] += 1
            elif d is None and attempt >= 2:
                stat["err"] += 1
            else:
                stat["nodate"] += 1
            if stat["n"] % save_every == 0:
                save()
                el = time.time() - t0
                print(f"[dates]   {stat['n']}/{len(todo)} · 확보 {stat['ok']} · "
                      f"{stat['n']/el*60:.0f}건/분 · 남은 {(len(todo)-stat['n'])/(stat['n']/el)/60:.0f}분",
                      flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(work, todo))
    save()
    print(f"[dates] 완료: 확보 {stat['ok']} · 날짜없음 {stat['nodate']} · 실패 {stat['err']} · "
          f"{(time.time()-t0)/60:.1f}분 → {OUT}", flush=True)
    return dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", action="append", help="국가 코드(반복 지정). 미지정 시 전체")
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES))
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    sys.path.insert(0, HERE)

    countries = args.country or COUNTRIES
    types = {t.strip() for t in args.types.split(",") if t.strip()}
    urls = targets(countries, types)
    print(f"[dates] {len(countries)}개국 · 타입 {sorted(types)} · URL {len(urls)}")
    collect(urls, args.concurrency)


if __name__ == "__main__":
    main()
