@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

rem ============================================================================
rem DS3 PythonEngineDS3 - PIPELINE COMPLETO
rem
rem Estructura:
rem   DARK SOULS III\
rem     Game\
rem       PythonEngineDS3\
rem         ds3_engine_config.py
rem         ds3_data0_reader.py
rem         build_ds3_reference_index.py
rem         ds3_enemy_database_builder.py
rem         run_ds3_pipeline.bat
rem
rem Orden obligatorio:
rem   1) ds3_data0_reader
rem   2) build_ds3_reference_index
rem   3) ds3_enemy_database_builder --replace
rem ============================================================================

set "ENGINE_DIR=%~dp0"
for %%I in ("%ENGINE_DIR%..") do set "GAME_ROOT=%%~fI"

echo.
echo ================================================================
echo DS3 PythonEngineDS3
echo ================================================================
echo Game root: %GAME_ROOT%
echo Engine:    %ENGINE_DIR%
echo.

if not exist "%GAME_ROOT%\Data0.bdt" (
    echo ERROR: no se encontro Game\Data0.bdt
    echo.
    pause
    exit /b 10
)

if not exist "%GAME_ROOT%\Data0.bhd" (
    echo AVISO: no se encontro Game\Data0.bhd.
    echo        El lector continuara porque el BDT es la entrada efectiva.
    echo.
)

set "PYTHON_CMD="
where py >nul 2>&1
if %errorlevel%==0 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    where python >nul 2>&1
    if %errorlevel%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo ERROR: no se encontro Python.
    echo Instala Python 3.10+ y asegurate de que "py" o "python" este en PATH.
    echo.
    pause
    exit /b 11
)

%PYTHON_CMD% --version
if errorlevel 1 (
    echo ERROR: no se pudo ejecutar Python.
    pause
    exit /b 12
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ERROR: se requiere Python 3.10 o superior.
    echo.
    pause
    exit /b 13
)

echo.
echo [1/3] Leyendo Game\Data0.bdt...
echo.

%PYTHON_CMD% "%ENGINE_DIR%ds3_data0_reader.py" --game-root "%GAME_ROOT%"
if errorlevel 1 (
    echo.
    echo ERROR en la etapa 1: ds3_data0_reader.py
    echo Las siguientes etapas NO se ejecutaran.
    pause
    exit /b 20
)

echo.
echo [2/3] Construyendo ds3_reference_index.json...
echo.

%PYTHON_CMD% "%ENGINE_DIR%build_ds3_reference_index.py" --game-root "%GAME_ROOT%"
if errorlevel 1 (
    echo.
    echo ERROR en la etapa 2: build_ds3_reference_index.py
    echo La base final NO sera reemplazada.
    pause
    exit /b 30
)

echo.
echo [3/3] Construyendo all_enemies completo y reemplazando...
echo.

%PYTHON_CMD% "%ENGINE_DIR%ds3_enemy_database_builder.py" --game-root "%GAME_ROOT%" --replace
if errorlevel 1 (
    echo.
    echo ERROR en la etapa 3: ds3_enemy_database_builder.py
    echo El reemplazo final fue abortado por validacion.
    pause
    exit /b 40
)

echo.
echo ================================================================
echo PIPELINE COMPLETADO SIN ERRORES
echo ================================================================
echo.
echo Archivos principales:
echo   %GAME_ROOT%\Data0.bdt
echo   %GAME_ROOT%\PythonEngineDS3\
echo   [mod]\EnemyTables\data0_original.json
echo   [mod]\EnemyTables\ds3_reference_index.json
echo   [mod]\EnemyTables\all_enemies.source.json
echo   [mod]\EnemyTables\all_enemies_complete.json
echo   [mod]\EnemyTables\all_enemies.json
echo.
echo El archivo all_enemies.source.json conserva la fuente MSB
echo usada para generar el indice y permite repetir el proceso.
echo.
pause
exit /b 0
