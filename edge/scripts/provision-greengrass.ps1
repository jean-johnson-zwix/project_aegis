# provision-greengrass.ps1 — One-time setup for the Greengrass core device.
#
# What this does:
#   1. Creates an IoT certificate + private key for the Greengrass core device.
#   2. Downloads Amazon Root CA 1.
#   3. Attaches the Greengrass core policy (created by CDK) to the certificate.
#   4. Attaches the certificate to the core device Thing.
#   5. Retrieves IoT endpoints and fills in config/greengrass-config.yaml.
#
# Prerequisites:
#   - AWS CLI v2 installed and configured (aws configure)
#   - `cdk deploy ProjectAegisGreengrassStack` has been run
#
# Run from the edge\ directory:
#   cd edge
#   powershell -ExecutionPolicy Bypass -File scripts\provision-greengrass.ps1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Region    = 'us-west-2'
$ThingName = 'project-aegis-greengrass-core'
$PolicyName = 'project-aegis-greengrass-core-policy'
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$EdgeDir    = Split-Path -Parent $ScriptDir
$CertDir    = Join-Path $EdgeDir 'certs'
$ConfigDir  = Join-Path $EdgeDir 'config'

# ---------------------------------------------------------------------------
# Verify AWS CLI is available
# ---------------------------------------------------------------------------
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Error "AWS CLI not found. Install from https://aws.amazon.com/cli/"
    exit 1
}

Write-Host "==> Creating certs directory at $CertDir"
New-Item -ItemType Directory -Force -Path $CertDir | Out-Null

# ---------------------------------------------------------------------------
# 1. Create IoT certificate + key
# ---------------------------------------------------------------------------
Write-Host "==> Creating IoT certificate..."
$CertJson = aws iot create-keys-and-certificate `
    --set-as-active `
    --region $Region `
    --output json | ConvertFrom-Json

$CertArn = $CertJson.certificateArn
$CertId  = $CertJson.certificateId

$CertJson.certificatePem      | Set-Content -NoNewline -Path (Join-Path $CertDir 'device.pem.crt')
$CertJson.keyPair.PrivateKey  | Set-Content -NoNewline -Path (Join-Path $CertDir 'private.pem.key')

Write-Host "  Certificate ARN: $CertArn"
Write-Host "  Certificate ID:  $CertId"

# ---------------------------------------------------------------------------
# 2. Download Amazon Root CA 1
# ---------------------------------------------------------------------------
Write-Host "==> Downloading Amazon Root CA 1..."
Invoke-WebRequest `
    -Uri 'https://www.amazontrust.com/repository/AmazonRootCA1.pem' `
    -OutFile (Join-Path $CertDir 'AmazonRootCA1.pem')

# ---------------------------------------------------------------------------
# 3. Attach Greengrass core policy to certificate
# ---------------------------------------------------------------------------
Write-Host "==> Attaching policy $PolicyName to certificate..."
aws iot attach-policy `
    --policy-name $PolicyName `
    --target $CertArn `
    --region $Region

# ---------------------------------------------------------------------------
# 4. Attach certificate to Thing
# ---------------------------------------------------------------------------
Write-Host "==> Attaching certificate to Thing $ThingName..."
aws iot attach-thing-principal `
    --thing-name $ThingName `
    --principal $CertArn `
    --region $Region

# ---------------------------------------------------------------------------
# 5. Retrieve IoT endpoints
# ---------------------------------------------------------------------------
Write-Host "==> Retrieving IoT endpoints..."
$IotDataEndpoint = aws iot describe-endpoint `
    --endpoint-type iot:Data-ATS `
    --region $Region `
    --query 'endpointAddress' `
    --output text

$IotCredEndpoint = aws iot describe-endpoint `
    --endpoint-type iot:CredentialProvider `
    --region $Region `
    --query 'endpointAddress' `
    --output text

Write-Host "  Data endpoint: $IotDataEndpoint"
Write-Host "  Cred endpoint: $IotCredEndpoint"

# ---------------------------------------------------------------------------
# 6. Generate greengrass-config.yaml from template
# ---------------------------------------------------------------------------
Write-Host "==> Generating config\greengrass-config.yaml..."
$Template = Get-Content -Raw (Join-Path $ConfigDir 'greengrass-config.yaml.template')
$Config = $Template `
    -replace '<REPLACE_ME_IOT_DATA_ENDPOINT>', $IotDataEndpoint `
    -replace '<REPLACE_ME_IOT_CRED_ENDPOINT>', $IotCredEndpoint
$Config | Set-Content -NoNewline -Path (Join-Path $ConfigDir 'greengrass-config.yaml')

Write-Host ""
Write-Host "Done. Files created:"
Write-Host "  $CertDir\device.pem.crt"
Write-Host "  $CertDir\private.pem.key"
Write-Host "  $CertDir\AmazonRootCA1.pem"
Write-Host "  $ConfigDir\greengrass-config.yaml"
Write-Host ""
Write-Host "Next: docker-compose up -d"
Write-Host "      bash scripts\deploy-component.sh"
