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

Start-Process "http://127.0.0.1:8765"
Set-Location $app
& $python -m wikilocal.cli --root $root serve --host 127.0.0.1 --port 8765
