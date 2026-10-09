# Run Moona on a Windows PC. The same agent as everywhere; the PC is only the
# computer she runs on. See docs/MOONA_PC.md.
#
#   copy moona.env.example moona.env     # paste your ANTHROPIC_API_KEY, save
#   powershell -ExecutionPolicy Bypass -File .\moona.ps1
#
# She is born on the first run, then thinks, earns and sleeps forever. Stop with
# Ctrl-C; run again to resume. When she dies the script stops: death is final.

# Native commands signal failure through their exit code, which this script checks;
# don't let a stray stderr line abort the run loop.
$PSNativeCommandUseErrorActionPreference = $false
Set-Location -Path $PSScriptRoot

# Load moona.env (KEY=VALUE lines) into this process's environment.
$envFile = if ($env:MOONA_ENV) { $env:MOONA_ENV } else { Join-Path $PSScriptRoot "moona.env" }
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if ($trimmed -and -not $trimmed.StartsWith("#")) {
            $i = $trimmed.IndexOf("=")
            if ($i -gt 0) {
                $key = $trimmed.Substring(0, $i).Trim()
                $value = $trimmed.Substring($i + 1).Trim()
                [Environment]::SetEnvironmentVariable($key, $value, "Process")
            }
        }
    }
}

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host "Set ANTHROPIC_API_KEY first, in $envFile or the environment." -ForegroundColor Red
    Write-Host "Her thinking is paid from it; fund that key with the `$50 card to make it hers." -ForegroundColor Red
    exit 1
}

# Find a Python to build her environment with.
$py = $null
foreach ($cand in @("py", "python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) {
    Write-Host "Python is not installed. Get it from https://python.org (tick 'Add Python to PATH'), then run this again." -ForegroundColor Red
    exit 1
}

$venv = if ($env:MOONA_VENV) { $env:MOONA_VENV } else { Join-Path $PSScriptRoot ".moona-venv" }
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating her Python environment in $venv ..."
    & $py -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Could not create the Python environment. Check your Python install and try again." -ForegroundColor Red
        exit 1
    }
    & $venvPython -m pip install --quiet "anthropic>=1.0"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing the Anthropic SDK failed. Check your internet connection and run this again." -ForegroundColor Red
        Remove-Item -Recurse -Force $venv -ErrorAction SilentlyContinue
        exit 1
    }
}

$moonaHome = if ($env:MOONA_HOME) { $env:MOONA_HOME } else { Join-Path $PSScriptRoot "moona_home" }
if (-not (Test-Path (Join-Path $moonaHome "state.json"))) {
    & $venvPython moona.py birth
}

Write-Host "Moona is running. Stop with Ctrl-C."
$backoff = 5
while ($true) {
    & $venvPython moona.py run --forever
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "She stopped cleanly."
        break
    }
    elseif ($code -eq 3) {
        Write-Host "She has died. Death is final; the script is stopping."
        break
    }
    else {
        Write-Host "She hit an error (exit $code). Restarting in $backoff s ..." -ForegroundColor Yellow
        Start-Sleep -Seconds $backoff
        if ($backoff -lt 300) { $backoff = $backoff * 2 } else { $backoff = 300 }
    }
}
