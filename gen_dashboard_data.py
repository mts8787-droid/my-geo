"""data/run_results 의 국가별 최신 run 을 읽어 대시보드용 집계 JSON 생성.

gen_audit_report.py 와 동일한 '국가별 최신 run 1개' 선정 로직을 쓴다.
run_results 의 score.breakdown 구조를 그대로 국가 단위로 집계한다.
출력: reports/dashboard_data.json (nested: countries → score.breakdown → items)
"""
import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "data", "run_results")
OUT = os.path.join(HERE, "reports", "dashboard_data.json")

STRATEGIC = ["us", "uk", "de", "es", "ca", "au", "br", "mx", "in", "vn"]
CATS = ["performance", "accessibility", "seo", "ai_readiness"]


def pick_latest_runs():
    """국가별 최신 정식 run(국가_날짜_run_<hash>.json) 1개씩. 백업/파생 파일 무시."""
    pat = re.compile(r"^([a-z]{2})_(\d{4}-\d{2}-\d{2})_run_[0-9a-f]+\.json$")
    best = {}
    for fn in os.listdir(RUNS):
        m = pat.match(fn)
        if not m:
            continue
        code, date = m.group(1), m.group(2)
        if code not in best or date > best[code][0]:
            best[code] = (date, fn)
    return {c: v for c, v in best.items()}


def aggregate_country(doc):
    """한 국가 run → 집계 dict."""
    results = [x["result"] for x in doc.get("summary", []) if x.get("result", {}).get("score")]
    n = len(results)
    if not n:
        return None

    parse_fail = sum(
        1 for r in results
        if r["score"]["breakdown"].get("seo", {}).get("items", {})
        .get("seo_title", {}).get("hint") == "HTML 파싱 실패"
    )
    grade_dist = defaultdict(int)
    total_sum = 0
    max_total = results[0]["score"].get("max", 100)

    # 카테고리 집계
    cat_pts = {c: 0.0 for c in CATS}
    cat_max = {c: 0 for c in CATS}
    cat_pass = {c: 0 for c in CATS}
    cat_items_total = {c: 0 for c in CATS}
    # 항목 집계: id → {label, category, pass_cnt, applicable_n}
    items_agg = {}

    for r in results:
        sc = r["score"]
        total_sum += sc["total"]
        grade_dist[sc.get("grade", "?")] += 1
        for cat in CATS:
            b = sc["breakdown"].get(cat)
            if not b:
                continue
            cat_pts[cat] += b.get("points", 0)
            cat_max[cat] = b.get("max", cat_max[cat])
            cat_pass[cat] += b.get("passed", 0)
            cat_items_total[cat] += b.get("total", 0)
            for iid, it in (b.get("items") or {}).items():
                a = items_agg.setdefault(iid, {
                    "label": it.get("label"), "category": cat,
                    "pass_cnt": 0, "applicable_n": 0,
                })
                # pass=None 은 '해당 페이지타입에서만 평가' → 적용대상서 제외
                if it.get("pass") is None:
                    continue
                a["applicable_n"] += 1
                if it.get("pass"):
                    a["pass_cnt"] += 1

    breakdown = {}
    for cat in CATS:
        it_tot = cat_items_total[cat]
        breakdown[cat] = {
            "points_avg": round(cat_pts[cat] / n, 2),
            "max": cat_max[cat],
            "pass_rate": round(cat_pass[cat] / it_tot, 4) if it_tot else None,
        }

    items = {}
    for iid, a in items_agg.items():
        items[iid] = {
            "label": a["label"],
            "category": a["category"],
            "pass_rate": round(a["pass_cnt"] / a["applicable_n"], 4) if a["applicable_n"] else None,
            "applicable_n": a["applicable_n"],
        }

    return {
        "sample_size": n,
        "parse_fail_rate": round(parse_fail / n, 4),
        "total_avg": round(total_sum / n, 2),
        "max": max_total,
        "grade_dist": dict(grade_dist),
        "breakdown": breakdown,
        "items": items,
    }


def main():
    latest = pick_latest_runs()
    countries = {}
    missing = []
    for c in STRATEGIC:
        if c not in latest:
            missing.append(c)
            continue
        date, fn = latest[c]
        doc = json.load(open(os.path.join(RUNS, fn)))
        agg = aggregate_country(doc)
        if agg is None:
            missing.append(c)
            continue
        agg["date"] = date
        agg["run_file"] = fn
        countries[c] = agg

    # 전체 요약(표본수 가중 평균)
    sample_total = sum(v["sample_size"] for v in countries.values())
    weighted = sum(v["total_avg"] * v["sample_size"] for v in countries.values())
    overall = {
        "countries": len(countries),
        "sample_total": sample_total,
        "total_avg_weighted": round(weighted / sample_total, 2) if sample_total else None,
        "missing": missing,
    }

    out = {"countries": countries, "overall": overall}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[dashboard] {len(countries)}국 집계 → {OUT}")
    if missing:
        print(f"[dashboard] 누락: {missing}")
    for c in STRATEGIC:
        v = countries.get(c)
        if v:
            print(f"  {c.upper():<3} {v['total_avg']:5.1f}  n={v['sample_size']:<4} "
                  f"parse_fail={v['parse_fail_rate']*100:.0f}%  {v['date']}")
    print(f"  가중평균 {overall['total_avg_weighted']}  (표본 {sample_total})")


if __name__ == "__main__":
    main()
