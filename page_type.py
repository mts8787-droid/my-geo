"""페이지 타입 감지.

page_types.json에 정의된 페이지 타입을 BeautifulSoup으로 감지한다.
감지 우선순위: meta name=template (가장 명시적) → body class → unknown fallback.

향후 schema_for_page_type 룰 등에서 ctx['page_type'] 으로 활용.
"""
import json
import logging
import os
import re
from typing import Optional, Tuple

log = logging.getLogger("geo_audit.page_type")

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_types.json")
_cache: Optional[dict] = None


def load_page_types(force: bool = False) -> dict:
    """page_types.json을 메모리에 로드 (캐시)."""
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception as e:
        log.warning("page_types.json 로드 실패: %s", e)
        _cache = {"page_types": [{"id": "unknown", "label": "분류 불가", "detection": None, "expected_schemas": [], "recommended_schemas": []}]}
    return _cache


def detect_page_type(soup, url: str = "") -> dict:
    """soup을 보고 가장 잘 맞는 페이지 타입을 반환.

    Returns: {"id": str, "label": str, "matched_by": str, "expected_schemas": list, "recommended_schemas": list}
    """
    cfg = load_page_types()
    page_types = cfg.get("page_types", [])

    # 1) meta name="template" content="..." 값 추출
    template_value: Optional[str] = None
    if soup:
        m = soup.find("meta", attrs={"name": "template"})
        if m and m.get("content"):
            template_value = m["content"].strip().lower()

    # 2) body class 추출
    body_classes: set = set()
    if soup:
        body = soup.find("body")
        if body and body.get("class"):
            cls = body.get("class")
            if isinstance(cls, list):
                body_classes = {c.lower() for c in cls}
            elif isinstance(cls, str):
                body_classes = {c.lower() for c in cls.split()}

    # 3) 정의된 타입 순회 (unknown 빼고). 우선순위: meta_template > body_class > url_pattern
    for pt in page_types:
        if pt.get("id") == "unknown":
            continue
        detection = pt.get("detection") or {}

        meta_targets = [v.lower() for v in (detection.get("meta_template") or [])]
        if template_value and template_value in meta_targets:
            return _build(pt, matched_by=f"meta_template={template_value}")

        body_targets = [v.lower() for v in (detection.get("body_class") or [])]
        if body_classes and any(t in body_targets for t in body_classes):
            matched = next(t for t in body_targets if t in body_classes)
            return _build(pt, matched_by=f"body_class={matched}")

        url_targets = detection.get("url_pattern") or []
        if url and url_targets:
            for pat in url_targets:
                try:
                    if re.search(pat, url, re.IGNORECASE):
                        return _build(pt, matched_by=f"url_pattern={pat}")
                except re.error:
                    continue

    # 4) fallback — unknown
    unk = next((pt for pt in page_types if pt.get("id") == "unknown"), None)
    if unk:
        return _build(unk, matched_by="fallback")
    return {"id": "unknown", "label": "분류 불가", "matched_by": "fallback", "expected_schemas": [], "recommended_schemas": []}


def _build(pt: dict, matched_by: str) -> dict:
    return {
        "id":                  pt.get("id"),
        "label":               pt.get("label"),
        "matched_by":          matched_by,
        "expected_schemas":    pt.get("expected_schemas") or [],
        "recommended_schemas": pt.get("recommended_schemas") or [],
    }
