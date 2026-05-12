"""
audit_data.json 단일 진입점.

main.py와 scheduler.py가 이 모듈을 통해서만 audit_data.json을 읽고 쓴다.
- 단일 asyncio.Lock으로 동시 쓰기 보호 (이전엔 두 모듈에 락이 분리돼 있어 race가 있었음)
- atomic write (tempfile → os.replace)로 부분 쓰기/손상 방지
- 마이그레이션은 load() 호출 시점에 1회 수행
"""
import asyncio
import json
import os
import tempfile
import uuid

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

_AUDIT_DATA_PATH = os.path.join(DATA_DIR, "audit_data.json")
_lock = asyncio.Lock()


def _migrate(data: dict) -> bool:
    """기존 스키마 → 신규 스키마 정규화. 변경이 있었으면 True."""
    mutated = False
    if "groups" not in data:
        data["groups"] = []; mutated = True
    if "schedules" not in data:
        data["schedules"] = []; mutated = True
    if "runs" not in data:
        data["runs"] = []; mutated = True

    for g in data["groups"]:
        if "id" not in g:
            g["id"] = f"grp_{uuid.uuid4().hex[:10]}"
            mutated = True
    for s in data["schedules"]:
        if "id" not in s:
            s["id"] = f"sch_{uuid.uuid4().hex[:10]}"
            mutated = True
        if "freq" in s and "frequency" not in s:
            s["frequency"] = s["freq"]
            mutated = True
        if "groupIdx" in s and "group_id" not in s:
            try:
                idx = int(s["groupIdx"])
                if 0 <= idx < len(data["groups"]):
                    s["group_id"] = data["groups"][idx]["id"]
                    mutated = True
            except Exception:
                pass
    return mutated


def _read_sync() -> dict:
    try:
        with open(_AUDIT_DATA_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_atomic(data: dict) -> None:
    dir_ = os.path.dirname(_AUDIT_DATA_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".audit_", suffix=".json.tmp", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _AUDIT_DATA_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load() -> dict:
    """동기 로드 — 마이그레이션이 필요하면 디스크에 저장한다."""
    data = _read_sync()
    if _migrate(data):
        try:
            _write_atomic(data)
        except Exception as e:
            print(f"[audit_store] migration save failed: {e}")
    return data


async def save(data: dict) -> None:
    """비동기 저장 — 단일 lock 보호 + atomic write."""
    async with _lock:
        _write_atomic(data)
