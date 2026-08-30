"""data/run_results 의 국가별 최신 run 을 읽어 대시보드용 집계 JSON 생성.

gen_audit_report.py 와 동일한 '국가별 최신 run 1개' 선정 로직을 쓴다.
출력: reports/dashboard_data.json (countries → breakdown/items)

저장된 run 을 그대로 합산하지 않고 scoring_config.json 의 '현재' 기준으로 재채점한다.
과거 run 을 다시 돌리지 않고도 기준 변경이 대시보드에 반영되게 하기 위함이다.

재채점 규칙 (우선순위 순):
  1. 카테고리 재매핑   — 저장된 run 은 옛 4개 카테고리 구조다. 항목 ID 로 현재 설정의
                        카테고리(6개)에 다시 붙인다. 총점은 passed/total 이라 영향 없고
                        breakdown 만 바뀐다.
  2. 비활성 항목 제외  — enabled:false 인 항목(#5, #8 등)은 분모에서 뺀다.
  3. 페이지타입 제한   — applies_to_page_types 가 있으면 해당 타입에서만 평가 (#34).
  4. psi_metric        — data/psi_cache.json 의 PSI 측정값으로 판정 (#1). 미수집이면 N/A.
  5. 임계값 변경       — header_max_age_min(#4) 등은 저장된 value 문자열로 재판정.
  6. 그 외             — 저장된 pass 를 그대로 쓴다.

집계 대상에서 빠지는 페이지:
  - B2B(business) / 프로모션(promotion)  — GEO 대상이 아님
  - 분류불가(unknown) / 홈페이지(home)   — 측정 의미 없음
  - 회사소개(about)                      — GEO 검수 대상 아님 (2026-08-28 결정)
  - 비-200 페이지(404·500·fetch 실패)    — 전 체크가 cascade-FAIL 이라 개선 대상이 아님
  전부 sample_size 에도 포함하지 않는다.

같은 PSI 캐시의 agentic-browsing 관측치는 채점과 분리해 "agentic" 블록으로 보고한다
(9월 감사부터 채점 예정 — 현재는 수집·관측만).
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

STRATEGIC = ["us", "uk", "de", "es", "ca", "au", "br", "mx", "in", "vn", "global"]

# 표시명. 코드와 다르게 부르는 곳만 적는다.
COUNTRY_LABELS = {"global": "Global-Site"}

# 집계 제외 page_type
EXCLUDED_PAGE_TYPES = {"business", "promotion", "unknown", "home", "about"}

_MAXAGE = re.compile(r"max-age\s*=\s*(\d+)")


class Criteria:
    """scoring_config.json 을 재채점에 필요한 형태로 펼쳐둔 것."""

    def __init__(self, path=CONFIG):
        cfg = json.load(open(path, encoding="utf-8"))
        self.cats = [k for k in cfg if k != "grade"]
        self.grade = cfg.get("grade", {})
        self.label = {}       # item_id → 표시명
        self.category = {}    # item_id → 카테고리 키
        self.applies = {}     # item_id → 평가 대상 page_type 집합
        self.psi_rules = {}   # item_id → (metric, max_value)
        self.maxage = {}      # item_id → min_seconds
        self.cat_label = {c: cfg[c].get("label", c) for c in self.cats}

        for cat in self.cats:
            for cr in cfg[cat].get("criteria", []):
                if not cr.get("enabled", True):
                    continue
                iid = cr["id"]
                self.label[iid] = cr.get("name", iid)
                self.category[iid] = cat
                if cr.get("applies_to_page_types"):
                    self.applies[iid] = set(cr["applies_to_page_types"])
                rule = cr.get("rule") or {}
                params = rule.get("params", {})
                if rule.get("type") == "psi_metric":
                    self.psi_rules[iid] = (params.get("metric"), float(params.get("max_value", 0)))
                elif rule.get("type") == "header_max_age_min":
                    self.maxage[iid] = int(params.get("min_seconds", 1))


def load_psi_cache():
    try:
        with open(PSI_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def psi_group_medians(results, psi, metric):
    """(page_type) 별 실측 중앙값. 미측정 URL 의 추정 보정에 쓴다.

    PSI 는 호출당 60초라 전수 측정이 비현실적이다(5,065건 = 11시간+). 그룹별
    무작위 표본만 측정하고 나머지는 같은 그룹 중앙값으로 채운다 — 같은 국가
    안에서도 page_type 간 중앙값이 55~391ms 로 갈리는 반면 그룹 내 편차는
    그보다 작아, 그룹 대표값 근사가 전수 대비 비용 효율이 높다.
    """
    buckets = defaultdict(list)
    for r in results:
        rec = psi.get(r["url"])
        if rec and not rec.get("error") and rec.get(metric) is not None:
            buckets[current_page_type(r)].append(float(rec[metric]))
    med = {k: sorted(v)[len(v) // 2] for k, v in buckets.items() if v}
    allv = [x for v in buckets.values() for x in v]
    med["__all__"] = sorted(allv)[len(allv) // 2] if allv else None
    return med


def item_pass(iid, it, url, page_type, cr, psi, psi_med=None):
    """항목 pass 판정. None 이면 N/A — 집계 분모에서 빠진다."""
    applies = cr.applies.get(iid)
    if applies and page_type not in applies:
        return None

    if iid in cr.psi_rules:
        metric, max_v = cr.psi_rules[iid]
        rec = psi.get(url)
        if rec and not rec.get("error") and rec.get(metric) is not None:
            return float(rec[metric]) < max_v
        # 미측정 → 같은 page_type 실측 중앙값으로 추정 보정. 그룹 표본이 아예
        # 없으면 전체 중앙값, 그것도 없으면 N/A.
        if psi_med:
            est = psi_med.get(page_type)
            if est is None:
                est = psi_med.get("__all__")
            if est is not None:
                return float(est) < max_v
        return None

    if iid in cr.maxage:
        # max-age 디렉티브가 있으면(0 포함) 통과. no-cache/no-store 동반은 무관.
        m = _MAXAGE.search(str(it.get("value") or ""))
        return bool(m) and int(m.group(1)) >= cr.maxage[iid]

    return it.get("pass")


def current_page_type(r):
    """저장된 page_type 대신 현재 분류기로 다시 판정한다.

    분류 규칙이 바뀌면(예: content → experience 로 축소, Coveo 상품목록 반영)
    재감사 없이 집계만 다시 돌려 반영할 수 있다. 저장값을 그대로 쓰면 옛 분류에
    묶인다.
    """
    url = r.get("url")
    if url:
        try:
            from page_type import detect_page_type
            return detect_page_type(None, url).get("id")
        except Exception:
            pass
    return (r.get("page_type") or {}).get("id")


def is_excluded(r):
    """집계 대상에서 빼야 할 페이지면 사유 문자열, 아니면 None."""
    pt = current_page_type(r)
    if pt in EXCLUDED_PAGE_TYPES:
        return pt
    if r.get("page_error"):
        return "non_200"
    # fetch 는 됐지만 상태코드가 200 이 아닌 경우 — 저장된 #41 항목으로 판별
    for b in (r.get("score", {}).get("breakdown") or {}).values():
        st = (b.get("items") or {}).get("ai_status_200")
        if st and st.get("pass") is False:
            return "non_200"
    return None


def aggregate_country(doc, cr, psi):
    excluded = defaultdict(int)
    results = []
    for x in doc.get("summary", []):
        r = x.get("result") or {}
        if not r.get("score"):
            continue
        why = is_excluded(r)
        if why:
            excluded[why] += 1
        else:
            results.append(r)

    n = len(results)
    if not n:
        return None

    psi_metric = next((m for m, _ in cr.psi_rules.values()), None)
    psi_med = psi_group_medians(results, psi, psi_metric) if psi_metric else None
    psi_measured = sum(1 for r in results
                       if (psi.get(r["url"]) or {}).get(psi_metric) is not None) if psi_metric else 0

    grade_dist = defaultdict(int)
    total_sum = 0
    cat_pts = defaultdict(float)
    cat_pass = defaultdict(int)
    cat_total = defaultdict(int)
    items_agg = {}

    for r in results:
        pt = current_page_type(r)
        url = r["url"]
        # 저장된 카테고리 구조는 무시하고 항목만 모아 현재 카테고리로 재매핑한다
        stored = {}
        for b in (r["score"].get("breakdown") or {}).values():
            stored.update(b.get("items") or {})

        # #17 통합 백필 — seo_indexable 은 신규 id 라 과거 run 에 없다.
        # 저장된 meta/헤더 판정을 OR 로 합성해 재감사 없이 채운다.
        if "seo_indexable" in cr.category and "seo_indexable" not in stored:
            parts = [stored.get("seo_robots"), stored.get("seo_robots_hdr")]
            parts = [x for x in parts if x and x.get("pass") is not None]
            if parts:
                stored["seo_indexable"] = {
                    "label": cr.label["seo_indexable"],
                    "pass": any(x.get("pass") for x in parts),
                    "value": " · ".join(str(x.get("value"))[:24] for x in parts),
                    "hint": None,
                }

        c_passed = defaultdict(int)
        c_total = defaultdict(int)
        for iid, it in stored.items():
            cat = cr.category.get(iid)
            if cat is None:          # 현재 기준에서 비활성 (#5, #8 등)
                continue
            if it.get("pass") is None and iid not in cr.psi_rules:
                continue             # 저장 시점에 이미 N/A
            p = item_pass(iid, it, url, pt, cr, psi, psi_med)
            if p is None:
                continue
            a = items_agg.setdefault(iid, {
                "label": cr.label[iid], "category": cat, "pass_cnt": 0, "applicable_n": 0})
            a["applicable_n"] += 1
            c_total[cat] += 1
            if p:
                a["pass_cnt"] += 1
                c_passed[cat] += 1

        r_passed = sum(c_passed.values())
        r_total = sum(c_total.values())
        for cat in cr.cats:
            cat_pass[cat] += c_passed[cat]
            cat_total[cat] += c_total[cat]
            cat_pts[cat] += round(c_passed[cat] / c_total[cat] * 100) if c_total[cat] else 0

        total = round(r_passed / r_total * 100) if r_total else 0
        total_sum += total
        grade_dist[
            "Good" if total >= cr.grade.get("good", 90) else
            "Need Improvement" if total >= cr.grade.get("need_improvement", 70) else
            "Poor"
        ] += 1

    breakdown = {
        cat: {
            "label": cr.cat_label[cat],
            "points_avg": round(cat_pts[cat] / n, 2),
            "max": 100,
            "pass_rate": round(cat_pass[cat] / cat_total[cat], 4) if cat_total[cat] else None,
            "items_n": len([i for i, c in cr.category.items() if c == cat]),
        }
        for cat in cr.cats
    }
    items = {
        iid: {
            "label": a["label"], "category": a["category"],
            "pass_rate": round(a["pass_cnt"] / a["applicable_n"], 4) if a["applicable_n"] else None,
            "applicable_n": a["applicable_n"],
        }
        for iid, a in items_agg.items()
    }

    return {
        "sample_size": n,
        "excluded": dict(excluded),
        "excluded_count": sum(excluded.values()),
        "total_avg": round(total_sum / n, 2),
        "max": 100,
        "grade_dist": dict(grade_dist),
        "breakdown": breakdown,
        "items": items,
        "agentic": agentic_summary(results, psi),
        "psi_coverage": {"measured": psi_measured, "estimated": n - psi_measured,
                         "rate": round(psi_measured / n, 4) if n else None},
    }


def agentic_summary(results, psi):
    """agentic-browsing 관측 집계. 9월 감사부터 채점 예정 — 지금은 관측만."""
    scores, audits = [], defaultdict(lambda: {"pass": 0, "measured": 0})
    for r in results:
        rec = psi.get(r["url"])
        if not rec or rec.get("error"):
            continue
        ag = rec.get("agentic") or {}
        if ag.get("score") is not None:
            scores.append(ag["score"])
        for aid, sc in (ag.get("audits") or {}).items():
            if sc is None:      # 평가 불가(WebMCP 미배포 등) — 분모에도 넣지 않는다
                continue
            audits[aid]["measured"] += 1
            if sc >= 1:
                audits[aid]["pass"] += 1
    if not scores and not audits:
        return None
    return {
        "measured_n": len(scores),
        "score_avg": round(sum(scores) / len(scores), 4) if scores else None,
        "audits": {a: {"pass_rate": round(v["pass"] / v["measured"], 4), "measured_n": v["measured"]}
                   for a, v in sorted(audits.items())},
    }


def pick_latest_runs():
    """국가별 최신 정식 run 1개씩. 백업/파생 파일 무시.

    미완성 run(status != "ok")은 후보에서 제외한다. 진행 중인 감사가 더 최신 날짜라는
    이유로 이전 완료본을 밀어내면, 표본이 절반만 찬 상태로 집계돼 점수가 왜곡된다
    (2026-08-28 UK 를 부분 데이터로 -3.9 오판한 사례).
    같은 날짜에 완료본이 여럿이면 성공 건수가 많은 것을 쓴다.
    """
    pat = re.compile(r"^([a-z][a-z_]*)_(\d{4}-\d{2}-\d{2})_run_[0-9a-f]+\.json$")
    best = {}
    for fn in os.listdir(RUNS):
        m = pat.match(fn)
        if not m:
            continue
        code, date = m.group(1), m.group(2)
        try:
            doc = json.load(open(os.path.join(RUNS, fn)))
        except Exception:
            continue
        if doc.get("status") != "ok":
            continue                      # 진행 중/중단된 run 은 집계하지 않는다
        ok = sum(1 for x in doc.get("summary", []) if (x.get("result") or {}).get("score"))
        rank = (date, ok)
        if code not in best or rank > best[code][0]:
            best[code] = (rank, fn)
    return {c: (v[0][0], v[1]) for c, v in best.items()}


def main():
    cr = Criteria()
    psi = load_psi_cache()
    latest = pick_latest_runs()
    countries, missing = {}, []

    for c in STRATEGIC:
        if c not in latest:
            missing.append(c)
            continue
        date, fn = latest[c]
        agg = aggregate_country(json.load(open(os.path.join(RUNS, fn))), cr, psi)
        if agg is None:
            missing.append(c)
            continue
        agg["date"], agg["run_file"] = date, fn
        agg["label"] = COUNTRY_LABELS.get(c, c.upper())
        countries[c] = agg

    sample_total = sum(v["sample_size"] for v in countries.values())
    weighted = sum(v["total_avg"] * v["sample_size"] for v in countries.values())
    excl = defaultdict(int)
    for v in countries.values():
        for k, num in v["excluded"].items():
            excl[k] += num

    out = {
        "countries": countries,
        "criteria": {
            "categories": {c: {"label": cr.cat_label[c],
                               "items_n": len([i for i, k in cr.category.items() if k == c])}
                           for c in cr.cats},
            "scored_items": len(cr.category),
        },
        "overall": {
            "countries": len(countries),
            "sample_total": sample_total,
            "excluded_total": sum(excl.values()),
            "excluded_breakdown": dict(excl),
            "total_avg_weighted": round(weighted / sample_total, 2) if sample_total else None,
            "missing": missing,
        },
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[dashboard] {len(countries)}국 · 채점항목 {len(cr.category)}개 "
          f"({len(cr.cats)}개 카테고리) → {OUT}")
    if missing:
        print(f"[dashboard] 누락: {missing}")
    for c in STRATEGIC:
        v = countries.get(c)
        if v:
            print(f"  {v.get('label', c.upper()):<12} {v['total_avg']:5.1f}  "
                  f"n={v['sample_size']:<5} (제외 {v['excluded_count']})  {v['date']}")
    print(f"  가중평균 {out['overall']['total_avg_weighted']}  "
          f"(표본 {sample_total}, 제외 {sum(excl.values())} {dict(excl)})")


if __name__ == "__main__":
    main()
