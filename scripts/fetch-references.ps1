# Clone every repository listed in references/manifest.toml and check out its pinned commit.
# Existing clones are verified; a clone at a different commit is reported, not silently changed.

param([switch]$Checkout)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$manifest = Join-Path $root 'references\manifest.toml'

$repos = @()
$current = $null
foreach ($line in Get-Content $manifest) {
    if ($line -match '^\s*\[\[repo\]\]') { $current = @{}; $repos += $current; continue }
    if ($current -and $line -match '^\s*(\w+)\s*=\s*"([^"]*)"') { $current[$Matches[1]] = $Matches[2] }
}

$failed = 0
foreach ($repo in $repos) {
    $dest = Join-Path $root "references\$($repo.name)"
    if (-not (Test-Path (Join-Path $dest '.git'))) {
        git init --quiet $dest
        git -C $dest remote add origin $repo.url
    }
    $head = git -C $dest rev-parse HEAD 2>$null
    if ($head -eq $repo.commit) { '{0,-20} OK       {1}' -f $repo.name, $head; continue }
    if ($head -and -not $Checkout) {
        '{0,-20} MISMATCH at {1}, pinned {2} (rerun with -Checkout)' -f $repo.name, $head, $repo.commit
        $failed++
        continue
    }
    git -C $dest fetch --quiet --depth 1 origin $repo.commit
    if ($LASTEXITCODE -ne 0) { '{0,-20} FAIL     fetch {1}' -f $repo.name, $repo.commit; $failed++; continue }
    git -C $dest checkout --quiet --detach FETCH_HEAD
    '{0,-20} FETCHED  {1}' -f $repo.name, (git -C $dest rev-parse HEAD)
}
if ($failed -gt 0) { exit 1 }
