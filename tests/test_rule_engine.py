"""
rule_engine 단위 테스트.

서버를 띄우지 않고 픽스처 HTML/JSON-LD/headers를 직접 컨텍스트에 주입해
각 룰 핸들러의 PASS/FAIL을 검증한다.

실행:
    python3 -m unittest tests.test_rule_engine -v
"""
import os
import sys
import asyncio
import unittest
from bs4 import BeautifulSoup

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rule_engine import evaluate_rule, evaluate_rule_async, _HANDLERS, _ASYNC_HANDLERS, RULE_TYPES  # noqa: E402


def _ctx(html="<html><head></head><body></body></html>", **page_data_overrides):
    """기본 컨텍스트 빌더."""
    soup = BeautifulSoup(html, "html.parser")
    page_data = {
        "status":         "ok",
        "soup":           soup,
        "http_status":    200,
        "final_url":      "https://example.com/",
        "redirect_count": 0,
        "headers":        {},
        "http_version":   "HTTP/2",
        "html_bytes":     len(html.encode("utf-8")),
        "ttfb_ms":        300,
        "raw_html":       html,
    }
    page_data.update(page_data_overrides)
    return {
        "soup":            soup,
        "page_data":       page_data,
        "jsonld_types":    set(),
        "jsonld_raw":      [],
        "base_url":        "https://example.com",
        "current_url":     page_data["final_url"],
        "csr_ratio_dict":  {"status": "unavailable", "ratio": None},
    }


def _eval(rule_type, params, ctx):
    return evaluate_rule({"type": rule_type, "params": params}, ctx)


# ── Performance / HTTP ────────────────────────────────────────────────────────

class TestPerformanceRules(unittest.TestCase):
    def test_compression_pass(self):
        ctx = _ctx(headers={"content-encoding": "gzip"})
        r = _eval("header_value_in", {"header": "content-encoding", "values": "gzip,br,deflate"}, ctx)
        self.assertTrue(r["pass"])

    def test_compression_fail(self):
        ctx = _ctx(headers={"content-encoding": "identity"})
        r = _eval("header_value_in", {"header": "content-encoding", "values": "gzip,br,deflate"}, ctx)
        self.assertFalse(r["pass"])

    def test_compression_missing_header(self):
        ctx = _ctx(headers={})
        r = _eval("header_value_in", {"header": "content-encoding", "values": "gzip,br,deflate"}, ctx)
        self.assertFalse(r["pass"])

    def test_cache_max_age_pass(self):
        ctx = _ctx(headers={"cache-control": "public, max-age=3600"})
        r = _eval("header_max_age_min", {"min_seconds": 1}, ctx)
        self.assertTrue(r["pass"])
        self.assertIn("3600", r["value"])

    def test_cache_max_age_no_store_fail(self):
        ctx = _ctx(headers={"cache-control": "no-store"})
        r = _eval("header_max_age_min", {"min_seconds": 1}, ctx)
        self.assertFalse(r["pass"])

    def test_x_robots_no_noindex_pass(self):
        ctx = _ctx(headers={"x-robots-tag": "all"})
        r = _eval("header_no_value", {"header": "x-robots-tag", "token": "noindex"}, ctx)
        self.assertTrue(r["pass"])

    def test_x_robots_noindex_fail(self):
        ctx = _ctx(headers={"x-robots-tag": "noindex, nofollow"})
        r = _eval("header_no_value", {"header": "x-robots-tag", "token": "noindex"}, ctx)
        self.assertFalse(r["pass"])

    def test_ttfb_pass(self):
        ctx = _ctx(ttfb_ms=300)
        r = _eval("ttfb_under_ms", {"max_ms": 600}, ctx)
        self.assertTrue(r["pass"])

    def test_ttfb_fail(self):
        ctx = _ctx(ttfb_ms=900)
        r = _eval("ttfb_under_ms", {"max_ms": 600}, ctx)
        self.assertFalse(r["pass"])

    def test_http_protocol_h2_pass(self):
        ctx = _ctx(http_version="HTTP/2")
        r = _eval("http_protocol_min", {"min_version": "HTTP/2"}, ctx)
        self.assertTrue(r["pass"])

    def test_http_protocol_h11_fail(self):
        ctx = _ctx(http_version="HTTP/1.1")
        r = _eval("http_protocol_min", {"min_version": "HTTP/2"}, ctx)
        self.assertFalse(r["pass"])

    def test_html_size_under_pass(self):
        ctx = _ctx(html_bytes=50000)
        r = _eval("html_size_under_kb", {"max_kb": 100}, ctx)
        self.assertTrue(r["pass"])

    def test_html_size_over_fail(self):
        ctx = _ctx(html_bytes=200000)
        r = _eval("html_size_under_kb", {"max_kb": 100}, ctx)
        self.assertFalse(r["pass"])

    def test_status_code_pass(self):
        ctx = _ctx(http_status=200)
        r = _eval("status_code_eq", {"code": 200}, ctx)
        self.assertTrue(r["pass"])

    def test_status_code_fail(self):
        ctx = _ctx(http_status=404)
        r = _eval("status_code_eq", {"code": 200}, ctx)
        self.assertFalse(r["pass"])

    def test_soft_404_pass_normal_page(self):
        long_text = "본문 " * 200
        html = f"<html><body>{long_text}</body></html>"
        ctx = _ctx(html=html, http_status=200)
        r = _eval("soft_404_check", {"min_text_length": 200}, ctx)
        self.assertTrue(r["pass"])

    def test_soft_404_fail_thin_page(self):
        html = "<html><body>oops</body></html>"
        ctx = _ctx(html=html, http_status=200)
        r = _eval("soft_404_check", {"min_text_length": 200}, ctx)
        self.assertFalse(r["pass"])

    def test_soft_404_skip_when_not_200(self):
        ctx = _ctx(http_status=404)
        r = _eval("soft_404_check", {"min_text_length": 200}, ctx)
        # 404는 검증 대상이 아니므로 PASS 처리
        self.assertTrue(r["pass"])


# ── Accessibility ─────────────────────────────────────────────────────────────

class TestAccessibilityRules(unittest.TestCase):
    def test_landmark_count_pass(self):
        html = """
        <html><body>
          <header>head</header>
          <nav>nav</nav>
          <main>main</main>
          <footer>foot</footer>
        </body></html>"""
        r = _eval("landmark_count_min", {"min_landmarks": 3, "require_main": "yes"}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_landmark_count_fail_no_main(self):
        html = "<html><body><nav></nav><header></header><footer></footer></body></html>"
        r = _eval("landmark_count_min", {"min_landmarks": 3, "require_main": "yes"}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_landmark_count_fail_too_few(self):
        html = "<html><body><main></main></body></html>"
        r = _eval("landmark_count_min", {"min_landmarks": 3, "require_main": "yes"}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_heading_no_jump_pass(self):
        html = "<h1>1</h1><h2>2</h2><h3>3</h3><h2>2</h2>"
        r = _eval("heading_no_jump", {}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_heading_no_jump_fail(self):
        html = "<h1>1</h1><h3>3</h3>"  # h2 건너뜀
        r = _eval("heading_no_jump", {}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_aria_missing_pass(self):
        html = """
        <button aria-label="close">X</button>
        <a href="/" title="home">Home</a>
        <input type="submit" value="Send">
        """
        r = _eval("aria_missing_ratio_max", {"max_ratio": 0.1}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_aria_missing_fail(self):
        html = "<button></button>" * 5 + "<a href='/'></a>" * 5
        r = _eval("aria_missing_ratio_max", {"max_ratio": 0.1}, _ctx(html))
        self.assertFalse(r["pass"])


# ── SEO / 콘텐츠 ──────────────────────────────────────────────────────────────

class TestSEORules(unittest.TestCase):
    def test_canonical_self_pass(self):
        html = '<link rel="canonical" href="https://example.com/page">'
        ctx = _ctx(html, final_url="https://example.com/page")
        r = _eval("canonical_self", {}, ctx)
        self.assertTrue(r["pass"])

    def test_canonical_self_pass_relative(self):
        html = '<link rel="canonical" href="/page">'
        ctx = _ctx(html, final_url="https://example.com/page")
        r = _eval("canonical_self", {}, ctx)
        self.assertTrue(r["pass"])

    def test_canonical_self_fail_different(self):
        html = '<link rel="canonical" href="https://other.com/page">'
        ctx = _ctx(html, final_url="https://example.com/page")
        r = _eval("canonical_self", {}, ctx)
        self.assertFalse(r["pass"])

    def test_canonical_missing(self):
        ctx = _ctx("<html></html>")
        r = _eval("canonical_self", {}, ctx)
        self.assertFalse(r["pass"])

    def test_mixed_content_zero_pass(self):
        html = '<img src="https://example.com/a.jpg"><script src="/x.js"></script>'
        ctx = _ctx(html, final_url="https://example.com/")
        r = _eval("mixed_content_zero", {}, ctx)
        self.assertTrue(r["pass"])

    def test_mixed_content_zero_fail(self):
        html = '<img src="http://example.com/a.jpg">'
        ctx = _ctx(html, final_url="https://example.com/")
        r = _eval("mixed_content_zero", {}, ctx)
        self.assertFalse(r["pass"])

    def test_render_blocking_zero_pass(self):
        html = '<head><script src="/x.js" defer></script><script src="/y.js" async></script></head>'
        r = _eval("render_blocking_zero", {}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_render_blocking_zero_fail(self):
        html = '<head><script src="/blocking.js"></script></head>'
        r = _eval("render_blocking_zero", {}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_og_required_pass(self):
        html = """
        <meta property="og:title" content="Title">
        <meta property="og:image" content="https://example.com/img.jpg">
        """
        r = _eval("og_required_pairs", {"required": "title,image"}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_og_required_fail_missing_image(self):
        html = '<meta property="og:title" content="Title">'
        r = _eval("og_required_pairs", {"required": "title,image"}, _ctx(html))
        self.assertFalse(r["pass"])
        self.assertIn("image", r["hint"])


# ── AI Readiness — JSON-LD ────────────────────────────────────────────────────

class TestSchemaRules(unittest.TestCase):
    def _ctx_with_jsonld(self, jsonld_data):
        ctx = _ctx()
        ctx["jsonld_raw"] = jsonld_data if isinstance(jsonld_data, list) else [jsonld_data]
        return ctx

    def test_organization_required_pass(self):
        data = {
            "@type": "Organization",
            "contactPoint": {"@type": "ContactPoint", "telephone": "+1"},
            "address": {"streetAddress": "123 Main"},
            "geo": {"latitude": 0, "longitude": 0},
            "hasMap": "https://maps.google.com/?q=...",
        }
        r = _eval("schema_required_fields",
                  {"type": "Organization", "fields": "contactPoint,address,geo,hasMap"},
                  self._ctx_with_jsonld(data))
        self.assertTrue(r["pass"])

    def test_organization_required_fail_missing(self):
        data = {"@type": "Organization", "contactPoint": {}, "address": {}}
        r = _eval("schema_required_fields",
                  {"type": "Organization", "fields": "contactPoint,address,geo,hasMap"},
                  self._ctx_with_jsonld(data))
        self.assertFalse(r["pass"])
        self.assertIn("geo", r["hint"])

    def test_product_dotted_path(self):
        data = {
            "@type": "Product",
            "name": "Test", "description": "D", "sku": "X1", "brand": {"@type": "Brand", "name": "LG"},
            "offers": {"@type": "Offer", "price": "999", "availability": "InStock"},
            "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5"},
            "review": [{"@type": "Review", "reviewRating": {"ratingValue": "5"}}],
        }
        r = _eval("schema_required_fields",
                  {"type": "Product", "fields": "name,description,sku,brand,offers.price,offers.availability,aggregateRating.ratingValue,review"},
                  self._ctx_with_jsonld(data))
        self.assertTrue(r["pass"])

    def test_product_dotted_path_missing_nested(self):
        data = {
            "@type": "Product",
            "name": "Test", "description": "D", "sku": "X1", "brand": {"@type": "Brand", "name": "LG"},
            "offers": {"@type": "Offer", "price": "999"},  # availability 누락
        }
        r = _eval("schema_required_fields",
                  {"type": "Product", "fields": "name,description,sku,brand,offers.price,offers.availability"},
                  self._ctx_with_jsonld(data))
        self.assertFalse(r["pass"])
        self.assertIn("offers.availability", r["hint"])

    def test_faqpage_pass(self):
        data = {
            "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": "Q1", "acceptedAnswer": {"text": "A"}}],
        }
        r = _eval("schema_required_fields",
                  {"type": "FAQPage", "fields": "mainEntity"},
                  self._ctx_with_jsonld(data))
        self.assertTrue(r["pass"])

    def test_schema_no_match_type(self):
        data = {"@type": "Article", "headline": "..."}
        r = _eval("schema_required_fields",
                  {"type": "Product", "fields": "name"},
                  self._ctx_with_jsonld(data))
        self.assertFalse(r["pass"])
        self.assertIn("Product", r["hint"])


# ── AI Readiness — Content ────────────────────────────────────────────────────

class TestContentRules(unittest.TestCase):
    def test_definition_pattern_pass(self):
        html = "<p>GEO는 검색 엔진 최적화를 말한다.</p><dfn>RAG</dfn>"
        r = _eval("definition_pattern_min", {"min_count": 1}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_definition_pattern_fail(self):
        html = "<p>그냥 일반 문장입니다.</p>"
        r = _eval("definition_pattern_min", {"min_count": 1}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_citable_density_pass(self):
        # 3문장 중 2문장에 패턴 → 66%
        html = "<p>매출이 30% 증가했다. 2024년 보고서에 따르면 좋다. 그냥 일반 문장.</p>"
        r = _eval("citable_density_min", {"min_ratio": 0.3}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_citable_density_fail(self):
        html = "<p>좋아요. 멋져요. 훌륭해요. 환상적입니다. 완벽합니다.</p>"
        r = _eval("citable_density_min", {"min_ratio": 0.1}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_image_filename_keyword_pass(self):
        html = '<img src="/products/lg-oled-c4.jpg"><img src="/products/lg-gram-pro.png">'
        r = _eval("image_filename_keyword", {"keywords": "lg,oled,gram", "min_ratio": 0.5}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_image_filename_keyword_fail(self):
        html = '<img src="/img/IMG_1234.jpg"><img src="/img/photo_5678.png">'
        r = _eval("image_filename_keyword", {"keywords": "lg,oled,gram", "min_ratio": 0.5}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_author_meta_pass(self):
        html = '<meta name="author" content="홍길동">'
        r = _eval("author_or_source", {}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_author_byline_pass(self):
        html = '<div class="byline">By 김기자</div>'
        r = _eval("author_or_source", {}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_author_date_plus_source_pass(self):
        html = '<time datetime="2026-01-01">2026-01-01</time><cite>LG Research</cite>'
        r = _eval("author_or_source", {}, _ctx(html))
        self.assertTrue(r["pass"])

    def test_author_fail(self):
        html = "<p>그냥 글</p>"
        r = _eval("author_or_source", {}, _ctx(html))
        self.assertFalse(r["pass"])

    def test_ssr_text_ratio_pass(self):
        ctx = _ctx()
        ctx["csr_ratio_dict"] = {"status": "ok", "ratio": 0.8}
        r = _eval("ssr_text_ratio_min", {"min_ratio": 0.6}, ctx)
        self.assertTrue(r["pass"])

    def test_ssr_text_ratio_fail(self):
        ctx = _ctx()
        ctx["csr_ratio_dict"] = {"status": "ok", "ratio": 0.3}
        r = _eval("ssr_text_ratio_min", {"min_ratio": 0.6}, ctx)
        self.assertFalse(r["pass"])

    def test_ssr_text_ratio_unavailable(self):
        ctx = _ctx()
        ctx["csr_ratio_dict"] = {"status": "unavailable", "ratio": None}
        r = _eval("ssr_text_ratio_min", {"min_ratio": 0.6}, ctx)
        self.assertFalse(r["pass"])


# ── 핸들러 / RULE_TYPES 일관성 ────────────────────────────────────────────────

class TestRegistryConsistency(unittest.TestCase):
    def test_all_rule_types_have_handlers(self):
        all_handlers = set(_HANDLERS.keys()) | set(_ASYNC_HANDLERS.keys())
        for rt in RULE_TYPES:
            self.assertIn(rt, all_handlers, f"룰 타입 '{rt}'에 핸들러 미등록")

    def test_all_handlers_in_rule_types(self):
        all_handlers = set(_HANDLERS.keys()) | set(_ASYNC_HANDLERS.keys())
        for h in all_handlers:
            self.assertIn(h, RULE_TYPES, f"핸들러 '{h}'가 RULE_TYPES에 미정의")

    def test_scoring_config_rules_resolvable(self):
        import json as _json
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "scoring_config.json"), "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        all_handlers = set(_HANDLERS.keys()) | set(_ASYNC_HANDLERS.keys())
        for cat_key, cat in cfg.items():
            if not isinstance(cat, dict) or "criteria" not in cat:
                continue
            for cr in cat["criteria"]:
                rt = cr.get("rule", {}).get("type", "")
                if rt:
                    self.assertIn(rt, all_handlers,
                                  f"{cat_key}.{cr['id']}: 룰 타입 '{rt}' 핸들러 없음")


if __name__ == "__main__":
    unittest.main(verbosity=2)
