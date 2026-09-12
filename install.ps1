# Install long-to-short as a Claude Code skill.
#
# Copies this directory to ~/.claude/skills/long-to-short, which is where
# Claude Code looks for personal skills. Override with $env:SKILLS_DIR.
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = if ($env:SKILLS_DIR) { $env:SKILLS_DIR } else { Join-Path $HOME '.claude\skills' }
$dest = Join-Path $root 'long-to-short'

if ($here -eq $dest) {
    Write-Host "already installed at $dest"
    exit 0
}

if (Test-Path $dest) {
    $reply = Read-Host "$dest exists. Overwrite? [y/N]"
    if ($reply -notmatch '^[yY]') { Write-Host 'aborted'; exit 1 }
    Remove-Item -Recurse -Force $dest
}

New-Item -ItemType Directory -Force -Path $root | Out-Null
Copy-Item -Recurse -Force $here $dest
foreach ($junk in @('.git', '__pycache__', 'tools\__pycache__')) {
    $p = Join-Path $dest $junk
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

Write-Host "installed -> $dest"
Write-Host ''
Write-Host 'Check your dependencies:'

foreach ($exe in @('ffmpeg', 'ffprobe')) {
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        Write-Host "  $exe  ok"
    } else {
        Write-Host "  $exe  MISSING -- put it on PATH"
    }
}

python -c "import numpy, PIL" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host '  numpy+Pillow  ok'
} else {
    Write-Host '  numpy+Pillow  MISSING -- pip install -r requirements.txt'
}
