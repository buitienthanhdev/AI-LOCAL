# Run a command at BelowNormal priority, wait for it and return its exit code; output is appended to a log.
#   pwsh run-low.ps1 -Log <file> -- python -m brain index
param([Parameter(Mandatory)][string]$Log, [Parameter(ValueFromRemainingArguments)][string[]]$Cmd)
$line = ($Cmd | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
$p = Start-Process -FilePath cmd.exe -ArgumentList "/c $line >> `"$Log`" 2>&1" -PassThru -WindowStyle Hidden
try { $p.PriorityClass = 'BelowNormal' } catch {}
$p.WaitForExit()
exit $p.ExitCode
