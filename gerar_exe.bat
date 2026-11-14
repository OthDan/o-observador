@echo off
chcp 65001 >nul
title Gerar O-Observador.exe
cd /d "%~dp0"

echo ==========================================
echo   Gerando O-Observador.exe
echo ==========================================
echo.

rem Prefere o Python 3.13 (ou 3.12), que tem todas as bibliotecas prontas.
set "PY="
py -3.13 --version >nul 2>nul && set "PY=py -3.13"
if not defined PY (
    py -3.12 --version >nul 2>nul && set "PY=py -3.12"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERRO] Python nao encontrado.
    echo Instale o Python 3.13 em https://www.python.org/downloads/ e marque "Add python.exe to PATH".
    pause
    exit /b 1
)
echo Usando: %PY%
%PY% --version
echo.

if not exist app.py (
    echo [ERRO] app.py precisa estar na mesma pasta deste arquivo.
    pause
    exit /b 1
)

if not exist config.yaml (
    echo [ERRO] config.yaml precisa estar na mesma pasta deste arquivo.
    pause
    exit /b 1
)

if not exist catalogo.yaml (
    echo [ERRO] catalogo.yaml precisa estar na mesma pasta deste arquivo.
    pause
    exit /b 1
)

echo [1/3] Instalando dependencias...
%PY% -m pip install --upgrade pyinstaller fastapi uvicorn httpx pyyaml plyer
if errorlevel 1 (
    echo.
    echo [ERRO] Falha ao instalar as dependencias.
    echo Se aparecer "Python 3.14" ou "3.15" acima, instale o Python 3.13 e rode de novo.
    pause
    exit /b 1
)

echo.
echo [2/3] Criando o executavel...
%PY% -m PyInstaller --onefile --console --name O-Observador --collect-submodules uvicorn --collect-data certifi --hidden-import yaml --hidden-import plyer.platforms.win.notification --add-data "catalogo.yaml;." app.py
if errorlevel 1 (
    echo [ERRO] Falha ao gerar o executavel.
    pause
    exit /b 1
)

echo.
echo [3/3] Finalizando...
copy /y dist\O-Observador.exe O-Observador.exe >nul
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q O-Observador.spec 2>nul

echo.
echo ==========================================
echo   Pronto! Abra o arquivo O-Observador.exe
echo   Mantenha o config.yaml na mesma pasta dele.
  O banco observador.db sera criado ali na primeira execucao.
echo ==========================================
pause