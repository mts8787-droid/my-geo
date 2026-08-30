#!/bin/bash
# 매월 전략 10국 GEO/AI Readability 감사 배치.
# launchd com.geoaudit.monthly (매월 25일)가 호출한다.
# 각 국가: run_render_audit.py <code> → data/run_results/<code>_<date>_run_*.json
#          완료 시 reports/audit_report.txt 자동 갱신(run_render_audit 내부).
# 한 국가가 실패해도 나머지는 계속 진행.
#
# 8국은 Render bulk(lightweight httpx), AU/IN은 Render 데이터센터 IP가 Akamai
# 403 차단을 받아 과소 산정되므로 --local(Mac Mini 주거용 IP httpx)로 우회한다.
#
# 실패 시 재감사: 감사 후 결과 품질을 확인해 실패면 최대 MAX_ATTEMPTS 회까지 재감사한다.
#   - 결과 0건(네트워크 단절 등)  → run_render_audit 의 resume 으로 이어서 재감사
#   - 파싱실패 50% 초과            → 결과가 resume 되어 남으므로 파일을 격리하고 처음부터
set -u
cd /Users/dubaba/my-geo-project/my-geo-audit || exit 1

PY=/usr/bin/python3   # httpx 포함 env (--local 은 analyzer import 필요)
RENDER_COUNTRIES="us uk de es ca br mx vn global"   # global = lg.com/global/newsroom (Global-Site)
LOCAL_COUNTRIES="au in"
MAX_ATTEMPTS=3
RETRY_GAP=1800        # 초 — 네트워크 복구 대기 (3회 × 30분 ≈ 1시간 커버)
STAMP=$(date +%Y%m%d_%H%M%S)
# 배치 전체가 한 날짜로 기록되도록 고정 — 국가별 실행이 UTC 자정을 넘겨도 갈리지 않는다
export AUDIT_RUN_DATE=$(date -u +%Y-%m-%d)
LOG="data/monthly_audit_${STAMP}.log"

# 오늘자 run 결과 품질 판정: 0=정상, 2=결과 없음, 3=파싱실패 과다
_check_run() {
  "$PY" - "$1" <<'PY'
import datetime, glob, json, sys
code = sys.argv[1]
date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
res = []
for f in glob.glob(f"data/run_results/{code}_{date}_run_*.json"):
    d = json.load(open(f))
    res += [x["result"] for x in d.get("summary", []) if (x.get("result") or {}).get("score")]
if not res:
    sys.exit(2)
pf = sum(1 for r in res
         if r["score"]["breakdown"].get("seo", {}).get("items", {})
         .get("seo_title", {}).get("hint") == "HTML 파싱 실패") / len(res)
print(f"[monthly-audit] {code}: 성공 {len(res)}건 · 파싱실패 {pf*100:.0f}%")
sys.exit(3 if pf > 0.5 else 0)
PY
}

# 못 쓰는 결과를 격리 — resume 대상에서 빠져 처음부터 다시 감사된다
_quarantine_run() {
  local c="$1" f
  for f in data/run_results/${c}_$(date -u +%Y-%m-%d)_run_*.json; do
    [ -e "$f" ] && mv "$f" "${f}.bad_${STAMP}"
  done
}

run_country() {
  local c="$1" flag="${2:-}" attempt rc
  for attempt in $(seq 1 $MAX_ATTEMPTS); do
    echo "===== ${c} ${flag:-render} try${attempt} $(date +%H:%M:%S) =====" >> "$LOG"
    "$PY" run_render_audit.py "$c" $flag >> "$LOG" 2>&1
    _check_run "$c" >> "$LOG" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
      echo "[monthly-audit] OK ${c} (try${attempt})" >> "$LOG"
      return 0
    fi
    if [ $rc -eq 3 ]; then
      _quarantine_run "$c"
      echo "[monthly-audit] WARN ${c} try${attempt} 파싱실패 과다 → 결과 격리 후 재감사" >> "$LOG"
    else
      echo "[monthly-audit] WARN ${c} try${attempt} 결과 없음 → 재감사" >> "$LOG"
    fi
    [ $attempt -lt $MAX_ATTEMPTS ] && sleep "$RETRY_GAP"
  done
  echo "[monthly-audit] ERROR ${c} ${MAX_ATTEMPTS}회 재감사 후에도 실패" >> "$LOG"
  return 1
}

echo "[monthly-audit] start ${STAMP}" >> "$LOG"

# ── 감사 전 US PDP 목록 갱신 ──────────────────────────────────────────────────
# 사이트맵은 신제품을 늦게 반영한다(2026-08-28 실측: 활성 제품 211개 누락, 반대로
# 단종품 3,253개 잔존). PLP 가 실제로 쓰는 Coveo API 에서 활성 PDP 를 받아
# 누락분만 CSV 에 추가한다. 비활성 URL 은 지우지 않는다 — 단종 페이지도
# #41 Status·#42 Soft 404 감사 대상이다.
# 전략 10국 전체. US 와 그 외가 Coveo 조직이 달라 plp_discover 가 알아서 분기한다.
# 딜러/교육/파트너 채널 스토어 경로는 제외한다 — 같은 제품의 채널별 사본이라
# 소비자 사이트맵에 없는 게 정상이고 GEO 감사 대상도 아니다(DE 기준 73%).
# 약 30분. 실패해도 감사에는 영향이 없으므로 best-effort.
echo "===== plp_discover (10국) $(date +%H:%M:%S) =====" >> "$LOG"
"$PY" plp_discover.py --all --merge --quiet >> "$LOG" 2>&1 \
  || echo "[monthly-audit] WARN plp_discover 실패 — 기존 URL 목록으로 진행" >> "$LOG"

for c in $RENDER_COUNTRIES; do
  run_country "$c"
done
for c in $LOCAL_COUNTRIES; do
  run_country "$c" --local
done
# ── PSI(Lighthouse) 측정값 수집 ───────────────────────────────────────────────
# 감사 직후 이어서 수집한다. 크롤러 자체 TTFB 는 동시 크롤 큐잉에 오염돼 실측 대비
# 6~200배 부풀려졌고(1,536 URL 대조: 크롤러 통과 12.4% vs PSI 97.7%),
# PSI 미수집이면 #1 이 전 페이지 na 로 빠진다.
#
# 전수는 호출당 60초라 비현실적이라 (국가, page_type) 그룹별 표본만 실측하고
# 나머지는 gen_dashboard_data 가 그룹 중앙값으로 보정한다.
# 속도는 5건/분 — 더 올리면 Google 네트워크 단위 차단을 맞는다(2026-08-26 경험).
if [ -z "${PSI_API_KEY:-}" ] && [ -f .psi_key ]; then
  PSI_API_KEY=$(tr -d '[:space:]' < .psi_key)
fi
if [ -n "${PSI_API_KEY:-}" ]; then
  echo "===== psi collect ${AUDIT_RUN_DATE} $(date +%H:%M:%S) =====" >> "$LOG"
  PSI_API_KEY="$PSI_API_KEY" "$PY" psi_collect.py \
    --run "data/run_results/*_${AUDIT_RUN_DATE}_run_*.json" \
    --per-group "${PSI_PER_GROUP:-8}" --rate "${PSI_RATE:-5}" --concurrency 4 >> "$LOG" 2>&1 \
    || echo "[monthly-audit] WARN psi_collect 실패 — #1 은 그룹 중앙값 추정으로 대체" >> "$LOG"
  "$PY" gen_dashboard_data.py >> "$LOG" 2>&1 \
    || echo "[monthly-audit] WARN 대시보드 재집계 실패" >> "$LOG"
else
  echo "[monthly-audit] WARN .psi_key 없음 — PSI 건너뜀 (#1 은 직전 캐시로 추정)" >> "$LOG"
fi

# 갱신된 대시보드 집계를 Render로 반영(원격 /mcp 엔드포인트가 최신 데이터 서빙).
# best-effort: 변경 없거나 push 실패해도 감사 결과엔 영향 없음.
if ! git diff --quiet reports/dashboard_data.json 2>/dev/null; then
  git add reports/dashboard_data.json >> "$LOG" 2>&1
  git commit -m "data(dashboard): 월간 감사 집계 갱신 $(date +%Y-%m)" >> "$LOG" 2>&1
  git push origin master >> "$LOG" 2>&1 && echo "[monthly-audit] dashboard_data push OK" >> "$LOG" \
    || echo "[monthly-audit] WARN dashboard push 실패" >> "$LOG"
fi
echo "[monthly-audit] done $(date +%Y%m%d_%H%M%S)" >> "$LOG"
