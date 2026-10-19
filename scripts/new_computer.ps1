# Set Iriis up on a new (or replacement) Windows computer in one go.
#
#   powershell -ExecutionPolicy Bypass -File scripts\new_computer.ps1
#   (or, before cloning: download this file and run it; it clones the repo itself)
#
# Nothing personal lives on the computer: memories, notes, reminders, tasks, money records, conversations and the
# Google sign-in are in the cloud (Railway Postgres); Seryn is on Railway too. This script only installs the
# program, then asks for three values you copy from the iriis-voice service's Variables on Railway:
# IRIIS_SERVER_URL (the service's public URL), IRIIS_API_KEY and IRIIS_DESKTOP_TOKEN.
# The one thing that does stay per-computer is her browser profile: sign in to websites again inside her window.

param(
    [string]$Dir = "$HOME\rift\iriis-voice",
    [string]$Repo = "https://github.com/amschel99/Iriis-voice.git",
    [switch]$NoAutostart
)
$ErrorActionPreference = "Stop"

function Need($cmd, $wingetId, $what) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    Write-Host "Installing $what (winget $wingetId)..." -ForegroundColor Yellow
    winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$what installed, but '$cmd' isn't on PATH yet: open a new terminal and run this again." }
}

Need git "Git.Git" "Git"
Need python "Python.Python.3.12" "Python 3.12"
Need node "OpenJS.NodeJS.LTS" "Node.js (for her browser)"
if (-not (Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe") -and
    -not (Test-Path "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") -and
    -not (Test-Path "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")) {
    Write-Host "Installing Google Chrome (her browser)..." -ForegroundColor Yellow
    winget install --id Google.Chrome -e --accept-source-agreements --accept-package-agreements
}

if (-not (Test-Path "$Dir\.git")) {
    New-Item -ItemType Directory -Force (Split-Path $Dir) | Out-Null
    git clone $Repo $Dir
}
Set-Location $Dir
git pull --ff-only

if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\pip install -r requirements.txt -q
.venv\Scripts\python scripts\download_models.py --skip-ollama

# The three values from Railway (iriis-voice service -> Variables). Saved to .env, which git ignores.
.venv\Scripts\python -m iriis.cli setup

# Put `iriis` on PATH for this user.
$bin = "$Dir\bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") { [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User") }

if (-not $NoAutostart) {
    # Start the desktop agent at every login (it reads .env itself).
    $act = New-ScheduledTaskAction -Execute "$Dir\bin\iriis.cmd" -Argument "start" -WorkingDirectory $Dir
    Register-ScheduledTask -TaskName "Iriis" -Action $act -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Limited -Force | Out-Null
    Write-Host "Iriis will start when you log in." -ForegroundColor Green
}

& "$Dir\bin\iriis.cmd" start
& "$Dir\bin\iriis.cmd" status
Write-Host ""
Write-Host "Done. Your memories, notes, tasks, money records and Google connection came back from the cloud." -ForegroundColor Green
Write-Host "Sign in to websites again inside her browser window when she opens it (and scan WhatsApp Web's QR once)."
