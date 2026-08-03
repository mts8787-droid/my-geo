#!/bin/bash
# 매월 전략 10국 GEO/AI Readability 감사 배치.
# launchd com.geoaudit.monthly (매월 25일)가 호출한다.
# 각 국가: run_render_audit.py <code> → data/run_results/<code>_<date>_run_*.json
#          완료 시 reports/audit_report.txt 자동 갱신(run_render_audit 내부).
# 한 국가가 실패해도 나머지는 계속 진행.
#
# 8국은 Render bulk(lightweight httpx), AU/IN은 Render 데이터센터 IP가 Akamai
# 403 차단을 받아 과소 산정되므로 --local(Mac Mini 주거용 IP httpx)로 우회한다.
set -u
cd /Users/dubaba/my-geo-project/my-geo-audit || exit 1

PY=/usr/bin/python3   # httpx 포함 env (--local 은 analyzer import 필요)
RENDER_COUNTRIES="us uk de es ca br mx vn"
LOCAL_COUNTRIES="au in"
STAMP=$(date +%Y%m%d_%H%M%S)
LOG="data/monthly_audit_${STAMP}.log"

echo "[monthly-audit] start ${STAMP}" >> "$LOG"
for c in $RENDER_COUNTRIES; do
  echo "===== ${c} (render) $(date +%H:%M:%S) =====" >> "$LOG"
  "$PY" run_render_audit.py "$c" >> "$LOG" 2>&1 || echo "[monthly-audit] WARN ${c} exit $?" >> "$LOG"
done
for c in $LOCAL_COUNTRIES; do
  echo "===== ${c} (local) $(date +%H:%M:%S) =====" >> "$LOG"
  "$PY" run_render_audit.py "$c" --local >> "$LOG" 2>&1 || echo "[monthly-audit] WARN ${c} exit $?" >> "$LOG"
done
# 갱신된 대시보드 집계를 Render로 반영(원격 /mcp 엔드포인트가 최신 데이터 서빙).
# best-effort: 변경 없거나 push 실패해도 감사 결과엔 영향 없음.
if ! git diff --quiet reports/dashboard_data.json 2>/dev/null; then
  git add reports/dashboard_data.json >> "$LOG" 2>&1
  git commit -m "data(dashboard): 월간 감사 집계 갱신 $(date +%Y-%m)" >> "$LOG" 2>&1
  git push origin master >> "$LOG" 2>&1 && echo "[monthly-audit] dashboard_data push OK" >> "$LOG" \
    || echo "[monthly-audit] WARN dashboard push 실패" >> "$LOG"
fi
echo "[monthly-audit] done $(date +%Y%m%d_%H%M%S)" >> "$LOG"
