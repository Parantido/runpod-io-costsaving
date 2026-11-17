#!/bin/bash
# Test script for RunPod alerting channels
# Tests: Telegram, Loki, and Email notifications

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

echo "============================================"
echo "RunPod Alerting Test Script"
echo "============================================"
echo ""

# Test 1: Email
test_email() {
    echo "[TEST 1] Testing Email Alert..."
    local subject="[TEST] RunPod Alert Test"
    local body="This is a test email from the RunPod alerting system.\n\nTimestamp: $(date)\nHost: $(hostname)\n\nIf you received this email, email alerting is working correctly."
    
    echo -e "$body" | mail -s "$subject" "$EMAIL_RECIPIENT" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "  ✓ Email sent to: $EMAIL_RECIPIENT"
        return 0
    else
        echo "  ✗ Failed to send email (mail command may not be available)"
        return 1
    fi
}

# Test 2: Telegram
test_telegram() {
    echo "[TEST 2] Testing Telegram Alert..."
    
    if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
        echo "  ✗ Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)"
        return 1
    fi
    
    cd "$SCRIPT_DIR"
    python3 -c "
from runpod_costsaving import TelegramNotifier
t = TelegramNotifier()
result = t.send_alert('🧪 *Test Alert*\n\nThis is a test message from the RunPod alerting system.\n\nTimestamp: $(date)\nHost: $(hostname)\n\nIf you received this, Telegram alerting is working correctly.', 'INFO')
exit(0 if result else 1)
" 2>/dev/null
    
    if [ $? -eq 0 ]; then
        echo "  ✓ Telegram message sent to chat: $TELEGRAM_CHAT_ID"
        return 0
    else
        echo "  ✗ Failed to send Telegram message"
        return 1
    fi
}

# Test 3: Loki
test_loki() {
    echo "[TEST 3] Testing Loki Logging..."
    
    if [ -z "$LOKI_URL" ]; then
        echo "  ✗ Loki not configured (missing LOKI_URL)"
        return 1
    fi
    
    cd "$SCRIPT_DIR"
    python3 -c "
from runpod_costsaving import LokiLogger
l = LokiLogger()
result = l.log('Test log entry from alerting test script', 'INFO', operation='test', status='success')
exit(0 if result else 1)
" 2>/dev/null
    
    if [ $? -eq 0 ]; then
        echo "  ✓ Log entry sent to Loki: $LOKI_URL"
        
        # Verify log was received
        sleep 1
        local query_result=$(curl -s -G "${LOKI_URL}/loki/api/v1/query" \
            --data-urlencode 'query={job="runpod-costsaving", operation="test"}' \
            --data-urlencode "limit=1" 2>/dev/null)
        
        if echo "$query_result" | grep -q "test"; then
            echo "  ✓ Log entry verified in Loki"
        else
            echo "  ⚠ Log sent but could not verify (may take a moment to appear)"
        fi
        return 0
    else
        echo "  ✗ Failed to send log to Loki"
        return 1
    fi
}

# Test 4: Test all Telegram alert types
test_telegram_all_levels() {
    echo "[TEST 4] Testing all Telegram alert levels..."
    
    cd "$SCRIPT_DIR"
    python3 << 'EOF'
from runpod_costsaving import TelegramNotifier
import time

t = TelegramNotifier()

levels = [
    ("SUCCESS", "This is a SUCCESS level alert"),
    ("INFO", "This is an INFO level alert"),
    ("WARNING", "This is a WARNING level alert"),
    ("ERROR", "This is an ERROR level alert"),
    ("CRITICAL", "This is a CRITICAL level alert"),
]

print("  Sending test alerts for each level...")
for level, msg in levels:
    result = t.send_alert(f"🧪 *Test: {level}*\n\n{msg}", level)
    status = "✓" if result else "✗"
    print(f"    {status} {level}")
    time.sleep(0.5)  # Small delay to avoid rate limiting

print("  Done!")
EOF
    return $?
}

# Test 5: Simulate a failure scenario alert
test_failure_scenario() {
    echo "[TEST 5] Testing simulated failure scenario..."
    
    cd "$SCRIPT_DIR"
    python3 << 'EOF'
from runpod_costsaving import TelegramNotifier, LokiLogger

t = TelegramNotifier()
l = LokiLogger()

# Simulate the exact alert that would be sent on GPU unavailability
print("  Simulating 'No GPU Available' alert...")

# Send to Loki
l.log_no_gpu_available("US-GA-2", "NVIDIA H200", 1, 3)

# Send to Telegram
t.alert_no_gpu_retry(
    datacenter="US-GA-2",
    gpu_type="NVIDIA H200",
    retry=1,
    max_retries=3,
    next_retry_seconds=300
)

print("  ✓ Simulated failure alerts sent")
print("  Check your Telegram and Grafana/Loki for the alerts")
EOF
    return $?
}

# Run all tests
echo "Running alerting tests..."
echo ""

test_email
echo ""

test_telegram
echo ""

test_loki
echo ""

# Ask if user wants to run additional tests
read -p "Run additional tests (all alert levels + failure simulation)? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    test_telegram_all_levels
    echo ""
    test_failure_scenario
fi

echo ""
echo "============================================"
echo "Test Summary"
echo "============================================"
echo "Email recipient: $EMAIL_RECIPIENT"
echo "Telegram chat:   $TELEGRAM_CHAT_ID"
echo "Loki URL:        $LOKI_URL"
echo ""
echo "Check your email inbox and Telegram chat for test messages."
echo "Check Grafana Explore with query: {job=\"runpod-costsaving\"}"
echo "============================================"
