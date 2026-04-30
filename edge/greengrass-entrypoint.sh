#!/bin/bash
# greengrass-entrypoint.sh — Greengrass V2 Docker entrypoint
#
# Supports PROVISION=false (project-aegis default): starts the nucleus with a
# pre-provisioned config file mounted at INIT_CONFIG.
#
# Pre-provisioning is handled by scripts/provision-greengrass.sh on the host.
# PROVISION=true is intentionally not supported here — the IoT cert + TES role
# are created by CDK (ProjectAegisGreengrassStack) and the provision script.

set -euo pipefail

GREENGRASS_ROOT="${GREENGRASS_ROOT:-/greengrass/v2}"
INIT_CONFIG="${INIT_CONFIG:-/tmp/config/greengrass-config.yaml}"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"

if [ "${PROVISION:-false}" = "true" ]; then
    echo "ERROR: PROVISION=true is not supported by this entrypoint." >&2
    echo "Run scripts/provision-greengrass.sh on the host, then restart with PROVISION=false." >&2
    exit 1
fi

if [ ! -f "$INIT_CONFIG" ]; then
    echo "ERROR: Init config not found at $INIT_CONFIG" >&2
    echo "Run scripts/provision-greengrass.sh to generate edge/config/greengrass-config.yaml" >&2
    exit 1
fi

echo "Starting Greengrass nucleus..."
echo "  Root:        $GREENGRASS_ROOT"
echo "  Init config: $INIT_CONFIG"
echo "  Region:      $AWS_DEFAULT_REGION"

# exec replaces the shell so the JVM is PID 1 and receives SIGTERM directly.
# --init-config is used on first boot to bootstrap from the config file;
# on subsequent boots Greengrass reads its persisted state from the volume.
exec java \
    -Droot="$GREENGRASS_ROOT" \
    -Dlog.store=FILE \
    -jar /opt/greengrassv2/lib/Greengrass.jar \
    --init-config "$INIT_CONFIG" \
    --aws-region "$AWS_DEFAULT_REGION"
