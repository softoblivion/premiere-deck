<#
.SYNOPSIS
  Sign the prepared CEP panel folder with ZXPSignCmd and unpack the signed package (docs/BUILD.md 4).
.DESCRIPTION
  Runs `ZXPSignCmd -sign <PanelDir> <zxp> <p12> <password>`, verifies the package and unpacks it into OutDir, so the
  folder the exe bundles carries META-INF/signatures.xml. The certificate path and password come from the
  environment (DECK_SIGN_P12, DECK_SIGN_PASSWORD); ZXPSignCmd from DECK_ZXPSIGNCMD or D:\Adobe\plugins\ZXP_Signer.
  Create a self-signed certificate once, outside the repository:
    ZXPSignCmd -selfSignedCert US CA softoblivion "Premiere Deck" <password> <path>\deck-cert.p12
  Exit code 0 = signed, 2 = no certificate configured (nothing done), 1 = failure.
.PARAMETER PanelDir
  The prepared panel folder (VERSION and manifest already generated).
.PARAMETER OutDir
  Folder that receives the signed panel (replaced).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PanelDir,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = "Stop"

$P12 = $env:DECK_SIGN_P12
$Password = $env:DECK_SIGN_PASSWORD
if (-not $P12 -or -not $Password) {
    Write-Warning "DECK_SIGN_P12 / DECK_SIGN_PASSWORD are not set: the panel stays unsigned (it then loads only with PlayerDebugMode)."
    exit 2
}
if (-not (Test-Path $P12)) { Write-Error "Certificate not found: $P12"; exit 1 }

$Candidates = @()
if ($env:DECK_ZXPSIGNCMD) { $Candidates += $env:DECK_ZXPSIGNCMD }
$Candidates += "D:\Adobe\plugins\ZXP_Signer\ZXPSignCmd.exe"
$Signer = $Candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Signer) { Write-Error "ZXPSignCmd.exe not found (set DECK_ZXPSIGNCMD)."; exit 1 }

$Work = Split-Path -Parent $OutDir
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Zxp = Join-Path $Work "premiere-deck-panel.zxp"
if (Test-Path $Zxp) { Remove-Item -Force $Zxp }

function Invoke-Native {
    param([string]$What, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Signer @Arguments | ForEach-Object { Write-Host "  $_" } } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

Write-Host "Signing the panel with $Signer ..."
Invoke-Native "ZXPSignCmd -sign" @("-sign", $PanelDir, $Zxp, $P12, $Password)
Invoke-Native "ZXPSignCmd -verify" @("-verify", $Zxp)

if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($Zxp, $OutDir)
if (-not (Test-Path (Join-Path $OutDir "META-INF\signatures.xml"))) { Write-Error "The signed package has no META-INF\signatures.xml"; exit 1 }
Write-Host "Signed panel in $OutDir"
exit 0
