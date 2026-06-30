@echo off
set PYTHON=venv\Scripts\python
set PIP=venv\Scripts\pip

if "%1"=="install" (
    python -m venv venv
    %PIP% install -e .[dev]
    %PIP% install pre-commit
    venv\Scripts\pre-commit install
    exit /b
)

if "%1"=="run" (
    %PYTHON% src\main.py
    exit /b
)

if "%1"=="test" (
    %PYTHON% -m pytest
    exit /b
)

if "%1"=="lint" (
    %PYTHON% -m flake8 src\ tests\
    exit /b
)

if "%1"=="dry-run" (
    set DRY_RUN=true && %PYTHON% src\main.py
    exit /b
)

if "%1"=="weekly-report" (
    %PYTHON% src\weekly_report.py
    exit /b
)

if "%1"=="task-check" (
    %PYTHON% src\task_auditor.py
    exit /b
)

if "%1"=="owner-track" (
    %PYTHON% src\owner_tracker.py
    exit /b
)

if "%1"=="broker-score" (
    if "%2"=="" (
        echo Error: BROKER_ID is required. Usage: make.cmd broker-score 123
        exit /b
    )
    %PYTHON% src\broker_score.py %2
    exit /b
)

if "%1"=="chat-poll" (
    %PYTHON% src\chat_poller.py
    exit /b
)

if "%1"=="docker-build" (
    docker build -t b24-ai-auditor:latest .
    exit /b
)

echo Usage: make.cmd [install^|run^|test^|lint^|dry-run^|docker-build]