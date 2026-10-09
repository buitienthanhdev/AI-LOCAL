# Build + cài brain_core (Rust/PyO3) cho Python hiện tại, chạy test Rust và kiểm tra parity trên dữ liệu thật.
# Dùng: powershell -ExecutionPolicy Bypass -File scripts\build_rust.ps1
$ErrorActionPreference = "Stop"
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$root = Split-Path $PSScriptRoot -Parent
Push-Location "$root\rust"
cargo test -q
Pop-Location
Push-Location "$root\rust\brain_core_py"
python -m maturin build --release
$wheel = Get-ChildItem "$root\rust\target\wheels\brain_core-*.whl" | Sort-Object LastWriteTime | Select-Object -Last 1
python -m pip install --force-reinstall --no-deps $wheel.FullName
Pop-Location
$env:PYTHONIOENCODING = "utf-8"
python "$root\scripts\parity_check.py"
