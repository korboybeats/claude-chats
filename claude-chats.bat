@echo off
:: claude-chats - Browse, resume, and manage Claude Code conversations
setlocal enabledelayedexpansion

set "_resume=%TEMP%\.claude-chats-resume.%RANDOM%"
set "_CLAUDE_CHATS_RESUME=%_resume%"
python "%~dp0.claude-chats.py" %*

if exist "%_resume%" (
    for /f "usebackq" %%s in ("%_resume%") do set "filesize=%%~zs"
    if !filesize! GTR 0 (
        set /p "dir=" < "%_resume%"
        for /f "usebackq skip=1 delims=" %%c in ("%_resume%") do set "cmd=%%c"
        del /q "%_resume%" 2>nul
        if not exist "!dir!" mkdir "!dir!" 2>nul
        cd /d "!dir!"
        !cmd!
        exit /b
    )
)
if exist "%_resume%" del /q "%_resume%" 2>nul
