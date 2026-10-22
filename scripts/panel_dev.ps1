<#
.SYNOPSIS
  Point Premiere's CEP extension folder of Premiere Deck at the source panel (docs/PANEL.md 4.5).
.DESCRIPTION
  Replaces <extensions>\com.softoblivion.premieredeck with a directory junction to src\premiere_deck\panel and
  writes the panel's .debug file (Chrome DevTools: http://localhost:8098 for the panel, 8099 for the agent).
  The host never installs over a junction, so edits in the source show after a panel reload (manifest edits after
  a Premiere restart). PlayerDebugMode must be on (it is on the development machine).
  <extensions> is %APPDATA%\Adobe\CEP\extensions, or PREMIERE_DECK_CEP_DIR when set.
  An installed (copied) panel folder is moved aside to <name>.dev-backup, not deleted.
  An existing .debug file is kept (a developer may have chosen other ports).
.PARAMETER Remove
  Remove the junction instead (the .debug file stays); the next host start installs the bundled panel again.
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Root "src\premiere_deck\panel"
$Extensions = if ($env:PREMIERE_DECK_CEP_DIR) { $env:PREMIERE_DECK_CEP_DIR } else { Join-Path $env:APPDATA "Adobe\CEP\extensions" }
$Target = Join-Path $Extensions "com.softoblivion.premieredeck"
$DebugFile = Join-Path $Source ".debug"

function Test-Junction([string]$Path) {
    if (-not (Test-Path $Path)) { return $false }
    $item = Get-Item -Force $Path
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

if ($Remove) {
    if (Test-Junction $Target) {
        cmd /c rmdir "$Target" | Out-Null  # removes the link only, never the source files
        Write-Host "Removed the junction $Target"
    }
    return
}

if (-not (Test-Path (Join-Path $Source "CSXS\manifest.xml"))) { throw "Panel source not found: $Source" }
New-Item -ItemType Directory -Force -Path $Extensions | Out-Null
if (Test-Junction $Target) {
    cmd /c rmdir "$Target" | Out-Null
} elseif (Test-Path $Target) {
    $Backup = "$Target.dev-backup"
    if (Test-Path $Backup) { Remove-Item -Recurse -Force $Backup }
    Move-Item $Target $Backup
    Write-Host "Moved the installed panel to $Backup"
}
cmd /c mklink /J "$Target" "$Source" | Out-Null
if (-not (Test-Junction $Target)) { throw "Could not create the junction $Target" }

$Debug = @"
<?xml version="1.0" encoding="UTF-8"?>
<ExtensionList>
  <Extension Id="com.softoblivion.premieredeck.panel">
    <HostList><Host Name="PPRO" Port="8098"/></HostList>
  </Extension>
  <Extension Id="com.softoblivion.premieredeck.agent">
    <HostList><Host Name="PPRO" Port="8099"/></HostList>
  </Extension>
</ExtensionList>
"@
if (Test-Path $DebugFile) {
    Write-Host "Keeping the existing $DebugFile"
} else {
    [IO.File]::WriteAllText($DebugFile, $Debug, (New-Object System.Text.UTF8Encoding($false)))
}
Write-Host "Junction $Target -> $Source"
Write-Host "DevTools: http://localhost:8098 (panel), http://localhost:8099 (agent). Restart Premiere after manifest changes."
