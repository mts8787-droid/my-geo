$html = Get-Content -Path "static\geo-agent-deck.html" -Raw -Encoding UTF8

# 1. Nav pills
$oldNav = @"
  <div class="nav-pills">
    <a href="#s1" class="pill">1. 개발 계획 & Workflow</a>
    <a href="#s2" class="pill">2. 체크리스트 (1/2)</a>
    <a href="#s3" class="pill">3. 체크리스트 (2/2)</a>
    <a href="#s4" class="pill">4. Workflow 확대</a>
  </div>
"@
$newNav = @"
  <div class="nav-pills">
    <a href="#s1" class="pill">1. 개발 계획 (Road Map)</a>
    <a href="#s2" class="pill">2. Work Flow</a>
    <a href="#s3" class="pill">3. 체크리스트 (1/2)</a>
    <a href="#s4" class="pill">4. 체크리스트 (2/2)</a>
  </div>
"@
$html = $html.Replace($oldNav, $newNav)

# 2. Slide 1 (Remove tiny workflow, center Roadmap)
$startWF = $html.IndexOf("  <!-- Work Flow -->")
$endWF = $html.IndexOf("  <div class=`"slide-num`">1 / 4</div>", $startWF)
if ($startWF -gt 0 -and $endWF -gt $startWF) {
    $html = $html.Remove($startWF, $endWF - $startWF)
}
$html = $html.Replace("`n  <div class=`"divider-dashed`" style=`"top: 365px;`"></div>`n", "")

$html = $html.Replace('class="section-label" style="top: 130px;"', 'class="section-label" style="top: 250px;"')
$html = $html.Replace('class="timeline"`n', 'class="timeline" style="top: 285px;"`n')
$html = $html.Replace('style="left: calc(100px + (1192px) * 0.167);"', 'style="left: calc(100px + (1192px) * 0.167); top: 220px;"')
$html = $html.Replace('style="left: calc(100px + (1192px) * 0.500);"', 'style="left: calc(100px + (1192px) * 0.500); top: 220px;"')
$html = $html.Replace('style="left: calc(100px + (1192px) * 0.833);"', 'style="left: calc(100px + (1192px) * 0.833); top: 220px;"')
$html = $html.Replace('class="phase-body" style="left: calc(100px + (1192px) * 0.167);"', 'class="phase-body" style="left: calc(100px + (1192px) * 0.167); top: 320px;"')
$html = $html.Replace('class="phase-body" style="left: calc(100px + (1192px) * 0.500);"', 'class="phase-body" style="left: calc(100px + (1192px) * 0.500); top: 320px;"')
$html = $html.Replace('class="phase-body" style="left: calc(100px + (1192px) * 0.833);"', 'class="phase-body" style="left: calc(100px + (1192px) * 0.833); top: 320px;"')

# CSS class "timeline" update
$html = $html.Replace("top: 165px;", "top: 285px;")

# 3. Rename Slide 2 to Slide 3, Slide 3 to Slide 4
$s2Idx = $html.IndexOf("<!-- ═════ SLIDE 2")
$s3Idx = $html.IndexOf("<!-- ═════ SLIDE 3")
$s4Idx = $html.IndexOf("<!-- ═════ SLIDE 4")

$part1 = $html.Substring(0, $s2Idx)
$part23 = $html.Substring($s2Idx, $s4Idx - $s2Idx)
$part4 = $html.Substring($s4Idx)

# Change IDs and Slide numbers in part23
$part23 = $part23.Replace('id="s3"', 'id="s4"')
$part23 = $part23.Replace('3 / 4', '4 / 4')
$part23 = $part23.Replace('id="s2"', 'id="s3"')
$part23 = $part23.Replace('2 / 4', '3 / 4')
$part23 = $part23.Replace('<!-- ═════ SLIDE 3', '<!-- ═════ SLIDE 4')
$part23 = $part23.Replace('<!-- ═════ SLIDE 2', '<!-- ═════ SLIDE 3')

# Make part4 the new Slide 2
$part4 = $part4.Replace('id="s4"', 'id="s2"')
$part4 = $part4.Replace('4 / 4', '2 / 4')
$part4 = $part4.Replace('<!-- ═════ SLIDE 4 — Workflow 확대 ═════════════════════════════════════ -->', '<!-- ═════ SLIDE 2 — Workflow ════════════════════════════════════════════ -->')
$part4 = $part4.Replace('<div class="slide" style="padding: 24px;">', "<div class=`"slide`" style=`"padding: 24px;`">`n  <h1 style=`"font-size: 26px; font-weight: 800; color: #1e293b; line-height: 1.2; margin-bottom: 24px; text-align: center;`">GEO Agent Work Flow</h1>")

# Reassemble
$newHtml = $part1 + $part4 + $part23

# We need to make sure the script tag is preserved at the end properly.
# The script tag is inside part4! We must move it back to the end.
$scriptIdx = $newHtml.IndexOf("<!-- 슬라이드 자동 축소")
if ($scriptIdx -gt 0) {
    $scriptTag = $newHtml.Substring($scriptIdx)
    $newHtml = $newHtml.Remove($scriptIdx) + $scriptTag
}

Set-Content -Path "static\geo-agent-deck.html" -Value $newHtml -Encoding UTF8
