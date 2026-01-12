param (
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,

    [Parameter(Mandatory = $true)]
    [string]$EmailSystemDir
)

# Resolve host absolute path (portable)
$ROOT = (Get-Location).Path

# Sanity checks
if (-not (Test-Path $ProjectDir)) {
    throw "Project directory '$ProjectDir' not found relative to $ROOT"
}

if (-not (Test-Path "${ProjectDir}/${EmailSystemDir}")) {
    throw "Email system directory '$EmailSystemDir' not found relative to $ROOT"
}

docker run --rm `
  --entrypoint sh `
  -v "${ROOT}:/usr/src" `
  -w /usr/src `
  abslang/absc:latest `
  -c "absc --erlang abs/$ProjectDir/*.abs abs/$ProjectDir/$EmailSystemDir/*.abs abs/$ProjectDir/$EmailSystemDir/orchestrations/*.abs"
