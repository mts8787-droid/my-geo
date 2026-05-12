import asyncio
import csv
import io
import smtplib
from email.message import EmailMessage
import httpx
from datetime import datetime, timezone
import logging
from typing import List
import xml.etree.ElementTree as ET

import db

log = logging.getLogger("geo_audit.sitemap_agent")

def add_log(msg: str):
    db.add_system_log(msg)
    log.info(msg)

async def parse_sitemap(sitemap_url: str) -> List[str]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(sitemap_url)
        r.raise_for_status()
        
        urls = []
        try:
            root = ET.fromstring(r.content)
            for elem in root.iter():
                if elem.tag.endswith('loc') and elem.text:
                    url = elem.text.strip()
                    if url.startswith("http"):
                        urls.append(url)
        except Exception as e:
            log.error(f"Failed to parse sitemap XML: {e}")
            raise ValueError(f"XML 파싱 실패: {e}")
            
        return list(set(urls))

async def run_sitemap_audit_task(sitemap_url: str, email: str, site_name: str, smtp_config: dict):
    from analyzer import analyze_url
    
    try:
        add_log(f"[{site_name}] 사이트맵 파싱 시작: {sitemap_url}")
        urls = await parse_sitemap(sitemap_url)
        add_log(f"[{site_name}] 사이트맵 파싱 완료. {len(urls)}개 URL 발견. Audit 시작...")
    except Exception as e:
        err_msg = f"[{site_name}] 파싱 실패: {e}"
        log.error(err_msg)
        add_log(err_msg)
        return

    sem = asyncio.Semaphore(5)
    results = []

    async def _audit(u: str):
        async with sem:
            try:
                res = await analyze_url(u, lightweight=True)
                return {"url": u, "result": res}
            except Exception as e:
                return {"url": u, "error": str(e)}

    tasks = [_audit(u) for u in urls]
    audit_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Create CSV
    csv_file = io.StringIO()
    writer = csv.writer(csv_file)
    header = ["Site Name", "URL", "Inspection Time", "Total Score", "Grade", "Error"]
    
    # Extract all criteria keys from the first successful result
    first_success = next((r for r in audit_results if isinstance(r, dict) and "result" in r), None)
    criteria_keys = []
    if first_success:
        for cat, cdata in first_success["result"].get("categories", {}).items():
            for cr in cdata.get("criteria", []):
                criteria_keys.append(cr["name"])
    
    writer.writerow(header + criteria_keys)
    
    # KST timezone for inspection time
    inspection_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for r in audit_results:
        if isinstance(r, Exception):
            continue
            
        url = r.get("url", "")
        if "error" in r:
            writer.writerow([site_name, url, inspection_time, "", "", r["error"]] + [""] * len(criteria_keys))
            continue
        
        score = r["result"].get("score", {}).get("total", "")
        grade = r["result"].get("score", {}).get("grade", "")
        row = [site_name, url, inspection_time, score, grade, ""]
        
        # Itemized results
        for cr_name in criteria_keys:
            val = "0"
            for cat, cdata in r["result"].get("categories", {}).items():
                for cr in cdata.get("criteria", []):
                    if cr["name"] == cr_name:
                        val = str(cr.get("points", 0))
                        break
            row.append(val)
        
        writer.writerow(row)

    # Send Email
    msg = EmailMessage()
    msg['Subject'] = f'[{site_name}] GEO Audit 자동 점검 리포트'
    
    smtp_from = smtp_config.get("SMTP_FROM") or smtp_config.get("SMTP_USER")
    if not smtp_from:
        smtp_from = "no-reply@example.com"
        
    msg['From'] = smtp_from
    msg['To'] = email
    
    body = (
        f"안녕하세요,\n\n"
        f"요청하신 '{site_name}'의 사이트맵({sitemap_url}) 기반 자동 감사가 완료되었습니다.\n"
        f"총 {len(urls)}개의 URL이 점검되었습니다.\n\n"
        f"첨부된 CSV 파일에서 상세 항목별 결과를 확인해 주세요.\n\n"
        f"감사합니다.\nGEO Audit 시스템 드림"
    )
    msg.set_content(body)

    # CSV file attachment with BOM for Excel compatibility
    csv_bytes = csv_file.getvalue().encode('utf-8-sig')
    msg.add_attachment(csv_bytes, maintype='text', subtype='csv', filename=f'geo_audit_report_{site_name}.csv')

    try:
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
        await asyncio.to_thread(_send)
        add_log(f"✅ [{site_name}] 이메일 전송 완료: {email}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        add_log(f"❌ [{site_name}] 이메일 전송 실패: {str(e)}")
