@echo off
setlocal EnableExtensions
chcp 65001 >nul
title DS3 Enemy Randomizer
cd /d "%~dp0"

set "ENGINE_DIR=%~dp0"
for %%I in ("%ENGINE_DIR%..") do set "GAME_ROOT=%%~fI"

echo ================================================================
echo DS3 Enemy Randomizer v2
echo ================================================================
echo Game root: %GAME_ROOT%
echo Engine:    %ENGINE_DIR%
echo.

set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo ERROR: No se encontro Python 3 en PATH.
    echo Instala Python 3.10+ y vuelve a ejecutar este BAT.
    echo.
    pause
    exit /b 11
)

%PYTHON_CMD% --version
if errorlevel 1 (
    echo ERROR: Python no se pudo ejecutar.
    echo.
    pause
    exit /b 12
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo ERROR: Se requiere Python 3.10 o superior.
    echo.
    pause
    exit /b 13
)

if not exist "%GAME_ROOT%\map\mapstudio" (
    echo ERROR: No existe Game\map\mapstudio.
    echo Ruta: %GAME_ROOT%\map\mapstudio
    echo.
    pause
    exit /b 20
)

if not exist "%ENGINE_DIR%RandomizeEnemies.py" (
    echo ERROR: Falta RandomizeEnemies.py en PythonEngineDS3.
    echo.
    pause
    exit /b 21
)

echo.
echo ================================================================
echo EJECUTANDO RANDOMIZER
echo ================================================================
echo.
echo La ventana permanecera abierta aunque ocurra un error.
echo.

%PYTHON_CMD% "%ENGINE_DIR%RandomizeEnemies.py" --game-root "%GAME_ROOT%" --apply %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo ================================================================
    echo RANDOMIZACION COMPLETADA
    echo ================================================================
    echo.
    echo Revisa:
    echo   [mod]\EnemyTables\Randomized_enemies.json
    echo   [mod]\EnemyTables\RandomizeEnemies_report.txt
    echo   [mod]\map\mapstudio\*.msb.dcx
) else (
    echo ================================================================
    echo RANDOMIZACION ABORTADA - CODIGO %RC%
    echo ================================================================
    echo.
    echo Revisa el mensaje de ERROR de arriba y el reporte de EnemyTables.
)

echo.
echo Presiona una tecla para cerrar esta ventana.
pause >nul
exit /b %RC%
