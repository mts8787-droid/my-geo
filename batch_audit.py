"""
로컬 전체 사이트 점검 배치 스크립트.

CSV (예: reports/lg_urls_jp.csv) → analyze_url 호출 → NDJSON 누적 → BigQuery 적재.

사용 예:
    # 1. JP 사이트맵 전체를 Full CSR로 점검하고 NDJSON에 누적만
    python3 batch_audit.py reports/lg_urls_jp.csv --concurrency 10 --no-upload

    # 2. NDJSON이 이미 있고 BigQuery 적재만 따로
    python3 batch_audit.py reports/lg_urls_jp.csv --only-upload \\
        --project my-gcp-project --dataset lg_geo_audit --table audit_results

    # 3. 분석 + 자동 적재
    python3 batch_audit.py reports/lg_urls_jp.csv \\
        --project my-gcp-project --dataset lg_geo_audit --table audit_results

특징:
    - 체크포인트: NDJSON에 이미 있는 URL은 자동 skip (Ctrl-C 후 재시작 가능)
    - 동시성: --concurrency (Full CSR이면 5~10 권장; lightweight이면 50)
    - URL당 타임아웃 + 진행 로그
    - GOOGLE_APPLICATION_CREDENTIALS 환경변수로 서비스 계정 키 인증
"""
import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# analyzer import는 main entry에서 — argparse가 먼저 실행되도록

log = logging.getLogger("batch_audit")


# ── CSV 읽기 ─────────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://\S+")


def read_urls_from_csv(csv_path: Path) -> list:
    """CSV에서 URL 추출. BOM, 따옴표, 개행, 공백 모두 정리."""
    import csv
    urls = []
    seen = set()
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            for cell in row:
                m = URL_RE.search(cell)
                if not m:
                    continue
                u = m.group(0).strip().rstrip("\",'")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
    return urls


def detect_country(csv_path: Path) -> Optional[str]:
    """파일명 lg_urls_<country>.csv → country."""
    m = re.match(r"lg_urls_([a-z]+)\.csv$", csv_path.name.lower())
    return m.group(1) if m else None


# ── NDJSON 체크포인트 ─────────────────────────────────────────────────────────

def load_done_urls(ndjson_path: Path) -> set:
    """기존 NDJSON에서 완료된 URL 집합 로드."""
    if not ndjson_path.exists():
        return set()
    done = set()
    with open(ndjson_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("url"):
                    done.add(obj["url"])
            except json.JSONDecodeError:
                continue
    return done


def append_ndjson(ndjson_path: Path, record: dict) -> None:
    """단일 row append (flush 포함 — 크래시 시 데이터 보존)."""
    with open(ndjson_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        f.flush()


# ── analyze_url 결과 → BigQuery row 매핑 ──────────────────────────────────────

def _to_bq_row(
    url: str,
    country: Optional[str],
    source_csv: str,
    inspected_at: str,
    result: Optional[dict],
    error: Optional[str],
) -> dict:
    if error or not result:
        return {
            "url": url,
            "country": country,
            "source_csv": source_csv,
            "inspected_at": inspected_at,
            "total_score": None,
            "max_score": None,
            "grade": None,
            "error": error or "no result",
            "csr_ratio": None,
            "breakdown": [],
        }
    score = result.get("score", {}) or {}
    csr = result.get("csr_ratio", {}) or {}
    breakdown_raw = score.get("breakdown", {}) or {}
    breakdown_rows = []
    for cat_key, cat in breakdown_raw.items():
        items_rows = []
        for item_id, item in (cat.get("items") or {}).items():
            items_rows.append({
                "id": item_id,
                "label": item.get("label"),
                "passed": bool(item.get("pass")) if item.get("pass") is not None else None,
                "value": str(item["value"]) if item.get("value") is not None else None,
                "hint": item.get("hint"),
                "rule_type": item.get("rule_type"),
            })
        breakdown_rows.append({
            "category": cat_key,
            "points": cat.get("points"),
            "max": cat.get("max"),
            "passed": cat.get("passed"),
            "total": cat.get("total"),
            "items": items_rows,
        })
    return {
        "url": url,
        "country": country,
        "source_csv": source_csv,
        "inspected_at": inspected_at,
        "total_score": score.get("total"),
        "max_score": score.get("max"),
        "grade": score.get("grade"),
        "error": None,
        "csr_ratio": {
            "status": csr.get("status"),
            "ssr_chars": csr.get("ssr_chars"),
            "csr_chars": csr.get("csr_chars"),
            "ratio": csr.get("ratio"),
        },
        "breakdown": breakdown_rows,
    }


# ── audit 메인 루프 ──────────────────────────────────────────────────────────

async def run_audit(
    urls: list,
    ndjson_path: Path,
    country: Optional[str],
    source_csv: str,
    concurrency: int,
    per_url_timeout: float,
    lightweight: bool,
    progress_every: int = 10,
) -> dict:
    from analyzer import analyze_url

    sem = asyncio.Semaphore(concurrency)
    total = len(urls)
    counters = {"done": 0, "ok": 0, "err": 0, "start": time.time()}
    stop = {"flag": False}

    def _on_signal(*_):
        stop["flag"] = True
        log.warning("중단 신호 수신 — 진행 중인 작업 완료 후 종료")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except ValueError:
            pass  # 일부 환경(스레드)에서 등록 불가

    async def _one(url: str):
        if stop["flag"]:
            return
        async with sem:
            if stop["flag"]:
                return
            inspected_at = datetime.now(timezone.utc).isoformat()
            try:
                res = await asyncio.wait_for(
                    analyze_url(url, lightweight=lightweight), timeout=per_url_timeout
                )
                row = _to_bq_row(url, country, source_csv, inspected_at, res, None)
                counters["ok"] += 1
            except asyncio.TimeoutError:
                row = _to_bq_row(url, country, source_csv, inspected_at, None,
                                 f"timeout ({per_url_timeout}s)")
                counters["err"] += 1
            except Exception as e:
                row = _to_bq_row(url, country, source_csv, inspected_at, None,
                                 f"{type(e).__name__}: {str(e)[:200]}")
                counters["err"] += 1
            append_ndjson(ndjson_path, row)
            counters["done"] += 1
            if counters["done"] % progress_every == 0 or counters["done"] == total:
                elapsed = time.time() - counters["start"]
                rate = counters["done"] / elapsed if elapsed else 0
                eta = (total - counters["done"]) / rate if rate else 0
                log.info(
                    "진행 %d/%d (ok=%d err=%d) — %.1f URL/s, ETA %.1f분",
                    counters["done"], total, counters["ok"], counters["err"],
                    rate, eta / 60,
                )

    await asyncio.gather(*[_one(u) for u in urls], return_exceptions=True)
    return counters


# ── BigQuery 적재 ────────────────────────────────────────────────────────────

def load_to_bigquery(
    ndjson_path: Path,
    project: str,
    dataset: str,
    table: str,
    schema_path: Path,
    source_csv: str,
    location: str = "US",
) -> None:
    """NDJSON을 BigQuery에 적재.

    재실행 안전성: 같은 source_csv의 기존 row를 먼저 DELETE한 뒤 전체를 INSERT.
    이 패턴이면 같은 CSV를 다시 돌려도 중복이 쌓이지 않는다.
    """
    from google.cloud import bigquery
    client = bigquery.Client(project=project)

    table_ref = f"{project}.{dataset}.{table}"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_json = json.load(f)
    schema = [bigquery.SchemaField.from_api_repr(s) for s in schema_json]

    # 1) 같은 source_csv의 기존 row 삭제 (테이블 없으면 silent skip)
    try:
        delete_job = client.query(
            f"DELETE FROM `{table_ref}` WHERE source_csv = @csv",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("csv", "STRING", source_csv),
                ]
            ),
            location=location,
        )
        deleted = delete_job.result().num_dml_affected_rows or 0
        if deleted:
            log.info("기존 %d개 row 삭제 (source_csv=%s)", deleted, source_csv)
    except Exception as e:
        # 첫 실행이면 테이블이 아직 없어서 NotFound — 무시
        if "Not found" not in str(e) and "Table" not in str(e):
            log.warning("DELETE 단계 경고 (무시): %s", e)

    # 2) NDJSON 전체 적재
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        ignore_unknown_values=False,
    )
    log.info("BigQuery 적재 시작 — %s ← %s", table_ref, ndjson_path)
    with open(ndjson_path, "rb") as f:
        job = client.load_table_from_file(
            f, table_ref, job_config=job_config, location=location
        )
    job.result()
    dest = client.get_table(table_ref)
    log.info("BigQuery 적재 완료 — 테이블 총 row 수: %d", dest.num_rows)


# ── 엔트리 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LG 사이트 전체 점검 배치 + BigQuery 적재")
    parser.add_argument("csv", type=Path, help="URL 목록 CSV (예: reports/lg_urls_jp.csv)")
    parser.add_argument("--ndjson", type=Path, default=None,
                        help="중간 NDJSON 경로 (기본: results/<csv-stem>.ndjson)")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="동시 분석 수 (Full CSR이면 5~10, lightweight면 50 권장)")
    parser.add_argument("--per-url-timeout", type=float, default=120.0,
                        help="URL당 최대 분석 시간(초)")
    parser.add_argument("--lightweight", action="store_true",
                        help="Playwright 사용 안 함 (CSR 분석 생략, 더 빠름)")
    parser.add_argument("--limit", type=int, default=0,
                        help="처리할 최대 URL 수 (0 = 전체)")
    parser.add_argument("--progress-every", type=int, default=10,
                        help="진행 로그 출력 주기")

    # BigQuery 옵션
    parser.add_argument("--project", default=os.environ.get("BQ_PROJECT"),
                        help="GCP 프로젝트 ID (env BQ_PROJECT)")
    parser.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "lg_geo_audit"),
                        help="BigQuery 데이터셋 (env BQ_DATASET, 기본: lg_geo_audit)")
    parser.add_argument("--table", default=os.environ.get("BQ_TABLE", "audit_results"),
                        help="BigQuery 테이블 (env BQ_TABLE, 기본: audit_results)")
    parser.add_argument("--location", default=os.environ.get("BQ_LOCATION", "US"),
                        help="BigQuery 데이터셋 위치 (기본 US)")
    parser.add_argument("--schema", type=Path, default=Path("bq_schema.json"),
                        help="BigQuery 스키마 JSON")
    parser.add_argument("--no-upload", action="store_true",
                        help="BigQuery 적재 생략 (NDJSON만 생성)")
    parser.add_argument("--only-upload", action="store_true",
                        help="분석은 생략하고 기존 NDJSON만 BigQuery에 적재")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.csv.exists():
        log.error("CSV 파일을 찾을 수 없습니다: %s", args.csv)
        sys.exit(1)

    country = detect_country(args.csv)
    ndjson_path = args.ndjson or Path("results") / f"{args.csv.stem}.ndjson"
    ndjson_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("입력: %s | country=%s | 출력: %s", args.csv, country, ndjson_path)

    if not args.only_upload:
        urls = read_urls_from_csv(args.csv)
        log.info("CSV에서 URL %d개 로드", len(urls))
        done = load_done_urls(ndjson_path)
        if done:
            log.info("기존 NDJSON에 %d개 완료 — 이어서 진행", len(done))
            urls = [u for u in urls if u not in done]
        if args.limit > 0:
            urls = urls[: args.limit]
            log.info("--limit 적용: %d개만 처리", len(urls))
        if not urls:
            log.info("처리할 URL 없음 (모두 완료 또는 limit=0)")
        else:
            log.info(
                "Audit 시작 — concurrency=%d, lightweight=%s, per_url_timeout=%.1fs",
                args.concurrency, args.lightweight, args.per_url_timeout,
            )
            counters = asyncio.run(run_audit(
                urls=urls,
                ndjson_path=ndjson_path,
                country=country,
                source_csv=args.csv.name,
                concurrency=args.concurrency,
                per_url_timeout=args.per_url_timeout,
                lightweight=args.lightweight,
                progress_every=args.progress_every,
            ))
            elapsed = time.time() - counters["start"]
            log.info(
                "Audit 종료 — done=%d ok=%d err=%d, 소요 %.1f분",
                counters["done"], counters["ok"], counters["err"], elapsed / 60,
            )

    if args.no_upload:
        log.info("--no-upload 지정 — BigQuery 적재 생략")
        return

    if not args.project:
        log.error("BigQuery 적재를 위해서는 --project 또는 BQ_PROJECT 환경변수가 필요합니다.")
        log.error("(적재 건너뛰려면 --no-upload 옵션 사용)")
        sys.exit(2)
    if not args.schema.exists():
        log.error("스키마 파일을 찾을 수 없습니다: %s", args.schema)
        sys.exit(2)
    if not ndjson_path.exists() or ndjson_path.stat().st_size == 0:
        log.error("NDJSON이 비어있어 적재할 데이터가 없습니다: %s", ndjson_path)
        sys.exit(2)

    load_to_bigquery(
        ndjson_path=ndjson_path,
        project=args.project,
        dataset=args.dataset,
        table=args.table,
        schema_path=args.schema,
        source_csv=args.csv.name,
        location=args.location,
    )


if __name__ == "__main__":
    main()
