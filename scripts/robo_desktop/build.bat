@echo off
REM ==========================================================================
REM  Robo de Lances Desktop - Licitacoes Brasil
REM  Compila main.py em um executavel unico: dist\RoboLances_LicitacoesBrasil.exe
REM  Requisito: Python 3.10+ no PATH (python.org, marcar "Add python.exe to PATH").
REM ==========================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERRO] Python nao encontrado no PATH.
  exit /b 1
)

echo [1/3] Instalando dependencias (pyinstaller, requests, urllib3)...
python -m pip install --upgrade pyinstaller requests urllib3
if errorlevel 1 (
  echo [ERRO] Falha ao instalar as dependencias.
  exit /b 1
)

echo [2/3] Compilando o executavel...
python -m PyInstaller --onefile --clean --noconfirm --name="RoboLances_LicitacoesBrasil" --icon=NONE main.py
if errorlevel 1 (
  echo [ERRO] Falha na compilacao.
  exit /b 1
)

echo [3/3] Copiando o modelo de configuracao...
if not exist "dist\config.json" copy /y "config.exemplo.json" "dist\config.json" >nul

echo.
echo Pronto: %~dp0dist\RoboLances_LicitacoesBrasil.exe
echo Edite dist\config.json (url_backend e item_id) antes de executar.
echo O modo padrao e "simulacao": nenhum lance e enviado ao portal.
endlocal
