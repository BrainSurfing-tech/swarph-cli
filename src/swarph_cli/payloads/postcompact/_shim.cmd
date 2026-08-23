@echo off
rem swarph postcompact wiring (#566) — Windows shim: harnesses that spawn
rem hooks via cmd never run the shebang; wrap the .sh with the same name.
bash "%~dp0@SCRIPT@" %*
