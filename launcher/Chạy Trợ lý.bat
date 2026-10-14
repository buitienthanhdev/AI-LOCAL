@echo off
chcp 65001 >nul
rem Mo "Tro ly may tinh" voi moi thu MOI NHAT:
rem   [1/4] keo code/skill/cau hinh moi tu GitHub (git pull, chi khi khong xung dot)
rem   [2/4] build lai neu ma nguon moi hon file chay (skill, tools, policy doc truc tiep tu thu muc nen khong can build)
rem   [3/4] cap nhat kho tri thuc (bo qua neu vua cap nhat trong vong 1 gio)
rem   [4/4] mo cua so chat
cd /d "%~dp0.."

set "EXE=target\release\agent-desktop.exe"
set "LOG=logs\cap-nhat-tri-thuc.log"

tasklist /FI "IMAGENAME eq agent-desktop.exe" 2>nul | find /i "agent-desktop.exe" >nul
if not errorlevel 1 (
  echo Tro ly dang chay san. De nap ban moi nhat hay chay "Tắt Trợ lý.bat" roi chay lai file nay.
  goto :end
)

echo [1/4] Kiem tra ban moi tren GitHub...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo Khong phai thu muc git, bo qua.
) else (
  git diff --quiet HEAD -- >nul 2>&1
  if errorlevel 1 (
    echo Co thay doi chua commit trong thu muc, khong tu keo de tranh de len. Bo qua.
  ) else (
    git pull --ff-only
    if errorlevel 1 echo Khong keo duoc ^(mat mang hoac lich su khac nhau^). Van dung ban hien co.
  )
)

echo [2/4] Kiem tra ma nguon moi...
set "NEED_BUILD=0"
if not exist "%EXE%" set "NEED_BUILD=1"
if "%NEED_BUILD%"=="0" (
  powershell -NoProfile -Command "$exe=(Get-Item '%EXE%').LastWriteTime; $new=Get-ChildItem -Recurse -File -Include *.rs,Cargo.toml,Cargo.lock,*.html -Path core,apps,services\gemini-gateway -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\target\\|\\node_modules\\' -and $_.LastWriteTime -gt $exe } | Select-Object -First 1; if ($new) { exit 1 } else { exit 0 }"
  if errorlevel 1 set "NEED_BUILD=1"
)
if "%NEED_BUILD%"=="1" (
  echo Co ma moi hon file chay: dang build lai, vui long cho...
  cargo build --release
  if errorlevel 1 (
    echo Build loi.
    if not exist "%EXE%" (
      pause
      exit /b 1
    )
    echo Van mo ban cu.
  )
) else (
  echo File chay da la ban moi nhat.
)

echo [3/4] Kiem tra kho tri thuc...
if not exist "logs" mkdir "logs"
set "SKIP_KB=0"
powershell -NoProfile -Command "if ((Test-Path '%LOG%') -and ((Get-Date) - (Get-Item '%LOG%').LastWriteTime).TotalMinutes -lt 60) { exit 1 } else { exit 0 }"
if errorlevel 1 set "SKIP_KB=1"
if "%SKIP_KB%"=="1" (
  echo Tri thuc vua cap nhat gan day, bo qua.
) else (
  echo Dang cap nhat tri thuc ^(muc uu tien thap^)...
  pushd "services\company-brain"
  echo ===== %date% %time% =====>> "..\..\%LOG%"
  start "" /b /wait /belownormal python -m brain scan --all >> "..\..\%LOG%" 2>&1
  start "" /b /wait /belownormal python -m brain index >> "..\..\%LOG%" 2>&1
  popd
)

echo [4/4] Mo Tro ly...
start "" "%EXE%"

:end
timeout /t 2 >nul
