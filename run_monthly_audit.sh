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
RENDER_COUNTRIES="us uk de es ca br mx vn"
LOCAL_COUNTRIES="au in"
MAX_ATTEMPTS=3
RETRY_GAP=1800        # 초 — 네트워크 복구 대기 (3회 × 30분 ≈ 1시간 커버)
STAMP=$(date +%Y%m%d_%H%M%S)
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
for c in $RENDER_COUNTRIES; do
  run_country "$c"
done
for c in $LOCAL_COUNTRIES; do
  run_country "$c" --local
done
# ── PSI(Lighthouse) 측정값 ────────────────────────────────────────────────────
# 여기서 수집하지 않는다. 전수 5,700여 건을 6건/분(PSI 지속 부하 한계)으로 받으면
# 약 16시간이라 월간 감사를 블로킹한다. 별도 야간 잡이 예산만큼씩 나눠 채운다:
#   run_psi_nightly.sh · launchd com.geoaudit.psi (매일 01:00, 기본 6시간 예산)
# 야간 잡은 캐시에 없는 URL 만 집으므로, 이번 감사에서 새로 생긴 URL 이 자동으로
# 다음 밤부터 채워진다. 미수집 구간은 #1 이 N/A 로 빠질 뿐 집계는 정상 동작한다.

# 갱신된 대시보드 집계를 Render로 반영(원격 /mcp 엔드포인트가 최신 데이터 서빙).
# best-effort: 변경 없거나 push 실패해도 감사 결과엔 영향 없음.
if ! git diff --quiet reports/dashboard_data.json 2>/dev/null; then
  git add reports/dashboard_data.json >> "$LOG" 2>&1
  git commit -m "data(dashboard): 월간 감사 집계 갱신 $(date +%Y-%m)" >> "$LOG" 2>&1
  git push origin master >> "$LOG" 2>&1 && echo "[monthly-audit] dashboard_data push OK" >> "$LOG" \
    || echo "[monthly-audit] WARN dashboard push 실패" >> "$LOG"
fi
echo "[monthly-audit] done $(date +%Y%m%d_%H%M%S)" >> "$LOG"
