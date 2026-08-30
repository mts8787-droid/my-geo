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



# ── B2B URL 오버라이드 ────────────────────────────────────────────────────────
# LG 가 business/sitemap.xml 에 등재한 URL = B2B. 경로 규칙으로는 못 가른다:
# US 는 B2B 제품이 /us/digital-signage/, /us/hospitality-tvs/ 처럼 최상위에 있고
# /us/laptops/·/us/computer-monitors/ 는 소비자용과 경로를 공유한다(549건 오분류).
# 다른 국가는 전부 /{cc}/business/ 아래라 이 오버라이드가 걸리지 않는다.
_B2B_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "b2b")
_PLP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "plp")
_b2b_cache: dict = {}
_plp_cache: dict = {}


def _b2b_urls(code: str) -> set:
    if code not in _b2b_cache:
        path = os.path.join(_B2B_DIR, f"{code}.txt")
        try:
            with open(path, encoding="utf-8") as f:
                _b2b_cache[code] = {ln.strip() for ln in f if ln.strip()}
        except Exception:
            _b2b_cache[code] = set()
    return _b2b_cache[code]


def _plp_urls(code: str) -> set:
    """PLP 상품 API(Coveo)로 수집한 활성 PDP 목록. plp_discover.py 산출."""
    if code not in _plp_cache:
        path = os.path.join(_PLP_DIR, f"{code}.txt")
        try:
            with open(path, encoding="utf-8") as f:
                _plp_cache[code] = {ln.strip().rstrip("/") for ln in f if ln.strip()}
        except Exception:
            _plp_cache[code] = set()
    return _plp_cache[code]


def is_plp_product(url: str) -> bool:
    """PLP 가 실제로 노출하는 상품이면 True — URL 패턴보다 정확한 PDP 근거.

    모델명 형식이 국가마다 달라 패턴 매칭은 오분류가 크다. AU 냉장고
    /au/fridge-freezers/french-door/gf-l500mwh/ 373건 중 PDP 로 잡힌 건 10건뿐이고
    242건이 unknown 이었다.
    """
    m = re.match(r"https?://www\.lg\.com/([^/]+)/", url or "")
    if not m:
        return False
    code = m.group(1).lower()
    base = (url or "").split("?")[0].rstrip("/")
    for key in (code, code.split("_")[0]):
        if base in _plp_urls(key):
            return True
    return False


def is_b2b_url(url: str) -> bool:
    m = re.match(r"https?://www\.lg\.com/([^/]+)/", url or "")
    if not m:
        return False
    code = m.group(1).lower()
    # ca_en 처럼 사이트 경로와 감사 코드가 다른 경우까지 커버
    for key in (code, code.split("_")[0]):
        if url.rstrip("/") in {u.rstrip("/") for u in _b2b_urls(key)}:
            return True
    return False


def detect_page_type(soup, url: str = "") -> dict:
    """soup을 보고 가장 잘 맞는 페이지 타입을 반환.

    Returns: {"id": str, "label": str, "matched_by": str, "expected_schemas": list, "recommended_schemas": list}
    """
    cfg = load_page_types()
    page_types = cfg.get("page_types", [])

    # 0) business/sitemap.xml 등재 URL 은 무조건 B2B
    if url and is_b2b_url(url):
        for pt in page_types:
            if pt["id"] == "business":
                return {"id": "business", "label": pt.get("label", "B2B (사업자)"),
                        "matched_by": "business_sitemap",
                        "expected_schemas": pt.get("expected_schemas", []),
                        "recommended_schemas": pt.get("recommended_schemas", [])}

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

    # 3) 정의된 타입 순회 (약한 판정은 보류했다가 PLP 상품목록과 대조)
    weak_hit = None
    WEAK = {"microsite", "plp"}
    # 우선순위: url_pattern > meta_template > body_class
    # — URL이 페이지 의도를 가장 직접 표현하고, LG가 template를 재사용하는 케이스
    # (business→home-page, why-lg-oled→pdp-page)를 무력화하기 위해 URL 우선.
    # 두 패스로 분리: 1패스 URL 매칭, 2패스 meta/body fallback.

    # 1패스: url_pattern
    if url:
        for pt in page_types:
            if pt.get("id") == "unknown":
                continue
            url_targets = (pt.get("detection") or {}).get("url_pattern") or []
            for pat in url_targets:
                try:
                    if re.search(pat, url, re.IGNORECASE):
                        # 약한 판정은 즉시 확정하지 않고 보류 — 뒤에서 PLP 상품
                        # 목록과 대조해 PDP 로 교정할 수 있다.
                        if pt.get("id") in WEAK:
                            if weak_hit is None:
                                weak_hit = _build(pt, matched_by=f"url_pattern={pat}")
                            break
                        return _build(pt, matched_by=f"url_pattern={pat}")
                except re.error:
                    continue

    # 2패스: meta_template / body_class fallback (URL 패턴 없는 page_type 대응 — pdp, plp 등)
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

    # 3-b) 약한 판정(microsite/plp)이 나왔는데 PLP 상품 목록에 있으면 PDP 로 교정.
    #      상품 API 가 내려준 URL 은 정의상 상품 상세다.
    if url and weak_hit and is_plp_product(url):
        for pt in page_types:
            if pt.get("id") == "pdp":
                return _build(pt, matched_by="plp_product_api(교정)")
    if weak_hit:
        return weak_hit

    # 4) PLP 상품 API 구제 — 패턴이 못 잡은 것만 PDP 로 올린다.
    #    business·support 처럼 확신 있는 판정은 덮지 않는다(덮으면 BR support 493건,
    #    DE business 688건이 PDP 로 뒤집힌다). 모델명 형식이 국가마다 달라 패턴이
    #    놓치는 경우만 대상: AU /au/fridge-freezers/french-door/gf-l500mwh/ 242건 등.
    if url and is_plp_product(url):
        for pt in page_types:
            if pt.get("id") == "pdp":
                return _build(pt, matched_by="plp_product_api")

    # 5) fallback — unknown
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
