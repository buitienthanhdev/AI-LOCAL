@echo off
chcp 65001 >nul
rem Tat Tro ly may tinh: dong cua so chat, viec dang chay, gateway, worker, Qdrant va giai phong GPU cua Ollama.
rem KHONG hoc/quet du lieu may o day: viec hoc chay nen trong ung dung (Cai dat > Kien thuc > Cap nhat).
rem Khong dong cac chuong trinh khac cua ban.
cd /d "%~dp0.."

echo Dang tat Tro ly...
taskkill /IM agent-desktop.exe /F >nul 2>&1
taskkill /IM agent.exe /F >nul 2>&1
"target\release\agent.exe" gateway stop >nul 2>&1
timeout /t 3 >nul
taskkill /IM lga-gateway.exe /F >nul 2>&1

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*lga_worker*' -or $_.CommandLine -like '*-m brain*' -or $_.CommandLine -like '*gemini-webapi*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-Process qdrant -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*AI LOCAL*' } | Stop-Process -Force"
ollama stop gemma4:12b >nul 2>&1
ollama stop qwen3-embedding:0.6b >nul 2>&1

echo Da tat Tro ly may tinh.
timeout /t 2 >nul
exit /b 0
