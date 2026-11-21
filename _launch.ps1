# netforce crawler launcher: discover Python at runtime, no hardcoded drive/path.
# Called by run_crawler.bat via `powershell -File`. Runs hidden (SYSTEM task),
# works after drive changes, folder moves, or account switches.
param([string]$WorkDir = $PSScriptRoot)
if (-not $WorkDir) { $WorkDir = $PSScriptRoot }
Set-Location -LiteralPath $WorkDir
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$log = Join-Path $WorkDir '_crawler.log'
function Write-Log([string]$m) {
    Add-Content -LiteralPath $log -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8
}

# ---- atomic single-instance lock ----
# Two launchers can both see "no crawler" then both start one. A file opened
# exclusively serializes them: only the owner proceeds, the loser exits.
$lockPath = Join-Path $WorkDir '_kb\crawler.launch.lock'
try {
    $lockFile = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'Write', 'None')
} catch {
    Write-Log 'another launcher holds the lock, exit'
    exit 0
}
try {
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*resource_finder*' }
    if ($running) { exit 0 }

    # ---- discover Python at runtime: each candidate must pass `import sys` ----
    function Test-Py([string]$exe) {
        if (-not $exe -or -not (Test-Path -LiteralPath $exe -ErrorAction SilentlyContinue)) { return $false }
        try {
            & $exe -c 'import sys' 2>$null | Out-Null
            return ($LASTEXITCODE -eq 0)
        } catch { return $false }
    }

    $good = [System.Collections.Generic.List[string]]::new()
    $last = [System.Collections.Generic.List[string]]::new()
    function Add-Py([string]$p, [bool]$preferred = $true) {
        if (-not $p) { return }
        if ($preferred) {
            if (-not $good.Contains($p)) { $good.Add($p) }
        } else {
            if (-not $last.Contains($p)) { $last.Add($p) }
        }
    }

    # 1) explicit env override (highest)
    if ($env:NETFORCE_PYTHON) { Add-Py $env:NETFORCE_PYTHON }

    # 2) PATH python (Store stub is auto-rejected by Test-Py)
    foreach ($n in @('python.exe','python3.exe','python')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { Add-Py $c.Source }
    }

    # 3) uv managed python under user dirs (standalone, clean deps -> preferred)
    foreach ($base in @($env:APPDATA, $env:LOCALAPPDATA,
                       [Environment]::GetFolderPath('UserProfile'))) {
        if (-not $base) { continue }
        $uv = Join-Path $base 'uv\python'
        if (Test-Path -LiteralPath $uv) {
            Get-ChildItem -LiteralPath $uv -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Add-Py (Join-Path $_.FullName 'python.exe') }
        }
    }

    # 4) project-local venv (moves with project)
    foreach ($rel in @('.venv\Scripts\python.exe','venv\Scripts\python.exe')) {
        Add-Py (Join-Path $WorkDir $rel)
    }

    # 5) paths of running python processes (cross-account fallback, but often
    #    short-lived MCP venvs -> tried only after the standalone ones)
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { if ($_.ExecutablePath) { Add-Py $_.ExecutablePath $false } }

    # pick first that actually runs: preferred pool first, then fallback pool
    $py = $null
    foreach ($c in $good) { if (Test-Py $c) { $py = $c; break } }
    if (-not $py) { foreach ($c in $last) { if (Test-Py $c) { $py = $c; break } } }
    if (-not $py) {
        Write-Log 'NO PYTHON FOUND (set NETFORCE_PYTHON to specify one)'
        exit 1
    }

    # prefer pythonw (no console window)
    $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
    if (Test-Path -LiteralPath $pyw) { $py = $pyw }

    Write-Log ("launch crawler python={0}" -f $py)
    $target = Join-Path $WorkDir 'resource_finder.py'
    Start-Process -FilePath $py -ArgumentList @('-u', $target) `
        -WorkingDirectory $WorkDir -WindowStyle Hidden | Out-Null
} finally {
    $lockFile.Close()
    $lockFile.Dispose()
}
