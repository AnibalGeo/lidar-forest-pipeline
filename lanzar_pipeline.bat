@echo off
REM ============================================================
REM Lanzador de la GUI del pipeline LiDAR (gui\pipeline_gui.py).
REM Activa el env conda declarado en environment.yml (name:) y
REM abre la GUI. Si algo falla, pausa para poder leer el error.
REM NOTA: sin bloques ( ) alrededor de "call conda" — conda.bat
REM falla dentro de parentesis de cmd; por eso el flujo con goto.
REM ============================================================
setlocal

REM -- nombre del env desde environment.yml (linea "name: ...")
set "ENV_NAME="
for /f "tokens=2 delims=: " %%a in ('findstr /b /c:"name:" "%~dp0environment.yml"') do set "ENV_NAME=%%a"
if not defined ENV_NAME set "ENV_NAME=lidar-forest"

REM -- localizar conda: bare "conda" del PATH (SIN comillas: call "conda"
REM    falla en cmd) o ruta completa entre comillas de miniconda/anaconda.
set CONDA_BAT=conda
where conda >nul 2>nul
if not errorlevel 1 goto :activar
if not exist "%USERPROFILE%\miniconda3\condabin\conda.bat" goto :try_anaconda
set CONDA_BAT="%USERPROFILE%\miniconda3\condabin\conda.bat"
goto :activar
:try_anaconda
if not exist "%USERPROFILE%\anaconda3\condabin\conda.bat" goto :sin_conda
set CONDA_BAT="%USERPROFILE%\anaconda3\condabin\conda.bat"
goto :activar
:sin_conda
echo [!] No se encontro conda. Instala Miniconda o agrega conda al PATH.
pause
exit /b 1

:activar
REM (sin "2>nul": redirigir stderr de conda.bat rompe la llamada siguiente)
call %CONDA_BAT% activate %ENV_NAME%
if not errorlevel 1 goto :lanzar
echo [!] No existe el env "%ENV_NAME%" ^(crear con: conda env create -f environment.yml^).
echo     Intentando con el env "base"...
call %CONDA_BAT% activate base
if not errorlevel 1 goto :lanzar
echo [!] Tampoco se pudo activar "base".
pause
exit /b 1

:lanzar
python "%~dp0gui\pipeline_gui.py"
if errorlevel 1 goto :fallo
endlocal
exit /b 0

:fallo
echo.
echo [!] La GUI termino con error ^(codigo %errorlevel%^). Lee el mensaje de arriba.
pause
endlocal
exit /b 1
