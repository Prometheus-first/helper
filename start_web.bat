@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title helper - 本地管理台
cd /d "%~dp0"
if errorlevel 1 goto directory_error

if defined WXBOT_PYTHON goto custom_python
if exist "%~dp0.venv\Scripts\python.exe" goto venv_python
where python.exe >nul 2>nul
if not errorlevel 1 goto system_python
where py.exe >nul 2>nul
if not errorlevel 1 goto python_launcher

echo 未找到 Python。请先安装 64 位 Python，或设置 WXBOT_PYTHON 为解释器完整路径。
pause
exit /b 1

:custom_python
"%WXBOT_PYTHON%" -X utf8 "%~dp0web.py" %*
goto finished

:venv_python
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0web.py" %*
goto finished

:system_python
python.exe -X utf8 "%~dp0web.py" %*
goto finished

:python_launcher
py.exe -3 -X utf8 "%~dp0web.py" %*
goto finished

:directory_error
echo 无法打开项目目录，请将项目放在本机可访问的文件夹中。
pause
exit /b 1

:finished
set "HELPER_EXIT_CODE=%errorlevel%"
if "%HELPER_EXIT_CODE%"=="0" exit /b 0
echo.
echo 管理台启动或运行失败，请查看上方提示。
echo 如果服务已经运行，请直接打开 http://127.0.0.1:8765
pause
exit /b %HELPER_EXIT_CODE%
