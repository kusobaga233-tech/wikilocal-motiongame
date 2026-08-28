$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$app = Join-Path $root "app"
$python = Join-Path $app ".venv\Scripts\python.exe"
$env:OLLAMA_MODELS = Join-Path $root "models"

if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first." }
$ollama = Get-Command ollama -ErrorAction Stop
try {
  Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "http://127.0.0.1:11434/api/tags" | Out-Null
} catch {
  Start-Process -FilePath $ollama.Source -ArgumentList "serve" -WindowStyle Hidden
}

$server = Start-Process -FilePath $python -ArgumentList @(
  "-m", "wikilocal.cli", "--root", "`"$root`"", "serve", "--host", "127.0.0.1", "--port", "8765"
) -WindowStyle Hidden -PassThru
$healthUrl = "http://127.0.0.1:8765/api/health"
$deadline = (Get-Date).AddSeconds(20)
$ready = $false

while ((Get-Date) -lt $deadline -and -not $ready) {
  try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 $healthUrl | Out-Null
    $ready = $true
  } catch {
    Start-Sleep -Milliseconds 250
  }
}

if (-not $ready) {
  if (-not $server.HasExited) {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  }
  Write-Error "WikiLocal did not become ready at $healthUrl within 20 seconds."
  exit 1
}

Start-Process -FilePath "http://127.0.0.1:8765"
