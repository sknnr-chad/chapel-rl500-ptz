#requires -RunAsAdministrator
<#
.SYNOPSIS
  Removes the pew-ptz scheduled task, firewall rule, and shortcut.
  Leaves the source tree and logs in place — delete the InstallDir manually
  if you want a clean slate.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = "C:\tools\pew-ptz",
  [string]$TaskUser   = "Chapel-AV",
  [int]   $Port       = 8080,
  [string]$TaskName   = "Pew PTZ Controller"
)
$ErrorActionPreference = "Continue"

Write-Host "==> Stopping + unregistering scheduled task" -ForegroundColor Cyan
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | ForEach-Object {
  try { Stop-ScheduledTask -TaskName $_.TaskName } catch { }
  Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
}

Write-Host "==> Removing firewall rule" -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName "$TaskName (TCP $Port)" -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule

Write-Host "==> Killing any running pythonw -m pew_ptz processes" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match "pew_ptz" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "==> Removing desktop shortcut" -ForegroundColor Cyan
$lnk = "C:\Users\$TaskUser\Desktop\Pew PTZ.url"
if (Test-Path $lnk) { Remove-Item $lnk -Force }

Write-Host ""
Write-Host "Done. Source tree left at $InstallDir (delete manually if desired)." -ForegroundColor Green
