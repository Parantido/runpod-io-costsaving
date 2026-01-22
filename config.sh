#!/bin/bash
# Configuration file for runpod management scripts

# API Configuration
API_URL="https://api.drop.co/GPUMachineUpdate"
VALIDATION_TOKEN="5d6ac3e1-8bce-43d3-b8b7-3bd3a95f5ce0"
GPU_MACHINE_ID="3"

# Email Configuration
EMAIL_RECIPIENT="danilo.santoro@techfusion.it"

# Logging Configuration (defined early as other settings depend on LOG_DIR)
LOG_DIR="/root/mgmt/runpod/logs"
LOG_FILE="${LOG_DIR}/runpod.log"

# Retry Configuration
MAX_WAIT_SECONDS=600       # 10 minutes may be needed for the pod to be online
POLL_INTERVAL=5

# GPU Retry Configuration (for initial pod start)
GPU_RETRY_INTERVAL=30      # seconds between retries
GPU_RETRY_MAX_TIME=600     # 10 minutes maximum

# Failover Configuration - GPU Requirements
FAILOVER_MIN_GPU_MEM=140          # Minimum GPU memory in GB
FAILOVER_MIN_VCPU=20              # Minimum vCPU count
FAILOVER_MAX_ONDEMAND_PRICE=4.00  # Maximum ondemand $/HR
FAILOVER_PREFERRED_GPU="NVIDIA H200"  # Preferred GPU type (will be tried first if available)

# Failover Configuration - RunPod Resources
FAILOVER_NETWORK_VOLUME_ID="4f5te8mnxe"
FAILOVER_TEMPLATE_ID="02rh6eeawc"
FAILOVER_IMAGE_NAME="docker.io/parantido/riva-speech-runpod:2.19.0"
FAILOVER_POD_NAME="tr-gpu-$$-prod"

# Pod ID Storage (for failover tracking)
POD_ID_FILE="${LOG_DIR}/current_pod_id"

# Debug Configuration
# Set to "true" to receive email notifications for informational and successful steps
# Set to "false" to only receive error notifications
DEBUGGING="true"
