param([string]$InstallDir = 'D:\kb-runtime\opensearch-3.10.0')

$ErrorActionPreference = 'Stop'
$checkout = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $checkout '.local'
$yaml = Join-Path $InstallDir 'config\opensearch.yml'
$launcher = Join-Path $InstallDir 'bin\opensearch.bat'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'OpenSearch Windows archive is not extracted.' }
$managed = '# Managed by the knowledge base local development launcher.'
$current = Get-Content -LiteralPath $yaml -Raw
if ($current -notmatch [regex]::Escape($managed)) {
    $active = $current -split '\r?\n' | Where-Object { $_ -match '^\s*[^#\s]' }
    if ($active) { throw 'OpenSearch already has active configuration. Use a fresh extraction for this project.' }
}
@"
$managed
cluster.name: kb-local
network.host: 127.0.0.1
http.port: 19200
transport.port: 19300
discovery.type: single-node
plugins.security.disabled: true
"@ | Set-Content -LiteralPath $yaml -Encoding ascii

New-Item -ItemType Directory -Path $logs -Force | Out-Null
$env:OPENSEARCH_JAVA_HOME = Join-Path $InstallDir 'jdk'
$env:OPENSEARCH_JAVA_OPTS = '-Xms256m -Xmx256m'
Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d', '/c', ('"{0}"' -f $launcher)) -WorkingDirectory $InstallDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'opensearch.out') -RedirectStandardError (Join-Path $logs 'opensearch.err')
Write-Output 'OpenSearch launch requested at http://127.0.0.1:19200.'
