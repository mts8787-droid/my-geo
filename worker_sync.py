"""허브(Render)에서 audit_data.json을 주기적으로 pull하고 스케줄러를 reload.

워커 모드(Mac Mini)에서만 동작한다 — main.py lifespan에서 task로 실행.
허브의 /admin/audit-data 응답에서 groups/schedules만 로컬에 반영하고,
runs는 로컬(Mac Mini의 audit.db)에 그대로 유지한다.
"""
import asyncio
import json
import logging

import httpx

import audit_store
from scheduler import reload_schedules

log = logging.getLogger("geo_audit.worker_sync")

SYNC_INTERVAL_SEC = 60


async def start_hub_sync(hub_url: str, secret: str) -> None:
    hub_url = hub_url.rstrip("/")
    last_snapshot: str = ""

    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{hub_url}/admin/audit-data",
                    headers={"X-Worker-Secret": secret},
                )
                resp.raise_for_status()
                remote = resp.json()

            # groups + schedules만 비교 (runs는 워커 로컬이 source of truth)
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
                log.info("hub sync 적용 — 활성 스케줄 %d개", n)
                last_snapshot = snapshot
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("hub sync 실패: %s", e)

        try:
            await asyncio.sleep(SYNC_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
