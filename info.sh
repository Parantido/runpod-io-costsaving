#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

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
        echo "You must specify a pod id!"
        exit 1
    fi
    echo "Using stored pod ID: $POD_ID"
fi

# Get pod info
POD_INFO=$("${SCRIPT_DIR}/runpod_costsaving.py" "$POD_ID" info --json)

# Info
echo "Pod info: $POD_INFO"

# Extract IP and port (using jq)
PUBLIC_IP=$(echo "$POD_INFO" | jq -r '.public_ip')
PORT_50051=$(echo "$POD_INFO" | jq -r '.port_mappings[] | select(.container_port==50051) | .public_port')

# Info
echo "[ii] Found $PUBLIC_IP:$PORT_50051"
