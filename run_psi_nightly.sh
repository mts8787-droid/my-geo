#!/bin/bash
# PSI(Lighthouse) 측정값 야간 분할 수집.
# launchd com.geoaudit.psi (매일 01:00)가 호출한다.
#
# 전수 5,700여 건을 6건/분으로 받으면 약 16시간이라 한 번에 못 끝낸다.
# 매일 밤 예산(기본 6시간)만큼만 돌고 종료하고, 다음 날 밤이 이어받는다.
# 캐시에 성공 기록이 있는 URL 은 건너뛰므로 다 채워지면 즉시 종료한다.
#
# 속도를 6건/분으로 묶은 이유: PSI 는 짧은 버스트만 14건/분을 받아주고 지속
# 부하에는 페널티 창을 건다. 2026-08-26 여기서 Google 네트워크 단위 차단
# (429 "automated queries")을 맞았다. psi_collect 의 전역 게이트가 속도를
# 강제하고 연속 실패 시 전체를 멈춘다.
set -u
cd /Users/dubaba/my-geo-project/my-geo-audit || exit 1

PY=/usr/bin/python3
BUDGET_MIN=${PSI_BUDGET_MIN:-360}     # 1회 실행 시간 예산(분)
RATE=${PSI_RATE:-6}                   # 건/분
STAMP=$(date +%Y%m%d_%H%M%S)
LOG="data/psi_nightly_${STAMP}.log"

if [ -z "${PSI_API_KEY:-}" ] && [ -f .psi_key ]; then
  PSI_API_KEY=$(tr -d '[:space:]' < .psi_key)
fi
if [ -z "${PSI_API_KEY:-}" ]; then
  echo "[psi-nightly] .psi_key 없음 — 중단" >> "$LOG"
  exit 1
fi

# 감사 산출물의 실제 날짜를 쓴다 (감사가 UTC 자정을 넘길 수 있음)
RUN_DATE=$(ls -t data/run_results/*_run_*.json 2>/dev/null | head -1 \
           | sed -E 's/.*_([0-9]{4}-[0-9]{2}-[0-9]{2})_run_.*/\1/')
RUN_DATE=${RUN_DATE:-$(date -u +%Y-%m-%d)}

echo "[psi-nightly] start ${STAMP} · date=${RUN_DATE} · 예산 ${BUDGET_MIN}분 · ${RATE}건/분" >> "$LOG"
PSI_API_KEY="$PSI_API_KEY" "$PY" psi_collect.py \
  --run "data/run_results/*_${RUN_DATE}_run_*.json" \
  --rate "$RATE" --max-minutes "$BUDGET_MIN" >> "$LOG" 2>&1
rc=$?

# 수집분을 대시보드에 반영 (부분 수집이어도 반영된다 — 미수집분은 N/A)
"$PY" gen_dashboard_data.py >> "$LOG" 2>&1 \
  || echo "[psi-nightly] WARN 대시보드 재집계 실패" >> "$LOG"

echo "[psi-nightly] done $(date +%Y%m%d_%H%M%S) rc=${rc}" >> "$LOG"
