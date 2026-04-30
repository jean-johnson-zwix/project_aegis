# deploy-component.ps1 — Deploy com.project-aegis.publisher to the local Greengrass nucleus.
#
# Copies Python files + config files into the versioned artifacts directory inside
# the container, then calls greengrass-cli to deploy the component locally.
#
# Run from the edge\ directory:
#   cd edge
#   powershell -ExecutionPolicy Bypass -File scripts\deploy-component.ps1
#
# To change publish interval for demo mode:
#   $env:PUBLISH_INTERVAL=5; powershell -ExecutionPolicy Bypass -File scripts\deploy-component.ps1
#
# To inject an anomaly scenario:
#   $env:SCENARIO='FanFailure'; powershell -ExecutionPolicy Bypass -File scripts\deploy-component.ps1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Container       = 'project-aegis-greengrass'
$Component       = 'com.project-aegis.publisher'
$Version         = '1.0.0'
$PublishInterval = if ($env:PUBLISH_INTERVAL) { $env:PUBLISH_INTERVAL } else { '60' }
$Scenario        = if ($env:SCENARIO)         { $env:SCENARIO }         else { '' }
$ScriptDir       = Split-Path -Parent $MyInvocation.MyCommand.Path
$EdgeDir         = Split-Path -Parent $ScriptDir

# ---------------------------------------------------------------------------
# Verify container is running
# ---------------------------------------------------------------------------
Write-Host "==> Verifying Greengrass container is running..."
$running = docker ps --format '{{.Names}}' | Where-Object { $_ -eq $Container }
if (-not $running) {
    Write-Error "Container '$Container' is not running. Run: docker-compose up -d"
    exit 1
}

# ---------------------------------------------------------------------------
# Wait for the Greengrass CLI to be ready.
# The CLIService creates /greengrass/v2/bin/greengrass-cli on startup.
# ---------------------------------------------------------------------------
Write-Host "==> Waiting for Greengrass CLI to be ready..."
$CliPath  = '/greengrass/v2/bin/greengrass-cli'
$MaxTries = 30
$Ready    = $false
for ($i = 1; $i -le $MaxTries; $i++) {
    docker exec $Container test -x $CliPath
    if ($LASTEXITCODE -eq 0) {
        $Ready = $true
        break
    }
    Write-Host "  ($i/$MaxTries) Not ready yet, waiting 5s..."
    Start-Sleep -Seconds 5
}
if (-not $Ready) {
    Write-Error "greengrass-cli not found after $($MaxTries * 5)s.`n  Check nucleus logs: docker logs $Container`n  Ensure the project-aegis-install-cli deployment succeeded in AWS IoT Greengrass."
    exit 1
}

# ---------------------------------------------------------------------------
# Copy Python artifacts into the container
# ---------------------------------------------------------------------------
Write-Host "==> Copying Python artifacts to container..."
$ArtifactDest = "${Container}:/greengrass/local-artifacts/${Component}/${Version}"
docker exec $Container mkdir -p "/greengrass/local-artifacts/${Component}/${Version}"
foreach ($f in @('publisher.py','modbus_sim.py','validator.py','buffer.py','requirements.txt')) {
    docker cp (Join-Path $EdgeDir "artifacts\publisher\$f") "${ArtifactDest}/${f}"
}

Write-Host "==> Copying config files to container..."
foreach ($f in @('crac_units.yaml','anomaly_scenarios.yaml')) {
    docker cp (Join-Path $EdgeDir "config\$f") "${ArtifactDest}/${f}"
}

# ---------------------------------------------------------------------------
# Copy recipe into the container
# ---------------------------------------------------------------------------
Write-Host "==> Copying recipe to container..."
docker exec $Container mkdir -p /greengrass/local-recipes
docker cp (Join-Path $EdgeDir 'recipe.yaml') `
    "${Container}:/greengrass/local-recipes/${Component}-${Version}.yaml"

# ---------------------------------------------------------------------------
# Build merge-config JSON and deploy
# ---------------------------------------------------------------------------
$MergeJson = "{`"publishIntervalSeconds`":`"${PublishInterval}`""
if ($Scenario) {
    $MergeJson += ",`"ACTIVE_SCENARIO`":`"${Scenario}`""
}
$MergeJson += '}'

Write-Host "==> Deploying component (interval=${PublishInterval}s, scenario=$(if ($Scenario) { $Scenario } else { 'None' }))..."
# greengrass-cli --update-config only accepts a JSON string, not a file path.
# PowerShell strips embedded double quotes when passing strings to docker exec,
# so we write a shell script with the JSON single-quoted inside it — single quotes
# in sh prevent any further interpretation of the double quotes.
$UpdateConfigJson = "{`"${Component}`":{`"MERGE`":${MergeJson}}}"
$DeployScript = @"
#!/bin/sh
/greengrass/v2/bin/greengrass-cli deployment create --recipeDir /greengrass/local-recipes --artifactDir /greengrass/local-artifacts --merge '${Component}=${Version}' --update-config '${UpdateConfigJson}'
"@
$TmpScript = "$env:TEMP\gg-deploy.sh"
$DeployScript | Set-Content -NoNewline -Encoding ASCII $TmpScript
docker cp $TmpScript "${Container}:/tmp/gg-deploy.sh"
docker exec $Container sh /tmp/gg-deploy.sh

Write-Host ""
Write-Host "Deployment submitted. Monitor logs:"
Write-Host "  docker logs -f $Container"
Write-Host "  docker exec $Container tail -f /greengrass/v2/logs/${Component}.log"
