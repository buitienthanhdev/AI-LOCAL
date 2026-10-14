@echo off
chcp 65001 >nul
rem Tat Tro ly may tinh: (1) dong het cua so chat, viec dang chay, gateway, worker;
rem (2) cap nhat kho tri thuc moi nhat (quet + danh chi muc, chay o muc uu tien thap);
rem (3) tat Qdrant va giai phong GPU cua Ollama. Khong dong cac chuong trinh khac cua ban.
rem Muon bo qua buoc cap nhat tri thuc: chay  "Tắt Trợ lý.bat" nhanh
cd /d "%~dp0.."

echo [1/3] Dang tat Tro ly...
taskkill /IM agent-desktop.exe /F >nul 2>&1
taskkill /IM agent.exe /F >nul 2>&1
"target\release\agent.exe" gateway stop >nul 2>&1
timeout /t 3 >nul
taskkill /IM lga-gateway.exe /F >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*lga_worker*' -or $_.CommandLine -like '*-m brain*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

if /i "%~1"=="nhanh" goto :giaiphong

echo [2/3] Dang cap nhat kho tri thuc (quet tai lieu moi/doi, danh chi muc)...
if not exist "logs" mkdir "logs"
pushd "services\company-brain"
echo ===== %date% %time% =====>> "..\..\logs\cap-nhat-tri-thuc.log"
start "" /b /wait /belownormal python -m brain scan --all >> "..\..\logs\cap-nhat-tri-thuc.log" 2>&1
if errorlevel 1 echo Quet tri thuc co loi, xem logs\cap-nhat-tri-thuc.log
start "" /b /wait /belownormal python -m brain index >> "..\..\logs\cap-nhat-tri-thuc.log" 2>&1
if errorlevel 1 echo Danh chi muc tri thuc co loi, xem logs\cap-nhat-tri-thuc.log
popd

:giaiphong
echo [3/3] Dang giai phong bo nho...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*-m brain*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-Process qdrant -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*AI LOCAL*' } | Stop-Process -Force"
ollama stop gemma4:12b >nul 2>&1
ollama stop qwen3-embedding:0.6b >nul 2>&1

echo Đã tắt Trợ lý máy tính.
timeout /t 2 >nul
