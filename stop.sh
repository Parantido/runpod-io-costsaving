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
    echo "[$timestamp] [$level] [stop] $message" | tee -a "$LOG_FILE"
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
        log "ERROR" "You must specify a pod id!"
        echo "You must specify a pod id!"
        exit 1
    fi
    log "INFO" "Using stored pod ID: $POD_ID"
fi

log "INFO" "Stopping pod $POD_ID..."
send_debug_email "RunPod Stop Initiated" "Stopping pod $POD_ID..."

# Stop the pod
POD_INFO=$("${SCRIPT_DIR}/runpod_costsaving.py" "$POD_ID" stop 2>&1)
STOP_EXIT_CODE=$?

log "INFO" "Stop action output: $POD_INFO"

if [ $STOP_EXIT_CODE -ne 0 ]; then
    log "ERROR" "Failed to stop pod: $POD_INFO"
    send_email "RunPod Stop Failed - Pod $POD_ID" "Failed to stop pod $POD_ID.\n\nError: $POD_INFO"
    exit 1
fi

log "INFO" "Stop process completed successfully for pod $POD_ID"
send_debug_email "RunPod Stop Completed Successfully" "Pod $POD_ID stopped successfully."
echo "Pod $POD_ID stopped successfully."
