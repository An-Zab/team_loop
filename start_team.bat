@echo off
rem --------------------------------------------------------------------------
rem  start_team.bat - double-click launcher for the team_loop team.
rem  Opens PowerShell, switches the console to UTF-8 and runs start_team.py,
rem  which checks the local model, writes MODEL_NAME and MAX_CONTEXT_TOKENS
rem  into .env, clears handoff.md for a new task and starts the loop.
rem  This file is intentionally ASCII-only: .bat is read in the OEM codepage,
rem  so all Russian text is printed by Python, not by cmd.
rem --------------------------------------------------------------------------
setlocal
title team_loop - Strata team launcher
set "TEAM_LOOP_DIR=%~dp0"

where python >nul 2>nul && (set "PY=python") || (set "PY=py -3")

powershell -NoLogo -NoProfile -Command "chcp 65001 > $null; Set-Location -LiteralPath $env:TEAM_LOOP_DIR; & $env:PY (Join-Path $env:TEAM_LOOP_DIR 'start_team.py')"
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
