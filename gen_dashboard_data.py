"""data/run_results 의 국가별 최신 run 을 읽어 대시보드용 집계 JSON 생성.

gen_audit_report.py 와 동일한 '국가별 최신 run 1개' 선정 로직을 쓴다.
run_results 의 score.breakdown 구조를 그대로 국가 단위로 집계한다.
출력: reports/dashboard_data.json (nested: countries → score.breakdown → items)

집계 시 세 가지 보정을 적용한다:
  1. B2B(business) / 프로모션(promotion) page_type 페이지는 표본에서 제외 (sample_size 에도 미포함).
  2. 저장된 run 의 항목 결과를 scoring_config.json 의 '현재' 기준으로 재채점 —
     비활성(enabled:false) 항목은 빼고, 임계값이 바뀐 항목은 저장된 실측값으로 pass 를 재판정.
     (과거 run 을 다시 돌리지 않고도 기준 변경이 대시보드에 반영되도록)
  3. 현재 룰이 psi_metric 인 항목(#1 TTFB)은 data/psi_cache.json 의 PSI 측정값으로 판정.
     미수집 URL 은 N/A 로 분모에서 빠진다 — 수집이 진행될수록 applicable_n 이 늘어난다.
     같은 캐시의 agentic-browsing 관측치는 채점과 분리해 "agentic" 블록으로 보고한다.
"""
import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "data", "run_results")
CONFIG = os.path.join(HERE, "scoring_config.json")
PSI_CACHE = os.path.join(HERE, "data", "psi_cache.json")
OUT = os.path.join(HERE, "reports", "dashboard_data.json")

STRATEGIC = ["us", "uk", "de", "es", "ca", "au", "br", "mx", "in", "vn"]
CATS = ["performance", "accessibility", "seo", "ai_readiness"]

# 대시보드 집계 제외 page_type — B2B(사업자) / 프로모션·약관 페이지
EXCLUDED_PAGE_TYPES = {"business", "promotion"}

_MS = re.compile(r"(\d+(?:\.\d+)?)\s*ms")


def load_criteria():
    """scoring_config.json → (활성항목 맵, 재판정 맵, PSI 룰 맵, 등급 임계값).

    활성항목 맵 : {category: {item_id: label}}   — 여기 없는 항목은 집계 제외
    재판정 맵   : {item_id: max_ms}              — 저장된 value 로 pass 재계산
    PSI 룰 맵   : {item_id: (metric, max_value)} — PSI 캐시에서 읽어 재판정
    """
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    active, rescore, psi_rules = {}, {}, {}
    for cat in CATS:
        active[cat] = {}
        for cr in cfg.get(cat, {}).get("criteria", []):
            if not cr.get("enabled", True):
                continue
            active[cat][cr["id"]] = cr.get("name", cr["id"])
            rule = cr.get("rule") or {}
            params = rule.get("params", {})
            if rule.get("type") == "ttfb_under_ms":
                rescore[cr["id"]] = float(params.get("max_ms", 0))
            elif rule.get("type") == "psi_metric":
                psi_rules[cr["id"]] = (params.get("metric"), float(params.get("max_value", 0)))
    return active, rescore, psi_rules, cfg.get("grade", {})


def load_psi_cache():
    """psi_collect.py 가 적재한 PSI 측정값. 없으면 빈 dict."""
    try:
        with open(PSI_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def item_pass(iid, it, url, rescore, psi_rules, psi):
    """항목 pass 판정. None 이면 N/A — 집계 분모에서 빠진다.

    우선순위:
      1. 현재 룰이 psi_metric  → PSI 캐시에서 읽어 판정 (미수집이면 None)
      2. 임계값만 바뀐 항목    → 저장된 value 문자열로 재판정
      3. 그 외                → 저장된 pass 그대로
    """
    if iid in psi_rules:
        metric, max_v = psi_rules[iid]
        rec = psi.get(url)
        if not rec or rec.get("error") or rec.get(metric) is None:
            return None
        return float(rec[metric]) < max_v

    max_ms = rescore.get(iid)
    if max_ms is not None:
        m = _MS.search(str(it.get("value") or ""))
        if m:
            return float(m.group(1)) < max_ms
    return it.get("pass")


def agentic_summary(results, psi):
    """에이전트형 브라우징(agentic-browsing) 관측 집계. 채점에는 쓰지 않는다.

    scoring_config 의 항목이 아니라 '수집된 사실'로만 보고한다.
    WebMCP audit 은 대상 페이지가 Origin Trial 토큰을 서빙해야 평가되므로
    미배포 상태에서는 measured_n=0 으로 남는다.
    """
    scores, audits = [], defaultdict(lambda: {"pass": 0, "measured": 0})
    for r in results:
        rec = psi.get(r["url"])
        if not rec or rec.get("error"):
            continue
        ag = rec.get("agentic") or {}
        if ag.get("score") is not None:
            scores.append(ag["score"])
        for aid, sc in (ag.get("audits") or {}).items():
            if sc is None:          # 평가 불가 — 분모에도 넣지 않는다
                continue
            audits[aid]["measured"] += 1
            if sc >= 1:
                audits[aid]["pass"] += 1
    if not scores and not audits:
        return None
    return {
        "measured_n": len(scores),
        "score_avg": round(sum(scores) / len(scores), 4) if scores else None,
        "audits": {
            aid: {"pass_rate": round(v["pass"] / v["measured"], 4), "measured_n": v["measured"]}
            for aid, v in sorted(audits.items())
        },
    }


def aggregate_country(doc, active, rescore, psi_rules, psi, grade_cfg):
    """한 국가 run → 집계 dict. 제외 page_type 은 표본에서 뺀다."""
    excluded = defaultdict(int)
    results = []
    for x in doc.get("summary", []):
        r = x.get("result") or {}
        if not r.get("score"):
            continue
        pt = (r.get("page_type") or {}).get("id")
        if pt in EXCLUDED_PAGE_TYPES:
            excluded[pt] += 1
            continue
        results.append(r)

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

    # 카테고리 집계
    cat_pts = {c: 0.0 for c in CATS}
    cat_pass = {c: 0 for c in CATS}
    cat_items_total = {c: 0 for c in CATS}
    # 항목 집계: id → {label, category, pass_cnt, applicable_n}
    items_agg = {}

    for r in results:
        bd = r["score"]["breakdown"]
        r_passed = r_total = 0
        for cat in CATS:
            b = bd.get(cat)
            if not b:
                continue
            c_passed = c_total = 0
            for iid, it in (b.get("items") or {}).items():
                # 현재 기준에서 비활성인 항목은 집계에서 제외 (#8 등)
                if iid not in active.get(cat, {}):
                    continue
                # pass=None 은 '평가 대상 아님'(페이지타입 불일치 / PSI 미수집) → 분모 제외
                if it.get("pass") is None:
                    continue
                p = item_pass(iid, it, r["url"], rescore, psi_rules, psi)
                if p is None:
                    continue
                a = items_agg.setdefault(iid, {
                    "label": active[cat][iid], "category": cat,
                    "pass_cnt": 0, "applicable_n": 0,
                })
                a["applicable_n"] += 1
                c_total += 1
                if p:
                    a["pass_cnt"] += 1
                    c_passed += 1
            cat_pass[cat] += c_passed
            cat_items_total[cat] += c_total
            cat_pts[cat] += round(c_passed / c_total * 100) if c_total else 0
            r_passed += c_passed
            r_total += c_total

        total = round(r_passed / r_total * 100) if r_total else 0
        total_sum += total
        grade_dist[
            "Good" if total >= grade_cfg.get("good", 90) else
            "Need Improvement" if total >= grade_cfg.get("need_improvement", 70) else
            "Poor"
        ] += 1

    breakdown = {}
    for cat in CATS:
        it_tot = cat_items_total[cat]
        breakdown[cat] = {
            "points_avg": round(cat_pts[cat] / n, 2),
            "max": 100,
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
        "agentic": agentic_summary(results, psi),
        "excluded_page_types": dict(excluded),
        "excluded_count": sum(excluded.values()),
        "parse_fail_rate": round(parse_fail / n, 4),
        "total_avg": round(total_sum / n, 2),
        "max": 100,
        "grade_dist": dict(grade_dist),
        "breakdown": breakdown,
        "items": items,
    }


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


def main():
    active, rescore, psi_rules, grade_cfg = load_criteria()
    psi = load_psi_cache()
    latest = pick_latest_runs()
    countries = {}
    missing = []
    for c in STRATEGIC:
        if c not in latest:
            missing.append(c)
            continue
        date, fn = latest[c]
        doc = json.load(open(os.path.join(RUNS, fn)))
        agg = aggregate_country(doc, active, rescore, psi_rules, psi, grade_cfg)
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
        "excluded_total": sum(v["excluded_count"] for v in countries.values()),
        "excluded_page_types": sorted(EXCLUDED_PAGE_TYPES),
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
                  f"(제외 {v['excluded_count']})  parse_fail={v['parse_fail_rate']*100:.0f}%  {v['date']}")
    print(f"  가중평균 {overall['total_avg_weighted']}  (표본 {sample_total}, 제외 {overall['excluded_total']})")


if __name__ == "__main__":
    main()
