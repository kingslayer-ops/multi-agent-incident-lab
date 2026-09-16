$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Run: python -m venv .venv --system-site-packages"
}

Push-Location $root
try {
    & $python -c "import psycopg, redis"
    if ($LASTEXITCODE -ne 0) {
        throw 'Missing drivers. Run: .\.venv\Scripts\python.exe -m pip install ".[production]"'
    }

    docker compose -f docker-compose.integration.yml up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "Integration services failed to start" }

    $env:INCIDENT_LAB_TEST_POSTGRES_URL = `
        "postgresql://incident_lab:incident_lab@127.0.0.1:55432/incident_lab_test"
    $env:INCIDENT_LAB_TEST_REDIS_URL = "redis://127.0.0.1:56379/0"
    & $python -m pytest tests/test_postgres_integration.py tests/test_redis_integration.py -vv
    if ($LASTEXITCODE -ne 0) { throw "Production runtime integration tests failed" }
}
finally {
    Remove-Item Env:INCIDENT_LAB_TEST_POSTGRES_URL -ErrorAction SilentlyContinue
    Remove-Item Env:INCIDENT_LAB_TEST_REDIS_URL -ErrorAction SilentlyContinue
    docker compose -f docker-compose.integration.yml down -v --remove-orphans
    Pop-Location
}
