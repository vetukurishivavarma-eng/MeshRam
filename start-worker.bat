@echo off
rem usage: start-worker.bat --coordinator 192.168.1.31 --code 123456 [--max-ram-gb 4]
cd /d "%~dp0"
python -m meshram worker %*
