param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$env:GH_PROMPT_DISABLED = '1'
$env:GIT_TERMINAL_PROMPT = '0'
$repo = 'hjparkolivia/Github-infobot'
$runtime = Join-Path $PSScriptRoot '.pc-runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$log = Join-Path $runtime 'checks.log'
$gh = (Get-Command gh -ErrorAction Stop).Source
$mutex = New-Object Threading.Mutex($false, 'Local\GithubInfobotPCRescue')
$locked = $false
function Log([string]$message) {
    Add-Content -LiteralPath $log -Encoding UTF8 -Value ((Get-Date).ToString('o') + ' ' + $message)
}
function ApiJson([string]$path, [switch]$AllowMissing) {
    $text = (& $gh api $path 2>$null | Out-String)
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        if ($AllowMissing -and $text -match '"status"\s*:\s*"404"|"message"\s*:\s*"Not Found"') { return $null }
        throw 'GitHub API or authentication failed. Check gh auth status; no secret values are logged.'
    }
    return ($text | ConvertFrom-Json)
}
function Dispatch([string]$workflow, [string[]]$fields) {
    if ($CheckOnly) { Log "CHECK ONLY: would dispatch $workflow"; return }
    & $gh workflow run $workflow --repo $repo @fields 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GitHub workflow dispatch failed or result is unknown. No automatic API retry.' }
    Log "Dispatched $workflow"
}
try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { exit 0 }
    $now = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Korea Standard Time')
    $day = $now.ToString('yyyy-MM-dd')
    $item = ApiJson "repos/$repo/contents/state/daily/$day.json" -AllowMissing
    $record = $null
    if ($null -ne $item) {
        $record = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($item.content)) | ConvertFrom-Json
        if ($record.version -ne 1 -or $record.day -ne $day) { throw 'Daily delivery state is invalid; no automatic reset.' }
    }
    if ($record -and $record.status -eq 'completed') { Log "$day completed; no AI, Telegram or dispatch."; exit 0 }
    if ($now.Hour -ge 9) {
        Dispatch 'watchdog.yml' @('-f', ('check_id=pc-' + $now.ToString('yyyyMMdd-HHmm')))
        Log "$day delivery not confirmed; independent watchdog requested."
        exit 0
    }
    if ($record -and $record.run_id) { Log "$day already claimed; no duplicate preparation."; exit 0 }
    $runs = ApiJson "repos/$repo/actions/workflows/main.yml/runs?per_page=30"
    $active = @($runs.workflow_runs | Where-Object {
        $created = [DateTimeOffset]::Parse($_.created_at).ToOffset([TimeSpan]::FromHours(9))
        $created.ToString('yyyy-MM-dd') -eq $day -and $_.status -in @('queued','in_progress','requested','waiting','pending')
    })
    if ($active.Count -gt 0) { Log "$day existing active delivery workflow; no duplicate dispatch."; exit 0 }
    Dispatch 'main.yml' @('-f','mode=scheduled')
    Log "$day backup preparation requested; bot will wait until 09:00 KST AFTER search."
} catch {
    Log ('FAILED: ' + $_.Exception.Message)
    if (-not $CheckOnly) {
        # If GitHub itself is unavailable it cannot send Telegram. Show a local
        # warning as well; never export GitHub/Telegram credentials to this PC.
        try {
            $shell = New-Object -ComObject WScript.Shell
            $shell.Popup('GitHub bot backup check failed. Check gh auth status and the local checks.log. Telegram cannot be guaranteed while GitHub is unavailable.', 15, 'GitHub Infobot backup', 48) | Out-Null
        } catch { }
    }
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
