import asyncio
import csv
import io
import os
import smtplib
from email.message import EmailMessage
import httpx
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
import logging
from typing import List
import xml.etree.ElementTree as ET

import db

log = logging.getLogger("geo_audit.sitemap_agent")

# 환경변수로 운영 중 튜닝 가능
MAX_URLS = int(os.environ.get("SITEMAP_AGENT_MAX_URLS", 50))
PER_URL_TIMEOUT = float(os.environ.get("SITEMAP_AGENT_URL_TIMEOUT", 60))
SITEMAP_MAX_DEPTH = int(os.environ.get("SITEMAP_AGENT_MAX_DEPTH", 3))
CONCURRENCY = int(os.environ.get("SITEMAP_AGENT_CONCURRENCY", 50))


def add_log(msg: str):
    db.add_system_log(msg)
    log.info(msg)


def _local_tag(tag: str) -> str:
    """XML 태그에서 namespace 제거."""
    return tag.split("}")[-1].lower() if "}" in tag else tag.lower()


async def _fetch_sitemap_one(client: httpx.AsyncClient, sitemap_url: str) -> tuple:
    """단일 사이트맵 fetch → (child_sitemaps, page_urls)."""
    r = await client.get(sitemap_url)
    r.raise_for_status()
    try:
        root = ET.fromstring(r.content)
    except Exception as e:
        raise ValueError(f"XML 파싱 실패 ({sitemap_url}): {e}")

    root_tag = _local_tag(root.tag)
    child_sitemaps: List[str] = []
    page_urls: List[str] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "loc" or not elem.text:
            continue
        u = elem.text.strip()
        if not u.startswith("http"):
            continue
        if root_tag == "sitemapindex":
            child_sitemaps.append(u)
        else:
            page_urls.append(u)
    return child_sitemaps, page_urls


async def parse_sitemap(sitemap_url: str, max_depth: int = SITEMAP_MAX_DEPTH) -> List[str]:
    """사이트맵 파싱. sitemap-index는 재귀로 자식 사이트맵을 펼친다."""
    seen_sitemaps = set()
    all_urls: set = set()

    parse_concurrency = max(1, int(os.environ.get("SITEMAP_PARSE_CONCURRENCY", 10)))
    # Akamai 화이트리스트는 운영 UA(MyGEOAudit/1.0)에 등록 — analyzer와 동일 UA 사용해서 403 회피
    ua = os.environ.get(
        "AUDIT_USER_AGENT",
        "MyGEOAudit/1.0 (Audit agent operated by D2C Digital Marketing Team, LG Electronics)",
    )
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": ua},
    ) as client:
        sem = asyncio.Semaphore(parse_concurrency)

        async def _walk(url: str, depth: int):
            if url in seen_sitemaps or depth > max_depth:
                return
            seen_sitemaps.add(url)
            async with sem:
                try:
                    children, pages = await _fetch_sitemap_one(client, url)
                except Exception as e:
                    log.warning(f"Sub-sitemap 실패 ({url}): {e}")
                    if depth == 0:
                        raise  # 최상위는 사용자에게 알린다
                    return
            all_urls.update(pages)
            if children:
                await asyncio.gather(*[_walk(c, depth + 1) for c in children])

        await _walk(sitemap_url, 0)

    return list(all_urls)

async def run_sitemap_audit_task(sitemap_url: str, email: str, site_name: str, smtp_config: dict):
    from analyzer import analyze_url

    try:
        add_log(f"[{site_name}] 사이트맵 파싱 시작: {sitemap_url}")
        urls = await parse_sitemap(sitemap_url)
        if not urls:
            add_log(f"❌ [{site_name}] 사이트맵에서 URL을 찾지 못했습니다.")
            return
        if len(urls) > MAX_URLS:
            add_log(f"[{site_name}] {len(urls)}개 URL 발견 → 상위 {MAX_URLS}개로 제한 (env SITEMAP_AGENT_MAX_URLS로 변경 가능)")
            urls = urls[:MAX_URLS]
        else:
            add_log(f"[{site_name}] 사이트맵 파싱 완료. {len(urls)}개 URL. Audit 시작 (동시 {CONCURRENCY}개, URL당 {PER_URL_TIMEOUT}s 타임아웃)")
    except Exception as e:
        err_msg = f"❌ [{site_name}] 파싱 실패: {e}"
        log.exception(err_msg)
        add_log(err_msg)
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    total = len(urls)
    completed = {"n": 0}

    async def _audit(u: str):
        async with sem:
            try:
                res = await asyncio.wait_for(
                    analyze_url(u, lightweight=False), timeout=PER_URL_TIMEOUT
                )
                r = {"url": u, "result": res}
            except asyncio.TimeoutError:
                r = {"url": u, "error": f"timeout ({PER_URL_TIMEOUT}s)"}
            except Exception as e:
                r = {"url": u, "error": f"{type(e).__name__}: {str(e)[:160]}"}
            completed["n"] += 1
            # 10개마다 또는 마지막에 진행 로그
            if completed["n"] % 10 == 0 or completed["n"] == total:
                add_log(f"[{site_name}] 진행 {completed['n']}/{total}")
            return r

    try:
        audit_results = await asyncio.gather(
            *[_audit(u) for u in urls], return_exceptions=True
        )
        add_log(f"[{site_name}] Audit 완료. CSV 생성 중...")
    except Exception as e:
        err_msg = f"❌ [{site_name}] Audit 실행 중 예외: {type(e).__name__}: {e}"
        log.exception(err_msg)
        add_log(err_msg)
        return

    try:
        await _build_and_send_report(
            audit_results=audit_results,
            urls=urls,
            sitemap_url=sitemap_url,
            email=email,
            site_name=site_name,
            smtp_config=smtp_config,
        )
    except Exception as e:
        err_msg = f"❌ [{site_name}] 리포트 생성/전송 중 예외: {type(e).__name__}: {e}"
        log.exception(err_msg)
        add_log(err_msg)


def _build_csv(audit_results: list, site_name: str) -> bytes:
    csv_file = io.StringIO()
    writer = csv.writer(csv_file)
    header = ["Site Name", "URL", "Inspection Time",
              "Page Type", "Page Type Label", "Detected By",
              "Total Score", "Grade", "Error"]

    first_success = next((r for r in audit_results if isinstance(r, dict) and "result" in r), None)
    criteria_keys: List[tuple] = []
    if first_success:
        breakdown = first_success["result"].get("score", {}).get("breakdown", {})
        for cat_key, cdata in breakdown.items():
            for item_id, item in cdata.get("items", {}).items():
                criteria_keys.append((cat_key, item_id, item.get("label", item_id)))

    writer.writerow(header + [label for _, _, label in criteria_keys])

    inspection_time = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    empty_pt_cells = ["", "", ""]  # page_type / label / matched_by

    for r in audit_results:
        if isinstance(r, Exception):
            continue
        url = r.get("url", "")
        if "error" in r:
            writer.writerow([site_name, url, inspection_time] + empty_pt_cells + ["", "", r["error"]] + [""] * len(criteria_keys))
            continue

        result_obj = r["result"]
        score_obj = result_obj.get("score", {})
        score = score_obj.get("total", "")
        grade = score_obj.get("grade", "")
        pt = result_obj.get("page_type", {}) or {}
        pt_cells = [pt.get("id", ""), pt.get("label", ""), pt.get("matched_by", "")]
        row = [site_name, url, inspection_time] + pt_cells + [score, grade, ""]

        breakdown = score_obj.get("breakdown", {})
        for cat_key, item_id, _label in criteria_keys:
            item = breakdown.get(cat_key, {}).get("items", {}).get(item_id, {})
            row.append("PASS" if item.get("pass") else "FAIL")

        writer.writerow(row)

    return csv_file.getvalue().encode("utf-8-sig")


async def _build_and_send_report(
    audit_results: list,
    urls: List[str],
    sitemap_url: str,
    email: str,
    site_name: str,
    smtp_config: dict,
) -> None:
    csv_bytes = _build_csv(audit_results, site_name)

    msg = EmailMessage()
    msg["Subject"] = f"[{site_name}] GEO Audit 자동 점검 리포트"
    smtp_from = smtp_config.get("SMTP_FROM") or smtp_config.get("SMTP_USER") or "no-reply@example.com"
    msg["From"] = smtp_from
    msg["To"] = email
    body = (
        f"안녕하세요,\n\n"
        f"요청하신 '{site_name}'의 사이트맵({sitemap_url}) 기반 자동 감사가 완료되었습니다.\n"
        f"총 {len(urls)}개의 URL이 점검되었습니다.\n\n"
        f"첨부된 CSV 파일에서 상세 항목별 결과를 확인해 주세요.\n\n"
        f"감사합니다.\nGEO Audit 시스템 드림"
    )
    msg.set_content(body)
    msg.add_attachment(
        csv_bytes,
        maintype="text",
        subtype="csv",
        filename=f"geo_audit_report_{site_name}.csv",
    )

    add_log(f"[{site_name}] 이메일 발송 준비 중... ({email})")

    def _send():
        host = smtp_config.get("SMTP_HOST", "smtp.gmail.com")
        port = int(smtp_config.get("SMTP_PORT", 587))
        user = smtp_config.get("SMTP_USER", "")
        password = smtp_config.get("SMTP_PASS", "")
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)

    try:
        await asyncio.to_thread(_send)
        add_log(f"✅ [{site_name}] 이메일 전송 완료: {email}")
    except Exception as e:
        log.exception(f"Failed to send email: {e}")
        add_log(f"❌ [{site_name}] 이메일 전송 실패: {str(e)}")
