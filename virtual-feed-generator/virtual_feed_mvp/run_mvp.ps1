$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = 'C:\Users\hs0902.chung\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

if (Test-Path -LiteralPath $bundledPython) {
    $pythonExe = $bundledPython
} else {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
}

Write-Host 'LG Virtual Feed MVP를 시작합니다.'
Write-Host '브라우저에서 http://127.0.0.1:8765 를 자동으로 엽니다.'
$browserJob = Start-Job -ScriptBlock {
    Start-Sleep -Seconds 1
    Start-Process 'http://127.0.0.1:8765'
}
try {
    & $pythonExe (Join-Path $appRoot 'app.py')
} finally {
    Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
}
