$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$app = Join-Path $root "app"
$models = Join-Path $root "models"
$python = Join-Path $app ".venv\Scripts\python.exe"

$lark = Get-Command lark-cli -ErrorAction Stop
& $lark.Source auth status --verify
if ($LASTEXITCODE -ne 0) { throw "lark-cli user authorization verification failed." }

if (-not (Test-Path $python)) {
  py -3.12 -m venv (Join-Path $app ".venv")
}

& $python -m pip install --upgrade pip
& $python -m pip install -e ".[test,vector]"

New-Item -ItemType Directory -Force -Path $models | Out-Null
$env:OLLAMA_MODELS = $models
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $models, "User")

$ollama = Get-Command ollama -ErrorAction Stop
& $ollama.Source pull qwen3:4b
& $ollama.Source pull bge-m3
& $ollama.Source pull bge-reranker-v2-m3
