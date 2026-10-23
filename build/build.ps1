<#
.SYNOPSIS
  Build release\PremiereDeck.exe (one-file, windowed) and release\SHA256SUMS.
.DESCRIPTION
  1. Creates / refreshes .venv and installs the requirements.
  2. Runs the unit tests (scratch PREMIERE_DECK_HOME and PREMIERE_DECK_CEP_DIR) unless -SkipTests.
  3. Makes build\icon.ico and renders src\premiere_deck\ui\themes.json into the generated theme CSS
     (build\gen_themes.py: the kit copy's css\themes.css and the token block of panel\css\panel.css).
  4. Generates the panel's VERSION and the manifest versions from __version__, copies the panel to
     build\work\panel_bundle (without deck.json / .debug) and signs it with build\sign_panel.ps1 when
     DECK_SIGN_P12 and DECK_SIGN_PASSWORD are set (else a warning: unsigned panel).
  5. Runs PyInstaller with build\PremiereDeck.spec, copies the exe to release\ and writes release\SHA256SUMS.
  Details: docs\BUILD.md.
.PARAMETER SkipTests
  Do not run the unit tests.
.PARAMETER SkipBuild
  Only prepare .venv.
.PARAMETER PythonVersion
  Python version for the `py` launcher when .venv has to be created (default 3.14).
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [string]$PythonVersion = "3.14"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    # Windows PowerShell 5.1 turns stderr text of native tools into errors when output is redirected; only the exit
    # code decides here.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

# 1. virtual environment ---------------------------------------------------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating .venv with Python $PythonVersion ..."
    Invoke-Checked "venv creation" { py "-$PythonVersion" -m venv (Join-Path $Root ".venv") }
}
Write-Host "Installing requirements ..."
Invoke-Checked "pip install" { & $VenvPython -m pip install --quiet -r requirements.txt -r requirements-dev.txt }
& $VenvPython --version
& $VenvPython -m pip list --format=freeze |
    Select-String -Pattern "^(pywebview|pythonnet|websockets|watchdog|pillow|pyinstaller)=="
if ($SkipBuild) { Write-Host "Environment ready (-SkipBuild)."; return }

$Version = (Select-String -Path (Join-Path $Root "src\premiere_deck\__init__.py") -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Premiere Deck $Version"

# 2. tests -----------------------------------------------------------------------------------------------------------
if (-not $SkipTests) {
    $Scratch = Join-Path ([IO.Path]::GetTempPath()) ("premiere_deck_tests_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $Scratch | Out-Null
    $env:PREMIERE_DECK_HOME = $Scratch
    $env:PREMIERE_DECK_CEP_DIR = Join-Path $Scratch "cep\extensions"
    $env:PYTHONPATH = Join-Path $Root "src"
    try {
        Write-Host "Running unit tests ..."
        Invoke-Checked "unit tests" { & $VenvPython -m unittest discover -s tests -t . }
    }
    finally {
        Remove-Item Env:\PREMIERE_DECK_HOME -ErrorAction SilentlyContinue
        Remove-Item Env:\PREMIERE_DECK_CEP_DIR -ErrorAction SilentlyContinue
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $Scratch -ErrorAction SilentlyContinue
    }
}

# 3. icon ------------------------------------------------------------------------------------------------------------
Invoke-Checked "icon" { & $VenvPython build\make_icon.py }

# 3b. themes: ui\themes.json -> the kit copy's css\themes.css and the token block of panel\css\panel.css ------------
Invoke-Checked "theme generator" { & $VenvPython build\gen_themes.py }

# 4. panel: version, bundle copy, signature --------------------------------------------------------------------------
$PanelSrc = Join-Path $Root "src\premiere_deck\panel"
$Manifest = Join-Path $PanelSrc "CSXS\manifest.xml"
if (-not (Test-Path $Manifest)) { throw "Panel manifest not found: $Manifest" }
[IO.File]::WriteAllText((Join-Path $PanelSrc "VERSION"), $Version, $Utf8)
$Xml = [IO.File]::ReadAllText($Manifest, $Utf8)
$Xml = [regex]::Replace($Xml, 'ExtensionBundleVersion="[^"]*"', "ExtensionBundleVersion=""$Version""")
$Xml = [regex]::Replace($Xml, '(<Extension\s+Id="[^"]+"\s+Version=")[^"]*(")', "`${1}$Version`${2}")
[IO.File]::WriteAllText($Manifest, $Xml, $Utf8)
Write-Host "Panel VERSION and manifest set to $Version"

$Work = Join-Path $Root "build\work"
$Bundle = Join-Path $Work "panel_bundle"
$Plain = Join-Path $Work "panel_plain"
New-Item -ItemType Directory -Force -Path $Work | Out-Null
foreach ($dir in @($Bundle, $Plain)) { if (Test-Path $dir) { Remove-Item -Recurse -Force $dir } }
Copy-Item -Recurse -Path $PanelSrc -Destination $Plain
foreach ($name in @("deck.json", ".debug")) {
    $file = Join-Path $Plain $name
    if (Test-Path $file) { Remove-Item -Force $file }
}
Get-ChildItem -Path $Plain -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sign_panel.ps1") -PanelDir $Plain -OutDir $Bundle
$SignCode = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($SignCode -eq 2) {
    Write-Warning "Building with an UNSIGNED panel."
    Copy-Item -Recurse -Path $Plain -Destination $Bundle
}
elseif ($SignCode -ne 0) { throw "Panel signing failed (exit $SignCode)" }
$env:DECK_PANEL_BUNDLE = $Bundle

# 5. PyInstaller -----------------------------------------------------------------------------------------------------
Write-Host "Running PyInstaller (one-file) ..."
try {
    Invoke-Checked "PyInstaller" {
        & $VenvPython -m PyInstaller --noconfirm --clean `
            --distpath (Join-Path $Root "build\dist") --workpath (Join-Path $Root "build\work\pyinstaller") `
            (Join-Path $Root "build\PremiereDeck.spec")
    }
}
finally { Remove-Item Env:\DECK_PANEL_BUNDLE -ErrorAction SilentlyContinue }

$Built = Join-Path $Root "build\dist\PremiereDeck.exe"
if (-not (Test-Path $Built)) { throw "PyInstaller did not produce $Built" }
$ReleaseDir = Join-Path $Root "release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
$Release = Join-Path $ReleaseDir "PremiereDeck.exe"
Copy-Item -Force $Built $Release
$SizeMb = [math]::Round((Get-Item $Release).Length / 1MB, 1)
Write-Host "Built $Release ($SizeMb MB)"

# The self-updater verifies the downloaded exe against the "SHA256SUMS" release asset: "<sha256>  PremiereDeck.exe".
$Hash = (Get-FileHash -Algorithm SHA256 -Path $Release).Hash.ToLowerInvariant()
$SumsLine = "$Hash  PremiereDeck.exe"
[IO.File]::WriteAllText((Join-Path $ReleaseDir "SHA256SUMS"), "$SumsLine`n", $Utf8)
Write-Host "Wrote $(Join-Path $ReleaseDir 'SHA256SUMS')"
Write-Host $SumsLine
