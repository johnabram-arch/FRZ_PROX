@echo off
REM double-click this file to test the refresh manually at any time.
REM Task Scheduler is set up to run this exact file - if it works when
REM you double-click it, it'll work on schedule too.

cd /d "%~dp0"

echo. >> refresh_log.txt
echo ============================================== >> refresh_log.txt
echo Run started: %date% %time% >> refresh_log.txt
echo ============================================== >> refresh_log.txt

python refresh_all.py >> refresh_log.txt 2>&1

echo Run finished: %date% %time% >> refresh_log.txt

REM if double-clicked by hand, pause so the window doesn't vanish instantly
REM (SCHEDULED_TASK is set by setup_scheduled_task.ps1 so the automatic
REM run doesn't sit there waiting for a keypress nobody will give it)
if not defined SCHEDULED_TASK pause
