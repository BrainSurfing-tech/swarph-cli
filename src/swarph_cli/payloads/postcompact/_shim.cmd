@echo off
rem swarph postcompact wiring (#566) — Windows shim: harnesses that spawn
rem hooks via cmd never run the shebang; wrap the .sh with the same name.
rem @BASH@ is resolved to an ABSOLUTE Git-bash at install time: a bare
rem "bash" resolves to the System32 WSL launcher on boxes with WSL, which
rem silently no-ops the hook (cursor-win accept run, 2026-08-24).
"@BASH@" "%~dp0@SCRIPT@" %*
