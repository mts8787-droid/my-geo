import asyncio
import os
import argparse
from datetime import datetime

# 로컬 PC 자원을 최대로 쓰기 위한 환경변수 설정
os.environ["PLAYWRIGHT_CONCURRENCY"] = "15"  # 크롬 창 최대 15개 동시 실행
os.environ["SITEMAP_AGENT_CONCURRENCY"] = "50" # 네트워크 요청 최대 50개 동시 실행

from sitemap_agent import parse_sitemap, _build_csv, CONCURRENCY, PER_URL_TIMEOUT
from analyzer import analyze_url

async def main():
    parser = argparse.ArgumentParser(description="GEO Audit Local Runner")
    parser.add_argument("--urls", type=str, help="Comma-separated list of URLs to audit directly")
    parser.add_argument("--sitemap", type=str, help="Sitemap URL to parse")
    parser.add_argument("--name", type=str, help="Site name for report")
    parser.add_argument("--max", type=int, default=50, help="Max URLs to audit")
    args = parser.parse_args()

    print("="*60)
    print("  [GEO Audit] - 로컬 전용 초고속 점검 프로그램 (CSR 측정 포함)")
    print("="*60)
    
    urls = []
    site_name = args.name
    max_urls = args.max

    if args.urls:
        urls = [u.strip() for u in args.urls.split(",") if u.strip()]
        if not site_name:
            site_name = "Direct_URLs"
        print(f"\n[Direct] 직접 입력된 {len(urls)}개 URL에 대해 점검을 시작합니다.")
    else:
        sitemap_url = args.sitemap or input("\n1. 점검할 사이트맵 URL을 입력하세요\n   (예: https://www.lg.com/uk/sitemap.xml): ").strip()
        if not sitemap_url:
            print("입력된 URL이 없습니다. 프로그램을 종료합니다.")
            return
            
        if not site_name:
            site_name = input("\n2. 사이트 이름을 입력하세요 (결과 파일명에 사용됩니다)\n   (예: UK): ").strip() or "Local"
        
        if not args.max and not args.sitemap: # Only prompt max if not provided via args
            max_input = input("\n3. 최대 점검할 개수를 입력하세요 (기본값: 50)\n   (전체 점검을 원하시면 10000 등 큰 숫자를 입력하세요): ").strip()
            max_urls = int(max_input) if max_input.isdigit() else 50
        
        print(f"\n[Sitemap] [{site_name}] 사이트맵 파싱을 시작합니다... (잠시만 기다려주세요)")
        try:
            urls = await parse_sitemap(sitemap_url)
        except Exception as e:
            print(f"[Error] 사이트맵 파싱 실패: {e}")
            return
            
        if not urls:
            print("[Error] 사이트맵에서 URL을 찾을 수 없습니다.")
            return
            
        if len(urls) > max_urls:
            print(f"[Info] 총 {len(urls)}개 URL이 발견되었습니다. 설정에 따라 상위 {max_urls}개만 점검합니다.")
            urls = urls[:max_urls]
        else:
            print(f"[Info] 총 {len(urls)}개 URL이 발견되었습니다. 전체 점검을 시작합니다.")
        
    sem = asyncio.Semaphore(CONCURRENCY)
    total = len(urls)
    completed = {"n": 0}
    
    async def _audit(u: str):
        async with sem:
            try:
                # 로컬 전용이므로 무조건 브라우저를 띄워 CSR 비율을 측정합니다 (lightweight=False)
                res = await asyncio.wait_for(analyze_url(u, lightweight=False), timeout=PER_URL_TIMEOUT)
                r = {"url": u, "result": res}
            except Exception as e:
                r = {"url": u, "error": str(e)}
            completed["n"] += 1
            print(f"[Progress] 진행 상황: {completed['n']} / {total} 완료", end='\r')
            return r
            
    print(f"\n[Start] 백그라운드에서 크롬 브라우저를 띄워 정밀 렌더링 및 분석을 시작합니다.")
    print(f"   (주의: 화면에는 보이지 않으나 PC 자원을 꽤 사용합니다.)\n")
    
    start_time = datetime.now()
    audit_results = await asyncio.gather(*[_audit(u) for u in urls], return_exceptions=True)
    end_time = datetime.now()
    
    print(f"\n\n[Done] 분석 완료! (소요 시간: {end_time - start_time})")
    print("CSV 보고서 파일을 생성하는 중입니다...")
    
    csv_bytes = _build_csv(audit_results, site_name)
    
    # 보고서를 reports/local_audits 하위 폴더에 저장
    report_dir = os.path.join("reports", "local_audits")
    os.makedirs(report_dir, exist_ok=True)
    filename = os.path.join(report_dir, f"local_audit_{site_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    
    with open(filename, "wb") as f:
        f.write(csv_bytes)
        
    print(f"[Success] 성공적으로 저장되었습니다!")
    print(f"[File] 파일 위치: {os.path.abspath(filename)}")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(main())
