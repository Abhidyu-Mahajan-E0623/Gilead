@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo [error] Failed to create virtual environment.
    exit /b 1
  )
)

set "PY=.venv\Scripts\python.exe"

%PY% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo [setup] Bootstrapping pip...
  %PY% -m ensurepip --upgrade
)

set "INSTALL_DEPS=0"
for %%P in (fastapi uvicorn openai python-dotenv rapidfuzz) do (
  %PY% -m pip show %%P >nul 2>nul
  if errorlevel 1 set "INSTALL_DEPS=1"
)

if "%INSTALL_DEPS%"=="1" (
  echo [setup] Installing dependencies...
  %PY% -m pip install --upgrade pip
  %PY% -m pip install -r config\requirements.txt
  if errorlevel 1 (
    echo [error] Dependency installation failed.
    exit /b 1
  )
) else (
  echo [setup] Dependencies already installed. Skipping pip install.
)

echo [run] Starting server at http://127.0.0.1:8000
%PY% -m uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload
