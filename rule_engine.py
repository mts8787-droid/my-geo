"""
룰 엔진 — 어드민에서 정의한 규칙을 실제로 평가합니다.

context dict:
  - soup: BeautifulSoup (HTML 파싱 결과)
  - page_data: dict (redirect_count, final_url, http_status, html_bytes, ttfb_ms, http_version, headers)
  - jsonld_types: set[str] (소문자 변환된 모든 @type 값)
  - jsonld_raw: list[dict] (원본 JSON-LD 데이터 — 필수 필드 검증용)
  - base_url: str (llms.txt 등 HTTP 체크용)
  - current_url: str (canonical 비교용 — final_url)
  - csr_ratio_dict: dict (SSR/CSR 비율 정보)
"""

import re
import operator as op
import httpx
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

# ── 룰 타입 메타데이터 (어드민 UI에서 사용) ───────────────────────────────────

RULE_TYPES = {
    "css_exists": {
        "label": "CSS 요소 존재",
        "description": "CSS 셀렉터에 매칭되는 요소가 1개 이상 존재하는지 확인",
        "params": {"selector": {"label": "CSS 셀렉터", "type": "text", "placeholder": "#reviews_container"}},
    },
    "css_count": {
        "label": "CSS 요소 개수",
        "description": "CSS 셀렉터에 매칭되는 요소 개수를 비교",
        "params": {
            "selector": {"label": "CSS 셀렉터", "type": "text", "placeholder": "h1"},
            "operator": {"label": "비교 연산자", "type": "select", "options": ["==", ">=", "<=", ">", "<"]},
            "value":    {"label": "비교 값", "type": "number", "placeholder": "1"},
        },
    },
    "css_text_min_length": {
        "label": "요소 텍스트 최소 길이",
        "description": "CSS 셀렉터의 첫 요소 텍스트 길이가 최소값 이상인지 확인",
        "params": {
            "selector":   {"label": "CSS 셀렉터", "type": "text", "placeholder": "title"},
            "min_length": {"label": "최소 길이", "type": "number", "placeholder": "10"},
        },
    },
    "css_attr_exists": {
        "label": "속성값 존재",
        "description": "CSS 셀렉터의 요소에 특정 속성이 비어있지 않은 값으로 존재하는지 확인",
        "params": {
            "selector":   {"label": "CSS 셀렉터", "type": "text", "placeholder": 'meta[property="og:title"]'},
            "attr":       {"label": "속성명", "type": "text", "placeholder": "content"},
            "min_length": {"label": "최소 길이 (선택)", "type": "number", "placeholder": "0"},
        },
    },
    "css_all_have_attr": {
        "label": "모든 요소에 속성 존재",
        "description": "CSS 셀렉터에 매칭되는 모든 요소에 특정 속성이 존재하는지 확인 (0개면 통과)",
        "params": {
            "selector": {"label": "CSS 셀렉터", "type": "text", "placeholder": "img"},
            "attr":     {"label": "속성명", "type": "text", "placeholder": "alt"},
        },
    },
    "css_attr_not_contains": {
        "label": "속성에 텍스트 미포함",
        "description": "CSS 셀렉터 요소의 속성값에 특정 텍스트가 포함되지 않으면 통과 (요소 없으면 통과)",
        "params": {
            "selector": {"label": "CSS 셀렉터", "type": "text", "placeholder": 'meta[name="robots"]'},
            "attr":     {"label": "속성명", "type": "text", "placeholder": "content"},
            "value":    {"label": "미포함 텍스트", "type": "text", "placeholder": "noindex"},
        },
    },
    "class_id_contains": {
        "label": "class/id에 키워드 포함",
        "description": "요소의 class 또는 id 속성에 키워드가 포함되는지 확인",
        "params": {
            "keywords": {"label": "키워드 (쉼표 구분)", "type": "text", "placeholder": "faq,자주 묻는,accordion"},
            "tags":     {"label": "검색 태그 (쉼표 구분)", "type": "text", "placeholder": "div,section,aside"},
        },
    },
    "text_has_pattern": {
        "label": "텍스트 정규식 매칭",
        "description": "특정 태그들의 텍스트에서 정규식 패턴이 매칭되는 단어가 존재하는지 확인",
        "params": {
            "pattern": {"label": "정규식 패턴", "type": "text", "placeholder": "\\d"},
            "tags":    {"label": "검색 태그 (쉼표 구분)", "type": "text", "placeholder": "p,li,td,h1,h2,h3"},
        },
    },
    "http_status": {
        "label": "HTTP 상태 확인",
        "description": "특정 경로로 HTTP 요청을 보내 응답 상태 코드를 확인",
        "params": {
            "path":   {"label": "경로", "type": "text", "placeholder": "/llms.txt"},
            "status": {"label": "기대 상태코드", "type": "number", "placeholder": "200"},
        },
    },
    "schema_type_exists": {
        "label": "JSON-LD @type 존재",
        "description": "페이지의 JSON-LD 구조화 데이터에 특정 @type이 존재하는지 확인 (OR 조건: 쉼표로 여러 타입 지정 가능)",
        "params": {
            "type": {"label": "@type (쉼표 구분 시 OR 조건)", "type": "text", "placeholder": "Product,IndividualProduct"},
        },
    },
    "heading_order": {
        "label": "제목 논리적 순서",
        "description": "H1이 H2/H3/H4보다 먼저 나타나는지 확인",
        "params": {},
    },
    "redirect_max": {
        "label": "리다이렉트 최대 횟수",
        "description": "페이지 접근 시 리다이렉트 횟수가 최대값 이하인지 확인",
        "params": {
            "max_count": {"label": "최대 횟수", "type": "number", "placeholder": "3"},
        },
    },

    # ── Performance / HTTP 헤더 룰 ──────────────────────────────────────────────
    "header_value_in": {
        "label": "응답 헤더 값 화이트리스트",
        "description": "특정 응답 헤더 값이 허용 리스트에 포함되는지 확인 (예: Content-Encoding ∈ gzip,br,deflate)",
        "params": {
            "header": {"label": "헤더명", "type": "text", "placeholder": "content-encoding"},
            "values": {"label": "허용 값 (쉼표 구분)", "type": "text", "placeholder": "gzip,br,deflate"},
        },
    },
    "header_max_age_min": {
        "label": "Cache-Control max-age 검증",
        "description": "Cache-Control 헤더의 max-age 값이 최소 임계 이상인지 확인",
        "params": {
            "min_seconds": {"label": "최소 max-age 값 (초)", "type": "number", "placeholder": "1"},
        },
    },
    "header_no_value": {
        "label": "응답 헤더에 특정 토큰 부재",
        "description": "응답 헤더 값에 특정 토큰이 없으면 통과 (예: X-Robots-Tag에 noindex 부재)",
        "params": {
            "header": {"label": "헤더명", "type": "text", "placeholder": "x-robots-tag"},
            "token":  {"label": "금지 토큰", "type": "text", "placeholder": "noindex"},
        },
    },
    "ttfb_under_ms": {
        "label": "TTFB 임계값 이하",
        "description": "Time To First Byte가 지정 임계 이하인지 확인",
        "params": {
            "max_ms": {"label": "최대 TTFB (ms)", "type": "number", "placeholder": "600"},
        },
    },
    "http_protocol_min": {
        "label": "HTTP 프로토콜 버전",
        "description": "HTTP 응답이 지정 버전 이상인지 확인 (HTTP/2, HTTP/3 등)",
        "params": {
            "min_version": {"label": "최소 버전", "type": "select", "options": ["HTTP/2", "HTTP/3"]},
        },
    },
    "html_size_under_kb": {
        "label": "HTML 본문 크기 제한",
        "description": "HTML 응답 바이트 크기가 임계 이하인지 확인",
        "params": {
            "max_kb": {"label": "최대 크기 (KB)", "type": "number", "placeholder": "100"},
        },
    },
    "status_code_eq": {
        "label": "HTTP 상태 코드 일치",
        "description": "페이지 응답 status code가 지정 값과 같은지 확인",
        "params": {
            "code": {"label": "기대 코드", "type": "number", "placeholder": "200"},
        },
    },
    "soft_404_check": {
        "label": "Soft 404 검출",
        "description": "status 200이지만 본문 텍스트 길이가 임계 미만이면 Soft 404로 판정",
        "params": {
            "min_text_length": {"label": "최소 본문 텍스트 길이", "type": "number", "placeholder": "200"},
        },
    },

    # ── Accessibility 룰 ───────────────────────────────────────────────────────
    "landmark_count_min": {
        "label": "Semantic 랜드마크 수",
        "description": "main 태그 + 랜드마크(nav/header/footer/article/section/aside) 합계가 임계 이상인지 확인",
        "params": {
            "min_landmarks": {"label": "최소 랜드마크 수", "type": "number", "placeholder": "3"},
            "require_main":  {"label": "main 태그 필수", "type": "select", "options": ["yes", "no"]},
        },
    },
    "heading_no_jump": {
        "label": "Heading 계층 점프 검증",
        "description": "h1→h3과 같은 헤딩 레벨 점프가 0건인지 확인",
        "params": {},
    },
    "aria_missing_ratio_max": {
        "label": "ARIA 라벨 누락 비율 제한",
        "description": "인터랙티브 요소(button/input/a) 중 접근성 텍스트(aria-label/aria-labelledby/title/text) 누락 비율 임계 이하",
        "params": {
            "max_ratio": {"label": "최대 누락 비율 (0.0~1.0)", "type": "number", "placeholder": "0.1"},
        },
    },

    # ── SEO / 콘텐츠 룰 ────────────────────────────────────────────────────────
    "canonical_self": {
        "label": "Canonical Self-Referencing",
        "description": "link[rel=canonical] href가 현재 URL과 동일한지 확인 (정규화 비교)",
        "params": {},
    },
    "mixed_content_zero": {
        "label": "Mixed Content 부재",
        "description": "HTTPS 페이지에서 http:// 리소스(img/script/link/iframe)가 0개인지 확인",
        "params": {},
    },
    "render_blocking_zero": {
        "label": "Render-Blocking Script 부재",
        "description": "head 내 defer/async 없는 외부 script가 0개인지 확인",
        "params": {},
    },
    "og_required_pairs": {
        "label": "Open Graph 필수 메타",
        "description": "og: 메타 태그 중 지정한 키들이 모두 존재하는지 확인",
        "params": {
            "required": {"label": "필수 og 키 (쉼표 구분)", "type": "text", "placeholder": "title,image"},
        },
    },
    "sitemap_recent": {
        "label": "Sitemap 최신성 (다국가 자동 감지)",
        "description": "Sitemap의 lastmod/Last-Modified 헤더가 N일 이내인지 확인. URL의 국가 디렉토리(/kr/, /us/ 등)를 자동 감지해 우선 시도, 실패 시 도메인 루트로 fallback",
        "params": {
            "path":     {"label": "Sitemap 경로 (기본 fallback)", "type": "text", "placeholder": "/sitemap.xml"},
            "max_days": {"label": "최대 일수", "type": "number", "placeholder": "30"},
            "auto_country": {"label": "국가 디렉토리 자동 감지", "type": "select", "options": ["yes", "no"]},
        },
    },

    # ── 페이지 타입별 스키마 검증 ──────────────────────────────────────────────
    "schema_for_page_type": {
        "label": "페이지 타입별 JSON-LD 스키마 검증",
        "description": "page_types.json에 정의된 expected_schemas (페이지 타입별 기대 스키마)가 모두 존재하는지 확인. 페이지 타입 감지는 자동.",
        "params": {
            "include_recommended": {"label": "권장 스키마(recommended_schemas)도 함께 평가", "type": "select", "options": ["no", "yes"]},
        },
    },

    # ── AI Readiness / 콘텐츠 ──────────────────────────────────────────────────
    "schema_required_fields": {
        "label": "JSON-LD 필수 필드 검증",
        "description": "특정 @type의 JSON-LD 노드가 필수 필드를 모두 가지는지 확인 (점 표기법 지원: offers.price)",
        "params": {
            "type":   {"label": "@type", "type": "text", "placeholder": "Product"},
            "fields": {"label": "필수 필드 (쉼표 구분, dot path)", "type": "text", "placeholder": "name,description,sku,brand,offers.price"},
        },
    },
    "definition_pattern_min": {
        "label": "정의문 패턴 카운트",
        "description": "본문에서 정의문 패턴(X는 Y이다 / dfn / abbr)이 임계 이상 발견되는지 확인",
        "params": {
            "min_count": {"label": "최소 발견 수", "type": "number", "placeholder": "1"},
        },
    },
    "citable_density_min": {
        "label": "인용 가능 문장 밀도",
        "description": "통계/숫자/연도/출처 패턴을 포함한 문장 비율이 임계 이상인지 확인",
        "params": {
            "min_ratio": {"label": "최소 비율 (0.0~1.0)", "type": "number", "placeholder": "0.1"},
        },
    },
    "image_filename_keyword": {
        "label": "이미지 파일명 키워드 포함",
        "description": "img src URL의 파일명에 지정 키워드 중 하나가 포함되는지 확인",
        "params": {
            "keywords":  {"label": "키워드 (쉼표 구분)", "type": "text", "placeholder": "lg,oled,gram"},
            "min_ratio": {"label": "최소 비율 (0.0~1.0, 선택)", "type": "number", "placeholder": "0.5"},
        },
    },
    "author_or_source": {
        "label": "저자 또는 출처+날짜",
        "description": "meta[name=author], .byline, .author 또는 datePublished+source 조합이 존재하는지 확인",
        "params": {},
    },
    "ssr_text_ratio_min": {
        "label": "SSR 텍스트 비중",
        "description": "원본 HTML 텍스트 / 렌더 후 텍스트 비율이 임계 이상 (SSR 비중 ≥ X)",
        "params": {
            "min_ratio": {"label": "최소 비율 (0.0~1.0)", "type": "number", "placeholder": "0.6"},
        },
    },
    "area_coverage": {
        "label": "영역 커버리지 (N개 이상)",
        "description": "여러 selector 영역 중 N개 이상에서 요소가 존재하면 PASS (예: PDP의 제품명/가격/갤러리/사양/CTA 중 3개 이상)",
        "params": {
            "groups":     {"label": "영역 정의 (JSON list)", "type": "text",
                           "placeholder": '[{"label":"제품명","selector":"h1"}, ...]'},
            "min_groups": {"label": "최소 매칭 그룹 수", "type": "number", "placeholder": "3"},
        },
    },
}


# ── 전역 캐시 (도메인 단위 네트워크 요청 방어용) ──────────────────────────────────
import time
import asyncio
_DOMAIN_CACHE = {}

def _get_cache(key: str):
    return _DOMAIN_CACHE.get(key)

def _set_cache(key: str, val):
    _DOMAIN_CACHE[key] = val


# ── 평가 함수 ─────────────────────────────────────────────────────────────────

def evaluate_rule(rule: dict, context: dict) -> dict:
    """규칙을 평가하여 결과를 반환합니다."""
    rule_type = rule.get("type", "")
    params = rule.get("params", {})

    handler = _HANDLERS.get(rule_type)
    if not handler:
        return {"pass": False, "value": None, "hint": f"알 수 없는 규칙 타입: {rule_type}"}

    try:
        return handler(params, context)
    except Exception as e:
        return {"pass": False, "value": None, "hint": f"규칙 평가 오류: {str(e)}"}


def _eval_css_exists(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    selector = params.get("selector", "")
    els = soup.select(selector)
    found = len(els) > 0
    return {
        "pass": found,
        "value": f"{len(els)}개 발견" if found else None,
        "hint": None if found else f"'{selector}' 요소를 찾을 수 없습니다.",
    }


def _eval_css_count(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    selector = params.get("selector", "")
    operator_str = params.get("operator", ">=")
    target = int(params.get("value", 1))

    els = soup.select(selector)
    count = len(els)

    ops = {"==": op.eq, ">=": op.ge, "<=": op.le, ">": op.gt, "<": op.lt}
    compare = ops.get(operator_str, op.ge)
    passed = compare(count, target)

    return {
        "pass": passed,
        "value": f"{count}개",
        "hint": None if passed else f"'{selector}' {count}개 — {operator_str} {target} 필요",
    }


def _eval_css_text_min_length(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    selector = params.get("selector", "")
    min_length = int(params.get("min_length", 1))

    el = soup.select_one(selector)
    if not el:
        return {"pass": False, "value": None, "hint": f"'{selector}' 요소를 찾을 수 없습니다."}

    text = el.get_text(strip=True)
    passed = len(text) >= min_length

    return {
        "pass": passed,
        "value": text[:80] if text else None,
        "hint": None if passed else f"텍스트 길이 {len(text)} — {min_length}자 이상 필요",
    }


def _eval_css_attr_exists(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    selector = params.get("selector", "")
    attr = params.get("attr", "")
    min_length = int(params.get("min_length", 0))

    el = soup.select_one(selector)
    if not el:
        return {"pass": False, "value": None, "hint": f"'{selector}' 요소를 찾을 수 없습니다."}

    val = (el.get(attr) or "").strip()
    passed = bool(val) and len(val) >= max(min_length, 1)

    return {
        "pass": passed,
        "value": val[:120] if val else None,
        "hint": None if passed else f"'{attr}' 속성이 없거나 {min_length}자 미만입니다." if min_length else f"'{attr}' 속성이 없습니다.",
    }


def _eval_css_all_have_attr(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    selector = params.get("selector", "")
    attr = params.get("attr", "")

    els = soup.select(selector)
    if not els:
        return {"pass": True, "value": "해당 요소 없음", "hint": None}

    missing = [el for el in els if el.get(attr) is None]
    passed = len(missing) == 0

    return {
        "pass": passed,
        "value": f"{len(els)}개 중 {len(els) - len(missing)}개 '{attr}' 보유",
        "hint": None if passed else f"{len(missing)}개 요소에 '{attr}' 속성이 없습니다.",
    }


def _eval_css_attr_not_contains(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    selector = params.get("selector", "")
    attr = params.get("attr", "")
    bad_value = params.get("value", "").lower()

    el = soup.select_one(selector)
    if not el:
        return {"pass": True, "value": "태그 없음 (기본 허용)", "hint": None}

    attr_val = (el.get(attr) or "").strip()
    passed = bad_value not in attr_val.lower()

    return {
        "pass": passed,
        "value": attr_val or None,
        "hint": None if passed else f"'{bad_value}'가 포함되어 있습니다.",
    }


def _eval_class_id_contains(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}

    keywords_raw = params.get("keywords", "")
    if isinstance(keywords_raw, list):
        keywords = [k.strip().lower() for k in keywords_raw if k.strip()]
    else:
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]

    tags_raw = params.get("tags", "")
    if isinstance(tags_raw, list):
        tags = [t.strip() for t in tags_raw if t.strip()]
    else:
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    if not keywords:
        return {"pass": False, "value": None, "hint": "키워드가 지정되지 않았습니다."}

    search_tags = tags if tags else True  # True = all tags

    for el in soup.find_all(search_tags):
        cls = " ".join(el.get("class", [])).lower()
        el_id = (el.get("id") or "").lower()
        combined = cls + " " + el_id
        for kw in keywords:
            if kw in combined:
                return {
                    "pass": True,
                    "value": f"<{el.name}> class/id에서 '{kw}' 발견",
                    "hint": None,
                }

    # heading 텍스트도 확인
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = heading.get_text(strip=True).lower()
        for kw in keywords:
            if kw in text:
                return {
                    "pass": True,
                    "value": f"<{heading.name}> 텍스트에서 '{kw}' 발견",
                    "hint": None,
                }

    # details 요소 3개 이상도 체크
    details = soup.find_all("details")
    if len(details) >= 3:
        return {"pass": True, "value": f"<details> {len(details)}개 발견", "hint": None}

    return {
        "pass": False,
        "value": None,
        "hint": f"키워드({', '.join(keywords[:3])}...)를 포함한 요소를 찾을 수 없습니다.",
    }


def _eval_text_has_pattern(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}

    pattern_str = params.get("pattern", r"\d")
    tags_raw = params.get("tags", "")
    if isinstance(tags_raw, list):
        tags = [t.strip() for t in tags_raw if t.strip()]
    else:
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    if not tags:
        tags = ["p", "li", "td", "h1", "h2", "h3", "h4", "h5", "h6"]

    text_parts = []
    for el in soup.find_all(tags):
        text_parts.append(el.get_text(strip=True))
    text = " ".join(text_parts)

    words = [w for w in re.split(r"\s+", text) if len(w) > 1]
    if not words:
        return {"pass": False, "value": "텍스트 없음", "hint": "분석할 텍스트가 없습니다."}

    pattern = re.compile(pattern_str)
    matched = [w for w in words if pattern.search(w)]
    passed = len(matched) > 0

    ratio = round(len(matched) / len(words) * 100, 1) if words else 0

    return {
        "pass": passed,
        "value": f"{len(matched)}개 매칭 ({ratio}%)" if passed else None,
        "hint": None if passed else f"패턴 '{pattern_str}'에 매칭되는 텍스트가 없습니다.",
    }


async def _eval_http_status(params: dict, ctx: dict) -> dict:
    base_url = ctx.get("base_url", "")
    path = params.get("path", "/")
    expected = int(params.get("status", 200))

    if not base_url:
        return {"pass": False, "value": None, "hint": "base_url이 없습니다."}

    cache_key = f"http_status_{base_url}_{path}_{expected}"
    cached = _get_cache(cache_key)
    if cached:
        return await cached

    # dedicated UA를 사용해야 Akamai 등 봇 보호에 차단되지 않는다 — lazy import로 순환 회피 (#13)
    from analyzer import build_request_headers

    async def _fetch():
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(f"{base_url}{path}", headers=build_request_headers())
            passed = r.status_code == expected
            return {
                "pass": passed,
                "value": f"HTTP {r.status_code}",
                "hint": None if passed else f"HTTP {r.status_code} — {expected} 기대",
            }
        except Exception as e:
            return {"pass": False, "value": None, "hint": f"요청 실패: {str(e)}"}

    task = asyncio.create_task(_fetch())
    _set_cache(cache_key, task)
    return await task


def _eval_schema_type_exists(params: dict, ctx: dict) -> dict:
    jsonld_types = ctx.get("jsonld_types", set())
    type_raw = params.get("type", "")
    target_types = [t.strip().lower() for t in type_raw.split(",") if t.strip()]

    if not target_types:
        return {"pass": False, "value": None, "hint": "@type이 지정되지 않았습니다."}

    found = [t for t in target_types if t in jsonld_types]
    passed = len(found) > 0

    return {
        "pass": passed,
        "value": ", ".join(found) if found else None,
        "hint": None if passed else f"@type '{type_raw}'을(를) 찾을 수 없습니다.",
    }


def _eval_heading_order(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}

    seen_h1 = False
    logical = True
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        if tag.name == "h1":
            seen_h1 = True
        elif not seen_h1:
            logical = False
            break

    return {
        "pass": logical,
        "value": "논리적 순서 정상" if logical else None,
        "hint": None if logical else "H2/H3/H4가 H1보다 먼저 나타납니다.",
    }


def _eval_redirect_max(params: dict, ctx: dict) -> dict:
    page_data = ctx.get("page_data", {})
    max_count = int(params.get("max_count", 3))
    actual = page_data.get("redirect_count", 0)
    passed = actual <= max_count

    return {
        "pass": passed,
        "value": f"{actual}회 리다이렉트",
        "hint": None if passed else f"리다이렉트 {actual}회 — {max_count}회 이하 권장",
    }


# ── 신규 핸들러: HTTP / Performance ─────────────────────────────────────────

def _get_header(ctx: dict, name: str) -> str:
    headers = ctx.get("page_data", {}).get("headers", {}) or {}
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return str(v)
    return ""


def _eval_header_value_in(params: dict, ctx: dict) -> dict:
    header = params.get("header", "")
    raw_values = params.get("values", "")
    allowed = [v.strip().lower() for v in (raw_values if isinstance(raw_values, str) else ",".join(raw_values)).split(",") if v.strip()]
    val = _get_header(ctx, header).lower()
    matched = next((v for v in allowed if v and v in val), None)
    passed = matched is not None
    return {
        "pass": passed,
        "value": val or None,
        "hint": None if passed else f"'{header}' 헤더가 {allowed} 중 하나여야 합니다.",
    }


def _eval_header_max_age_min(params: dict, ctx: dict) -> dict:
    min_seconds = int(params.get("min_seconds", 1))
    cc = _get_header(ctx, "cache-control").lower()
    if not cc or "no-store" in cc or "no-cache" in cc:
        return {"pass": False, "value": cc or None, "hint": "Cache-Control 부재 또는 no-store/no-cache."}
    m = re.search(r"max-age\s*=\s*(\d+)", cc)
    if not m:
        return {"pass": False, "value": cc, "hint": "max-age 디렉티브가 없습니다."}
    age = int(m.group(1))
    passed = age >= min_seconds
    return {
        "pass": passed,
        "value": f"max-age={age}",
        "hint": None if passed else f"max-age={age} — {min_seconds}초 이상 필요",
    }


def _eval_header_no_value(params: dict, ctx: dict) -> dict:
    header = params.get("header", "")
    token = (params.get("token") or "").strip().lower()
    val = _get_header(ctx, header).lower()
    passed = bool(token) and token not in val
    return {
        "pass": passed,
        "value": val or "헤더 없음",
        "hint": None if passed else f"'{header}'에 '{token}'이 포함되어 있습니다.",
    }


def _eval_ttfb_under_ms(params: dict, ctx: dict) -> dict:
    max_ms = int(params.get("max_ms", 600))
    ttfb = ctx.get("page_data", {}).get("ttfb_ms")
    if ttfb is None:
        return {"pass": False, "value": None, "hint": "TTFB 측정 불가"}
    passed = ttfb < max_ms
    return {
        "pass": passed,
        "value": f"{ttfb}ms",
        "hint": None if passed else f"TTFB {ttfb}ms — {max_ms}ms 미만 필요",
    }


def _eval_http_protocol_min(params: dict, ctx: dict) -> dict:
    min_v = params.get("min_version", "HTTP/2").upper()
    actual = (ctx.get("page_data", {}).get("http_version") or "").upper()
    if not actual:
        return {"pass": False, "value": None, "hint": "프로토콜 정보 없음"}

    def _ver_num(v: str) -> float:
        m = re.match(r"HTTP/(\d+)(?:\.(\d+))?", v)
        if not m:
            return 0.0
        return float(m.group(1)) + (float(m.group(2)) / 10 if m.group(2) else 0)

    passed = _ver_num(actual) >= _ver_num(min_v)
    return {
        "pass": passed,
        "value": actual,
        "hint": None if passed else f"{actual} — {min_v} 이상 필요",
    }


def _eval_html_size_under_kb(params: dict, ctx: dict) -> dict:
    max_kb = float(params.get("max_kb", 100))
    size = ctx.get("page_data", {}).get("html_bytes")
    if size is None:
        return {"pass": False, "value": None, "hint": "HTML 크기 측정 불가"}
    kb = size / 1024
    passed = kb < max_kb
    return {
        "pass": passed,
        "value": f"{kb:.1f}KB",
        "hint": None if passed else f"{kb:.1f}KB — {max_kb}KB 미만 필요",
    }


def _eval_status_code_eq(params: dict, ctx: dict) -> dict:
    code = int(params.get("code", 200))
    actual = ctx.get("page_data", {}).get("http_status")
    if actual is None:
        return {"pass": False, "value": None, "hint": "status code 없음"}
    passed = actual == code
    return {
        "pass": passed,
        "value": f"HTTP {actual}",
        "hint": None if passed else f"HTTP {actual} — {code} 기대",
    }


def _eval_soft_404_check(params: dict, ctx: dict) -> dict:
    min_len = int(params.get("min_text_length", 200))
    pd = ctx.get("page_data", {})
    status = pd.get("http_status")
    soup = ctx.get("soup")
    if status != 200:
        return {"pass": True, "value": f"HTTP {status} (Soft 404 검증 대상 아님)", "hint": None}
    if not soup:
        return {"pass": False, "value": None, "hint": "본문 파싱 실패"}
    text_len = len(soup.get_text(strip=True))
    passed = text_len >= min_len
    return {
        "pass": passed,
        "value": f"{text_len}자",
        "hint": None if passed else f"본문 {text_len}자 — Soft 404 의심 ({min_len}자 미만)",
    }


# ── 신규 핸들러: Accessibility ──────────────────────────────────────────────

def _eval_landmark_count_min(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    require_main = (params.get("require_main", "yes") or "yes").lower() == "yes"
    min_landmarks = int(params.get("min_landmarks", 3))

    landmarks = ["main", "nav", "header", "footer", "article", "section", "aside"]
    counts = {tag: len(soup.find_all(tag)) for tag in landmarks}
    total = sum(counts.values())

    main_ok = (counts["main"] >= 1) if require_main else True
    passed = main_ok and total >= min_landmarks
    return {
        "pass": passed,
        "value": f"main={counts['main']}, 합계={total}",
        "hint": None if passed else f"main {counts['main']}개, 랜드마크 합계 {total} — main 1+ AND 합계 {min_landmarks}+ 필요",
    }


def _eval_heading_no_jump(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    levels = [int(h.name[1]) for h in headings]
    if not levels:
        return {"pass": False, "value": "헤딩 없음", "hint": "헤딩 태그가 없습니다."}
    jumps = 0
    last = 0
    for lv in levels:
        if last > 0 and lv - last > 1:
            jumps += 1
        last = lv
    passed = jumps == 0
    return {
        "pass": passed,
        "value": f"{len(levels)}개 헤딩, 점프 {jumps}건",
        "hint": None if passed else f"레벨 점프 {jumps}건 발견 (예: h1→h3)",
    }


def _eval_aria_missing_ratio_max(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    max_ratio = float(params.get("max_ratio", 0.1))

    # hidden input, href 없는 anchor는 인터랙티브 요소가 아님 (#10)
    interactive = []
    for el in soup.find_all(["button", "input", "a"]):
        if el.name == "input" and (el.get("type") or "").lower() == "hidden":
            continue
        if el.name == "a" and not el.get("href"):
            continue
        interactive.append(el)
    if not interactive:
        return {"pass": True, "value": "인터랙티브 요소 없음", "hint": None}

    missing = 0
    for el in interactive:
        if el.get("aria-label") or el.get("aria-labelledby") or el.get("title"):
            continue
        if el.name == "input" and (el.get("value") or "").strip():
            continue
        if el.find("img", alt=True):
            continue
        if el.get_text(strip=True):
            continue
        missing += 1

    ratio = missing / len(interactive)
    passed = ratio < max_ratio
    return {
        "pass": passed,
        "value": f"{missing}/{len(interactive)} 누락 ({ratio*100:.1f}%)",
        "hint": None if passed else f"누락 {ratio*100:.1f}% — {max_ratio*100:.0f}% 미만 필요",
    }


# ── 신규 핸들러: SEO / 콘텐츠 ──────────────────────────────────────────────

def _normalize_url_for_canonical(u: str) -> str:
    if not u:
        return ""
    p = urlparse(u)
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    path = re.sub(r"/+", "/", p.path or "/").rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def _eval_canonical_self(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    current = ctx.get("current_url") or ctx.get("page_data", {}).get("final_url", "")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    el = soup.select_one("link[rel='canonical']")
    if not el or not el.get("href"):
        return {"pass": False, "value": None, "hint": "canonical 태그 없음"}
    href = el["href"]
    if href.startswith("/"):
        href = urljoin(current, href)
    passed = _normalize_url_for_canonical(href) == _normalize_url_for_canonical(current)
    return {
        "pass": passed,
        "value": href,
        "hint": None if passed else f"canonical='{href}' ≠ 현재 URL",
    }


def _eval_mixed_content_zero(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    current = ctx.get("current_url") or ctx.get("page_data", {}).get("final_url", "")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    if not current.startswith("https://"):
        return {"pass": True, "value": "HTTPS 페이지 아님 (검증 대상 외)", "hint": None}

    found = []
    for tag, attr in [("img", "src"), ("script", "src"), ("link", "href"),
                      ("iframe", "src"), ("audio", "src"), ("video", "src"),
                      ("source", "src")]:
        for el in soup.find_all(tag):
            v = (el.get(attr) or "").strip()
            if v.startswith("http://"):
                found.append(f"<{tag}>")
    passed = len(found) == 0
    return {
        "pass": passed,
        "value": f"{len(found)}개" if found else "0개",
        "hint": None if passed else f"http:// 리소스 {len(found)}개: {', '.join(found[:5])}",
    }


def _eval_render_blocking_zero(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    head = soup.find("head")
    if not head:
        return {"pass": True, "value": "head 없음", "hint": None}
    blocking = []
    for s in head.find_all("script"):
        if not s.get("src"):
            continue  # 인라인은 제외
        if s.has_attr("defer") or s.has_attr("async") or (s.get("type") or "").lower() == "module":
            continue
        blocking.append(s.get("src", "")[:60])
    passed = len(blocking) == 0
    return {
        "pass": passed,
        "value": f"{len(blocking)}개",
        "hint": None if passed else f"head 내 blocking script {len(blocking)}개",
    }


def _eval_og_required_pairs(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    raw = params.get("required", "")
    keys = [k.strip().lower() for k in (raw if isinstance(raw, str) else ",".join(raw)).split(",") if k.strip()]
    if not keys:
        return {"pass": False, "value": None, "hint": "필수 og 키 미지정"}

    found = {}
    for el in soup.find_all("meta"):
        prop = (el.get("property") or "").strip().lower()
        if prop.startswith("og:"):
            content = (el.get("content") or "").strip()
            if content:
                found[prop[3:]] = content[:80]

    missing = [k for k in keys if k not in found]
    passed = not missing
    return {
        "pass": passed,
        "value": f"{len(found)}개 og 메타 발견" if found else None,
        "hint": None if passed else f"누락: {', '.join(missing)}",
    }


# ── 신규 핸들러: AI Readiness ─────────────────────────────────────────────

def _walk_jsonld(data, type_lower: str, results: list):
    """JSON-LD 트리에서 @type 일치 노드 수집."""
    if isinstance(data, dict):
        t = data.get("@type")
        types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
        if any(str(x).lower() == type_lower for x in types):
            results.append(data)
        for v in data.values():
            _walk_jsonld(v, type_lower, results)
    elif isinstance(data, list):
        for item in data:
            _walk_jsonld(item, type_lower, results)


def _has_dotted_path(node: dict, path: str) -> bool:
    cur = node
    for part in path.split("."):
        if isinstance(cur, list) and cur:
            cur = cur[0]  # 배열은 첫 원소로 검사
        if not isinstance(cur, dict):
            return False
        if part not in cur:
            return False
        cur = cur[part]
    if isinstance(cur, (str, list, dict)) and not cur:
        return False
    return cur is not None and cur != ""


def _eval_schema_required_fields(params: dict, ctx: dict) -> dict:
    raw = ctx.get("jsonld_raw") or []
    type_name = (params.get("type") or "").strip()
    fields_raw = params.get("fields", "")
    fields = [f.strip() for f in (fields_raw if isinstance(fields_raw, str) else ",".join(fields_raw)).split(",") if f.strip()]
    if not type_name or not fields:
        return {"pass": False, "value": None, "hint": "type 또는 fields 미지정"}

    type_lower = type_name.lower()
    nodes: list = []
    for d in raw:
        _walk_jsonld(d, type_lower, nodes)
    if not nodes:
        return {"pass": False, "value": None, "hint": f"@type='{type_name}' 노드 없음"}

    for node in nodes:
        missing = [f for f in fields if not _has_dotted_path(node, f)]
        if not missing:
            return {"pass": True, "value": f"@type={type_name} 노드 1개 모든 필드 OK", "hint": None}
        last_missing = missing

    return {
        "pass": False,
        "value": f"@type={type_name} {len(nodes)}개 노드",
        "hint": f"필수 필드 누락: {', '.join(last_missing)}",
    }


_DEF_PATTERN = re.compile(
    r"(?:[가-힣A-Za-z0-9_]+(?:는|은|란|이란|이라는)\s+[가-힣A-Za-z0-9_,\s]+(?:이다|입니다|를 말한다|을 말한다))"
)

_NON_VISIBLE_TAGS = ("script", "style", "noscript", "svg", "path")


def _eval_schema_for_page_type(params: dict, ctx: dict) -> dict:
    """페이지 타입에 정의된 expected_schemas (+선택 recommended_schemas)가 JSON-LD에 모두 있는지."""
    page_type = ctx.get("page_type") or {}
    expected = [s for s in (page_type.get("expected_schemas") or [])]
    if params.get("include_recommended", "no").lower() in ("yes", "true", "1"):
        expected = expected + [s for s in (page_type.get("recommended_schemas") or [])]

    pt_id = page_type.get("id", "unknown")
    if not expected:
        return {
            "pass": True,
            "value": f"{pt_id}: 검증할 expected_schemas 없음 (자동 PASS)",
            "hint": None,
        }

    jsonld_types = ctx.get("jsonld_types", set())
    expected_lower = [s.lower() for s in expected]
    found   = [s for s, sl in zip(expected, expected_lower) if sl in jsonld_types]
    missing = [s for s, sl in zip(expected, expected_lower) if sl not in jsonld_types]

    if missing:
        return {
            "pass":  False,
            "value": f"page_type={pt_id} / 발견 {found or '없음'} / 누락 {missing}",
            "hint":  f"이 페이지 타입({pt_id})에서 기대되는 schema {missing}가 JSON-LD에 없음",
        }
    return {
        "pass":  True,
        "value": f"page_type={pt_id} / 모든 expected_schemas 발견: {found}",
        "hint":  None,
    }


def _visible_text(soup) -> str:
    """script/style 등 비가시 태그를 제외한 본문 텍스트 (#12)."""
    parts = []
    for s in soup.find_all(string=True):
        if s.parent and s.parent.name in _NON_VISIBLE_TAGS:
            continue
        parts.append(str(s))
    return " ".join(p.strip() for p in parts if p.strip())


def _eval_definition_pattern_min(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    min_count = int(params.get("min_count", 1))

    dfn_count = len(soup.find_all(["dfn", "abbr"]))
    text = _visible_text(soup)
    pattern_count = len(_DEF_PATTERN.findall(text))
    total = dfn_count + pattern_count
    passed = total >= min_count
    return {
        "pass": passed,
        "value": f"dfn/abbr {dfn_count}개 + 패턴 {pattern_count}건",
        "hint": None if passed else f"정의문 {total}건 — {min_count}건 이상 필요",
    }


_CITABLE_PATTERNS = [
    re.compile(r"\d+\s*%"),
    re.compile(r"\d{4}\s*년"),
    re.compile(r"\$\s*[\d,]+"),
    re.compile(r"\d+\s*배"),
    re.compile(r"에 따르면|보고서에 따르면|연구에 따르면|조사에 따르면"),
    re.compile(r"\d+(?:[.,]\d+)?\s*(?:만|억|천만|million|billion)"),
    re.compile(r"\d+(?:[.,]\d+)?\s*(?:kg|km|cm|mm|°C|℃|GB|MB|inches|inch|kWh)"),
]

def _eval_citable_density_min(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    min_ratio = float(params.get("min_ratio", 0.1))

    text = _visible_text(soup)
    sentences = [s for s in re.split(r"(?<=[.!?。\n])\s+", text) if len(s.strip()) > 5]
    if not sentences:
        return {"pass": False, "value": "문장 없음", "hint": "분석할 문장이 없습니다."}
    citable = sum(1 for s in sentences if any(p.search(s) for p in _CITABLE_PATTERNS))
    ratio = citable / len(sentences)
    passed = ratio >= min_ratio
    return {
        "pass": passed,
        "value": f"{citable}/{len(sentences)} ({ratio*100:.1f}%)",
        "hint": None if passed else f"인용 가능 밀도 {ratio*100:.1f}% — {min_ratio*100:.0f}% 이상 필요",
    }


def _eval_image_filename_keyword(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    raw = params.get("keywords", "")
    keywords = [k.strip().lower() for k in (raw if isinstance(raw, str) else ",".join(raw)).split(",") if k.strip()]
    if not keywords:
        return {"pass": False, "value": None, "hint": "키워드 미지정"}
    min_ratio = float(params.get("min_ratio", 0.5)) if params.get("min_ratio") not in (None, "", 0) else 0.0

    imgs = soup.find_all("img")
    if not imgs:
        return {"pass": False, "value": "이미지 없음", "hint": "img 태그가 없습니다."}

    matched = 0
    for img in imgs:
        src = (img.get("src") or img.get("data-src") or "").lower()
        fname = src.split("?")[0].split("/")[-1]
        if any(kw in fname for kw in keywords):
            matched += 1
    ratio = matched / len(imgs)
    if min_ratio > 0:
        passed = ratio >= min_ratio
    else:
        passed = matched >= 1
    return {
        "pass": passed,
        "value": f"{matched}/{len(imgs)} ({ratio*100:.1f}%)",
        "hint": None if passed else f"브랜드 키워드 포함 이미지 {matched}/{len(imgs)} ({ratio*100:.1f}%)",
    }


def _eval_author_or_source(params: dict, ctx: dict) -> dict:
    soup = ctx.get("soup")
    if not soup:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}

    # 1) meta[name=author]
    if soup.select_one("meta[name='author' i]") and (soup.select_one("meta[name='author' i]").get("content") or "").strip():
        return {"pass": True, "value": "meta[name=author] 발견", "hint": None}
    # 2) byline/author class
    for sel in [".byline", ".author", "[rel='author']", "[itemprop='author']"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return {"pass": True, "value": f"{sel} 발견", "hint": None}
    # 3) datePublished + source 조합
    has_date = bool(
        soup.select_one("[itemprop='datePublished']")
        or soup.select_one("time[datetime]")
        or soup.select_one("meta[property='article:published_time']")
    )
    has_source = bool(soup.select_one(".source, [data-source], cite"))
    if has_date and has_source:
        return {"pass": True, "value": "출처+날짜 조합 발견", "hint": None}

    return {
        "pass": False,
        "value": None,
        "hint": "저자(meta/byline) 또는 (출처+datePublished) 조합 없음",
    }


def _eval_ssr_text_ratio_min(params: dict, ctx: dict) -> dict:
    min_ratio = float(params.get("min_ratio", 0.6))
    csr = ctx.get("csr_ratio_dict", {}) or {}
    ratio = csr.get("ratio")
    status = csr.get("status", "unavailable")
    if ratio is None:
        return {
            "pass": False,
            "value": f"status={status}",
            "hint": "CSR 비율 측정 불가 (Playwright 미설치 또는 차단)",
        }
    passed = ratio >= min_ratio
    return {
        "pass": passed,
        "value": f"SSR {ratio*100:.0f}%",
        "hint": None if passed else f"SSR 비율 {ratio*100:.0f}% — {min_ratio*100:.0f}% 이상 필요",
    }


# ── 신규 ASYNC 핸들러: Sitemap ─────────────────────────────────────────────

# 일반적으로 사용되는 국가/언어 디렉토리 패턴 (LG.com 기준 + 국제 표준)
_COUNTRY_DIR_PATTERN = re.compile(
    r"^/((?:[a-z]{2}(?:[-_][a-z]{2,4})?))(?:/|$)",
    re.IGNORECASE,
)


def _detect_country_dir(url: str) -> Optional[str]:
    """URL의 첫 path segment가 국가/언어 코드면 반환 (예: '/kr/foo' → 'kr')."""
    try:
        path = urlparse(url).path or "/"
    except Exception:
        return None
    m = _COUNTRY_DIR_PATTERN.match(path)
    if not m:
        return None
    code = m.group(1).lower()
    # 흔한 false-positive 제외 (예: /js/, /css/는 2글자지만 국가 아님)
    if code in {"js", "css", "img", "api", "v1", "v2", "ws"}:
        return None
    return code


def _parse_sitemap_lastmod(text: str, last_mod_header: Optional[str]) -> Optional[datetime]:
    """Last-Modified 헤더 또는 XML 본문에서 가장 최근 lastmod 추출."""
    # 1) Last-Modified 헤더
    if last_mod_header:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(last_mod_header)
            if dt:
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    # 2) XML 본문의 <lastmod>
    last_mods = re.findall(r"<lastmod[^>]*>([^<]+)</lastmod>", text or "")
    latest = None
    for lm in last_mods:
        try:
            dt = datetime.fromisoformat(lm.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue
    return latest


async def _eval_sitemap_recent(params: dict, ctx: dict) -> dict:
    base_url = ctx.get("base_url", "")
    if not base_url:
        return {"pass": False, "value": None, "hint": "base_url 없음"}
    fallback_path = params.get("path", "/sitemap.xml")
    max_days = int(params.get("max_days", 30))
    auto_country = (params.get("auto_country", "yes") or "yes").lower() == "yes"

    cache_key = f"sitemap_recent_{base_url}_{fallback_path}_{max_days}_{auto_country}"
    cached = _get_cache(cache_key)
    if cached:
        return await cached

    threshold = datetime.now(timezone.utc) - timedelta(days=max_days)

    # 시도할 경로 목록 — 국가 디렉토리 우선 → 도메인 루트 fallback
    paths_to_try: List[str] = []
    country = _detect_country_dir(ctx.get("current_url", "")) if auto_country else None
    if country:
        paths_to_try.append(f"/{country}{fallback_path}")
        paths_to_try.append(f"/{country}/sitemap_index.xml")
    if fallback_path not in paths_to_try:
        paths_to_try.append(fallback_path)
    if "/sitemap_index.xml" not in paths_to_try:
        paths_to_try.append("/sitemap_index.xml")

    # dedicated UA — Akamai 등 봇 보호 우회 (#14)
    from analyzer import build_request_headers

    async def _fetch():
        tried_results = []
        headers = build_request_headers()
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                for path in paths_to_try:
                    try:
                        r = await client.get(f"{base_url}{path}", headers=headers)
                    except Exception as e:
                        tried_results.append(f"{path}: 오류({type(e).__name__})")
                        continue
                    if r.status_code != 200:
                        tried_results.append(f"{path}: HTTP {r.status_code}")
                        continue
                    # 200 발견 — lastmod 파싱
                    latest = _parse_sitemap_lastmod(r.text, r.headers.get("Last-Modified"))
                    if latest is None:
                        tried_results.append(f"{path}: 날짜 정보 없음")
                        continue
                    passed = latest >= threshold
                    origin = "국가 디렉토리" if country and path.startswith(f"/{country}") else "도메인 루트"
                    return {
                        "pass": passed,
                        "value": f"{origin}({path}) lastmod={latest.date().isoformat()}",
                        "hint": None if passed else f"마지막 갱신 {latest.date().isoformat()} — {max_days}일 이내 필요",
                    }
            return {
                "pass": False,
                "value": f"{len(paths_to_try)}개 경로 시도",
                "hint": "; ".join(tried_results[:3]) if tried_results else "sitemap 부재",
            }
        except Exception as e:
            return {"pass": False, "value": None, "hint": f"요청 실패: {str(e)}"}

    task = asyncio.create_task(_fetch())
    _set_cache(cache_key, task)
    return await task


# ── 핸들러 레지스트리 ─────────────────────────────────────────────────────────

def _eval_area_coverage(params: dict, ctx: dict) -> dict:
    """여러 selector 영역 중 N개 이상에서 요소가 존재하면 PASS.

    params:
      groups: list of {"label": str, "selector": str} — 각 영역
      min_groups: int — 최소 매칭 그룹 수
    """
    soup = ctx.get("soup")
    if soup is None:
        return {"pass": False, "value": None, "hint": "HTML 파싱 실패"}
    groups = params.get("groups") or []
    min_groups = int(params.get("min_groups", 1))

    matched_labels, missing_labels = [], []
    for g in groups:
        label = (g.get("label") or "?").strip()
        sel = (g.get("selector") or "").strip()
        if not sel:
            continue
        try:
            count = len(soup.select(sel))
        except Exception:
            count = 0
        if count > 0:
            matched_labels.append(f"{label}({count})")
        else:
            missing_labels.append(label)

    matched_n = len(matched_labels)
    total_n = len(matched_labels) + len(missing_labels)
    passed = matched_n >= min_groups
    return {
        "pass": passed,
        "value": f"{matched_n}/{total_n} 영역",
        "hint": None if passed else
                f"매칭: [{', '.join(matched_labels) or '없음'}] · 누락: [{', '.join(missing_labels)}] · 최소 {min_groups}개 필요",
    }


_HANDLERS = {
    # 기존
    "css_exists":             _eval_css_exists,
    "css_count":              _eval_css_count,
    "css_text_min_length":    _eval_css_text_min_length,
    "css_attr_exists":        _eval_css_attr_exists,
    "css_all_have_attr":      _eval_css_all_have_attr,
    "css_attr_not_contains":  _eval_css_attr_not_contains,
    "class_id_contains":      _eval_class_id_contains,
    "text_has_pattern":       _eval_text_has_pattern,
    "schema_type_exists":     _eval_schema_type_exists,
    "heading_order":          _eval_heading_order,
    "redirect_max":           _eval_redirect_max,
    # 신규: Performance / HTTP
    "header_value_in":        _eval_header_value_in,
    "header_max_age_min":     _eval_header_max_age_min,
    "header_no_value":        _eval_header_no_value,
    "ttfb_under_ms":          _eval_ttfb_under_ms,
    "http_protocol_min":      _eval_http_protocol_min,
    "html_size_under_kb":     _eval_html_size_under_kb,
    "status_code_eq":         _eval_status_code_eq,
    "soft_404_check":         _eval_soft_404_check,
    # 신규: Accessibility
    "landmark_count_min":     _eval_landmark_count_min,
    "heading_no_jump":        _eval_heading_no_jump,
    "aria_missing_ratio_max": _eval_aria_missing_ratio_max,
    # 신규: SEO / 콘텐츠
    "canonical_self":         _eval_canonical_self,
    "mixed_content_zero":     _eval_mixed_content_zero,
    "render_blocking_zero":   _eval_render_blocking_zero,
    "og_required_pairs":      _eval_og_required_pairs,
    # 신규: AI Readiness
    "schema_required_fields": _eval_schema_required_fields,
    "schema_for_page_type":   _eval_schema_for_page_type,
    "definition_pattern_min": _eval_definition_pattern_min,
    "citable_density_min":    _eval_citable_density_min,
    "image_filename_keyword": _eval_image_filename_keyword,
    "author_or_source":       _eval_author_or_source,
    "ssr_text_ratio_min":     _eval_ssr_text_ratio_min,
    "area_coverage":          _eval_area_coverage,
}

# async 핸들러
_ASYNC_HANDLERS = {
    "http_status":     _eval_http_status,
    "sitemap_recent":  _eval_sitemap_recent,
}


async def evaluate_rule_async(rule: dict, context: dict) -> dict:
    """동기 + 비동기 규칙을 모두 평가합니다."""
    rule_type = rule.get("type", "")
    params = rule.get("params", {})

    # async 핸들러 먼저 확인
    async_handler = _ASYNC_HANDLERS.get(rule_type)
    if async_handler:
        try:
            return await async_handler(params, context)
        except Exception as e:
            return {"pass": False, "value": None, "hint": f"규칙 평가 오류: {str(e)}"}

    # sync 핸들러
    return evaluate_rule(rule, context)
