import unittest
import urllib.error
from unittest.mock import patch

from virtual_feed_mvp.browser_fetch import search_lg_pdp
from virtual_feed_mvp.core import LGSitemapResolver, PDPExtractor


class BrowserFallbackTests(unittest.TestCase):
    def setUp(self):
        LGSitemapResolver._urls.clear()

    @patch("virtual_feed_mvp.browser_fetch.dump_dom")
    def test_search_returns_only_matching_official_pdp(self, dump_dom):
        dump_dom.return_value = """
        <a href="https://www.lg.com/uk/fridge-freezers/american-style-fridge-freezers/gsxv91mcae/">PDP</a>
        <a href="https://www.lg.com/uk/support/product-support/cs-GSXV91MCAE/">Support</a>
        <a href="https://example.com/GSXV91MCAE">Other</a>
        """
        self.assertEqual(
            ["https://www.lg.com/uk/fridge-freezers/american-style-fridge-freezers/gsxv91mcae/"],
            search_lg_pdp("uk", "GSXV91MCAE"),
        )

    @patch("virtual_feed_mvp.core.search_lg_pdp")
    @patch("virtual_feed_mvp.core.urllib.request.urlopen")
    def test_resolver_uses_browser_search_when_sitemap_is_forbidden(self, urlopen, search):
        urlopen.side_effect = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        expected = "https://www.lg.com/uk/fridge-freezers/american-style-fridge-freezers/gsxv91mcae/"
        search.return_value = [expected]
        self.assertEqual(expected, LGSitemapResolver.resolve("UK", "GSXV91MCAE", "REF"))
        search.assert_called_once_with("uk", "GSXV91MCAE")

    @patch("virtual_feed_mvp.core.dump_dom")
    @patch("virtual_feed_mvp.core.urllib.request.urlopen")
    def test_pdp_fetch_uses_browser_when_request_is_forbidden(self, urlopen, dump_dom):
        urlopen.side_effect = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        dump_dom.return_value = "<html>" + ("x" * 200) + "</html>"
        result = PDPExtractor().fetch("https://www.lg.com/uk/example")
        self.assertIn("<html>", result)


if __name__ == "__main__":
    unittest.main()
