# Fetch-DashboardMetrics.ps1
<#
Fetches metrics defined in metrics.json from OpenSearch Dashboards'
console proxy and appends rows to daily CSV files.
Production mode: one rolling five-minute query per metric. Historical checkpoints do not select the query start.
#>
param(
    [int] $MaxWindowMinutes = 5,
    [int] $MaxChunksPerRun = 1,
    [int] $MaxRunMinutes = 9,
    [string] $OutputRoot = "C:\Users\triba\OneDrive - IP-Tribe\Shortcuts\Tribal Eye - Tribal AI\OpenSearch data"
)
# ---------------- CONFIG ----------------
$Backfill = $false
$MaxWindowMinutes = 5
$MaxChunksPerRun = 1
$MetricsConfigPath = Join-Path $PSScriptRoot "metrics.json"
# Works on Windows and Linux.
$UserHomeDir = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::UserProfile
)
if ([string]::IsNullOrWhiteSpace($UserHomeDir)) {
    throw "Cannot determine the current user's home directory."
}
$OpenSearchDir = Join-Path $UserHomeDir ".opensearch"
$CredPath = Join-Path $OpenSearchDir "cred.xml"
$StateDir = Join-Path $OpenSearchDir "state"
$LogDir = $OpenSearchDir
$LogNamePrefix = "metrics_fetch_"
$LogRetentionDays = 14
$PinnedThumbprint = "B4DE9AA89B46E35EFA7A7F58BD12C4C49A76F517"
$LoginTimeoutSec = 60
$QueryTimeoutSec = 120
$runDeadline = (Get-Date).AddMinutes($MaxRunMinutes)
# -----------------------------------------
function Write-Log($message) {
    $now = Get-Date
    $line = "{0} - {1}" -f $now.ToString("yyyy-MM-dd HH:mm:ss"), $message
    Write-Host $line
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir -Force `
                -ErrorAction Stop | Out-Null
        }
        $path = Join-Path $LogDir (
            "{0}{1}.log" -f $LogNamePrefix, $now.ToString("yyyy-MM-dd")
        )
        Add-Content -LiteralPath $path -Value $line `
            -Encoding UTF8 -ErrorAction Stop
    }
    catch {
        # Logging errors must not terminate fetching.
        Write-Output $line
    }
}
function Remove-ExpiredLogs {
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            return
        }
        $cutoff = (Get-Date).Date.AddDays(-$LogRetentionDays)
        $pattern = "^{0}(\d{{4}}-\d{{2}}-\d{{2}})\.log$" -f (
            [regex]::Escape($LogNamePrefix)
        )
        foreach (
            $file in Get-ChildItem -LiteralPath $LogDir -File `
                -Filter "${LogNamePrefix}*.log"
        ) {
            $match = [regex]::Match($file.Name, $pattern)
            if (-not $match.Success) {
                continue
            }
            $fileDate = [DateTime]::MinValue
            $parsed = [DateTime]::TryParseExact(
                $match.Groups[1].Value,
                'yyyy-MM-dd',
                $null,
                [Globalization.DateTimeStyles]::None,
                [ref] $fileDate
            )
            if (-not $parsed) {
                continue
            }
            if ($fileDate -lt $cutoff) {
                Remove-Item -LiteralPath $file.FullName `
                    -Force -ErrorAction Stop
            }
        }
    }
    catch {
        Write-Log "WARN: log cleanup failed - $($_.Exception.Message)"
    }
}
function Export-CsvWithRetry($rows, $path) {
    $attempts = 0
    while ($true) {
        try {
            # Rolling windows overlap. Replace rows by timestamp + dimensions.
            $incoming = @($rows)
            if ($incoming.Count -eq 0) { return }
            $names = @($incoming[0].PSObject.Properties.Name)
            $keyNames = @($names | Select-Object -SkipLast 1)
            $merged = [ordered]@{}
            $existing = if (Test-Path -LiteralPath $path) {
                @(Import-Csv -LiteralPath $path -ErrorAction Stop)
            } else { @() }
            foreach ($entry in (@($existing) + $incoming)) {
                $keyValues = @($keyNames | ForEach-Object { [string]$entry.$_ })
                $key = ConvertTo-Json -InputObject $keyValues -Compress
                $merged[$key] = $entry
            }
            $temporary = "$path.$([Guid]::NewGuid().ToString('N')).tmp"
            try {
                @($merged.Values) | Export-Csv -LiteralPath $temporary `
                    -NoTypeInformation -Encoding UTF8 -Force -ErrorAction Stop
                Move-Item -LiteralPath $temporary -Destination $path -Force -ErrorAction Stop
            } finally {
                if (Test-Path -LiteralPath $temporary) {
                    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
                }
            }
            return
        }
        catch {
            $attempts++
            if ($attempts -ge 5) {
                throw
            }
            Start-Sleep -Seconds 2
        }
    }
}
function Find-TimestampRangeNode($node) {
    if ($null -eq $node) {
        return $null
    }
    if ($node -is [System.Management.Automation.PSCustomObject]) {
        if ($node.PSObject.Properties.Name -contains 'range') {
            $fieldProp = $node.range.PSObject.Properties |
                Select-Object -First 1
            if ($fieldProp) {
                return $fieldProp.Value
            }
        }
        foreach ($prop in $node.PSObject.Properties) {
            $found = Find-TimestampRangeNode $prop.Value
            if ($found) {
                return $found
            }
        }
    }
    elseif ($node -is [Array]) {
        foreach ($item in $node) {
            $found = Find-TimestampRangeNode $item
            if ($found) {
                return $found
            }
        }
    }
    return $null
}
function Get-MetricSchema($query) {
    $schema = @{
        Levels = @()
        ValueLabel = 'Count'
        ValueAggName = $null
    }
    if (-not $query.aggs) {
        return $schema
    }
    # First aggregation is expected to be the date histogram.
    $node = (
        $query.aggs.PSObject.Properties |
            Select-Object -First 1
    ).Value
    while ($node.aggs) {
        $childProp = $node.aggs.PSObject.Properties |
            Select-Object -First 1
        $child = $childProp.Value
        if ($child.PSObject.Properties.Name -contains 'terms') {
            $direction = 'Descending'
            if ($child.terms.order) {
                $orderValue = (
                    $child.terms.order.PSObject.Properties |
                        Select-Object -First 1
                ).Value
                if ("$orderValue" -eq 'asc') {
                    $direction = 'Ascending'
                }
            }
            $schema.Levels += @{
                AggName = $childProp.Name
                Label = "$($child.terms.field): $direction"
            }
            $node = $child
            continue
        }
        $metricProp = $child.PSObject.Properties |
            Where-Object {
                $_.Name -in @(
                    'sum',
                    'avg',
                    'min',
                    'max',
                    'cardinality',
                    'value_count'
                )
            } |
            Select-Object -First 1
        if ($metricProp) {
            $schema.ValueAggName = $childProp.Name
            $field = $metricProp.Value.field
            $schema.ValueLabel = switch ($metricProp.Name) {
                'sum'         { "Sum of $field" }
                'avg'         { "Average of $field" }
                'min'         { "Min of $field" }
                'max'         { "Max of $field" }
                'cardinality' { "Unique count of $field" }
                'value_count' { "Count of $field" }
            }
        }
        break
    }
    return $schema
}
function Expand-BucketRows($bucket, $schema, $depth, $prefix) {
    $levels = @($schema.Levels)
    if ($depth -ge $levels.Count) {
        $row = [ordered] @{}
        foreach ($key in $prefix.Keys) {
            $row[$key] = $prefix[$key]
        }
        $row[$schema.ValueLabel] = if ($schema.ValueAggName) {
            $bucket.($schema.ValueAggName).value
        }
        else {
            $bucket.doc_count
        }
        return @([PSCustomObject] $row)
    }
    $level = $levels[$depth]
    $sub = $bucket.($level.AggName)
    if (-not $sub -or -not $sub.buckets) {
        return @()
    }
    $rows = @()
    foreach ($subBucket in $sub.buckets) {
        $next = [ordered] @{}
        foreach ($key in $prefix.Keys) {
            $next[$key] = $prefix[$key]
        }
        $next[$level.Label] = $subBucket.key
        $rows += Expand-BucketRows $subBucket $schema ($depth + 1) $next
    }
    return $rows
}
function Export-MetricRows($rows, $path) {
    $expectedHeader = '"' + (
        (@($rows)[0].PSObject.Properties.Name) -join '","'
    ) + '"'
    if (Test-Path -LiteralPath $path) {
        $currentHeader = @(
            Get-Content -LiteralPath $path -TotalCount 1
        )[0]
        if ($currentHeader -and $currentHeader -ne $expectedHeader) {
            $dir = Split-Path $path -Parent
            $base = [IO.Path]::GetFileNameWithoutExtension($path)
            $n = 1
            do {
                $suffix = if ($n -eq 1) { '' } else { $n }
                $legacy = Join-Path $dir "$base.legacy$suffix.csv"
                $n++
            }
            while (Test-Path -LiteralPath $legacy)
            Move-Item -LiteralPath $path -Destination $legacy `
                -ErrorAction Stop
            Write-Log (
                "Rotated $([IO.Path]::GetFileName($path)) -> " +
                "$([IO.Path]::GetFileName($legacy)) (column layout changed)."
            )
        }
    }
    Export-CsvWithRetry $rows $path
}
try {
    Remove-ExpiredLogs
    $osUser = [Environment]::UserName
    Write-Log (
        "=== Run start (PS $($PSVersionTable.PSVersion), " +
        "user $osUser, output root '$OutputRoot', " +
        "window ${MaxWindowMinutes}m, " +
        "max $MaxChunksPerRun chunks/metric) ==="
    )
    [Net.ServicePointManager]::SecurityProtocol = (
        [Net.ServicePointManager]::SecurityProtocol -bor
        [Net.SecurityProtocolType]::Tls12
    )
    # Original certificate-validation behavior retained.
    [Net.ServicePointManager]::ServerCertificateValidationCallback = {
        param($senderObj, $certificate, $chain, $sslPolicyErrors)
        if ($sslPolicyErrors -eq [Net.Security.SslPolicyErrors]::None) {
            return $true
        }
        if (
            $sslPolicyErrors -eq
            [Net.Security.SslPolicyErrors]::RemoteCertificateNameMismatch
        ) {
            $presented = New-Object `
                System.Security.Cryptography.X509Certificates.X509Certificate2($certificate)
            if ($presented.Thumbprint -eq $PinnedThumbprint) {
                return $true
            }
            Write-Log (
                "Certificate thumbprint mismatch! " +
                "Expected $PinnedThumbprint but got " +
                "$($presented.Thumbprint). Rejecting connection."
            )
        }
        return $false
    }.GetNewClosure()
    if (-not (Test-Path -LiteralPath $CredPath -PathType Leaf)) {
        throw "Credential file not found at '$CredPath'. Run credential setup first."
    }
    if (-not (Test-Path -LiteralPath $MetricsConfigPath -PathType Leaf)) {
        throw "Metrics config not found at '$MetricsConfigPath'."
    }
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir `
            -Force -ErrorAction Stop | Out-Null
    }
    if (-not (Test-Path -LiteralPath $OutputRoot)) {
        New-Item -ItemType Directory -Path $OutputRoot `
            -Force -ErrorAction Stop | Out-Null
    }
    $cred = Import-Clixml -LiteralPath $CredPath -ErrorAction Stop
    if ($cred -isnot [System.Management.Automation.PSCredential]) {
        throw "Credential file '$CredPath' does not contain a PSCredential."
    }
    $networkCred = $cred.GetNetworkCredential()
    $metrics = Get-Content -LiteralPath $MetricsConfigPath `
        -Raw -Encoding UTF8 -ErrorAction Stop |
        ConvertFrom-Json -ErrorAction Stop
    $sessionsByBaseUrl = @{}
    foreach ($baseUrl in ($metrics.baseUrl | Select-Object -Unique)) {
        try {
            Write-Host (
                "Attempting OpenSearch login to $baseUrl " +
                "as user '$($networkCred.UserName)'..."
            )
            $loginBody = @{
                username = $networkCred.UserName
                password = $networkCred.Password
            } | ConvertTo-Json
            Invoke-WebRequest `
                -Uri "$baseUrl/_dashboards/auth/login" `
                -SkipCertificateCheck `
                -Method Post `
                -Headers @{ "osd-xsrf" = "true" } `
                -ContentType "application/json" `
                -Body $loginBody `
                -SessionVariable webSession `
                -UseBasicParsing `
                -TimeoutSec $LoginTimeoutSec `
                -ErrorAction Stop |
                Out-Null
            $sessionsByBaseUrl[$baseUrl] = $webSession
            $message = "SUCCESS: OpenSearch login request succeeded for $baseUrl."
            Write-Host $message
            Write-Log $message
        }
        catch {
            Write-Host "FAILED: OpenSearch login to $baseUrl"
            $exception = $_.Exception
            while ($null -ne $exception) {
                Write-Host (
                    "{0}: {1}" -f $exception.GetType().FullName,
                    $exception.Message
                )
                $exception = $exception.InnerException
            }
            Write-Log "ERROR: Login failed for $baseUrl - $($_.Exception.Message)"
        }
    }
    # A total login failure should produce a nonzero process exit code.
    if ($sessionsByBaseUrl.Count -eq 0) {
        throw "No OpenSearch login succeeded."
    }
    foreach ($metric in $metrics) {
        $name = $metric.name
        try {
            if (-not $sessionsByBaseUrl.ContainsKey($metric.baseUrl)) {
                throw "No valid session for base URL $($metric.baseUrl)"
            }
            $webSession = $sessionsByBaseUrl[$metric.baseUrl]
            $schema = Get-MetricSchema $metric.query
            $stateFile = Join-Path $StateDir "$name.json"
            # Floor UTC time to the start of the current minute.
            # This boundary excludes the current, incomplete minute.
            $clockUtc = [DateTime]::UtcNow
            $nowUtc = $clockUtc.AddTicks(
                -($clockUtc.Ticks % [TimeSpan]::TicksPerMinute)
            )

            $gteUtc = $nowUtc.AddMinutes(-5)

            Write-Log (
                "[$name] COMPLETE-MINUTE window: " +
                "$($gteUtc.ToString('o')) inclusive to " +
                "$($nowUtc.ToString('o')) exclusive."
            )
            Write-Log "[$name] LIVE window: $($gteUtc.ToString('o')) to $($nowUtc.ToString('o'))."
            if ($gteUtc -ge $nowUtc) {
                Write-Log "[$name] Nothing to fetch yet (gte >= now). Skipping."
                continue
            }
            $behindMinutes = [math]::Round(
                ($nowUtc - $gteUtc).TotalMinutes
            )
            if ($behindMinutes -gt $MaxWindowMinutes) {
                Write-Log (
                    "[$name] Behind by $behindMinutes min; " +
                    "catching up in ${MaxWindowMinutes}-minute chunks."
                )
            }
            $chunk = 0
            $totalRows = 0
            while (
                $gteUtc -lt $nowUtc -and
                $chunk -lt $MaxChunksPerRun
            ) {
                if ((Get-Date) -ge $runDeadline) {
                    Write-Log (
                        "[$name] Run time budget reached; stopping after " +
                        "$chunk chunk(s). Next run resumes from here."
                    )
                    break
                }
                $chunk++
                $lteUtc = $nowUtc
                if ($lteUtc -gt $nowUtc) {
                    $lteUtc = $nowUtc
                }
                $gteStr = $gteUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
                $lteStr = $lteUtc.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
                $queryCopy = $metric.query |
                    ConvertTo-Json -Depth 20 |
                    ConvertFrom-Json
                $rangeNode = Find-TimestampRangeNode $queryCopy
                if (-not $rangeNode) {
                    throw "Could not find a timestamp range filter in this metric's query template."
                }
                # queryCopy is created using ConvertFrom-Json, so rangeNode
                # is a PSCustomObject. Remove conflicting range boundaries.
                foreach ($property in @("gt", "lte", "lt")) {
                    $rangeNode.PSObject.Properties.Remove($property)
                }
                
                $rangeNode | Add-Member `
                    -MemberType NoteProperty `
                    -Name "gte" `
                    -Value $gteStr `
                    -Force

                $rangeNode | Add-Member `
                    -MemberType NoteProperty `
                    -Name "lt" `
                    -Value $lteStr `
                    -Force
                $queryBody = $queryCopy | ConvertTo-Json -Depth 20
                Write-Log "[$name] Querying chunk $chunk from $gteStr to $lteStr."
                $encodedPath = [Uri]::EscapeDataString(
                    "$($metric.index)/_search"
                )
                $proxyUrl = (
                    "$($metric.baseUrl)/_dashboards/api/console/proxy" +
                    "?path=$encodedPath&method=POST"
                )
                $headers = @{
                    "osd-xsrf" = "true"
                    "kbn-xsrf" = "true"
                }
                $response = $null
                $attempt = 0
                while ($true) {
                    $attempt++
                    try {
                        $response = Invoke-RestMethod `
                            -Uri $proxyUrl `
                            -SkipCertificateCheck `
                            -Method Post `
                            -Headers $headers `
                            -ContentType "application/json" `
                            -Body $queryBody `
                            -WebSession $webSession `
                            -TimeoutSec $QueryTimeoutSec `
                            -ErrorAction Stop
                        break
                    }
                    catch {
                        $statusCode = $null
                        if ($null -ne $_.Exception.Response) {
                            $statusCode = [int] $_.Exception.Response.StatusCode
                        }
                        $isTransient = (
                            $null -ne $statusCode -and
                            $statusCode -ge 500 -and
                            $statusCode -le 599
                        )
                        if (-not $isTransient -or $attempt -ge 3) {
                            throw
                        }
                        $backoffSeconds = 5 * $attempt
                        Write-Log (
                            "[$name] Transient error ($statusCode) on " +
                            "attempt $attempt, retrying in ${backoffSeconds}s..."
                        )
                        Start-Sleep -Seconds $backoffSeconds
                    }
                }
                $aggProp = $response.aggregations.PSObject.Properties |
                    Select-Object -First 1
                if (-not $aggProp) {
                    throw "Response had no aggregations."
                }
                $buckets = $aggProp.Value.buckets
                if (-not $buckets -or $buckets.Count -eq 0) {
                    Write-Log (
                        "[$name] No buckets returned for window " +
                        "$gteStr to $lteStr."
                    )
                                        if ($Backfill) {
                        @{ nextStartUtc = $lteUtc.AddMilliseconds(1).ToString("o"); endUtc = $nowUtc.ToString("o") } |
                            ConvertTo-Json | Set-Content -LiteralPath $stateFile -ErrorAction Stop
                    }
                    else {
                        @{ lastBucketUtc = $lteUtc.ToString("o") } |
                            ConvertTo-Json | Set-Content -LiteralPath $stateFile -ErrorAction Stop
                    }
                    $gteUtc = $lteUtc.AddMilliseconds(1)
                    continue
                }
                $rowsByDate = @{}
                $chunkRowCount = 0
                foreach ($bucket in $buckets) {
                    $bucketTimeSgt = $bucket.key_as_string
                    $bucketDate = $bucketTimeSgt.Substring(0, 10)
                    $bucketRows = Expand-BucketRows `
                        $bucket `
                        $schema `
                        0 `
                        ([ordered] @{ Timestamp = $bucketTimeSgt })
                    if (@($bucketRows).Count -eq 0) {
                        continue
                    }
                    if (-not $rowsByDate.ContainsKey($bucketDate)) {
                        $rowsByDate[$bucketDate] = New-Object `
                            System.Collections.ArrayList
                    }
                    foreach ($row in $bucketRows) {
                        [void] $rowsByDate[$bucketDate].Add($row)
                        $chunkRowCount++
                    }
                }
                $metricOutputDir = Join-Path $OutputRoot $name
                if (-not (Test-Path -LiteralPath $metricOutputDir)) {
                    New-Item -ItemType Directory -Path $metricOutputDir `
                        -Force -ErrorAction Stop | Out-Null
                }
                foreach ($bucketDate in ($rowsByDate.Keys | Sort-Object)) {
                    $dailyCsv = Join-Path $metricOutputDir "${name}_$bucketDate.csv"
                    Export-MetricRows $rowsByDate[$bucketDate] $dailyCsv
                    Write-Log "[$name] CSV written: $dailyCsv"
                }
                $totalRows += $chunkRowCount
                $maxEpochMs = (
                    $buckets |
                        Measure-Object -Property key -Maximum
                ).Maximum
                $maxBucketUtc = [DateTimeOffset]::FromUnixTimeMilliseconds(
                    [int64] $maxEpochMs
                ).UtcDateTime
                $nextGte = $lteUtc.AddMilliseconds(1)
                if ($nextGte -le $gteUtc) {
                    $nextGte = $lteUtc.AddMilliseconds(1)
                }
                                if ($Backfill) {
                    # Advance by successful query window, not histogram bucket start.
                    $nextGte = $lteUtc.AddMilliseconds(1)
                    @{ nextStartUtc = $nextGte.ToString("o"); endUtc = $nowUtc.ToString("o") } |
                        ConvertTo-Json | Set-Content -LiteralPath $stateFile -ErrorAction Stop
                }
                else {
                    @{ lastBucketUtc = $nextGte.AddMilliseconds(-1).ToString("o") } |
                        ConvertTo-Json | Set-Content -LiteralPath $stateFile -ErrorAction Stop
                }
                Write-Log (
                    "[$name] Appended $chunkRowCount rows for window " +
                    "$gteStr to $lteStr."
                )
                $gteUtc = $nextGte
            }
            if (
                $chunk -ge $MaxChunksPerRun -and
                $gteUtc -lt $nowUtc
            ) {
                $stillBehind = [math]::Round(
                    ($nowUtc - $gteUtc).TotalMinutes
                )
                Write-Log (
                    "[$name] Chunk cap ($MaxChunksPerRun) reached; " +
                    "still $stillBehind min behind. Next run continues."
                )
            }
            if ($Backfill -and $gteUtc -ge $nowUtc) {
                Write-Log "[$name] BACKFILL COMPLETE: reached fixed end $($nowUtc.ToString('o'))."
            }
            if ($chunk -gt 1) {
                Write-Log "[$name] Done: $totalRows rows across $chunk chunk(s)."
            }
        }
        catch {
            Write-Log "[$name] ERROR: $($_.Exception.Message)"
        }
    }
    Write-Log "=== Run end ==="
}
catch {
    $message = (
        "FATAL: $($_.Exception.GetType().Name) at line " +
        "$($_.InvocationInfo.ScriptLineNumber): " +
        "$($_.Exception.Message)"
    )
    Write-Host $message
    Write-Log $message
    throw
}