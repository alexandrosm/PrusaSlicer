@echo off
setlocal
rem Invoke from an x64 Visual Studio developer environment. No recursive build.
if "%~1"=="" (
  echo Usage: build_stl_restore.cmd NEW_OUTPUT_DIRECTORY
  exit /b 1
)
if exist "%~1" (
  echo Refusing to overwrite an existing output directory.
  exit /b 1
)
mkdir "%~1" || exit /b 1
cl /nologo /O2 /MT /EHsc /std:c++17 "%~dp0stl_restore.cpp" /Fo"%~1\stl_restore.obj" /Fe"%~1\RestoreSTL.exe" bcrypt.lib /link /OPT:REF /OPT:ICF
exit /b %errorlevel%
