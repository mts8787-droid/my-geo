$file = "d:\GEO-PJT\my-geo-audit\my-geo-audit\results\lg_urls_uk.ndjson"
$last_multiple = 0
$total = 24220

while ($true) {
    if (Test-Path $file) {
        $lines = 0
        try {
            $fs = New-Object System.IO.FileStream($file, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $sr = New-Object System.IO.StreamReader($fs)
            while ($sr.ReadLine() -ne $null) { $lines++ }
            $sr.Close()
            $fs.Close()
        } catch {
            # Ignore read errors
        }

        $current_multiple = [math]::Floor($lines / 2400)
        if ($current_multiple -gt $last_multiple) {
            $last_multiple = $current_multiple
            $percent = [math]::Round(($lines / $total) * 100, 1)
            $msg = "UK Audit Progress: $lines / $total 완료 ($percent%)"
            $wshell = New-Object -ComObject Wscript.Shell
            $wshell.Popup($msg, 10, "GEO Audit 알림", 64)
        }
        if ($lines -ge $total) {
            $wshell = New-Object -ComObject Wscript.Shell
            $wshell.Popup("UK Audit 분석 100% 완료! 빅쿼리 업로드가 곧 진행됩니다.", 20, "GEO Audit 알림", 64)
            break
        }
    }
    Start-Sleep -Seconds 60
}
