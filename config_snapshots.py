"""scoring_config 버전 스냅샷 관리.

data/scoring_config_snapshots/{name}.json 형태로 저장.
각 파일은 scoring_config과 동일한 dict 구조.
"""
import json
import os
import re
from datetime import datetime, timezone
from typing import List, Optional

DATA_DIR = os.environ.get(
    "DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
SNAPSHOT_DIR = os.path.join(DATA_DIR, "scoring_config_snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

_BAD_CHARS = re.compile(r"[/\\:\x00-\x1f]")


def _safe_filename(name: str) -> str:
    """이름 검증 + .json suffix 부여. 부적합하면 ValueError."""
    name = (name or "").strip()
    if not name:
        raise ValueError("이름이 비어있습니다.")
    if _BAD_CHARS.search(name) or ".." in name or name.startswith("."):
        raise ValueError("이름에 사용할 수 없는 문자가 있습니다.")
    if len(name) > 80:
        raise ValueError("이름이 너무 깁니다 (80자 이내).")
    return name + ".json"


def _path(name: str) -> str:
    return os.path.join(SNAPSHOT_DIR, _safe_filename(name))


def list_snapshots() -> List[dict]:
    """저장된 스냅샷 메타데이터 리스트. created_at desc 정렬."""
    items = []
    try:
        for fname in os.listdir(SNAPSHOT_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(SNAPSHOT_DIR, fname)
            try:
                st = os.stat(path)
                items.append({
                    "name": fname[:-5],
                    "created_at": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "size_bytes": st.st_size,
                })
            except OSError:
                continue
    except FileNotFoundError:
        return []
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def get_snapshot(name: str) -> Optional[dict]:
    try:
        path = _path(name)
    except ValueError:
        return None
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(name: str, config: dict) -> None:
    path = _path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def delete_snapshot(name: str) -> bool:
    try:
        path = _path(name)
    except ValueError:
        return False
    if not os.path.exists(path):
        return False
    os.unlink(path)
    return True
