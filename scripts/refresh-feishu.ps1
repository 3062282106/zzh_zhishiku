param(
    [string]$PrivateDir = (Join-Path (Split-Path -Parent $PSScriptRoot) '.local')
)

$ErrorActionPreference = 'Stop'
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PrivateDir 'sources.json') | ConvertFrom-Json
foreach ($source in $manifest.sources | Where-Object platform -EQ 'feishu') {
    $sections = @()
    $revision = $null
    foreach ($anchor in $source.sections) {
        $raw = lark-cli docs +fetch --as user --doc $source.id --scope section --start-block-id $anchor --doc-format xml --detail with-ids | Out-String
        if ($LASTEXITCODE -ne 0) { throw "Feishu read failed for section $anchor." }
        $response = $raw | ConvertFrom-Json
        $document = $response.data.document
        if (-not $response.ok -or $document.document_id -ne $source.id -or -not $document.revision_id) {
            throw "Unexpected Feishu response for section $anchor."
        }
        [xml]$fragment = $document.content
        if ($fragment.DocumentElement.Name -ne 'fragment' -or $fragment.DocumentElement.GetAttribute('requested-start') -ne $anchor) {
            throw "Feishu did not return the requested section $anchor."
        }
        if ($null -ne $revision -and $document.revision_id -ne $revision) {
            throw 'Source changed while reading sections. Retry refresh.'
        }
        $revision = $document.revision_id
        $sections += @{ anchor = $anchor; xml = $document.content }
    }
    if ($sections.Count -eq 0) { throw 'No Feishu sections configured.' }
    $target = Join-Path $PrivateDir $source.file
    $temp = "$target.$([Guid]::NewGuid()).tmp"
    try {
        $snapshot = @{ id = $source.id; revision_id = $revision; sections = $sections }
        $snapshot | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath $temp
        [System.IO.File]::Move($temp, $target, $true)
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp }
    }
    Write-Output "Refreshed $($sections.Count) Feishu sections at revision $revision."
}
