# m365bak 자동 실행 등록(현재 사용자, 로그온 상태에서만, 관리자 권한 불필요)
# - 매시간 + 로그온 3분 후 실행, 핫 카피 모드(앱을 닫지 않음)
# - 자격증명을 저장하지 않는다(Interactive 로그온 유형).
param(
    [int]$IntervalHours = 1,
    [string]$TaskName = 'M365 Local Backup'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "venv가 없습니다: $pythonw" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m m365bak run --mode hot' -WorkingDirectory $root
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(7) -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$logon.Delay = 'PT3M'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($repeat, $logon) -Settings $settings `
    -Principal $principal -Description 'Teams/Outlook 로컬 캐시를 M365-Archive에 누적 백업 (m365bak)' -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime, LastRunTime, LastTaskResult
