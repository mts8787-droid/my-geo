import asyncio
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import csv
from collections import defaultdict
import os

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:
    print("APScheduler가 설치되지 않았습니다. pip install apscheduler를 실행해주세요.")
    AsyncIOScheduler = None

async def parse_lg_sitemap_and_plan():
    print(f"[{datetime.now()}] LG 사이트맵 파싱 및 스케줄링 계획 생성을 시작합니다...")
    sitemap_index_url = "https://www.lg.com/sitemap.xml"
    
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        try:
            print(f"1. 사이트맵 인덱스({sitemap_index_url})를 가져옵니다.")
            r = await client.get(sitemap_index_url)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            
            sitemaps = []
            for elem in root.iter():
                if elem.tag.endswith('loc') and elem.text:
                    url = elem.text.strip()
                    if url.endswith('.xml'):
                        sitemaps.append(url)
            
            print(f"총 {len(sitemaps)}개의 하위 사이트맵을 발견했습니다.")
            
            all_urls = []
            site_groups = defaultdict(list)
            
            # 동시 처리를 위한 세마포어
            sem = asyncio.Semaphore(10)
            
            async def _fetch_sub_sitemap(sm_url):
                async with sem:
                    try:
                        sr = await client.get(sm_url)
                        sroot = ET.fromstring(sr.content)
                        for selem in sroot.iter():
                            if selem.tag.endswith('loc') and selem.text:
                                u = selem.text.strip()
                                if u.startswith("http") and not u.endswith('.xml'):
                                    all_urls.append(u)
                                    
                                    # 국가별/Site별 그룹화 (예: lg.com/us/...)
                                    parts = u.split('lg.com/')
                                    if len(parts) > 1:
                                        sub_path = parts[1].split('/')
                                        if len(sub_path) > 0 and len(sub_path[0]) in (2, 3):
                                            country = sub_path[0].lower()
                                        else:
                                            country = 'global'
                                    else:
                                        country = 'unknown'
                                    site_groups[country].append(u)
                    except Exception as e:
                        print(f"하위 사이트맵 파싱 실패 ({sm_url}): {e}")

            print("2. 하위 사이트맵에서 URL을 추출하고 국가별로 그룹화합니다...")
            await asyncio.gather(*[_fetch_sub_sitemap(sm) for sm in sitemaps])
            
            print(f"총 {len(all_urls)}개의 URL이 수집되었으며, {len(site_groups)}개의 국가/사이트로 분류되었습니다.")
            
            # 3. 리스트업 CSV 생성
            os.makedirs("reports", exist_ok=True)
            list_file = "reports/lg_urls_list.csv"
            with open(list_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Country/Site", "URL"])
                for c, urls in site_groups.items():
                    for u in urls:
                        writer.writerow([c, u])
                        
            print(f"- 전체 URL 리스트가 '{list_file}'에 저장되었습니다.")
            
            # 4. 예약 스케줄링 계획표 생성
            # 간단한 계획: 하루 1000개 제한
            plan_file = "reports/lg_scheduling_plan.csv"
            schedule_date = datetime.now()
            
            with open(plan_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Country/Site", "Total URLs", "Days Required", "Suggested Start Date", "Suggested End Date"])
                
                # 많은 URL을 가진 국가부터 정렬
                sorted_groups = sorted(site_groups.items(), key=lambda x: len(x[1]), reverse=True)
                
                for c, urls in sorted_groups:
                    count = len(urls)
                    days_needed = max(1, count // 1000 + (1 if count % 1000 != 0 else 0))
                    
                    end_date = schedule_date + timedelta(days=days_needed - 1)
                    
                    writer.writerow([
                        c, 
                        count, 
                        days_needed, 
                        schedule_date.strftime('%Y-%m-%d'), 
                        end_date.strftime('%Y-%m-%d')
                    ])
                    
                    schedule_date = end_date + timedelta(days=1)
                    
            print(f"- 예약 스케줄링 계획표가 '{plan_file}'에 저장되었습니다.")
            print("작업이 성공적으로 완료되었습니다!")
            
        except Exception as e:
            print(f"작업 중 오류 발생: {e}")

def schedule_task():
    if not AsyncIOScheduler:
        return
        
    scheduler = AsyncIOScheduler()
    
    now = datetime.now()
    target_time = now.replace(hour=11, minute=50, second=0, microsecond=0)
    
    if now > target_time:
        print(f"[{now}] 목표 시간(11:50)이 이미 지났습니다. 즉시 실행합니다.")
        asyncio.run(parse_lg_sitemap_and_plan())
    else:
        print(f"[{now}] 오전 11시 50분에 사이트맵 파싱 작업이 예약되었습니다.")
        scheduler.add_job(parse_lg_sitemap_and_plan, 'date', run_date=target_time)
        scheduler.start()
        
        try:
            asyncio.get_event_loop().run_forever()
        except (KeyboardInterrupt, SystemExit):
            print("스케줄러가 종료되었습니다.")

if __name__ == "__main__":
    schedule_task()
