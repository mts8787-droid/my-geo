$ErrorActionPreference = 'Stop'
$urlsFile = "reports\lg_urls_list.csv"
$planFile = "reports\lg_scheduling_plan.csv"

Write-Host "1. Split URLs by country..."
$csvData = Import-Csv $urlsFile
$grouped = $csvData | Group-Object -Property "Country/Site"

foreach ($g in $grouped) {
    $c = $g.Name
    $cSafe = $c -replace '[^a-zA-Z0-9]', '_'
    $outFile = "reports\lg_urls_$cSafe.csv"
    $g.Group | Select-Object URL | Export-Csv -Path $outFile -NoTypeInformation -Encoding UTF8
}

Write-Host "2. Read audit_data.json and add groups/schedules..."
$auditData = @{ groups = @(); schedules = @() }
if (Test-Path "audit_data.json") {
    try {
        $content = Get-Content "audit_data.json" -Raw
        $auditData = $content | ConvertFrom-Json -AsHashtable
    } catch {}
}

if (-not $auditData.groups) { $auditData.groups = [System.Collections.ArrayList]::new() }
if (-not $auditData.schedules) { $auditData.schedules = [System.Collections.ArrayList]::new() }

$newGroups = [System.Collections.ArrayList]::new()
foreach ($g in $auditData.groups) { if ($g.name -notmatch "^LG Sitemap - ") { $newGroups.Add($g) } }
$auditData.groups = $newGroups

$newSchedules = [System.Collections.ArrayList]::new()
foreach ($s in $auditData.schedules) { if ($s.name -notmatch "^LG Sitemap - ") { $newSchedules.Add($s) } }
$auditData.schedules = $newSchedules

$timeHour = 1
$timeMinute = 0

$planData = Import-Csv $planFile
foreach ($row in $planData) {
    $c = $row."Country/Site"
    $count = [int]$row."Total URLs"
    $cSafe = $c -replace '[^a-zA-Z0-9]', '_'
    
    $groupId = "grp_lg_$cSafe"
    $csvPath = "reports/lg_urls_$cSafe.csv"
    
    $auditData.groups.Add(@{
        id = $groupId
        name = "LG Sitemap - $c"
        url_count = $count
        csv_file = $csvPath
        urls = @()
    })
    
    $scheduleId = "sch_lg_$cSafe"
    $timeStr = "{0:D2}:{1:D2}" -f $timeHour, $timeMinute
    
    $auditData.schedules.Add(@{
        id = $scheduleId
        name = "LG Sitemap - $c (일 1000건)"
        group_id = $groupId
        frequency = "daily"
        time = $timeStr
        enabled = $true
        chunk_size = 1000
        chunk_index = 0
    })
    
    $timeMinute += 10
    if ($timeMinute -ge 60) {
        $timeMinute = 0
        $timeHour += 1
        if ($timeHour -ge 24) { $timeHour = 0 }
    }
}

$auditData | ConvertTo-Json -Depth 10 | Set-Content "audit_data.json" -Encoding UTF8
Write-Host "Done!"
