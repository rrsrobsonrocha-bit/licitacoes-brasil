@echo off
REM ==========================================================================
REM  Robo de Lances Desktop - Licitacoes Brasil
REM  Compila main.py em um executavel unico: dist\RoboLances_LicitacoesBrasil.exe
REM  Requisito: Python 3.10+ (python.org). Se nao estiver no PATH, informe o caminho
REM  antes de rodar:  set PYTHON=C:\caminho\python.exe
REM  As dependencias vao para um ambiente isolado (.venv-build), sem tocar no Python global.
REM ==========================================================================
setlocal
cd /d "%~dp0"

REM --- Localiza um Python que funcione (o atalho da Microsoft Store nao serve) ---
set "PY="
if defined PYTHON (
  "%PYTHON%" -c "import sys" >nul 2>nul && set "PY="%PYTHON%""
)
if not defined PY (
  py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERRO] Python nao encontrado. Instale o Python 3.10+ em python.org
  echo        ou rode antes: set PYTHON=C:\caminho\python.exe
  exit /b 1
)

echo [1/4] Preparando o ambiente de build (.venv-build)...
if not exist ".venv-build\Scripts\python.exe" (
  %PY% -m venv .venv-build
)
if not exist ".venv-build\Scripts\python.exe" (
  echo [ERRO] Falha ao criar o ambiente .venv-build.
  exit /b 1
)
set "VPY=.venv-build\Scripts\python.exe"

echo [2/4] Instalando dependencias (pyinstaller, requests, urllib3)...
"%VPY%" -m pip install --quiet --upgrade pyinstaller requests urllib3
if errorlevel 1 (
  echo [ERRO] Falha ao instalar as dependencias.
  exit /b 1
)

echo [3/4] Compilando o executavel...
"%VPY%" -m PyInstaller --onefile --clean --noconfirm --name="RoboLances_LicitacoesBrasil" --icon=NONE main.py
if errorlevel 1 (
  echo [ERRO] Falha na compilacao.
  exit /b 1
)

echo [4/4] Conferindo o executavel...
if not exist "dist\RoboLances_LicitacoesBrasil.exe" (
  echo [ERRO] Executavel nao encontrado em dist.
  exit /b 1
)

echo.
echo Pronto: %~dp0dist\RoboLances_LicitacoesBrasil.exe
echo Na primeira execucao, um assistente no terminal pede os dados (6 passos)
echo e grava o config.json ao lado do .exe. Para refazer: --configurar
echo No passo 4, digite "simulacao" para testar sem enviar lances ao portal.
endlocal
