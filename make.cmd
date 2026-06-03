@echo off
setlocal

if "%~1"=="" goto usage

if /i "%~1"=="install" goto install
if /i "%~1"=="lint" goto lint
if /i "%~1"=="test" goto test
if /i "%~1"=="test-b24" goto testb24
if /i "%~1"=="run" goto run
if /i "%~1"=="dry-run" goto dryrun
if /i "%~1"=="docker-build" goto dockerbuild

:usage
echo Usage: make.cmd ^<install^|lint^|test^|test-b24^|run^|dry-run^|docker-build^>
exit /b 1

:install
python -m venv venv
call venv\Scripts\pip install -r requirements.txt
call venv\Scripts\pip install -r requirements-dev.txt
call venv\Scripts\pip install -e .
exit /b %errorlevel%

:lint
call venv\Scripts\flake8 src/ tests/
exit /b %errorlevel%

:test
call venv\Scripts\pytest tests/ -v
exit /b %errorlevel%

:testb24
call venv\Scripts\python src/test_b24.py
exit /b %errorlevel%

:run
call venv\Scripts\python src/main.py
exit /b %errorlevel%

:dryrun
set DRY_RUN=true
call venv\Scripts\python src/main.py
exit /b %errorlevel%

:dockerbuild
docker build -t b24-ai-auditor:latest .
exit /b %errorlevel%
