import re

def process_html():
    with open('static/geo-agent-deck.html', 'r', encoding='utf-8') as f:
        html = f.read()

    # 1. 네비게이션 버튼 수정
    nav_old = """  <div class="nav-pills">
    <a href="#s1" class="pill">1. 개발 계획 & Workflow</a>
    <a href="#s2" class="pill">2. 체크리스트 (1/2)</a>
    <a href="#s3" class="pill">3. 체크리스트 (2/2)</a>
    <a href="#s4" class="pill">4. Workflow 확대</a>
  </div>"""
    nav_new = """  <div class="nav-pills">
    <a href="#s1" class="pill">1. 개발 계획 (Road Map)</a>
    <a href="#s2" class="pill">2. Work Flow</a>
    <a href="#s3" class="pill">3. 체크리스트 (1/2)</a>
    <a href="#s4" class="pill">4. 체크리스트 (2/2)</a>
  </div>"""
    html = html.replace(nav_old, nav_new)

    # 2. Slide 1에서 Work Flow 부분 제거 및 Road Map 가운데 정렬 처리
    # Slide 1 시작 태그
    s1_start = html.find('<div class="slide-frame" id="s1">')
    s2_start = html.find('<div class="slide-frame" id="s2">')
    
    slide1_html = html[s1_start:s2_start]
    
    # slide1에서 workflow-area 시작 전까지만 자르기
    workflow_start = slide1_html.find('<!-- Work Flow -->')
    if workflow_start != -1:
        # workflow 부분 날리고 슬라이드 번호만 남기기
        slide1_new = slide1_html[:workflow_start] + '\n  <div class="slide-num">1 / 4</div>\n</div>\n</div>\n\n'
        
        # Road Map 위치 살짝 아래로 조정 (중앙 배치를 위해 css 인라인 수정)
        slide1_new = slide1_new.replace('style="top: 130px;"', 'style="top: 250px;"')
        slide1_new = slide1_new.replace('top: 165px;', 'top: 285px;')
        slide1_new = slide1_new.replace('top: 100px;', 'top: 220px;')
        slide1_new = slide1_new.replace('top: 200px;', 'top: 320px;')
        
        html = html.replace(slide1_html, slide1_new)
        
    # 3. Slide 4 (Work flow zoom)을 Slide 2 자리로 이동
    s3_start = html.find('<div class="slide-frame" id="s3">')
    s4_start = html.find('<!-- ═════ SLIDE 4 — Workflow 확대 ═════════════════════════════════════ -->')
    
    slide4_html = html[s4_start:]
    
    # 기존 Slide 2, 3 영역
    s2_to_s3_html = html[html.find('<div class="slide-frame" id="s2">'):s4_start]
    
    # Slide 4 HTML 조작 (id="s2", 슬라이드 번호 2 / 4, 타이틀 수정)
    slide4_new = slide4_html.replace('id="s4"', 'id="s2"')
    slide4_new = slide4_new.replace('4 / 4', '2 / 4')
    # Slide 4의 상단에 제목 추가
    slide4_new = slide4_new.replace('<div class="slide" style="padding: 24px;">', '<div class="slide" style="padding: 24px;">\n  <h1 style="font-size: 26px; font-weight: 800; color: #1e293b; line-height: 1.2; margin-bottom: 24px; text-align: center;">GEO Agent Work Flow</h1>')
    
    # 기존 Slide 2는 Slide 3으로, Slide 3은 Slide 4로
    s2_new = s2_to_s3_html.replace('id="s2"', 'id="s3"').replace('2 / 4', '3 / 4').replace('id="s3"', 'id="s4"').replace('3 / 4', '4 / 4') # wait, replace might conflict. Let's do it safely.
    
    # better parsing for s2, s3
    part2 = html[html.find('<div class="slide-frame" id="s2">'):html.find('<div class="slide-frame" id="s3">')]
    part3 = html[html.find('<div class="slide-frame" id="s3">'):s4_start]
    
    part2_new = part2.replace('id="s2"', 'id="s3"').replace('2 / 4', '3 / 4')
    part3_new = part3.replace('id="s3"', 'id="s4"').replace('3 / 4', '4 / 4')
    
    # re-assemble
    final_html = html[:html.find('<div class="slide-frame" id="s2">')] + slide4_new + part2_new + part3_new
    
    # 맨 마지막 스크립트가 slide4 끝에 붙어있으므로 slide4_html 파싱 시 포함됨.
    # 단, part2_new와 part3_new 뒤에 스크립트가 와야 하므로 구조를 다시 잡아야 함.
    
    # Let's extract script part
    script_start = slide4_new.find('<!-- 슬라이드 자동 축소')
    if script_start != -1:
        script_part = slide4_new[script_start:]
        slide4_only = slide4_new[:script_start]
        final_html = html[:html.find('<div class="slide-frame" id="s2">')] + slide4_only + part2_new + part3_new + script_part
        
    with open('static/geo-agent-deck.html', 'w', encoding='utf-8') as f:
        f.write(final_html)

process_html()
