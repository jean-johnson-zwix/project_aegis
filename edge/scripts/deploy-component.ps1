# deploy-component.ps1 - Deploy com.project-aegis.publisher to the local
# Greengrass nucleus running in Docker.
#
# Run from edge\:
#   powershell -ExecutionPolicy Bypass -File scripts\deploy-component.ps1
#
# Optional:
#   $env:PUBLISH_INTERVAL='5'
#   $env:SCENARIO='FanFailure'

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Container       = 'project-aegis-greengrass'
$Component       = 'com.project-aegis.publisher'
$ComponentVersion = '1.0.0'
$Region          = 'us-west-2'
$CoreThingName   = 'project-aegis-greengrass-core'
$PublishInterval = if ($env:PUBLISH_INTERVAL) { $env:PUBLISH_INTERVAL } else { '60' }
$Scenario        = if ($env:SCENARIO) { $env:SCENARIO } else { '' }
$ScriptDir       = Split-Path -Parent $MyInvocation.MyCommand.Path
$EdgeDir         = Split-Path -Parent $ScriptDir

function Fail($Message) {
    Write-Error $Message
    exit 1
}

function Resolve-GreengrassCliPath {
    $resolved = docker exec $Container sh -lc 'if [ -x /greengrass/v2/bin/greengrass-cli ]; then echo /greengrass/v2/bin/greengrass-cli; exit 0; fi; for p in /greengrass/v2/packages/artifacts-unarchived/aws.greengrass.Cli/*/aws.greengrass.cli.client/cliclient/bin/greengrass-cli; do if [ -x "$p" ]; then echo "$p"; exit 0; fi; done; exit 1'
    if ($LASTEXITCODE -eq 0 -and $resolved) {
        return $resolved.Trim()
    }
    return ''
}

function Test-GreengrassCliIpcReady($Path) {
    if (-not $Path) {
        return $false
    }

    docker exec $Container sh -lc "$Path component list >/dev/null 2>&1"
    return ($LASTEXITCODE -eq 0)
}

Write-Host "==> Verifying Greengrass container is running..."
$running = docker ps --format '{{.Names}}' | Where-Object { $_ -eq $Container }
if (-not $running) {
    Fail "Container '$Container' is not running. Run: docker compose up -d"
}

Write-Host "==> Checking for Greengrass CLI binary..."
$CliPath = Resolve-GreengrassCliPath
$CliMissing = (-not $CliPath)
$SubmittedCliDeployment = $false

if ($CliMissing) {
    Write-Host "  CLI binary missing. Submitting Greengrass CLI deployment..."

    $NucleusVersion = docker exec $Container sh -lc "ls -1 /greengrass/v2/packages/artifacts-unarchived/aws.greengrass.Nucleus 2>/dev/null | head -1"
    $NucleusVersion = $NucleusVersion.Trim()
    if (-not $NucleusVersion) {
        Write-Host "  Could not detect nucleus artifact version; defaulting to 2.13.0"
        $NucleusVersion = '2.13.0'
    }

    $AccountId = aws sts get-caller-identity --query Account --output text --region $Region
    if ($LASTEXITCODE -ne 0 -or -not $AccountId) {
        Fail "Failed to detect AWS account. Check AWS credentials."
    }

    $ThingArn = "arn:aws:iot:${Region}:${AccountId}:thing/${CoreThingName}"
    $Timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $DeployJsonFile = Join-Path $env:TEMP 'gg-cli-deploy.json'

    $CliMergeJson = @{ AuthorizedPosixGroups = 'root' } | ConvertTo-Json -Compress
    $CliComponents = @{}
    $CliComponents['aws.greengrass.Cli'] = @{
        componentVersion = $NucleusVersion
        configurationUpdate = @{
            merge = $CliMergeJson
        }
    }
    $CliDeployment = @{
        targetArn = $ThingArn
        deploymentName = "cli-reinstall-$Timestamp"
        components = $CliComponents
    }
    $CliDeployment | ConvertTo-Json -Depth 10 | Set-Content -Encoding ASCII $DeployJsonFile

    $env:MSYS_NO_PATHCONV = '1'
    aws greengrassv2 create-deployment --cli-input-json "file://$DeployJsonFile" --region $Region
    if ($LASTEXITCODE -ne 0) {
        Fail "Failed to submit CLI deployment. Check AWS credentials and Greengrass permissions."
    }

    Write-Host "  CLI deployment submitted. Waiting for the component to install..."
    $SubmittedCliDeployment = $true
}

Write-Host "==> Waiting for Greengrass CLI to be ready..."
$MaxTries = 30
$Ready = $false
$RestartedForCli = $false
for ($i = 1; $i -le $MaxTries; $i++) {
    $CliPath = Resolve-GreengrassCliPath
    if ($CliPath -and (Test-GreengrassCliIpcReady $CliPath)) {
        $Ready = $true
        break
    }

    if ($CliPath -and $SubmittedCliDeployment -and -not $RestartedForCli) {
        Write-Host "  CLI files are present, but IPC auth is not ready. Restarting container once to load the CLI plugin..."
        docker restart $Container | Out-Null
        $RestartedForCli = $true
        Start-Sleep -Seconds 10
        continue
    }

    Write-Host "  ($i/$MaxTries) Not ready yet, waiting 5s..."
    Start-Sleep -Seconds 5
}
if (-not $Ready) {
    Fail "greengrass-cli not found after $($MaxTries * 5)s. Check: docker logs $Container"
}
Write-Host "  Using Greengrass CLI: $CliPath"

Write-Host "==> Copying Python artifacts to container..."
$ArtifactDest = "${Container}:/greengrass/local-artifacts/${Component}/${ComponentVersion}"
docker exec $Container mkdir -p "/greengrass/local-artifacts/${Component}/${ComponentVersion}"
foreach ($file in @('publisher.py', 'modbus_sim.py', 'validator.py', 'buffer.py', 'requirements.txt')) {
    docker cp (Join-Path $EdgeDir "artifacts\publisher\$file") "${ArtifactDest}/${file}"
}

Write-Host "==> Copying config files to container..."
foreach ($file in @('crac_units.yaml', 'anomaly_scenarios.yaml')) {
    docker cp (Join-Path $EdgeDir "config\$file") "${ArtifactDest}/${file}"
}

Write-Host "==> Copying recipe to container..."
docker exec $Container mkdir -p /greengrass/local-recipes
docker cp (Join-Path $EdgeDir 'recipe.yaml') "${Container}:/greengrass/local-recipes/${Component}-${ComponentVersion}.yaml"

$MergeConfig = @{
    publishIntervalSeconds = "$PublishInterval"
}
if ($Scenario) {
    $MergeConfig['activeScenario'] = "$Scenario"
}

$UpdateConfig = @{}
$UpdateConfig[$Component] = @{
    MERGE = $MergeConfig
}
$UpdateConfigJson = $UpdateConfig | ConvertTo-Json -Depth 10 -Compress

$ScenarioLabel = if ($Scenario) { $Scenario } else { 'None' }
Write-Host ('==> Deploying component (interval={0}s, scenario={1})...' -f $PublishInterval, $ScenarioLabel)

$TmpScript = Join-Path $env:TEMP 'gg-deploy.sh'
$DeployCommand = "$CliPath deployment create --recipeDir /greengrass/local-recipes --artifactDir /greengrass/local-artifacts --merge '${Component}=${ComponentVersion}' --update-config '${UpdateConfigJson}'"
(@('#!/bin/sh', $DeployCommand) -join "`n") | Set-Content -NoNewline -Encoding ASCII $TmpScript

docker cp $TmpScript "${Container}:/tmp/gg-deploy.sh"
docker exec $Container sh /tmp/gg-deploy.sh
if ($LASTEXITCODE -ne 0) {
    Fail "Local Greengrass deployment failed. Check /greengrass/v2/logs/greengrass.log."
}

Write-Host ""
Write-Host "Deployment submitted. Monitor logs:"
Write-Host "  docker logs -f $Container"
Write-Host "  docker exec $Container tail -f /greengrass/v2/logs/${Component}.log"
