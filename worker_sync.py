"""허브(Render)에서 audit_data.json을 주기적으로 pull하고 스케줄러를 reload.

워커 모드(Mac Mini)에서만 동작한다 — main.py lifespan에서 task로 실행.
허브의 /admin/audit-data 응답에서 groups/schedules만 로컬에 반영하고,
runs는 로컬(Mac Mini의 audit.db)에 그대로 유지한다.
"""
import asyncio
import json
import logging
import os

import httpx

import audit_store
from scheduler import reload_schedules

log = logging.getLogger("geo_audit.worker_sync")

# 허브(Render) 폴링 주기. 기본 12시간 — 스케줄 변경이 잦지 않다는 가정.
# 더 빠른 반영이 필요하면 HUB_SYNC_INTERVAL_SEC 환경변수로 조정.
SYNC_INTERVAL_SEC = int(os.environ.get("HUB_SYNC_INTERVAL_SEC", 12 * 3600))


_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
_URL_CLASSIFICATIONS_PATH = os.path.join(_DATA_DIR, "url_classifications.json")
_CSR_BASELINE_PATH        = os.path.join(_DATA_DIR, "csr_baseline.json")


def _write_aux_atomic(path: str, data: dict) -> None:
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".aux_", suffix=".json.tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


async def start_hub_sync(hub_url: str, secret: str) -> None:
    hub_url = hub_url.rstrip("/")
    last_snapshot: str = ""
    last_aux_snapshots: dict = {}

    while True:
        # 1) audit_data (groups/schedules) — 핵심 데이터
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(
                    f"{hub_url}/admin/audit-data",
                    headers={"X-Worker-Secret": secret},
                )
                resp.raise_for_status()
                remote = resp.json()

            relevant = {
                "groups": remote.get("groups", []),
                "schedules": remote.get("schedules", []),
            }
            snapshot = json.dumps(relevant, sort_keys=True, ensure_ascii=False)

            if snapshot != last_snapshot:
                local = audit_store.load()
                local["groups"] = relevant["groups"]
                local["schedules"] = relevant["schedules"]
                await audit_store.save(local)
                n = reload_schedules()
                log.info("hub sync audit_data 적용 — 활성 스케줄 %d개", n)
                last_snapshot = snapshot
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("hub sync audit_data 실패: %s", e)

        # 2) 보조 파일들 — Render에서 생성되는 데이터를 Mac Mini가 보존
        for endpoint, local_path, key in [
            ("/admin/url-classifications-raw", _URL_CLASSIFICATIONS_PATH, "url_classifications"),
            ("/admin/csr-baseline-raw",        _CSR_BASELINE_PATH,        "csr_baseline"),
        ]:
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(
                        f"{hub_url}{endpoint}",
                        headers={"X-Worker-Secret": secret},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                if not data:
                    continue
                snap = json.dumps(data, sort_keys=True, ensure_ascii=False)
                if last_aux_snapshots.get(key) != snap:
                    _write_aux_atomic(local_path, data)
                    log.info("hub sync %s 적용 — entries=%d", key, len(data))
                    last_aux_snapshots[key] = snap
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("hub sync %s 실패: %s", key, e)

        try:
            await asyncio.sleep(SYNC_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
