@echo off
rem Run Premiere Deck from source (docs\BUILD.md 2). Extra arguments are passed on, e.g. --fake or --debug.
rem PREMIERE_DECK_HOME defaults to <project>\premiere-deck_data; set it before calling to use another folder.
rem PREMIERE_DECK_CEP_DIR, when set, replaces Adobe's CEP extensions folder (tests, isolated runs).
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHONPATH=%ROOT%\src"
if not defined PREMIERE_DECK_HOME set "PREMIERE_DECK_HOME=%ROOT%\premiere-deck_data"
if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo .venv is missing: run "powershell -NoProfile -File build\build.ps1 -SkipBuild" first.
  exit /b 1
)
"%ROOT%\.venv\Scripts\python.exe" -m premiere_deck %*
exit /b %ERRORLEVEL%
