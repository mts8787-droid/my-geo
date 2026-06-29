#!/usr/bin/env python3
"""data/run_results 의 국가별 최신 run 을 읽어 '감점 사유 종합 리포트'(텍스트)를 생성.

국가 × page_type × 채점항목 단위로 FAIL 건수·비율·대표 사유(hint)를 전부 집계한다.
재감사 없이 기존 run JSON 만으로 만든다. 출력: reports/audit_report.txt
"""
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "data", "run_results")
OUT = os.path.join(HERE, "reports", "audit_report.txt")

# 신 배포 컷오프 — 2026-06-17 Render 재배포(#11 역순 / #23 plp / #34 author) 이후
# 채점된 run 은 신(新) 기준. run 파일명의 날짜로 판정(하드코딩 allowlist 대체).
NEW_CONFIG_DATE = "2026-06-17"


def _run_date(fn):
    m = re.match(r"^[a-z]{2}_(\d{4}-\d{2}-\d{2})_run_", fn)
    return m.group(1) if m else "0000-00-00"


def item_meta():
    cfg = json.load(open(os.path.join(HERE, "scoring_config.json")))
    m = {}
    for cat, cd in cfg.items():
        if isinstance(cd, dict) and "criteria" in cd:
            for c in cd["criteria"]:
                m[c["id"]] = {
                    "points": c.get("points"),
                    "spec": c.get("spec_id"),
                    "cat": cat,
                    "name": c.get("name"),
                }
    return m


def pick_latest_runs():
    """국가별 최신 정식 run(국가_날짜_run_<hash>.json) 1개씩."""
    pat = re.compile(r"^([a-z]{2})_(\d{4}-\d{2}-\d{2})_run_[0-9a-f]+\.json$")
    best = {}
    for fn in os.listdir(RUNS):
        m = pat.match(fn)
        if not m:
            continue
        code, date = m.group(1), m.group(2)
        if code not in best or date > best[code][0]:
            best[code] = (date, fn)
    return {c: v[1] for c, v in sorted(best.items())}


def pt_id(x):
    return (x.get("id") if isinstance(x, dict) else x) or "unknown"


def pt_label(x):
    if isinstance(x, dict):
        return x.get("label") or x.get("id") or "unknown"
    return x or "unknown"


def flat_items(res):
    items = {}
    for cat, cd in ((res.get("score") or {}).get("breakdown") or {}).items():
        items.update(cd.get("items") or {})
    return items


def fetch_state(items):
    """fetch 성공 여부. (state, reason) — state in {'ok','fail'}.

    ai_status_200 항목으로 판정(200 아니면 fetch 실패 → soup 없음 → 전 항목이
    'HTML 파싱 실패'로 중복 감점되므로 감점 분석에서 제외해야 한다)."""
    s = items.get("ai_status_200")
    if s is not None and s.get("pass") is not None:
        if s.get("pass") is True:
            return "ok", None
        return "fail", (s.get("hint") or "non-200")
    t = items.get("seo_title", {})
    if t.get("pass") is False and "파싱 실패" in (t.get("hint") or ""):
        return "fail", "HTML 파싱 실패"
    return "ok", None


def gen():
    meta = item_meta()
    runs = pick_latest_runs()
    lines = []
    w = lines.append

    w("=" * 78)
    w("LG.com GEO / AI Readability Audit — 감점 사유 종합 리포트")
    w(f"생성: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    w(f"대상: {len(runs)}개국 최신 run / 국가별 page_type ≤100 샘플")
    w("")
    new_codes = [c for c, fn in runs.items() if _run_date(fn) >= NEW_CONFIG_DATE]
    old_codes = [c for c, fn in runs.items() if _run_date(fn) < NEW_CONFIG_DATE]
    w("[채점 기준 주의] 2026-06-17 Render 재배포로 #11(헤딩 역순)·#23(FAQ plp)·")
    w("#34(author 스키마) 로직이 바뀌었습니다.")
    if old_codes:
        w(f"신(新) 기준: {', '.join(c.upper() for c in new_codes) or '없음'}")
        w(f"구(舊) 기준: {', '.join(c.upper() for c in old_codes)}")
        w("→ #11/#23/#34 항목은 신/구 국가 간 직접 비교가 부적절합니다(각 국가 헤더 [config] 표기).")
    else:
        w(f"전 국가({len(new_codes)}개) 신(新) 기준으로 채점됨 — 국가 간 직접 비교 가능(각 국가 헤더 [config] 표기).")
    w("=" * 78)
    w("")

    for code, fn in runs.items():
        d = json.load(open(os.path.join(RUNS, fn)))
        summ = d.get("summary", [])
        cfgtag = "신(2026-06-17 재배포)" if _run_date(fn) >= NEW_CONFIG_DATE else "구(재배포 이전)"

        # fetch 실패 분리 + 등급 분포(성공 페이지 기준)
        gd = Counter()
        fetch_fail = Counter()
        ok_n = 0

        # page_type 별 집계(성공 페이지만): item -> {evaluated, fail, hints, label}
        # hints 는 숫자 정규화('N')된 사유로 묶고, vals 에 실측 숫자 범위를 모은다.
        bypt = defaultdict(lambda: {"n": 0, "items": defaultdict(
            lambda: {"eval": 0, "fail": 0, "hints": Counter(),
                     "vals": defaultdict(list), "label": None, "rtype": None})})
        ptlabel = {}
        for it in summ:
            res = it.get("result") or {}
            items = flat_items(res)
            state, reason = fetch_state(items)
            if state == "fail":
                fetch_fail[reason] += 1
                continue
            ok_n += 1
            gd[(res.get("score") or {}).get("grade")] += 1
            ptid = pt_id(res.get("page_type"))
            ptlabel[ptid] = pt_label(res.get("page_type"))
            bucket = bypt[ptid]
            bucket["n"] += 1
            for iid, iv in items.items():
                p = iv.get("pass")
                if p is None:  # N/A (na=True) — 채점 제외
                    continue
                rec = bucket["items"][iid]
                rec["eval"] += 1
                rec["label"] = iv.get("label") or (meta.get(iid, {}) or {}).get("name") or iid
                rec["rtype"] = iv.get("rule_type")
                if p is False:
                    rec["fail"] += 1
                    h = iv.get("hint") or "(사유 미기재)"
                    norm = re.sub(r"\d[\d.,]*", "N", h)
                    rec["hints"][norm] += 1
                    nums = re.findall(r"\d+(?:\.\d+)?", h)
                    if nums:
                        rec["vals"][norm].append(float(nums[0]))

        w("#" * 78)
        w(f"## {code.upper()}   run={fn}")
        w(f"   URL {d.get('url_count')}건 · 감사성공 {ok_n} · [config] {cfgtag}")
        w(f"   등급(성공 기준): Good {gd['Good']} / Need Improvement {gd['Need Improvement']} / Poor {gd['Poor']}")
        if fetch_fail:
            tot_f = sum(fetch_fail.values())
            w(f"   ⚠ 감사 불가(fetch 실패) {tot_f}건 — 아래 감점 집계에서 제외:")
            for r, c in fetch_fail.most_common(6):
                w(f"        · {c}건: {r}")
        w("#" * 78)

        for ptid in sorted(bypt, key=lambda k: -bypt[k]["n"]):
            bucket = bypt[ptid]
            n = bucket["n"]
            # 감점(=FAIL≥1) 항목만, FAIL률 높은 순
            failed = [(iid, r) for iid, r in bucket["items"].items() if r["fail"] > 0]
            failed.sort(key=lambda x: (-(x[1]["fail"] / max(x[1]["eval"], 1)), -x[1]["fail"]))
            w("")
            w(f"### [{ptid}] {ptlabel.get(ptid, ptid)} — {n}건  (감점 항목 {len(failed)}개)")
            if not failed:
                w("    감점 항목 없음 (모든 채점 항목 PASS)")
                continue
            for iid, r in failed:
                mm = meta.get(iid, {})
                pts = mm.get("points")
                spec = mm.get("spec") or ""
                cat = mm.get("cat") or ""
                rate = 100.0 * r["fail"] / max(r["eval"], 1)
                w(f"  - {iid} ({spec} · {cat} · {pts}점) — "
                  f"FAIL {r['fail']}/{r['eval']} ({rate:.0f}%)  «{r['label']}»")
                for h, c in r["hints"].most_common(4):
                    vals = r["vals"].get(h)
                    rng = ""
                    if vals:
                        lo, hi = min(vals), max(vals)
                        rng = f" [측정 {lo:g}]" if lo == hi else f" [측정 {lo:g}~{hi:g}]"
                    w(f"      · {c:>3}건: {h}{rng}")
                if len(r["hints"]) > 4:
                    w(f"      · …외 사유 {len(r['hints'])-4}종")
        w("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"리포트 생성: {OUT}  ({len(lines)} 줄)")
    print("국가:", ", ".join(runs.keys()))


if __name__ == "__main__":
    gen()
