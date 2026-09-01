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

Set-Location $app
& $python -m pip install --upgrade pip
& $python -m pip install -e ".[test,vector]"

$permissionPreflight = @'
from wikilocal.feishu import FeishuClient, FeishuClientError
import sys

try:
    result = FeishuClient().permission_preflight()
except FeishuClientError as error:
    print(f"Feishu permission preflight failed: {error}", file=sys.stderr)
    sys.exit(1)

if result.missing_scopes:
    print("Feishu is missing required read-only scopes:", file=sys.stderr)
    for scope in result.missing_scopes:
        print(f"  {scope}", file=sys.stderr)
    print("Remediate with:", file=sys.stderr)
    for command in result.remediation_commands:
        print(f"  {command}", file=sys.stderr)
    sys.exit(1)
'@
& $python -c $permissionPreflight
if ($LASTEXITCODE -ne 0) { throw "Feishu permission preflight failed. See missing scopes and remediation above." }

New-Item -ItemType Directory -Force -Path $models | Out-Null
$env:OLLAMA_MODELS = $models
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $models, "User")

$ollama = Get-Command ollama -ErrorAction Stop
& $ollama.Source pull qwen3:4b
& $ollama.Source pull bge-m3
& $ollama.Source pull bge-reranker-v2-m3
