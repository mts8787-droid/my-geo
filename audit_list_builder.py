"""[2단계] page_type별 audit_list builder.

url_classifications.json의 분류 결과를 받아 page_type별 최대 N개 random sample을
생성. audit_data.json::audit_lists 리스트에 저장.

데이터 구조 (audit_data.json):
{
  ...
  "groups": [...],
  "schedules": [...],
  "audit_lists": [
    {
      "id":              "list_grp_lg_uk_pdp",
      "name":            "UK - PDP Sample (100)",
      "source_group_id": "grp_lg_uk",
      "page_type":       "pdp",
      "urls":            [...],
      "url_count":       100,
      "created_at":      "...",
      "updated_at":      "..."
    },
    ...
  ]
}

같은 source_group_id × page_type 조합은 덮어쓰기 (재실행 가능).
"""

import logging
import random
from datetime import datetime, timezone
from typing import List, Optional

log = logging.getLogger(__name__)

SAMPLES_PER_TYPE = 100

# 표본 추출 안 할 page_type (의미 적은 분류)
_SKIP_PAGE_TYPES = {"unknown"}


def _list_id(source_group_id: str, page_type: str) -> str:
    """audit_list ID 규약: list_<source_group_id>_<page_type>."""
    return f"list_{source_group_id}_{page_type}"


async def build_lists_for_group(source_group_id: str, max_per_type: int = SAMPLES_PER_TYPE) -> dict:
    """source group의 분류 결과를 받아 page_type별 max_per_type 개 sample audit_list 생성.

    Returns: {"status": "ok", "source_group_id", "lists": [...], "total_urls": N}
    """
    import audit_store
    import db
    from url_classifier import get_group_classification

    started_at = datetime.now(timezone.utc)

    # 1) 분류 결과 로드
    classification = get_group_classification(source_group_id)
    if not classification:
        return {"status": "error", "error": f"{source_group_id} 분류 결과 없음. 먼저 Classify 진행 필요."}

    classifications = classification.get("classifications") or {}
    if not classifications:
        return {"status": "error", "error": "classifications 비어있음"}

    # 2) page_type별 URL 풀 구성
    by_type = {}
    for url, info in classifications.items():
        pt = info.get("page_type")
        if not pt or pt in _SKIP_PAGE_TYPES:
            continue
        by_type.setdefault(pt, []).append(url)

    if not by_type:
        return {"status": "no_types", "source_group_id": source_group_id}

    # 3) audit_data 로드, audit_lists 초기화
    data = audit_store.load()
    audit_lists = data.get("audit_lists") or []
    # 기존 같은 source+type 항목 제거 (덮어쓰기)
    audit_lists = [l for l in audit_lists if l.get("source_group_id") != source_group_id]

    # source group의 name (label용)
    source_group = next((g for g in data.get("groups", []) if g.get("id") == source_group_id), None)
    source_name = (source_group or {}).get("name", source_group_id)
    # "LG Sitemap - uk" → "uk" 로 축약 (list name에 사용)
    short_name = source_name.replace("LG Sitemap - ", "").strip() or source_group_id

    # 4) 각 page_type별 sample 생성
    new_lists = []
    total_urls = 0
    for pt, urls in by_type.items():
        n = min(max_per_type, len(urls))
        sample = random.sample(urls, n)
        list_entry = {
            "id":              _list_id(source_group_id, pt),
            "name":            f"{short_name} - {pt.upper()} ({n})",
            "source_group_id": source_group_id,
            "page_type":       pt,
            "urls":            sorted(sample),
            "url_count":       n,
            "created_at":      datetime.now(timezone.utc).isoformat(),
            "updated_at":      datetime.now(timezone.utc).isoformat(),
        }
        new_lists.append(list_entry)
        total_urls += n

    audit_lists.extend(new_lists)
    data["audit_lists"] = audit_lists
    await audit_store.save(data)

    by_type_str = ", ".join(f"{l['page_type']}:{l['url_count']}" for l in new_lists)
    db.add_system_log(
        f"[audit-list] {source_group_id}: {len(new_lists)} lists 생성, total {total_urls} URLs (by_type: {by_type_str})"
    )

    return {
        "status":          "ok",
        "source_group_id": source_group_id,
        "lists_created":   len(new_lists),
        "total_urls":      total_urls,
        "by_type":         {l["page_type"]: l["url_count"] for l in new_lists},
        "took_sec":        round((datetime.now(timezone.utc) - started_at).total_seconds(), 2),
    }


def load_audit_lists(source_group_id: Optional[str] = None) -> list:
    """저장된 audit_lists 조회. source_group_id 지정하면 필터링."""
    import audit_store
    data = audit_store.load()
    lists = data.get("audit_lists") or []
    if source_group_id:
        lists = [l for l in lists if l.get("source_group_id") == source_group_id]
    return lists


async def delete_audit_list(list_id: str) -> dict:
    """audit_list 삭제."""
    import audit_store
    data = audit_store.load()
    audit_lists = data.get("audit_lists") or []
    before = len(audit_lists)
    data["audit_lists"] = [l for l in audit_lists if l.get("id") != list_id]
    if len(data["audit_lists"]) == before:
        return {"status": "not_found", "list_id": list_id}
    await audit_store.save(data)
    return {"status": "ok", "list_id": list_id}
