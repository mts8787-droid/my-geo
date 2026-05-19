import asyncio
import httpx

async def main():
    target_ip = "23.47.87.25"
    domain = "www.lg.com"
    # 영국(UK) 홈 화면을 기준으로 테스트
    test_url = f"https://{target_ip}/uk/"
    
    # 렌더 서버 에이전트와 동일한 User-Agent 사용
    # (실제 whitelisting을 요청했던 User-Agent가 다르다면 이 부분을 수정해주세요)
    user_agent = "Mozilla/5.0 (compatible; GEOAudit/1.0; +https://geoaudit.dev)"
    
    headers = {
        "Host": domain,
        "User-Agent": user_agent
    }
    
    print("==================================================")
    print(f"🔍 Akamai Staging 화이트리스트 검증 테스트 시작")
    print("==================================================")
    print(f"  - Target URL : https://{domain}/uk/ (Staging IP: {target_ip})")
    print(f"  - User-Agent : {user_agent}")
    print("--------------------------------------------------\n")
    
    # SSL 인증서가 도메인 기반이므로 IP 접속 시 발생하는 에러 무시 (verify=False)
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        try:
            response = await client.get(test_url, headers=headers)
            print(f"[결과] HTTP Status Code: {response.status_code}")
            
            # 상태 코드로 WAF 통과 여부 1차 판단
            if response.status_code == 403:
                print("  ❌ 403 Forbidden: 접근이 차단되었습니다. (화이트리스트 미적용 또는 WAF 차단)")
            elif response.status_code == 200:
                print("  ✅ 200 OK: 접근에 성공했습니다! (화이트리스트 적용 완료 추정)")
            else:
                print(f"  ⚠️ {response.status_code}: 다른 형태의 응답입니다.")
                
            print("\n[Akamai / WAF 관련 주요 응답 헤더 확인]")
            print("안내받은 '확인해야 할 헤더'가 아래 목록에 있는지 대조해 보세요.")
            for k, v in response.headers.items():
                print(f"  {k}: {v}")
                
        except Exception as e:
            print(f"\n❌ 에러 발생: {e}")

if __name__ == "__main__":
    asyncio.run(main())
