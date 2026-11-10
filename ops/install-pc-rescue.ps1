$ErrorActionPreference = 'Stop'
$script = (Resolve-Path (Join-Path $PSScriptRoot 'pc-rescue.ps1')).Path
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"'
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $PSScriptRoot
$triggers = @('08:44','08:49','08:54','09:05','09:15') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
$existing = Get-ScheduledTask -TaskName 'GitHubInfobot-PC-Backup' -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -notlike 'Owner-approved Github-infobot*') { throw 'Task name is already used by another task; refusing to replace it.' }
Register-ScheduledTask -TaskName 'GitHubInfobot-PC-Backup' -Description 'Owner-approved Github-infobot preparation backup and missed-delivery checks. Requires this Windows user logged in and online; never exports credentials.' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName 'GitHubInfobot-PC-Backup' | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName 'GitHubInfobot-PC-Backup' | Select-Object NextRunTime,LastTaskResult
