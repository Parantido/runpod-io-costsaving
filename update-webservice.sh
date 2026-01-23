#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Logging function
log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] [update-ws] $message" | tee -a "$LOG_FILE"
}

# Email notification function
send_email() {
    local subject="$1"
    local body="$2"
    log "INFO" "Sending email notification to $EMAIL_RECIPIENT"
    echo -e "$body" | mail -s "$subject" "$EMAIL_RECIPIENT" 2>/dev/null
    if [ $? -ne 0 ]; then
        log "WARN" "Failed to send email (mail command may not be available)"
    fi
}

# Debug email notification (only sends if DEBUGGING=true)
send_debug_email() {
    local subject="$1"
    local body="$2"
    if [ "$DEBUGGING" = "true" ]; then
        send_email "[DEBUG] $subject" "$body"
    fi
}

# Get stored pod ID (returns empty if file doesn't exist)
get_stored_pod_id() {
    if [ -f "$POD_ID_FILE" ]; then
        cat "$POD_ID_FILE"
    fi
}

# Get the pod ID - either from argument or from stored file
POD_ID=$1

if [ -z "$POD_ID" ]; then
    # Try to get from stored file
    POD_ID=$(get_stored_pod_id)
    if [ -z "$POD_ID" ]; then
        log "ERROR" "You must specify a pod id or have one stored!"
        echo "Usage: $0 [pod_id]"
        echo "No pod ID specified and none stored in $POD_ID_FILE"
        exit 1
    fi
    log "INFO" "Using stored pod ID: $POD_ID"
fi

log "INFO" "Fetching pod $POD_ID information..."

# Get pod info
POD_INFO=$("${SCRIPT_DIR}/runpod_costsaving.py" --json info "$POD_ID" 2>&1)
INFO_EXIT_CODE=$?

if [ $INFO_EXIT_CODE -ne 0 ]; then
    log "ERROR" "Failed to get pod info: $POD_INFO"
    send_email "RunPod Update Webservice Failed" "Failed to get pod $POD_ID information.\n\nError: $POD_INFO"
    exit 1
fi

# Extract IP and port
PUBLIC_IP=$(echo "$POD_INFO" | jq -r '.public_ip // empty' 2>/dev/null)
PORT_50051=$(echo "$POD_INFO" | jq -r '.port_mappings[] | select(.container_port==50051) | .public_port // empty' 2>/dev/null)
POD_STATUS=$(echo "$POD_INFO" | jq -r '.status // empty' 2>/dev/null)

log "INFO" "Pod status: $POD_STATUS"
log "INFO" "Public IP: $PUBLIC_IP"
log "INFO" "Port 50051 mapping: $PORT_50051"

# Check if pod is running
if [ "$POD_STATUS" != "RUNNING" ]; then
    log "ERROR" "Pod is not running (status: $POD_STATUS)"
    send_email "RunPod Update Webservice Failed" "Pod $POD_ID is not running.\n\nStatus: $POD_STATUS\n\nCannot update webservice."
    exit 1
fi

# Check if we have IP and port
if [ -z "$PUBLIC_IP" ] || [ -z "$PORT_50051" ] || [ "$PUBLIC_IP" == "null" ] || [ "$PORT_50051" == "null" ]; then
    log "ERROR" "Pod is running but IP or port 50051 not available"
    log "ERROR" "Pod info: $POD_INFO"
    send_email "RunPod Update Webservice Failed" "Pod $POD_ID is running but port 50051 is not exposed.\n\nPod Info: $POD_INFO"
    exit 1
fi

log "INFO" "Found endpoint: $PUBLIC_IP:$PORT_50051"

# Update the remote webservice
GPU_MACHINE_IP="${PUBLIC_IP}:${PORT_50051}"
PAYLOAD=$(jq -n \
    --arg token "$VALIDATION_TOKEN" \
    --arg machineId "$GPU_MACHINE_ID" \
    --arg machineIp "$GPU_MACHINE_IP" \
    '{ValidationToken: $token, GPUMachineId: $machineId, GPUMachineIp: $machineIp}')

log "INFO" "Updating webservice at $API_URL..."
log "INFO" "Payload: $PAYLOAD"

HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -n 1)

log "INFO" "API Response Code: $HTTP_CODE"
log "INFO" "API Response Body: $HTTP_BODY"

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    log "INFO" "Successfully updated webservice with pod information"
    send_debug_email "RunPod Webservice Updated" "Webservice updated successfully!\n\nPod ID: $POD_ID\nEndpoint: $GPU_MACHINE_IP\nAPI Response: $HTTP_CODE"
    echo "Webservice updated successfully. Endpoint: $GPU_MACHINE_IP"
else
    log "ERROR" "Failed to update webservice. HTTP Code: $HTTP_CODE, Response: $HTTP_BODY"
    send_email "RunPod Webservice Update Failed" "Failed to update webservice for pod $POD_ID.\n\nEndpoint: $GPU_MACHINE_IP\nHTTP Code: $HTTP_CODE\nResponse: $HTTP_BODY"
    exit 1
fi
