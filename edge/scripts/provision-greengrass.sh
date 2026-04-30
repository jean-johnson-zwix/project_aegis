#!/usr/bin/env bash
# provision-greengrass.sh — One-time setup for the Greengrass core device.
#
# Windows users: use the PowerShell script instead — it handles AWS CLI and
# Windows paths natively without PATH issues:
#
#   cd edge
#   powershell -ExecutionPolicy Bypass -File scripts\provision-greengrass.ps1
#
# Linux / macOS users: run this script directly:
#
#   cd edge && bash scripts/provision-greengrass.sh

set -euo pipefail

if ! command -v aws &>/dev/null; then
  echo "ERROR: aws CLI not found on PATH." >&2
  echo "On Windows, use: powershell -ExecutionPolicy Bypass -File scripts\\provision-greengrass.ps1" >&2
  exit 1
fi

REGION="us-west-2"
THING_NAME="project-aegis-greengrass-core"
POLICY_NAME="project-aegis-greengrass-core-policy"
CERT_DIR="$(dirname "$0")/../certs"
CONFIG_DIR="$(dirname "$0")/../config"

echo "==> Creating certs directory at $CERT_DIR"
mkdir -p "$CERT_DIR"

# ---------------------------------------------------------------------------
# 1. Create IoT certificate + key
# ---------------------------------------------------------------------------
echo "==> Creating IoT certificate..."
CERT_RESPONSE=$(aws iot create-keys-and-certificate \
  --set-as-active \
  --region "$REGION" \
  --output json)

CERT_ARN=$(echo "$CERT_RESPONSE" | jq -r '.certificateArn')
CERT_ID=$(echo "$CERT_RESPONSE" | jq -r '.certificateId')
echo "$CERT_RESPONSE" | jq -r '.certificatePem'     > "$CERT_DIR/device.pem.crt"
echo "$CERT_RESPONSE" | jq -r '.keyPair.PrivateKey' > "$CERT_DIR/private.pem.key"
chmod 600 "$CERT_DIR/private.pem.key"

echo "  Certificate ARN: $CERT_ARN"
echo "  Certificate ID:  $CERT_ID"

# ---------------------------------------------------------------------------
# 2. Download Amazon Root CA 1
# ---------------------------------------------------------------------------
echo "==> Downloading Amazon Root CA 1..."
curl -sS "https://www.amazontrust.com/repository/AmazonRootCA1.pem" \
  -o "$CERT_DIR/AmazonRootCA1.pem"

# ---------------------------------------------------------------------------
# 3. Attach Greengrass core policy to certificate
# ---------------------------------------------------------------------------
echo "==> Attaching policy $POLICY_NAME to certificate..."
aws iot attach-policy \
  --policy-name "$POLICY_NAME" \
  --target "$CERT_ARN" \
  --region "$REGION"

# ---------------------------------------------------------------------------
# 4. Attach certificate to Thing
# ---------------------------------------------------------------------------
echo "==> Attaching certificate to Thing $THING_NAME..."
aws iot attach-thing-principal \
  --thing-name "$THING_NAME" \
  --principal "$CERT_ARN" \
  --region "$REGION"

# ---------------------------------------------------------------------------
# 5. Retrieve IoT endpoints
# ---------------------------------------------------------------------------
echo "==> Retrieving IoT endpoints..."
IOT_DATA_ENDPOINT=$(aws iot describe-endpoint \
  --endpoint-type iot:Data-ATS \
  --region "$REGION" \
  --query 'endpointAddress' --output text)
IOT_CRED_ENDPOINT=$(aws iot describe-endpoint \
  --endpoint-type iot:CredentialProvider \
  --region "$REGION" \
  --query 'endpointAddress' --output text)

echo "  Data endpoint: $IOT_DATA_ENDPOINT"
echo "  Cred endpoint: $IOT_CRED_ENDPOINT"

# ---------------------------------------------------------------------------
# 6. Generate greengrass-config.yaml from template
# ---------------------------------------------------------------------------
echo "==> Generating config/greengrass-config.yaml..."
sed \
  -e "s|<REPLACE_ME_IOT_DATA_ENDPOINT>|$IOT_DATA_ENDPOINT|g" \
  -e "s|<REPLACE_ME_IOT_CRED_ENDPOINT>|$IOT_CRED_ENDPOINT|g" \
  "$CONFIG_DIR/greengrass-config.yaml.template" \
  > "$CONFIG_DIR/greengrass-config.yaml"

echo ""
echo "Done. Files created:"
echo "  $CERT_DIR/device.pem.crt"
echo "  $CERT_DIR/private.pem.key"
echo "  $CERT_DIR/AmazonRootCA1.pem"
echo "  $CONFIG_DIR/greengrass-config.yaml"
echo ""
echo "Next: docker-compose up -d && bash scripts/deploy-component.sh"
