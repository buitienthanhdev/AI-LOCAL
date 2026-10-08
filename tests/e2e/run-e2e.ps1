# End-to-end acceptance runs of the real agent (Gemini planning + real desktop). See docs/operations.md.
#   pwsh tests/e2e/run-e2e.ps1 -Test 1            # Notepad
#   pwsh tests/e2e/run-e2e.ps1 -Test all
# Each test prepares its preconditions, runs `agent run`, then checks the outcome independently of the
# agent's own verification (a second, external verification).

param([Parameter(Mandatory)][string]$Test, [string]$Mode = 'normal')

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\env.ps1')
$agent = Join-Path $env:LGA_ROOT 'target\debug\agent.exe'
if (-not (Test-Path $agent)) { $agent = Join-Path $env:LGA_ROOT 'target\release\agent.exe' }
if (-not (Test-Path $agent)) { throw 'build the CLI first: cargo build' }

function Invoke-Agent([string]$task, [string[]]$extra = @()) {
    & $agent run $task --mode $Mode @extra
    return $LASTEXITCODE
}

function Assert([bool]$condition, [string]$message) {
    if (-not $condition) { throw "FAILED: $message" }
    "  ok: $message"
}

$tests = [ordered]@{
    '1' = {
        $target = 'C:\temp\hello.txt'
        Remove-Item $target -ErrorAction SilentlyContinue
        $code = Invoke-Agent "Mở Notepad, viết hello world và lưu vào $target"
        Assert ($code -eq 0) 'agent reported completion'
        Assert (Test-Path $target) "$target exists"
        Assert ((Get-Content $target -Raw).Trim() -eq 'hello world') 'file contains hello world'
    }
    '4' = {
        $code = Invoke-Agent 'Chạy một lệnh PowerShell vô hại để lấy phiên bản PowerShell và tên máy tính, đọc kết quả rồi báo cho tôi' @('--yes')
        Assert ($code -eq 0) 'agent reported completion'
    }
    '8' = {
        # Destroy intermediate task state mid-run, then resume.
        $target = 'C:\temp\resume-test.txt'
        Remove-Item $target -ErrorAction SilentlyContinue
        $tasksDir = Join-Path $env:LGA_ROOT 'memory\tasks'
        $before = @(Get-ChildItem $tasksDir -Filter '*.json' -ErrorAction SilentlyContinue | ForEach-Object Name)
        $task = "Mở Notepad, viết 'resume test' và lưu vào $target"
        # Start-Process joins arguments with spaces without quoting them.
        $proc = Start-Process -FilePath $agent -PassThru -NoNewWindow -ArgumentList @('run', "`"$task`"", '--mode', $Mode)
        $state = $null
        $deadline = (Get-Date).AddMinutes(5)
        while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
            $new = Get-ChildItem $tasksDir -Filter '*.json' | Where-Object { $before -notcontains $_.Name } | Select-Object -First 1
            if ($new) {
                $record = Get-Content $new.FullName -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue
                if ($record -and $record.steps.Count -ge 2) { $state = $new; break }
            }
            Start-Sleep -Milliseconds 300
        }
        Assert ($null -ne $state) 'task reached its second step'
        Stop-Process -Id $proc.Id -Force
        $proc.WaitForExit()
        $taskId = $state.BaseName
        Set-Content -Path $state.FullName -Value '{ "corrupted": tru' -Encoding utf8
        Remove-Item "$($state.FullName).bak" -ErrorAction SilentlyContinue
        "  killed runner and destroyed state of $taskId"
        & $agent resume $taskId
        Assert ($LASTEXITCODE -eq 0) 'resumed task completed'
        Assert (Test-Path $target) "$target exists"
        Assert ((Get-Content $target -Raw).Trim() -eq 'resume test') 'file content is correct'
    }
}

$selected = if ($Test -eq 'all') { $tests.Keys } else { @($Test) }
$failed = 0
foreach ($name in $selected) {
    if (-not $tests.Contains($name)) { throw "unknown test $name (known: $($tests.Keys -join ', '))" }
    "== TEST $name"
    try { & $tests[$name] } catch { $_.Exception.Message; $failed++ }
}
if ($failed) { exit 1 }
