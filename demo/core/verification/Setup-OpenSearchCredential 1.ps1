<#
Run this ONCE, interactively, as the Windows user that will run the scheduled task.
It prompts for your OpenSearch username/password via the native Windows credential
dialog (Claude never sees these values) and saves them DPAPI-encrypted, readable
only by this Windows account on this machine.
#>

$credDir = Join-Path $env:USERPROFILE ".opensearch"
if (-not (Test-Path $credDir)) {
    New-Item -ItemType Directory -Path $credDir | Out-Null
}

$credPath = Join-Path $credDir "cred.xml"

$cred = Get-Credential -Message "Enter your OpenSearch username and password (used for API access via the Dashboards proxy)"

$cred | Export-Clixml -Path $credPath

Write-Host "Saved encrypted credential to $credPath"
Write-Host "This file can only be decrypted by your Windows account ($env:USERNAME) on this machine."
