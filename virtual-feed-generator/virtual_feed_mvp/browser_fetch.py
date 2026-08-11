from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote_plus, unquote


def find_browser() -> str | None:
    """Return a supported Chromium browser executable without requiring setup."""
    override = os.getenv("VF_BROWSER_PATH", "").strip()
    candidates = [override] if override else []
    if os.name == "nt":
        roots = [os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", ""), os.getenv("LOCALAPPDATA", "")]
        for root in roots:
            if root:
                candidates.extend([
                    str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
                    str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                ])
    candidates.extend(filter(None, [shutil.which("msedge"), shutil.which("google-chrome"), shutil.which("chromium")]))
    return next((item for item in candidates if item and Path(item).is_file()), None)


def dump_dom(url: str, timeout: int = 40) -> str:
    """Load a URL with the installed browser and return the rendered DOM."""
    browser = find_browser()
    if not browser:
        raise RuntimeError("Microsoft Edge 또는 Chrome을 찾지 못했습니다. PDP URL을 직접 입력해 주세요.")
    with tempfile.TemporaryDirectory(prefix="vf-browser-") as profile:
        command = [
            browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking", f"--user-data-dir={profile}",
            "--dump-dom", url,
        ]
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    output = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0 or len(output.strip()) < 100:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise RuntimeError(f"브라우저 수집 실패{': ' + detail if detail else ''}")
    return output


def search_lg_pdp(country_site_code: str, model: str, timeout: int = 40) -> list[str]:
    """Find official LG PDP candidates via browser search when sitemap access is blocked."""
    model_key = re.sub(r"[^A-Z0-9-]", "", model.upper().split(".", 1)[0])
    query = quote_plus(f"site:lg.com/{country_site_code} {model_key}")
    dom = dump_dom(f"https://www.bing.com/search?q={query}", timeout=timeout)
    dom = html.unescape(dom).replace("\\/", "/")
    pattern = rf"https://(?:www\.)?lg\.com/{re.escape(country_site_code)}/[^\s\"'<>]+"
    found: list[str] = []
    for raw in re.findall(pattern, dom, re.IGNORECASE):
        url = unquote(raw).split("&", 1)[0].rstrip(".,);]")
        lower = url.lower()
        if model_key.lower() not in lower or "/support/" in lower:
            continue
        if url not in found:
            found.append(url)
    return found
