# run this ONCE to set up automatic daily refreshing.
#
# right-click this file -> "Run with PowerShell", or open PowerShell in
# this folder and run:  .\setup_scheduled_task.ps1
#
# it registers a Windows Task Scheduler job that runs run_refresh.bat
# every day at 06:00.
#
# why daily and not weekly: the NOTAM feed itself is only regenerated
# once every 24 hours by NATS (its own header says ScheduleName=Daily) -
# so checking more often than that would gain nothing, but checking less
# often than that means you could be working from NOTAMs up to a week
# stale. daily is the sweet spot: as fresh as the source ever gets.
#
# the AIRAC zone data only actually changes every 28 days, but re-running
# it daily alongside the NOTAM check is harmless - if nothing's changed,
# the script just re-downloads the same file and skips the git push.

$batPath = Join-Path $PSScriptRoot "run_refresh.bat"

if (-not (Test-Path $batPath)) {
    Write-Host "ERROR: run_refresh.bat not found next to this script. Put them in the same folder." -ForegroundColor Red
    exit 1
}

# SCHEDULED_TASK=1 tells run_refresh.bat this is an unattended run, so it
# doesn't sit there waiting for a keypress that will never come
$argumentString = '/c set "SCHEDULED_TASK=1" && "' + $batPath + '"'

$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument $argumentString `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Daily -At 6am

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName "FRZ Tool Daily Data Refresh" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Refreshes frz_data.json, airstrip_data.json and notam_data.json, and pushes to GitHub Pages." `
    -Force

Write-Host ""
Write-Host "Done. The task 'FRZ Tool Daily Data Refresh' now runs every day at 06:00." -ForegroundColor Green
Write-Host "It will run even if you're logged off, as long as the computer is on."
Write-Host ""
Write-Host "To test it right now instead of waiting until tomorrow:"
Write-Host "  Start-ScheduledTask -TaskName 'FRZ Tool Daily Data Refresh'"
Write-Host "  (then check refresh_log.txt in this folder)"
Write-Host ""
Write-Host "To check when it last ran and whether it succeeded:"
Write-Host "  Get-ScheduledTaskInfo -TaskName 'FRZ Tool Daily Data Refresh'"
Write-Host ""
Write-Host "To remove it later:"
Write-Host "  Unregister-ScheduledTask -TaskName 'FRZ Tool Daily Data Refresh' -Confirm:`$false"
