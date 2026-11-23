param([string]$InstallDir = 'D:\kb-runtime\postgresql-18.7\pgsql')

$ErrorActionPreference = 'Stop'
$checkout = Split-Path -Parent $PSScriptRoot
$private = Join-Path $checkout '.local'
$data = Join-Path $private 'pgdata'
$log = Join-Path $private 'postgres.log'
$secret = Join-Path $private 'postgres-password.txt'
$bin = Join-Path $InstallDir 'bin'
if (-not (Test-Path -LiteralPath (Join-Path $bin 'pg_ctl.exe'))) { throw 'PostgreSQL Windows archive is not extracted.' }
New-Item -ItemType Directory -Path $private -Force | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
    if (-not (Test-Path -LiteralPath $secret)) {
        $bytes = [byte[]]::new(32)
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        [System.IO.File]::WriteAllText($secret, [Convert]::ToBase64String($bytes))
    }
    & (Join-Path $bin 'initdb.exe') -D $data -U kb_local '--auth=scram-sha-256' '--encoding=UTF8' '--locale=C' "--pwfile=$secret"
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL initialization failed.' }
    @"
listen_addresses = '127.0.0.1'
port = 15432
shared_buffers = '32MB'
max_connections = 15
"@ | Add-Content -LiteralPath (Join-Path $data 'postgresql.conf') -Encoding ascii
}
if (-not (Test-Path -LiteralPath $secret)) { throw 'Local PostgreSQL password file is missing.' }
& (Join-Path $bin 'pg_ctl.exe') -D $data status *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Output 'Local PostgreSQL is already running.'
    exit 0
}
Start-Process -FilePath (Join-Path $bin 'postgres.exe') -ArgumentList @('-D', ('"{0}"' -f $data)) -WorkingDirectory $InstallDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $private 'postgres.out') -RedirectStandardError $log
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    & (Join-Path $bin 'pg_isready.exe') -h 127.0.0.1 -p 15432 -q
    if ($LASTEXITCODE -eq 0) {
        Write-Output 'PostgreSQL is ready at 127.0.0.1:15432.'
        exit 0
    }
    Start-Sleep -Seconds 1
}
throw 'PostgreSQL did not become ready. See .local/postgres.log.'
