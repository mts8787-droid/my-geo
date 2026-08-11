from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from virtual_feed_mvp.core import ProductInput, load_products_from_file, parse_tabular_text, process_product, result_to_dict, taxonomy_config_path, enabled_country_sites
    from virtual_feed_mvp.exporter import build_workbook
    from virtual_feed_mvp.browser_fetch import find_browser
else:
    from .core import ProductInput, load_products_from_file, parse_tabular_text, process_product, result_to_dict, taxonomy_config_path, enabled_country_sites
    from .exporter import build_workbook
    from .browser_fetch import find_browser


APP_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = APP_DIR / "outputs"
APP_VERSION = "0.7.17"


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feed-job")

    def create(self, products: list[ProductInput], settings: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.jobs[job_id] = {
                "job_id": job_id, "status": "queued", "total": len(products), "completed": 0,
                "feeds": 0, "evidence": 0, "errors": 0, "settings": settings,
                "preview": [], "download_url": "", "message": "대기 중",
            }
        self.pool.submit(self._run, job_id, products, settings)
        return job_id

    def _run(self, job_id: str, products: list[ProductInput], settings: dict[str, Any]) -> None:
        with self.lock:
            self.jobs[job_id].update(status="running", message="PDP 및 원천 데이터를 처리하고 있습니다.")
        results = []
        concurrency = max(1, min(int(settings.get("concurrency", 4)), 10))
        try:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="product") as pool:
                futures = {
                    pool.submit(
                        process_product, product,
                        max_feeds=int(settings.get("max_feeds", 4)),
                        title_limit=int(settings.get("title_limit", 30)),
                        body_limit=int(settings.get("body_limit", 60)),
                        generator_mode=str(settings.get("generator_mode", "auto")),
                    ): product
                    for product in products
                }
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    with self.lock:
                        job = self.jobs[job_id]
                        job["completed"] += 1
                        job["feeds"] += len(result.feeds)
                        job["evidence"] += len(result.evidence)
                        job["errors"] += sum(1 for issue in result.issues if issue.severity == "error")
                        if len(job["preview"]) < 80:
                            job["preview"].extend(asdict(feed) for feed in result.feeds[:4])
                        job["message"] = f"{job['completed']} / {job['total']} 제품 처리 완료"

            results.sort(key=lambda r: r.product.request_id)
            output_path = OUTPUT_DIR / f"virtual_feeds_{job_id}.xlsx"
            build_workbook(results, output_path, settings)
            with self.lock:
                self.jobs[job_id].update(
                    status="complete", download_url=f"/api/jobs/{job_id}/download",
                    message="처리가 완료되었습니다.",
                )
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(status="failed", message=str(exc))

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None


JOBS = JobStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "VirtualFeedMVP/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            data = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/config":
            self._json({
                "app_version": APP_VERSION,
                "openai_enabled": bool(os.getenv("OPENAI_API_KEY", "").strip()),
                "openai_model": os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
                "max_concurrency": 10,
                "browser_fallback_enabled": bool(find_browser()),
                "taxonomy_config_path": str(taxonomy_config_path()),
                "taxonomy_config_connected": taxonomy_config_path().exists(),
                "taxonomy_config_modified": taxonomy_config_path().stat().st_mtime if taxonomy_config_path().exists() else None,
                "country_sites": enabled_country_sites(),
            })
            return
        if path == "/api/sample":
            data = (APP_DIR / "sample_uk_40.csv").read_text(encoding="utf-8-sig")
            self._json({"text": data})
            return

        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            job_id = parts[2]
            job = JOBS.get(job_id)
            if not job:
                self._json({"error": "작업을 찾을 수 없습니다."}, 404)
                return
            if len(parts) == 4 and parts[3] == "download":
                file_path = OUTPUT_DIR / f"virtual_feeds_{job_id}.xlsx"
                if not file_path.exists():
                    self._json({"error": "결과 파일이 아직 없습니다."}, 404)
                    return
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="virtual_feeds_{job_id}.xlsx"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._json(job)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/parse":
            try:
                payload = self._read_json()
                products = parse_tabular_text(payload.get("text", ""))
                self._json({"items": [asdict(x) for x in products], "count": len(products)})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return

        if path == "/api/import":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 15 * 1024 * 1024:
                    raise ValueError("파일은 15MB 이하여야 합니다.")
                filename = unquote(self.headers.get("X-Filename", "input.csv"))
                products = load_products_from_file(filename, self.rfile.read(length))
                self._json({"items": [asdict(x) for x in products], "count": len(products)})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return

        if path == "/api/jobs":
            try:
                payload = self._read_json()
                products = [ProductInput(**item) for item in payload.get("items", [])]
                if not products:
                    raise ValueError("처리할 제품이 없습니다.")
                if len(products) > 200:
                    raise ValueError("MVP에서는 한 작업에 최대 200개 제품을 허용합니다.")
                settings = payload.get("settings", {})
                job_id = JOBS.create(products, settings)
                self._json({"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}, HTTPStatus.ACCEPTED)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        self.send_error(404)


def main() -> None:
    host = os.getenv("VF_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("VF_PORT", "8766")))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Virtual Feed MVP: http://{host}:{port}")
    print("종료하려면 Ctrl+C를 누르세요.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
