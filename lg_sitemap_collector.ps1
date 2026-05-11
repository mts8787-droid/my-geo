$ErrorActionPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$allUrls = [System.Collections.Concurrent.ConcurrentBag[string]]::new()
$visited = [System.Collections.Concurrent.ConcurrentDictionary[string, byte]]::new()

function Process-Sitemap {
    param([string]$url)
    if ($visited.ContainsKey($url)) { return }
    $visited[$url] = 1

    try {
        $req = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
        [xml]$xml = $req.Content

        if ($xml.sitemapindex) {
            foreach ($sm in $xml.sitemapindex.sitemap) {
                $subUrl = $sm.loc
                if ($subUrl) { Process-Sitemap -url $subUrl }
            }
        }
        elseif ($xml.urlset) {
            foreach ($u in $xml.urlset.url) {
                $loc = $u.loc
                if ($loc -and $loc -notmatch '\.xml$') {
                    $allUrls.Add($loc)
                }
            }
        }
    } catch {
        # Ignore errors
    }
}

Write-Host "1. Fetching sitemaps recursively (this may take a few minutes)..."
Process-Sitemap -url "https://www.lg.com/sitemap.xml"

Write-Host "2. Grouping URLs by country/site..."
$siteGroups = @{}
foreach ($u in $allUrls) {
    $country = "global"
    if ($u -match 'lg\.com/([^/]+)/') {
        $code = $Matches[1].ToLower()
        if ($code.Length -eq 2 -or $code.Length -eq 3) {
            $country = $code
        }
    }
    if (-not $siteGroups.ContainsKey($country)) {
        $siteGroups[$country] = [System.Collections.Generic.List[string]]::new()
    }
    $siteGroups[$country].Add($u)
}

Write-Host "Total URLs: $($allUrls.Count), grouped into $($siteGroups.Count) countries."

if (-not (Test-Path "reports")) {
    New-Item -ItemType Directory -Path "reports" | Out-Null
}

Write-Host "3. Generating reports/lg_urls_list.csv..."
$listFile = "reports\lg_urls_list.csv"
$csvData = [System.Collections.Generic.List[PSCustomObject]]::new()
foreach ($c in $siteGroups.Keys) {
    foreach ($u in $siteGroups[$c]) {
        $csvData.Add([PSCustomObject]@{
            "Country/Site" = $c
            "URL" = $u
        })
    }
}
$csvData | Export-Csv -Path $listFile -NoTypeInformation -Encoding UTF8

Write-Host "4. Generating reports/lg_scheduling_plan.csv..."
$planFile = "reports\lg_scheduling_plan.csv"
$planData = [System.Collections.Generic.List[PSCustomObject]]::new()
$scheduleDate = [DateTime]::Now

$sortedKeys = $siteGroups.Keys | Sort-Object { $siteGroups[$_].Count } -Descending

foreach ($c in $sortedKeys) {
    $count = $siteGroups[$c].Count
    $daysNeeded = [math]::Max(1, [math]::Ceiling($count / 1000.0))
    $endDate = $scheduleDate.AddDays($daysNeeded - 1)
    
    $planData.Add([PSCustomObject]@{
        "Country/Site" = $c
        "Total URLs" = $count
        "Days Required" = $daysNeeded
        "Suggested Start Date" = $scheduleDate.ToString('yyyy-MM-dd')
        "Suggested End Date" = $endDate.ToString('yyyy-MM-dd')
    })
    
    $scheduleDate = $endDate.AddDays(1)
}
$planData | Export-Csv -Path $planFile -NoTypeInformation -Encoding UTF8

Write-Host "Job completed successfully!"
