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
    echo "[$timestamp] [$level] [start] $message" | tee -a "$LOG_FILE"
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

# Extract JSON from mixed output (handles stderr mixed with stdout JSON)
# Finds the last complete JSON object in the output (the final result)
extract_json() {
    local input="$1"
    # Use awk to extract from first { on a line to the last } on a line
    # This handles multi-line JSON with indentation
    echo "$input" | awk '
        /^[[:space:]]*\{/ { capture=1; json="" }
        capture { json = json $0 "\n" }
        /^[[:space:]]*\}/ && capture { print json; capture=0 }
    ' | tail -n +1
}

# Store pod ID to file
store_pod_id() {
    local pod_id="$1"
    echo "$pod_id" > "$POD_ID_FILE"
    log "INFO" "Stored pod ID $pod_id to $POD_ID_FILE"
}

# Get stored pod ID (returns empty if file doesn't exist)
get_stored_pod_id() {
    if [ -f "$POD_ID_FILE" ]; then
        cat "$POD_ID_FILE"
    fi
}

# Read RunPod API key from file
RUNPOD_API_KEY=""
if [ -f "${SCRIPT_DIR}/.apiKey" ]; then
    RUNPOD_API_KEY=$(cat "${SCRIPT_DIR}/.apiKey" | tr -d '[:space:]')
fi

# Migrate pod function - uses RunPod GraphQL API (beta feature)
# Returns 0 on success, 1 on failure (no instances available or other error)
migrate_pod() {
    local pod_id="$1"

    if [ -z "$RUNPOD_API_KEY" ]; then
        log "WARN" "No RunPod API key found in .apiKey file. Skipping migration."
        return 1
    fi

    log "INFO" "Attempting to migrate pod $pod_id using RunPod GraphQL API..."
    send_debug_email "RunPod Migration Initiated" "Attempting to migrate pod $pod_id using RunPod GraphQL API (beta feature)."

    # Build the GraphQL mutation payload
    local payload=$(cat <<EOF
{"operationName":"CreatePodMigration","variables":{"input":{"podId":"$pod_id"}},"query":"mutation CreatePodMigration(\$input: MigratePodInput!) { migratePod(input: \$input) { id sourcePodId targetPodId migrationType sourceMount status createdAt updatedAt __typename } }"}
EOF
)

    # Execute the GraphQL request
    MIGRATION_RESPONSE=$(curl -s -X POST 'https://api.runpod.io/graphql?operation=CreatePodMigration' \
        -H 'accept: application/json' \
        -H "authorization: Bearer $RUNPOD_API_KEY" \
        -H 'content-type: application/json' \
        -d "$payload")

    log "DEBUG" "Migration API response: $MIGRATION_RESPONSE"

    # Check for errors in the response
    local has_errors=$(echo "$MIGRATION_RESPONSE" | jq -r '.errors // empty')

    if [ -n "$has_errors" ]; then
        local error_message=$(echo "$MIGRATION_RESPONSE" | jq -r '.errors[0].message // "Unknown error"')
        log "WARN" "Migration failed: $error_message"

        # Check if the error is "no instances available"
        if echo "$error_message" | grep -qi "no instances currently available"; then
            log "INFO" "No instances available for migration. Will fall back to create/delete."
            send_debug_email "RunPod Migration Failed - No Instances" "Migration failed for pod $pod_id: No instances currently available.\n\nFalling back to create new pod and delete old one."
            return 1
        fi

        # Other error
        send_debug_email "RunPod Migration Failed" "Migration failed for pod $pod_id.\n\nError: $error_message\n\nFalling back to create new pod and delete old one."
        return 1
    fi

    # Check if migration was successful
    local target_pod_id=$(echo "$MIGRATION_RESPONSE" | jq -r '.data.migratePod.targetPodId // empty')
    local migration_status=$(echo "$MIGRATION_RESPONSE" | jq -r '.data.migratePod.status // empty')

    if [ -z "$target_pod_id" ] || [ "$target_pod_id" == "null" ]; then
        log "WARN" "Migration response missing targetPodId"
        return 1
    fi

    log "INFO" "Migration initiated successfully! Target pod: $target_pod_id, Status: $migration_status"
    send_debug_email "RunPod Migration Initiated" "Migration initiated for pod $pod_id.\n\nTarget Pod: $target_pod_id\nStatus: $migration_status\n\nWaiting for migration to complete..."

    # Store the new pod ID
    store_pod_id "$target_pod_id"

    # Return the new pod ID via echo (for capture)
    echo "$target_pod_id"
    return 0
}

# Failover function - creates a new pod with a different GPU (legacy - kept for compatibility)
perform_failover() {
    local old_pod_id="$1"

    log "INFO" "Starting failover process for pod $old_pod_id..."
    send_debug_email "RunPod Failover Started" "Starting failover process for pod $old_pod_id.\n\nSearching for available GPU with:\n- Min GPU Memory: ${FAILOVER_MIN_GPU_MEM}GB\n- Min vCPU: ${FAILOVER_MIN_VCPU}\n- Max Price: \$${FAILOVER_MAX_ONDEMAND_PRICE}/hr\n- Preferred GPU: ${FAILOVER_PREFERRED_GPU:-none}"

    # Execute failover using the Python script
    # Build command with optional preferred GPU argument
    if [ -n "$FAILOVER_PREFERRED_GPU" ]; then
        FAILOVER_RESULT=$("${SCRIPT_DIR}/runpod_costsaving.py" failover \
            --old-pod-id "$old_pod_id" \
            --min-mem "$FAILOVER_MIN_GPU_MEM" \
            --min-vcpu "$FAILOVER_MIN_VCPU" \
            --max-price "$FAILOVER_MAX_ONDEMAND_PRICE" \
            --preferred-gpu "$FAILOVER_PREFERRED_GPU" \
            --template-id "$FAILOVER_TEMPLATE_ID" \
            --network-volume-id "$FAILOVER_NETWORK_VOLUME_ID" \
            --image-name "$FAILOVER_IMAGE_NAME" \
            --name "$FAILOVER_POD_NAME" \
            --json 2>&1)
    else
        FAILOVER_RESULT=$("${SCRIPT_DIR}/runpod_costsaving.py" failover \
            --old-pod-id "$old_pod_id" \
            --min-mem "$FAILOVER_MIN_GPU_MEM" \
            --min-vcpu "$FAILOVER_MIN_VCPU" \
            --max-price "$FAILOVER_MAX_ONDEMAND_PRICE" \
            --template-id "$FAILOVER_TEMPLATE_ID" \
            --network-volume-id "$FAILOVER_NETWORK_VOLUME_ID" \
            --image-name "$FAILOVER_IMAGE_NAME" \
            --name "$FAILOVER_POD_NAME" \
            --json 2>&1)
    fi

    FAILOVER_EXIT_CODE=$?
    log "INFO" "Failover result: $FAILOVER_RESULT"

    # Extract JSON from mixed output (stderr progress messages + stdout JSON)
    FAILOVER_JSON=$(extract_json "$FAILOVER_RESULT")
    log "DEBUG" "Extracted JSON: $FAILOVER_JSON"

    if [ $FAILOVER_EXIT_CODE -ne 0 ]; then
        log "ERROR" "Failover failed: $FAILOVER_RESULT"
        send_email "RunPod Failover Failed" "Failover process failed for pod $old_pod_id.\n\nError: $FAILOVER_RESULT\n\nNo alternative GPU found meeting criteria:\n- Min GPU Memory: ${FAILOVER_MIN_GPU_MEM}GB\n- Min vCPU: ${FAILOVER_MIN_VCPU}\n- Max Price: \$${FAILOVER_MAX_ONDEMAND_PRICE}/hr"
        return 1
    fi

    # Check if failover was successful
    FAILOVER_SUCCESS=$(echo "$FAILOVER_JSON" | jq -r '.success // false')
    if [ "$FAILOVER_SUCCESS" != "true" ]; then
        FAILOVER_ERROR=$(echo "$FAILOVER_JSON" | jq -r '.error // "Unknown error"')
        log "ERROR" "Failover failed: $FAILOVER_ERROR"
        send_email "RunPod Failover Failed" "Failover process failed for pod $old_pod_id.\n\nError: $FAILOVER_ERROR"
        return 1
    fi

    # Extract new pod information
    NEW_POD_ID=$(echo "$FAILOVER_JSON" | jq -r '.new_pod_id')
    NEW_GPU_TYPE=$(echo "$FAILOVER_JSON" | jq -r '.gpu_type')
    NEW_GPU_PRICE=$(echo "$FAILOVER_JSON" | jq -r '.ondemand_price')

    log "INFO" "Failover successful! New pod: $NEW_POD_ID (GPU: $NEW_GPU_TYPE, \$$NEW_GPU_PRICE/hr)"

    # Store the new pod ID
    store_pod_id "$NEW_POD_ID"

    send_debug_email "RunPod Failover Successful" "Failover completed successfully!\n\nOld Pod: $old_pod_id (removed)\nNew Pod: $NEW_POD_ID\nGPU: $NEW_GPU_TYPE\nPrice: \$$NEW_GPU_PRICE/hr\nNetwork Volume: $FAILOVER_NETWORK_VOLUME_ID\nTemplate: $FAILOVER_TEMPLATE_ID"

    # Return the new pod ID via echo (for capture)
    echo "$NEW_POD_ID"
    return 0
}

# Smart restart function - uses GraphQL to restart or recreate pod with same/similar GPU
# This replaces the complex retry logic with a single intelligent command
# Can also handle first-time startup when no pod exists
restart_or_recreate_pod() {
    local pod_id="$1"

    if [ -n "$pod_id" ]; then
        log "INFO" "Starting smart restart/recreate for pod $pod_id..."
        send_debug_email "RunPod Smart Restart" "Starting smart restart for pod $pod_id.\n\nThis will:\n1. Try to restart the existing pod\n2. If GPU unavailable, find a similar GPU\n3. Create new pod with same config + similar GPU\n4. Delete old pod and update tracking"
    else
        log "INFO" "No pod ID provided. Will create new pod from fallback config..."
        send_debug_email "RunPod New Pod Creation" "No pod ID provided.\n\nCreating new pod with fallback config:\n- Template: $FAILOVER_TEMPLATE_ID\n- Network Volume: $FAILOVER_NETWORK_VOLUME_ID\n- Preferred GPU: ${FAILOVER_PREFERRED_GPU:-auto}"
    fi

    # Build the command with fallback parameters for first-time creation
    RESTART_RESULT=$("${SCRIPT_DIR}/runpod_costsaving.py" restart-or-recreate ${pod_id:+"$pod_id"} \
        --max-price "$FAILOVER_MAX_ONDEMAND_PRICE" \
        --pod-id-file "$POD_ID_FILE" \
        --fallback-template-id "$FAILOVER_TEMPLATE_ID" \
        --fallback-network-volume-id "$FAILOVER_NETWORK_VOLUME_ID" \
        --fallback-image-name "$FAILOVER_IMAGE_NAME" \
        --fallback-gpu "${FAILOVER_PREFERRED_GPU:-}" \
        --fallback-name "$FAILOVER_POD_NAME" \
        --fallback-min-mem "$FAILOVER_MIN_GPU_MEM" \
        --fallback-min-vcpu "$FAILOVER_MIN_VCPU" \
        --json 2>&1)

    RESTART_EXIT_CODE=$?
    log "INFO" "Restart/recreate result: $RESTART_RESULT"

    # Extract JSON from mixed output
    RESTART_JSON=$(extract_json "$RESTART_RESULT")
    log "DEBUG" "Extracted JSON: $RESTART_JSON"

    if [ $RESTART_EXIT_CODE -ne 0 ]; then
        log "ERROR" "Restart/recreate failed: $RESTART_RESULT"
        send_email "RunPod Restart Failed" "Failed to restart or recreate pod $pod_id.\n\nError: $RESTART_RESULT"
        return 1
    fi

    # Check if operation was successful
    RESTART_SUCCESS=$(echo "$RESTART_JSON" | jq -r '.success // false')
    if [ "$RESTART_SUCCESS" != "true" ]; then
        RESTART_ERROR=$(echo "$RESTART_JSON" | jq -r '.error // "Unknown error"')
        log "ERROR" "Restart/recreate failed: $RESTART_ERROR"
        send_email "RunPod Restart Failed" "Failed to restart or recreate pod $pod_id.\n\nError: $RESTART_ERROR"
        return 1
    fi

    # Check what action was taken
    ACTION=$(echo "$RESTART_JSON" | jq -r '.action // "unknown"')

    if [ "$ACTION" == "restarted" ]; then
        # Pod was restarted successfully
        RESULT_POD_ID=$(echo "$RESTART_JSON" | jq -r '.pod_id // empty')
        log "INFO" "Pod $RESULT_POD_ID restarted successfully!"
        send_debug_email "RunPod Restarted" "Pod $RESULT_POD_ID restarted successfully with existing GPU."
        echo "$RESULT_POD_ID"
        return 0
    elif [ "$ACTION" == "recreated" ]; then
        # Pod was recreated with new/similar GPU
        NEW_POD_ID=$(echo "$RESTART_JSON" | jq -r '.new_pod_id')
        OLD_POD_ID=$(echo "$RESTART_JSON" | jq -r '.old_pod_id // "none"')
        OLD_GPU=$(echo "$RESTART_JSON" | jq -r '.original_gpu // "unknown"')
        NEW_GPU=$(echo "$RESTART_JSON" | jq -r '.new_gpu // "unknown"')
        NEW_PRICE=$(echo "$RESTART_JSON" | jq -r '.new_gpu_price // "unknown"')

        log "INFO" "Pod recreated! Old: $OLD_POD_ID, New: $NEW_POD_ID (GPU: $OLD_GPU -> $NEW_GPU)"

        # Store the new pod ID (the Python script already does this via --pod-id-file)
        store_pod_id "$NEW_POD_ID"

        send_debug_email "RunPod Recreated" "Pod recreated successfully!\n\nOld Pod: $OLD_POD_ID (terminated)\nNew Pod: $NEW_POD_ID\nOriginal GPU: $OLD_GPU\nNew GPU: $NEW_GPU\nPrice: \$$NEW_PRICE/hr"

        echo "$NEW_POD_ID"
        return 0
    elif [ "$ACTION" == "created" ]; then
        # New pod was created (first time, no old pod)
        NEW_POD_ID=$(echo "$RESTART_JSON" | jq -r '.new_pod_id')
        NEW_GPU=$(echo "$RESTART_JSON" | jq -r '.new_gpu // "unknown"')
        NEW_PRICE=$(echo "$RESTART_JSON" | jq -r '.new_gpu_price // "unknown"')

        log "INFO" "New pod created! Pod ID: $NEW_POD_ID (GPU: $NEW_GPU)"

        # Store the new pod ID
        store_pod_id "$NEW_POD_ID"

        send_debug_email "RunPod Created" "New pod created successfully!\n\nPod ID: $NEW_POD_ID\nGPU: $NEW_GPU\nPrice: \$$NEW_PRICE/hr"

        echo "$NEW_POD_ID"
        return 0
    else
        log "WARN" "Unknown action: $ACTION"
        # Try to get pod ID from result
        RESULT_POD_ID=$(echo "$RESTART_JSON" | jq -r '.new_pod_id // .pod_id // empty')
        echo "${RESULT_POD_ID:-$pod_id}"
        return 0
    fi
}

# Function to create a new pod when none exists
create_new_pod() {
    log "INFO" "Creating new pod..."
    send_debug_email "RunPod Creating New Pod" "No existing pod found or pod doesn't exist.\n\nCreating new pod with:\n- Min GPU Memory: ${FAILOVER_MIN_GPU_MEM}GB\n- Min vCPU: ${FAILOVER_MIN_VCPU}\n- Max Price: \$${FAILOVER_MAX_ONDEMAND_PRICE}/hr\n- Preferred GPU: ${FAILOVER_PREFERRED_GPU:-none}\n- Network Volume: $FAILOVER_NETWORK_VOLUME_ID\n- Template: $FAILOVER_TEMPLATE_ID"

    # Use the create command (no old pod to remove)
    # Build command with optional preferred GPU argument
    if [ -n "$FAILOVER_PREFERRED_GPU" ]; then
        CREATE_RESULT=$("${SCRIPT_DIR}/runpod_costsaving.py" create \
            --min-mem "$FAILOVER_MIN_GPU_MEM" \
            --min-vcpu "$FAILOVER_MIN_VCPU" \
            --max-price "$FAILOVER_MAX_ONDEMAND_PRICE" \
            --preferred-gpu "$FAILOVER_PREFERRED_GPU" \
            --template-id "$FAILOVER_TEMPLATE_ID" \
            --network-volume-id "$FAILOVER_NETWORK_VOLUME_ID" \
            --image-name "$FAILOVER_IMAGE_NAME" \
            --name "$FAILOVER_POD_NAME" \
            --json 2>&1)
    else
        CREATE_RESULT=$("${SCRIPT_DIR}/runpod_costsaving.py" create \
            --min-mem "$FAILOVER_MIN_GPU_MEM" \
            --min-vcpu "$FAILOVER_MIN_VCPU" \
            --max-price "$FAILOVER_MAX_ONDEMAND_PRICE" \
            --template-id "$FAILOVER_TEMPLATE_ID" \
            --network-volume-id "$FAILOVER_NETWORK_VOLUME_ID" \
            --image-name "$FAILOVER_IMAGE_NAME" \
            --name "$FAILOVER_POD_NAME" \
            --json 2>&1)
    fi

    CREATE_EXIT_CODE=$?
    log "INFO" "Create result: $CREATE_RESULT"

    # Extract JSON from mixed output (stderr progress messages + stdout JSON)
    CREATE_JSON=$(extract_json "$CREATE_RESULT")
    log "DEBUG" "Extracted JSON: $CREATE_JSON"

    if [ $CREATE_EXIT_CODE -ne 0 ]; then
        log "ERROR" "Failed to create new pod: $CREATE_RESULT"
        send_email "RunPod Create Failed" "Failed to create new pod.\n\nError: $CREATE_RESULT\n\nNo GPU available meeting criteria:\n- Min GPU Memory: ${FAILOVER_MIN_GPU_MEM}GB\n- Min vCPU: ${FAILOVER_MIN_VCPU}\n- Max Price: \$${FAILOVER_MAX_ONDEMAND_PRICE}/hr"
        return 1
    fi

    # Check if creation was successful
    CREATE_SUCCESS=$(echo "$CREATE_JSON" | jq -r '.success // false')
    if [ "$CREATE_SUCCESS" != "true" ]; then
        CREATE_ERROR=$(echo "$CREATE_JSON" | jq -r '.error // "Unknown error"')
        log "ERROR" "Failed to create new pod: $CREATE_ERROR"
        send_email "RunPod Create Failed" "Failed to create new pod.\n\nError: $CREATE_ERROR"
        return 1
    fi

    # Extract new pod information
    NEW_POD_ID=$(echo "$CREATE_JSON" | jq -r '.new_pod_id')
    NEW_GPU_TYPE=$(echo "$CREATE_JSON" | jq -r '.gpu_type')
    NEW_GPU_PRICE=$(echo "$CREATE_JSON" | jq -r '.ondemand_price')

    log "INFO" "New pod created successfully! Pod ID: $NEW_POD_ID (GPU: $NEW_GPU_TYPE, \$$NEW_GPU_PRICE/hr)"

    # Store the new pod ID
    store_pod_id "$NEW_POD_ID"

    send_debug_email "RunPod New Pod Created" "New pod created successfully!\n\nPod ID: $NEW_POD_ID\nGPU: $NEW_GPU_TYPE\nPrice: \$$NEW_GPU_PRICE/hr\nNetwork Volume: $FAILOVER_NETWORK_VOLUME_ID\nTemplate: $FAILOVER_TEMPLATE_ID"

    # Return the new pod ID via echo (for capture)
    echo "$NEW_POD_ID"
    return 0
}

# Get the pod ID - either from argument or from stored file
POD_ID=$1

if [ -z "$POD_ID" ]; then
    # Try to get from stored file
    POD_ID=$(get_stored_pod_id)
    if [ -z "$POD_ID" ]; then
        log "INFO" "No pod ID specified and none stored."
    else
        log "INFO" "Using stored pod ID: $POD_ID"
    fi
else
    # Store the provided pod ID
    store_pod_id "$POD_ID"
fi

# Check if we have a pod ID and if it's already running
POD_RUNNING=false
if [ -n "$POD_ID" ]; then
    log "INFO" "Checking pod $POD_ID status..."
    POD_INFO=$("${SCRIPT_DIR}/runpod_costsaving.py" "$POD_ID" info --json 2>&1)
    INFO_EXIT_CODE=$?

    log "DEBUG" "Info command exit code: $INFO_EXIT_CODE"
    log "DEBUG" "Info command output: $POD_INFO"

    if [ $INFO_EXIT_CODE -eq 0 ]; then
        # Extract just the JSON part
        POD_INFO_JSON=$(echo "$POD_INFO" | sed -n '/{/,/}/p' | tr '\n' ' ')
        log "DEBUG" "Extracted JSON: $POD_INFO_JSON"

        PUBLIC_IP=$(echo "$POD_INFO_JSON" | jq -r '.public_ip // empty' 2>/dev/null)
        PORT_50051=$(echo "$POD_INFO_JSON" | jq -r '.port_mappings[] | select(.container_port==50051) | .public_port // empty' 2>/dev/null)

        log "DEBUG" "Parsed PUBLIC_IP: '$PUBLIC_IP', PORT_50051: '$PORT_50051'"

        if [ -n "$PUBLIC_IP" ] && [ -n "$PORT_50051" ] && [ "$PUBLIC_IP" != "null" ] && [ "$PORT_50051" != "null" ]; then
            log "INFO" "Pod is already running with IP=$PUBLIC_IP, Port=$PORT_50051. Skipping start, updating webservice only."
            send_debug_email "RunPod Already Running" "Pod $POD_ID is already running.\n\nIP: $PUBLIC_IP\nPort: $PORT_50051\n\nSkipping start, updating webservice only."
            POD_RUNNING=true
        fi
    else
        # Pod info failed - pod may not exist
        log "WARN" "Could not get pod info for $POD_ID - pod may not exist or be accessible"
    fi
fi

# If pod is not running (or doesn't exist), use smart restart/recreate
if [ "$POD_RUNNING" = false ]; then
    if [ -n "$POD_ID" ]; then
        log "INFO" "Pod is not running. Starting pod $POD_ID using smart restart/recreate..."
    else
        log "INFO" "No pod ID available. Creating new pod..."
    fi
    send_debug_email "RunPod Start Initiated" "Starting smart restart/recreate...\n\nPod ID: ${POD_ID:-none}\nMax price: \$${FAILOVER_MAX_ONDEMAND_PRICE}/hr"

    # Use the smart restart/recreate function
    # This handles ALL scenarios:
    # - Restart existing pod
    # - GPU unavailable -> recreate with similar GPU
    # - Pod doesn't exist -> create new with fallback config
    # - No pod ID at all -> create new with fallback config
    NEW_POD_ID=$(restart_or_recreate_pod "$POD_ID")
    RESTART_EXIT_CODE=$?

    if [ $RESTART_EXIT_CODE -ne 0 ]; then
        log "ERROR" "Smart restart/recreate failed. Exiting."
        exit 1
    fi

    # Update POD_ID if it changed (pod was recreated or created)
    if [ -n "$NEW_POD_ID" ]; then
        if [ "$NEW_POD_ID" != "$POD_ID" ]; then
            log "INFO" "Pod ID changed from ${POD_ID:-none} to $NEW_POD_ID"
        fi
        POD_ID="$NEW_POD_ID"
    fi

    # Poll for pod information with IP and port available
    log "INFO" "Waiting for pod information to become available (max ${MAX_WAIT_SECONDS}s)..."

    ELAPSED=0
    PUBLIC_IP=""
    PORT_50051=""

    while [ $ELAPSED -lt $MAX_WAIT_SECONDS ]; do
        # Get fresh pod info
        POD_INFO=$("${SCRIPT_DIR}/runpod_costsaving.py" "$POD_ID" info --json 2>&1)

        # Extract IP and port
        PUBLIC_IP=$(echo "$POD_INFO" | jq -r '.public_ip // empty')
        PORT_50051=$(echo "$POD_INFO" | jq -r '.port_mappings[] | select(.container_port==50051) | .public_port // empty' 2>/dev/null)

        if [ -n "$PUBLIC_IP" ] && [ -n "$PORT_50051" ] && [ "$PUBLIC_IP" != "null" ] && [ "$PORT_50051" != "null" ]; then
            log "INFO" "Pod information available: IP=$PUBLIC_IP, Port=$PORT_50051"
            break
        fi

        log "INFO" "Waiting for pod information... (${ELAPSED}s elapsed)"
        sleep $POLL_INTERVAL
        ELAPSED=$((ELAPSED + POLL_INTERVAL))
    done

    # Check if we got the information
    if [ -z "$PUBLIC_IP" ] || [ -z "$PORT_50051" ] || [ "$PUBLIC_IP" == "null" ] || [ "$PORT_50051" == "null" ]; then
        log "ERROR" "Timeout: Pod information not available after ${MAX_WAIT_SECONDS}s"
        log "ERROR" "Last pod info: $POD_INFO"
        send_email "RunPod Start Timeout - Pod $POD_ID" "Pod $POD_ID started but connection information was not available within ${MAX_WAIT_SECONDS} seconds.\n\nPod ID: $POD_ID\nLast Info: $POD_INFO"
        exit 1
    fi
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
    log "INFO" "Successfully updated webservice with new pod information"
    send_debug_email "RunPod Start Completed Successfully" "Pod $POD_ID started successfully!\n\nEndpoint: $GPU_MACHINE_IP\nAPI Response: $HTTP_CODE\n\nWebservice updated successfully."
    echo "Pod $POD_ID started successfully. Endpoint: $GPU_MACHINE_IP"
else
    log "ERROR" "Failed to update webservice. HTTP Code: $HTTP_CODE, Response: $HTTP_BODY"
    send_email "RunPod API Update Failed - Pod $POD_ID" "Pod $POD_ID started successfully but failed to update webservice.\n\nEndpoint: $GPU_MACHINE_IP\nHTTP Code: $HTTP_CODE\nResponse: $HTTP_BODY"
    exit 1
fi

log "INFO" "Start process completed successfully for pod $POD_ID"
