# Dockerfile — Greengrass V2 Core built from source (no pre-built registry image)
#
# Base: Amazon Linux 2023 from ECR Public (AWS-maintained, no pull rate limits).
# Nucleus: downloaded directly from the official AWS CloudFront CDN — you control
#          exactly what runs rather than trusting an opaque pre-built image.
#
# Adds: Python 3.11 + pip so the com.project-aegis.publisher component can run
#       its Install lifecycle step (pip install -r requirements.txt).
#
# Build: docker-compose up --build   (runs automatically on first `up`)

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ARG GREENGRASS_VERSION=2.13.0

# Install runtime dependencies
RUN dnf update -y && \
    dnf install -y \
        java-11-amazon-corretto-headless \
        python3.11 \
        tar \
        unzip \
        sudo \
        shadow-utils \
        procps-ng \
        which \
        sqlite \
    && dnf clean all

# Bootstrap pip for Python 3.11 and wire up default python3/pip3 symlinks
RUN python3.11 -m ensurepip --upgrade && \
    ln -sf /usr/bin/python3.11 /usr/bin/python3 && \
    ln -sf /usr/bin/pip3.11 /usr/bin/pip3

# Verify toolchain
RUN python3 --version && pip3 --version && java -version

# Create Greengrass system user/group required by the nucleus process manager
RUN groupadd --system ggc_group && \
    useradd --system --gid ggc_group --shell /bin/false --no-create-home ggc_user

# Download the Greengrass nucleus from the official AWS CloudFront distribution.
# SHA of the zip can be verified via: https://docs.aws.amazon.com/greengrass/v2/developerguide/greengrass-nucleus-component.html
RUN curl -sSf \
        "https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-${GREENGRASS_VERSION}.zip" \
        -o /tmp/greengrass.zip && \
    unzip -q /tmp/greengrass.zip -d /tmp/greengrass && \
    mkdir -p /opt/greengrassv2 && \
    cp -r /tmp/greengrass/* /opt/greengrassv2/ && \
    rm -rf /tmp/greengrass /tmp/greengrass.zip

# Greengrass root — persisted across container restarts via named volume
RUN mkdir -p /greengrass/v2

# Entrypoint handles PROVISION=false (pre-provisioned certs via volume mount).
# See scripts/provision-greengrass.sh for the one-time cert generation step.
COPY greengrass-entrypoint.sh /greengrass-entrypoint.sh
RUN chmod +x /greengrass-entrypoint.sh

ENTRYPOINT ["/greengrass-entrypoint.sh"]
