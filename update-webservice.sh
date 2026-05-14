#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# ============================================================
# Logging
# ============================================================
log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] [update-ws] $message" | tee -a "$LOG_FILE"
}

# ============================================================
# Telegram notification
# ============================================================
send_telegram() {
    local level="$1"   # SUCCESS | ERROR | INFO | WARNING
    local message="$2"

    if [ "$_TELEGRAM_ENABLED" != "true" ]; then
        return 0
    fi
    if [ -z "$_TELEGRAM_TOKEN" ] || [ -z "$_TELEGRAM_CHAT_ID" ]; then
        log "WARN" "Telegram token or chat ID not set, skipping notification"
        return 1
    fi

    local icon
    case "$level" in
        SUCCESS) icon="✅" ;;
        ERROR)   icon="🚨" ;;
        WARNING) icon="⚠️"  ;;
        *)       icon="ℹ️"  ;;
    esac

    local full_message="${icon} *${level}*
${message}"

    curl -s -X POST \
        "https://api.telegram.org/bot${_TELEGRAM_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
            --arg chat_id "$_TELEGRAM_CHAT_ID" \
            --arg text "$full_message" \
            '{chat_id: $chat_id, text: $text, parse_mode: "Markdown"}')" \
        >/dev/null 2>&1

    return $?
}

# ============================================================
# Email notification
# ============================================================
send_email() {
    local subject="$1"
    local body="$2"
    log "INFO" "Sending email notification to $EMAIL_RECIPIENT"
    echo -e "$body" | mail -s "$subject" "$EMAIL_RECIPIENT" 2>/dev/null
    if [ $? -ne 0 ]; then
        log "WARN" "Failed to send email (mail command may not be available)"
    fi
}

send_debug_email() {
    local subject="$1"
    local body="$2"
    if [ "$DEBUGGING" = "true" ]; then
        send_email "[DEBUG] $subject" "$body"
    fi
}

# ============================================================
# Usage
# ============================================================
usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Update a remote webservice with the current RunPod instance endpoint.

Options:
  --pod-id-file FILE       Path to file containing pod ID
                           (default: \$POD_ID_FILE from config.sh)
  --gpu-machine-id ID      GPU machine ID sent to webservice API
                           (default: \$GPU_MACHINE_ID from config.sh)
  --api-url URL            Webservice API URL
                           (default: \$API_URL from config.sh)
  --telegram-token TOKEN   Telegram bot token
                           (default: \$TELEGRAM_BOT_TOKEN from config.sh)
  --telegram-chat-id ID    Telegram chat ID
                           (default: \$TELEGRAM_CHAT_ID from config.sh)
  --no-telegram            Disable Telegram notifications
  --update-transcserver    Update TRANSCSERVER override for VM method (instead of GPU machine)
  --method METHOD           VM method for TRANSCSERVER update
                            Valid methods: captcha, mailboxfull-captcha, mailboxfull-diversion,
                            diversion, s2s-tmobile, s2s-att
  --transcserver-value VAL  TRANSCSERVER value (host:port format, e.g., 192.168.1.50:50051)
                            If not provided, will be retrieved from RunPod instance
  --update-riva-server     Register/update a dynamic RIVA server in riva-analysis-retry
                            (upsert by pod name; use --remove-riva-server for shutdown)
  --remove-riva-server     Remove a dynamic RIVA server from riva-analysis-retry by pod name
  --riva-api-url URL        riva-analysis-retry API base URL
                            (default: http://127.0.0.1:4000)
  --riva-pod-name NAME      Stable pod name used as the server key (e.g. tr-gpu-01-prod)
  --riva-priority N         Priority for the RIVA server (lower = preferred, default: 30)
  -h, --help               Show this help message

Examples:
  # Single instance (uses config.sh defaults) - GPU machine update
  $0

  # Instance 1 - GPU machine update
  $0 --pod-id-file /root/mgmt/runpod/logs/tr-gpu-01-prod.pod_id --gpu-machine-id 3

  # Instance 2 - GPU machine update
  $0 --pod-id-file /root/mgmt/runpod/logs/tr-gpu-02-prod.pod_id --gpu-machine-id 4

  # TRANSCSERVER update for diversion method (value fetched from pod automatically)
  $0 --update-transcserver --method diversion --pod-id-file /root/mgmt/runpod/logs/current_pod_id

  # TRANSCSERVER update for s2s-tmobile method (value fetched from pod automatically)
  $0 --update-transcserver --method s2s-tmobile

  # TRANSCSERVER update with manual value override
  $0 --update-transcserver --method s2s-tmobile --transcserver-value "192.168.1.50:50051"

  # Register/update RunPod RIVA server in Node.js API (on pod startup)
  $0 --update-riva-server --pod-id-file /root/mgmt/runpod/logs/tr-gpu-01-prod.pod_id --riva-pod-name tr-gpu-01-prod --riva-priority 30

  # Remove RunPod RIVA server from Node.js API (on pod shutdown)
  $0 --remove-riva-server --riva-pod-name tr-gpu-01-prod

EOF
    exit 0
}

# ============================================================
# Argument parsing
# ============================================================
_POD_ID_FILE="$POD_ID_FILE"
_GPU_MACHINE_ID="$GPU_MACHINE_ID"
_API_URL="$API_URL"
_TELEGRAM_TOKEN="$TELEGRAM_BOT_TOKEN"
_TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID"
_TELEGRAM_ENABLED="${TELEGRAM_ENABLED:-true}"
_UPDATE_TRANSCSERVER="${UPDATE_TRANSCSERVER:-false}"
_METHOD=""
_TRANSCSERVER_VALUE=""
_UPDATE_RIVA_SERVER="false"
_REMOVE_RIVA_SERVER="false"
_RIVA_API_URL="http://127.0.0.1:4000"
_RIVA_POD_NAME=""
_RIVA_PRIORITY="30"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pod-id-file)       _POD_ID_FILE="$2"; shift 2 ;;
        --gpu-machine-id)     _GPU_MACHINE_ID="$2"; shift 2 ;;
        --api-url)            _API_URL="$2"; shift 2 ;;
        --telegram-token)       _TELEGRAM_TOKEN="$2"; shift 2 ;;
        --telegram-chat-id)     _TELEGRAM_CHAT_ID="$2"; shift 2 ;;
        --no-telegram)         _TELEGRAM_ENABLED="false"; shift ;;
        --update-transcserver) _UPDATE_TRANSCSERVER="true"; shift ;;
        --method)              _METHOD="$2"; shift 2 ;;
        --transcserver-value)  _TRANSCSERVER_VALUE="$2"; shift 2 ;;
        --update-riva-server)  _UPDATE_RIVA_SERVER="true"; shift ;;
        --remove-riva-server)  _REMOVE_RIVA_SERVER="true"; shift ;;
        --riva-api-url)        _RIVA_API_URL="$2"; shift 2 ;;
        --riva-pod-name)       _RIVA_POD_NAME="$2"; shift 2 ;;
        --riva-priority)       _RIVA_PRIORITY="$2"; shift 2 ;;
        -h|--help)            usage ;;
        *)
            # Legacy positional: first arg may be pod_id (backward compat)
            if [ -z "$_LEGACY_POD_ID" ]; then
                _LEGACY_POD_ID="$1"
            else
                echo "Unknown argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# ============================================================
# Validate arguments and resolve common values
# ============================================================

# --remove-riva-server: no pod info needed, just the pod name
if [ "$_REMOVE_RIVA_SERVER" = "true" ]; then
    if [ -z "$_RIVA_POD_NAME" ]; then
        log "ERROR" "--remove-riva-server requires --riva-pod-name"
        usage
        exit 1
    fi

    log "INFO" "Removing dynamic RIVA server '${_RIVA_POD_NAME}' from ${_RIVA_API_URL}..."
    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X DELETE "${_RIVA_API_URL}/api/servers/dynamic/${_RIVA_POD_NAME}")
    HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
    HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -n 1)

    log "INFO" "API Response Code: $HTTP_CODE"
    log "INFO" "API Response Body: $HTTP_BODY"

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        log "INFO" "Successfully removed RIVA server '${_RIVA_POD_NAME}'"
        send_telegram "INFO" "RIVA server removed$'\n\n'Pod: \`${_RIVA_POD_NAME}\`"
        echo "RIVA server '${_RIVA_POD_NAME}' removed successfully."
        exit 0
    elif [ "$HTTP_CODE" = "404" ]; then
        log "INFO" "RIVA server '${_RIVA_POD_NAME}' not found (already removed or never added) - OK"
        echo "RIVA server '${_RIVA_POD_NAME}' not found (already removed)."
        exit 0
    else
        log "ERROR" "Failed to remove RIVA server. HTTP Code: $HTTP_CODE, Response: $HTTP_BODY"
        send_telegram "ERROR" "RIVA server removal failed$'\n\n'Pod: \`${_RIVA_POD_NAME}\`$'\n'HTTP Code: ${HTTP_CODE}"
        exit 1
    fi
fi

# Resolve pod ID (needed for TRANSCSERVER, GPU machine, and update-riva-server modes)
if [ -n "$_LEGACY_POD_ID" ]; then
    POD_ID="$_LEGACY_POD_ID"
    log "INFO" "Using pod ID from argument: $POD_ID"
elif [ -f "$_POD_ID_FILE" ]; then
    POD_ID=$(cat "$_POD_ID_FILE")
    log "INFO" "Using stored pod ID from ${_POD_ID_FILE}: $POD_ID"
else
    POD_ID=""
fi

# Fetch pod info if we need IP:port from the pod (TRANSCSERVER without manual value,
# GPU machine mode, or update-riva-server mode)
_NEED_POD_INFO="false"
if [ "$_UPDATE_TRANSCSERVER" = "true" ] && [ -z "$_TRANSCSERVER_VALUE" ]; then
    _NEED_POD_INFO="true"
fi
if [ "$_UPDATE_RIVA_SERVER" = "true" ]; then
    _NEED_POD_INFO="true"
fi
if [ "$_UPDATE_TRANSCSERVER" != "true" ] && [ "$_UPDATE_RIVA_SERVER" != "true" ]; then
    _NEED_POD_INFO="true"  # GPU machine mode always needs pod info
fi

if [ "$_NEED_POD_INFO" = "true" ] && [ -n "$POD_ID" ]; then
    # Fetch pod info — retry up to 6 times with 15s delay to handle transient
    # port-mapping disappearance (RunPod occasionally drops port allocations briefly
    # after resume; pod may take up to ~90s to fully expose ports).
    # stderr is redirected separately so debug/warn lines don't corrupt the JSON.
    log "INFO" "Fetching pod ${POD_ID} information..."

    _MAX_INFO_ATTEMPTS=6
    _INFO_DELAY=15
    PUBLIC_IP=""
    PORT_50051=""
    POD_STATUS=""
    POD_NAME=""
    POD_INFO=""

    for _attempt in $(seq 1 $_MAX_INFO_ATTEMPTS); do
        _INFO_STDERR_FILE=$(mktemp)
        POD_INFO=$("${SCRIPT_DIR}/runpod_costsaving.py" --json info "$POD_ID" 2>"$_INFO_STDERR_FILE")
        INFO_EXIT_CODE=$?
        _INFO_STDERR=$(cat "$_INFO_STDERR_FILE")
        rm -f "$_INFO_STDERR_FILE"

        if [ $INFO_EXIT_CODE -ne 0 ]; then
            log "ERROR" "Failed to get pod info (attempt ${_attempt}/${_MAX_INFO_ATTEMPTS}): ${_INFO_STDERR}"
            if [ $_attempt -lt $_MAX_INFO_ATTEMPTS ]; then
                log "INFO" "Retrying in ${_INFO_DELAY}s..."
                sleep $_INFO_DELAY
                continue
            fi
            send_telegram "ERROR" "update-webservice failed to get info for pod \`${POD_ID}\`$'\n\n'Error: ${_INFO_STDERR}"
            send_email "RunPod Update Webservice Failed" "Failed to get pod $POD_ID information.\n\nError: ${_INFO_STDERR}"
            exit 1
        fi

        # Extract IP, port, status
        PUBLIC_IP=$(echo "$POD_INFO"  | jq -r '.public_ip // empty' 2>/dev/null)
        PORT_50051=$(echo "$POD_INFO" | jq -r '.port_mappings[] | select(.container_port==50051 and .protocol=="tcp") | .public_port // empty' 2>/dev/null)
        POD_STATUS=$(echo "$POD_INFO" | jq -r '.status // empty' 2>/dev/null)
        POD_NAME=$(echo "$POD_INFO"   | jq -r '.name // empty' 2>/dev/null)

        log "INFO" "Pod name:   $POD_NAME (attempt ${_attempt}/${_MAX_INFO_ATTEMPTS})"
        log "INFO" "Pod status: $POD_STATUS"
        log "INFO" "Public IP:  $PUBLIC_IP"
        log "INFO" "Port 50051: $PORT_50051"

        # Check pod is running
        if [ "$POD_STATUS" != "RUNNING" ]; then
            log "ERROR" "Pod is not running (status: $POD_STATUS)"
            send_telegram "ERROR" "update-webservice: pod \`${POD_ID}\` (${POD_NAME}) is not RUNNING$'\n\n'Status: ${POD_STATUS}$'\n'Machine ID: ${_GPU_MACHINE_ID}"
            send_email "RunPod Update Webservice Failed" "Pod $POD_ID is not running.\n\nStatus: $POD_STATUS\n\nCannot update webservice."
            exit 1
        fi

        # If ports are available we're done
        if [ -n "$PUBLIC_IP" ] && [ -n "$PORT_50051" ] && [ "$PUBLIC_IP" != "null" ] && [ "$PORT_50051" != "null" ]; then
            break
        fi

        # Ports not yet available — retry if attempts remain
        if [ $_attempt -lt $_MAX_INFO_ATTEMPTS ]; then
            log "WARN" "Pod is running but port 50051 not available yet, retrying in ${_INFO_DELAY}s... (attempt ${_attempt}/${_MAX_INFO_ATTEMPTS})"
            sleep $_INFO_DELAY
        fi
    done

    # Final check after all attempts
    if [ -z "$PUBLIC_IP" ] || [ -z "$PORT_50051" ] || [ "$PUBLIC_IP" = "null" ] || [ "$PORT_50051" = "null" ]; then
        log "ERROR" "Pod is running but IP or port 50051 not available after ${_MAX_INFO_ATTEMPTS} attempts (~$((_MAX_INFO_ATTEMPTS * _INFO_DELAY))s)"
        log "ERROR" "Pod info: $POD_INFO"
        send_telegram "ERROR" "update-webservice: pod \`${POD_ID}\` (${POD_NAME}) is RUNNING but port 50051 is not exposed$'\n\n'Machine ID: ${_GPU_MACHINE_ID}"
        send_email "RunPod Update Webservice Failed" "Pod $POD_ID is running but port 50051 is not exposed after ${_MAX_INFO_ATTEMPTS} attempts.\n\nPod Info: $POD_INFO"
        exit 1
    fi

    log "INFO" "Found endpoint: $PUBLIC_IP:$PORT_50051"
    # Distribute to the right variable based on mode
    if [ "$_UPDATE_TRANSCSERVER" = "true" ]; then
        _TRANSCSERVER_VALUE="${PUBLIC_IP}:${PORT_50051}"
        log "INFO" "TRANSCSERVER value from pod: $_TRANSCSERVER_VALUE"
    elif [ "$_UPDATE_RIVA_SERVER" = "true" ]; then
        _RIVA_HOST="$PUBLIC_IP"
        _RIVA_PORT="$PORT_50051"
        # Use pod name from RunPod as riva pod name if not overridden
        if [ -z "$_RIVA_POD_NAME" ]; then
            _RIVA_POD_NAME="$POD_NAME"
        fi
        log "INFO" "RIVA endpoint from pod: ${_RIVA_HOST}:${_RIVA_PORT} (name: ${_RIVA_POD_NAME})"
    else
        GPU_MACHINE_IP="${PUBLIC_IP}:${PORT_50051}"
    fi
fi

# ============================================================
# Validate TRANSCSERVER mode
# ============================================================
if [ "$_UPDATE_TRANSCSERVER" = "true" ]; then
    if [ -z "$_METHOD" ]; then
        log "ERROR" "--update-transcserver requires --method"
        usage
        exit 1
    fi

    # TRANSCSERVER_VALUE is now set either from argument or from pod info
    if [ -z "$_TRANSCSERVER_VALUE" ]; then
        log "ERROR" "--update-transcserver requires --transcserver-value or --pod-id-file to fetch value from pod"
        usage
        exit 1
    fi

    VALID_METHODS=("captcha" "mailboxfull-captcha" "mailboxfull-diversion" "diversion" "s2s-tmobile" "s2s-att")
    if [[ ! " ${VALID_METHODS[@]} " =~ " ${_METHOD} " ]]; then
        log "ERROR" "Invalid method: $_METHOD. Valid methods: ${VALID_METHODS[*]}"
        usage
        exit 1
    fi

    log "INFO" "TRANSCSERVER update mode: method=$_METHOD, value=$_TRANSCSERVER_VALUE"
fi

# ============================================================
# Update remote webservice
# ============================================================
if [ "$_UPDATE_RIVA_SERVER" = "true" ]; then
    # RIVA server upsert mode
    if [ -z "$_RIVA_POD_NAME" ]; then
        log "ERROR" "--update-riva-server requires --riva-pod-name"
        usage
        exit 1
    fi
    if [ -z "$_RIVA_HOST" ]; then
        log "ERROR" "--update-riva-server: could not determine RIVA host (pod not running or no pod-id-file?)"
        exit 1
    fi

    log "INFO" "Upserting RIVA server '${_RIVA_POD_NAME}' -> ${_RIVA_HOST}:${_RIVA_PORT} at ${_RIVA_API_URL}..."
    PAYLOAD=$(jq -n \
        --arg name    "$_RIVA_POD_NAME" \
        --arg host    "$_RIVA_HOST" \
        --argjson port "$_RIVA_PORT" \
        --argjson priority "$_RIVA_PRIORITY" \
        --arg description "RunPod dynamic server (pod: ${POD_ID:-unknown})" \
        '{name: $name, host: $host, port: $port, priority: $priority, description: $description}')

    log "INFO" "Payload: $PAYLOAD"

    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X PUT "${_RIVA_API_URL}/api/servers/upsert-dynamic" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD")

    HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
    HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -n 1)

    log "INFO" "API Response Code: $HTTP_CODE"
    log "INFO" "API Response Body: $HTTP_BODY"

    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        log "INFO" "Successfully upserted RIVA server '${_RIVA_POD_NAME}'"
        send_telegram "SUCCESS" "RIVA server updated$'\n\n'Pod: \`${_RIVA_POD_NAME}\`$'\n'Endpoint: \`${_RIVA_HOST}:${_RIVA_PORT}\`"
        echo "RIVA server '${_RIVA_POD_NAME}' upserted: ${_RIVA_HOST}:${_RIVA_PORT}"
        exit 0
    else
        log "ERROR" "Failed to upsert RIVA server. HTTP Code: $HTTP_CODE, Response: $HTTP_BODY"
        send_telegram "ERROR" "RIVA server upsert failed$'\n\n'Pod: \`${_RIVA_POD_NAME}\`$'\n'Endpoint: \`${_RIVA_HOST}:${_RIVA_PORT}\`$'\n'HTTP Code: ${HTTP_CODE}"
        exit 1
    fi
elif [ "$_UPDATE_TRANSCSERVER" = "true" ]; then
    # TRANSCSERVER update mode
    log "INFO" "Updating TRANSCSERVER for method=$_METHOD at ${_API_URL}/api/settings/transcserver/$_METHOD..."
    PAYLOAD=$(jq -n --arg value "$_TRANSCSERVER_VALUE" '{value: $value}')
    
    log "INFO" "Payload: $PAYLOAD"
    
    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${_API_URL}/api/settings/transcserver/$_METHOD" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD")
    
    HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
    HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -n -1)
    
    log "INFO" "API Response Code: $HTTP_CODE"
    log "INFO" "API Response Body: $HTTP_BODY"
    
    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        log "INFO" "Successfully updated TRANSCSERVER for method=$_METHOD"
        send_telegram "SUCCESS" "TRANSCSERVER updated$'\n\n'Method: \`${_METHOD}\`$'\n'Value: \`${_TRANSCSERVER_VALUE}\`"
        send_debug_email "TRANSCSERVER Updated" "TRANSCSERVER updated successfully!\n\nMethod: $_METHOD\nValue: $_TRANSCSERVER_VALUE\nAPI Response: $HTTP_CODE"
        echo "TRANSCSERVER updated successfully. Method: $_METHOD, Value: $_TRANSCSERVER_VALUE"
        exit 0
    else
        log "ERROR" "Failed to update TRANSCSERVER. HTTP Code: $HTTP_CODE, Response: $HTTP_BODY"
        send_telegram "ERROR" "TRANSCSERVER update failed$'\n\n'Method: \`${_METHOD}\`$'\n'Value: \`${_TRANSCSERVER_VALUE}\`$'\n'HTTP Code: ${HTTP_CODE}$'\n'Response: $HTTP_BODY"
        send_email "TRANSCSERVER Update Failed" "Failed to update TRANSCSERVER for method $_METHOD.\n\nValue: $_TRANSCSERVER_VALUE\nHTTP Code: $HTTP_CODE\nResponse: $HTTP_BODY"
        exit 1
    fi
else
    # GPU machine update mode (existing logic)
    PAYLOAD=$(jq -n \
        --arg token "$VALIDATION_TOKEN" \
        --arg machineId "$_GPU_MACHINE_ID" \
        --arg machineIp "$GPU_MACHINE_IP" \
        '{ValidationToken: $token, GPUMachineId: $machineId, GPUMachineIp: $machineIp}')
    
    log "INFO" "Updating webservice at ${_API_URL}..."
    log "INFO" "Payload: $PAYLOAD"
    
    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$_API_URL" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD")
    
    HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
    HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -n -1)
    
    log "INFO" "API Response Code: $HTTP_CODE"
    log "INFO" "API Response Body: $HTTP_BODY"
    
    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
        log "INFO" "Successfully updated webservice with pod information"
        send_telegram "SUCCESS" "Webservice updated$'\n\n'Pod: \`${POD_ID}\` (${POD_NAME})$'\n'Machine ID: ${_GPU_MACHINE_ID}$'\n'Endpoint: \`${GPU_MACHINE_IP}\`"
        send_debug_email "RunPod Webservice Updated" "Webservice updated successfully!\n\nPod ID: $POD_ID\nName: $POD_NAME\nEndpoint: $GPU_MACHINE_IP\nMachine ID: ${_GPU_MACHINE_ID}\nAPI Response: $HTTP_CODE"
        echo "Webservice updated successfully. Endpoint: $GPU_MACHINE_IP"
    else
        log "ERROR" "Failed to update webservice. HTTP Code: $HTTP_CODE, Response: $HTTP_BODY"
        send_telegram "ERROR" "Webservice update failed$'\n\n'Pod: \`${POD_ID}\` (${POD_NAME})$'\n'Machine ID: ${_GPU_MACHINE_ID}$'\n'Endpoint: \`${GPU_MACHINE_IP}\`$'\n'HTTP Code: ${HTTP_CODE}$'\n'Response: $HTTP_BODY"
        send_email "RunPod Webservice Update Failed" "Failed to update webservice for pod $POD_ID.\n\nEndpoint: $GPU_MACHINE_IP\nHTTP Code: $HTTP_CODE\nResponse: $HTTP_BODY"
        exit 1
    fi
fi
