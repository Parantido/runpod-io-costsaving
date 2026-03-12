# RunPod Cost-Saving Pod Manager

A comprehensive tool for managing RunPod GPU pods with automatic failover, GPU availability handling, cost optimization, and integrated alerting/observability.

## Features

- **Smart Pod Restart**: Automatically restart pods or recreate them if the GPU is unavailable
- **GPU Failover**: Find similar GPUs when the original is unavailable
- **Retry with Backoff**: Automatic retry when no GPUs available (configurable attempts and intervals)
- **Network Volume Support**: Properly handles network volumes with datacenter constraints
- **GraphQL API Integration**: Uses RunPod's GraphQL API for reliable pod management
- **Automatic Pod ID Tracking**: Maintains pod ID across restarts and recreations
- **Multi-Channel Alerting**: Telegram, Email, and Loki log shipping
- **Grafana Integration**: Alert rules for monitoring via Loki queries
- **Cost Control**: Set maximum price limits for GPU selection
- **Dry-Run Mode**: Test and simulate restart scenarios without making changes

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Alerting & Observability](#alerting--observability)
- [Testing & Debugging Tools](#testing--debugging-tools)
- [File Structure](#file-structure)
- [GPU Selection Logic](#gpu-selection-logic)
- [Troubleshooting](#troubleshooting)
- [Automation](#automation)
- [API Reference](#api-reference)

## Prerequisites

- Python 3.8+
- `runpodctl` CLI tool (for some legacy operations)
- `jq` (for shell script JSON parsing)
- `curl` (for API calls)
- `mail` command (for email notifications)
- A RunPod account with API key

### Optional (for observability):
- Loki instance (for log aggregation)
- Grafana (for alerting and dashboards)
- Telegram Bot (for instant alerts)

## Installation

### 1. Clone or copy the files

```bash
mkdir -p /root/mgmt/runpod
cd /root/mgmt/runpod
# Copy all files to this directory
```

### 2. Set up your RunPod API key

Create a file named `.apiKey` containing your RunPod API key:

```bash
echo "your-runpod-api-key-here" > .apiKey
chmod 600 .apiKey
```

You can find your API key at: https://www.runpod.io/console/user/settings

### 3. Create the logs directory

```bash
mkdir -p logs
```

### 4. Configure settings

Edit `config.sh` to set your preferences (see [Configuration](#configuration) section).

### 5. Make scripts executable

```bash
chmod +x runpod_costsaving.py start.sh stop.sh info.sh update-webservice.sh test_alerts.sh dry_run_restart.py
```

## Configuration

### config.sh Options

#### Core Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `API_URL` | External API endpoint to update with pod info | `https://api.example.com/update` |
| `VALIDATION_TOKEN` | Token for external API authentication | `abc123...` |
| `GPU_MACHINE_ID` | ID for external service | `3` |
| `EMAIL_RECIPIENT` | Email for notifications | `admin@example.com` |
| `LOG_DIR` | Directory for log files | `/root/mgmt/runpod/logs` |
| `DEBUGGING` | Enable debug emails | `true` or `false` |

#### Timing Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `MAX_WAIT_SECONDS` | Max time to wait for pod ready | `600` |
| `POLL_INTERVAL` | Seconds between status checks | `5` |
| `GPU_RETRY_INTERVAL` | Seconds between GPU retry attempts | `30` |
| `GPU_RETRY_MAX_TIME` | Max time for GPU retries | `600` |

#### GPU Failover Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `FAILOVER_MIN_GPU_MEM` | Minimum GPU memory (GB) | `140` |
| `FAILOVER_MIN_VCPU` | Minimum vCPU count | `20` |
| `FAILOVER_MAX_ONDEMAND_PRICE` | Maximum hourly price | `4.00` |
| `FAILOVER_PREFERRED_GPU` | Preferred GPU type | `NVIDIA H200` |
| `FAILOVER_NETWORK_VOLUME_ID` | Network volume to attach | `abc123xyz` |
| `FAILOVER_TEMPLATE_ID` | Pod template ID | `xyz789abc` |
| `FAILOVER_IMAGE_NAME` | Docker image | `user/image:tag` |
| `FAILOVER_POD_NAME` | Name for new pods | `my-gpu-pod` |

#### Telegram Alerting

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token | `123456:ABC-xyz...` |
| `TELEGRAM_CHAT_ID` | Chat/Group ID for alerts | `-5264881832` |
| `TELEGRAM_ENABLED` | Enable Telegram alerts | `true` |

#### Loki Logging

| Variable | Description | Example |
|----------|-------------|---------|
| `LOKI_URL` | Loki push endpoint | `http://localhost:3100` |
| `LOKI_ENABLED` | Enable Loki logging | `true` |

#### GPU Retry Settings (restart-or-recreate)

| Variable | Description | Default |
|----------|-------------|---------|
| `GPU_RETRY_MAX_ATTEMPTS` | Number of retry attempts | `3` |
| `GPU_RETRY_INTERVAL_RECREATE` | Seconds between retries | `300` (5 min) |

## Usage

### Quick Start

Start or restart your pod (handles all scenarios automatically):

```bash
./start.sh
```

Stop your pod:

```bash
./stop.sh
```

### Python CLI Commands

The `runpod_costsaving.py` script provides several commands:

#### restart-or-recreate (Recommended)

The main command that handles all scenarios intelligently with retry logic:

```bash
# Basic usage - restart existing pod or recreate if GPU unavailable
./runpod_costsaving.py restart-or-recreate --json

# With custom max price and retry settings
./runpod_costsaving.py restart-or-recreate \
  --max-price 5.00 \
  --max-retries 3 \
  --retry-interval 300 \
  --json

# Create new pod from scratch (no existing pod)
./runpod_costsaving.py restart-or-recreate \
  --fallback-template-id your-template-id \
  --fallback-network-volume-id your-volume-id \
  --fallback-image-name "your-image:tag" \
  --fallback-gpu "NVIDIA H200" \
  --fallback-name "my-pod" \
  --max-price 5.00 \
  --json

# Disable alerting for this run
./runpod_costsaving.py restart-or-recreate --no-telegram --no-loki --json
```

**New CLI Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--max-retries` | Max retry attempts when no GPU available | `3` |
| `--retry-interval` | Seconds between retry attempts | `300` |
| `--telegram-token` | Override Telegram bot token | from config |
| `--telegram-chat-id` | Override Telegram chat ID | from config |
| `--no-telegram` | Disable Telegram alerts | `false` |
| `--loki-url` | Override Loki URL | from config |
| `--no-loki` | Disable Loki logging | `false` |

**What it does:**
1. Tries to restart the existing pod
2. If restart fails (GPU unavailable on host), **renames the old pod to backup** (`{name}-backup-{timestamp}`)
3. Searches for similar GPUs in the same datacenter
4. If no GPUs found, waits and retries (up to `max-retries` times)
5. Creates a new pod with the original name and best available GPU
6. **Terminates the backup pod** (safe cleanup after new pod is running)
7. Updates the pod ID file
8. Sends alerts at each step (Telegram + Loki)

**Why the backup approach?**
Creating a completely new pod (vs restarting the existing one) allows RunPod to place it on **any available host** in the datacenter, greatly improving GPU availability. The old pod is renamed rather than terminated first to preserve it as a rollback option until the new pod is confirmed running.

#### clone-pod

Create a new pod by cloning an existing pod's configuration:

```bash
# Clone with same GPU
./runpod_costsaving.py clone-pod <source_pod_id> --json

# Clone with different GPU
./runpod_costsaving.py clone-pod <source_pod_id> --gpu-type "NVIDIA A100 80GB" --json

# Clone with new name
./runpod_costsaving.py clone-pod <source_pod_id> --name "my-new-pod" --json
```

#### Legacy Pod Operations

Basic pod management using runpodctl:

```bash
# Get pod info
./runpod_costsaving.py <pod_id> info --json

# Start pod
./runpod_costsaving.py <pod_id> start --json

# Stop pod
./runpod_costsaving.py <pod_id> stop --json

# Remove/terminate pod
./runpod_costsaving.py <pod_id> remove --json

# Restart pod
./runpod_costsaving.py <pod_id> restart --json
```

#### Cloud Operations

Query available GPUs:

```bash
# List all available GPUs
./runpod_costsaving.py cloud list --json

# Filter by requirements
./runpod_costsaving.py cloud list --min-mem 80 --min-vcpu 16 --max-price 3.00 --json

# Find best GPU matching criteria
./runpod_costsaving.py cloud find-best --min-mem 80 --min-vcpu 16 --max-price 3.00 --json
```

### Shell Scripts

#### start.sh

The main orchestration script for starting pods:

```bash
# Start using stored pod ID
./start.sh

# Start specific pod
./start.sh <pod_id>
```

#### stop.sh

Stop a running pod:

```bash
# Stop using stored pod ID
./stop.sh

# Stop specific pod
./stop.sh <pod_id>
```

#### info.sh

Get pod information:

```bash
./info.sh <pod_id>
```

## Alerting & Observability

The system provides multi-channel alerting and observability:

### Alert Channels

| Channel | Use Case | Configuration |
|---------|----------|---------------|
| **Telegram** | Instant mobile alerts | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| **Email** | Backup notifications | `EMAIL_RECIPIENT` |
| **Loki** | Log aggregation & Grafana alerts | `LOKI_URL` |

### Alert Severity Levels

| Level | Emoji | When Used |
|-------|-------|-----------|
| SUCCESS | ✅ | Pod started/recreated successfully |
| INFO | ℹ️ | Informational messages |
| WARNING | ⚠️ | Potential issues, retry attempts |
| ERROR | ❌ | Operation failed |
| CRITICAL | 🚨 | Complete failure, manual intervention required |

### Alert Scenarios

#### Pod Start Failed
```
⚠️ RunPod Alert [WARNING]

Pod start failed
Pod ID: abc123
Name: my-pod
Error: Not enough free GPUs on host

Attempting to find alternative GPU...
```

#### GPU Retry
```
⚠️ RunPod Alert [WARNING]

No GPU available
Datacenter: US-GA-2
GPU Type: NVIDIA H200
Retry: 1/3

Next attempt in 5 minutes...
```

#### Complete Failure
```
🚨 RunPod Alert [CRITICAL]

CRITICAL: No GPU available after all retries
Datacenter: US-GA-2
Required GPU: NVIDIA H200
Max Price: $5.00/hr
Attempts: 3
Total wait time: 10 minutes

Manual intervention required!
```

#### Success
```
✅ RunPod Alert [SUCCESS]

Pod recreated successfully
Old Pod: abc123
New Pod: xyz789
Name: my-pod
GPU: NVIDIA H200
Datacenter: US-GA-2
Cost: $3.59/hr
```

### Loki Log Labels

Logs shipped to Loki include these labels for easy filtering:

| Label | Values | Description |
|-------|--------|-------------|
| `job` | `runpod-costsaving` | Job identifier |
| `host` | hostname | Source host |
| `level` | `INFO`, `WARNING`, `ERROR`, `CRITICAL` | Log level |
| `operation` | `start`, `stop`, `restart`, `recreate`, `gpu_search`, `create` | Operation type |
| `status` | `success`, `failed`, `attempting`, `no_availability` | Operation status |
| `pod_id` | pod ID | Associated pod |
| `retry` | `1`, `2`, `3`, ... | Retry attempt number |

**Example Loki queries:**
```logql
# All runpod logs
{job="runpod-costsaving"}

# Only errors
{job="runpod-costsaving", level="ERROR"}

# GPU search operations
{job="runpod-costsaving", operation="gpu_search"}

# Failed operations
{job="runpod-costsaving", status="failed"}
```

### Grafana Alert Rules

Alert rules are provisioned in Grafana at:
`/grafana/provisioning/alerting/runpod-alerts.yaml`

| Alert | Query | Severity |
|-------|-------|----------|
| RunPod Start Failed | `{job="runpod-costsaving", level="ERROR", operation="start"}` | critical |
| RunPod No GPU Available | `{job="runpod-costsaving", level="CRITICAL", status=~"no_availability.*"}` | critical |
| RunPod Recreation Failed | `{job="runpod-costsaving", level="CRITICAL", operation="recreate", status="failed"}` | critical |

## Testing & Debugging Tools

### test_alerts.sh - Alert Channel Testing

Test all alerting channels without running actual pod operations:

```bash
# Run basic tests (Email, Telegram, Loki)
./test_alerts.sh

# Example output:
# [TEST 1] Testing Email Alert...
#   ✓ Email sent to: your-email@example.com
#
# [TEST 2] Testing Telegram Alert...
#   ✓ Telegram message sent to chat: -5264881832
#
# [TEST 3] Testing Loki Logging...
#   ✓ Log entry sent to Loki
#   ✓ Log entry verified in Loki
```

When prompted, you can also run extended tests:
- **All alert levels**: Sends test alerts for SUCCESS, INFO, WARNING, ERROR, CRITICAL
- **Failure simulation**: Sends a realistic "No GPU Available" alert

### dry_run_restart.py - Restart Simulation

Simulate the restart-or-recreate flow without making any changes:

```bash
# Basic dry run
./dry_run_restart.py

# With custom parameters
./dry_run_restart.py --max-price 5.00 --max-retries 3

# Verbose output
./dry_run_restart.py --verbose

# Send test alert based on simulation result
./dry_run_restart.py --send-test-alert
```

**What it shows:**
1. Current pod configuration
2. Network volume and datacenter constraints
3. Real GPU availability in your datacenter
4. Simulated recreation order
5. Prediction of success/failure
6. Cost comparison if GPU type differs

**Example output:**
```
============================================================
 RunPod Restart-or-Recreate DRY RUN Simulation
============================================================
Timestamp: 2024-01-22 15:30:00
Max Price: $5.00/hr
Max Retries: 3
Retry Interval: 300s (5 minutes)

[Step 1] Loading Current Pod Configuration
--------------------------------------------------
Pod ID (from file): abc123xyz

[Step 2] Fetching Pod Configuration from RunPod API
--------------------------------------------------
Pod Name:          my-gpu-pod
GPU Type:          NVIDIA H200
GPU Count:         1
Template ID:       template123
Network Volume:    volume456
Current Cost:      $3.59/hr

[Step 3] Identifying Datacenter from Network Volume
--------------------------------------------------
Network Volume:    models-storage (10 GB)
Datacenter ID:     US-GA-2
Global Network:    True

[Step 4] SIMULATING: Pod Start Failure
--------------------------------------------------
>>> Simulating scenario where pod cannot start

[Step 5] Querying Real GPU Availability
--------------------------------------------------
Target GPU: NVIDIA H200 (140 GB)
Max Price: $5.00/hr

[Step 6] GPU Availability Results
--------------------------------------------------

✅ Found 2 available GPU(s):

#   GPU Type                  Memory     Price        Stock           Match
-------------------------------------------------------------------------------------
1   NVIDIA H200               141 GB     $3.59/hr     🔴 Low          ✓ EXACT
2   NVIDIA A100 80GB          80 GB      $2.49/hr     🟢 High

============================================================
 Dry Run Summary
============================================================
✅ RESULT: Recreation would SUCCEED
   Best option: NVIDIA H200 @ $3.59/hr
```

**Simulating failure scenarios:**
```bash
# Simulate no GPU available by setting very low max price
./dry_run_restart.py --max-price 0.50

# Output will show:
# ⚠️  NO GPUs AVAILABLE!
# In real scenario, this would trigger:
# - Retry 1/3: Wait 5 minutes, try again
# - Retry 2/3: Wait 5 minutes, try again
# - Retry 3/3: CRITICAL alert sent, script exits
```

### Manual Testing Commands

#### Test Telegram directly:
```bash
python3 -c "
from runpod_costsaving import TelegramNotifier
t = TelegramNotifier()
t.send_alert('Test message', 'INFO')
"
```

#### Test Loki directly:
```bash
python3 -c "
from runpod_costsaving import LokiLogger
l = LokiLogger()
l.log('Test log entry', 'INFO', operation='test')
"
```

#### Query Loki logs:
```bash
curl -s -G "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query={job="runpod-costsaving"}' | jq
```

#### Test email:
```bash
echo "Test email body" | mail -s "[TEST] RunPod Alert" your-email@example.com
```

## File Structure

```
/root/mgmt/runpod/
├── runpod_costsaving.py    # Main Python CLI tool
├── config.sh               # Configuration file (shared defaults)
├── config.sh.tpl           # Configuration template
├── start.sh                # Pod start orchestration (legacy single-instance)
├── stop.sh                 # Pod stop script (legacy single-instance)
├── info.sh                 # Pod info script
├── update-webservice.sh    # External service update (multi-instance capable)
├── test_alerts.sh          # Alert testing tool
├── dry_run_restart.py      # Restart simulation tool
├── .apiKey                 # RunPod API key (DO NOT COMMIT)
├── logs/
│   ├── runpod.log              # Activity log
│   ├── current_pod_id          # Legacy single-instance pod ID file
│   ├── tr-gpu-01-prod.pod_id   # Pod ID for instance 1 (multi-instance)
│   └── tr-gpu-02-prod.pod_id   # Pod ID for instance 2 (multi-instance)
└── README.md               # This file
```

### Grafana Provisioning Files

If using Grafana for alerting, these files are created:

```
/path/to/grafana/provisioning/alerting/
├── runpod-alerts.yaml       # Alert rules
├── contact-points.yaml      # Telegram contact point
└── notification-policies.yaml  # Alert routing
```

## GPU Selection Logic

When selecting a GPU, the tool prioritizes:

1. **Exact GPU match** - Same GPU type as the original
2. **Stock availability** - High > Medium > Low > Unknown
3. **Memory similarity** - Closest to original GPU memory
4. **Price** - Lower is better (within max price limit)

### Stock Status Indicators

| Status | Indicator | Meaning |
|--------|-----------|---------|
| High | 🟢 | Many GPUs available, likely to succeed |
| Medium | 🟡 | Some GPUs available |
| Low | 🔴 | Few GPUs available, may fail |
| Unknown | ⚪ | Availability unknown |

## Troubleshooting

### Pod won't start - "not enough free GPUs"

This means the specific GPU type is unavailable on the host. The tool will:
1. Search for the same GPU type in the same datacenter
2. Wait and retry if configured (default: 3 retries, 5 minutes apart)
3. Send alerts on each retry and on final failure

### Network volume not attaching

Network volumes are datacenter-specific. The tool automatically:
1. Detects the network volume's datacenter
2. Only searches for GPUs in that datacenter
3. Creates pods in the correct datacenter

**Note:** Cross-datacenter volume attachment is not supported. If no GPUs are available in your volume's datacenter, the script will retry but cannot move to another datacenter.

### Telegram alerts not sending

1. Verify bot token and chat ID in `config.sh`
2. Test manually:
   ```bash
   ./test_alerts.sh
   ```
3. Ensure the bot is added to the chat/group
4. For group chats, chat ID should be negative (e.g., `-5264881832`)

### Loki logs not appearing

1. Verify Loki URL in `config.sh`
2. Test connection:
   ```bash
   curl -s http://your-loki:3100/ready
   ```
3. Check if logs are being shipped:
   ```bash
   ./test_alerts.sh
   ```
4. Query Loki directly:
   ```bash
   curl -s -G "http://your-loki:3100/loki/api/v1/query" \
     --data-urlencode 'query={job="runpod-costsaving"}'
   ```

### Pod ID file not updating

Ensure the `logs/` directory exists and is writable:

```bash
mkdir -p logs
chmod 755 logs
```

### API key errors

Verify your API key is correct:

```bash
cat .apiKey
# Should show your RunPod API key
```

Test with a simple query:

```bash
./runpod_costsaving.py cloud list --json
```

### Email notifications not working

The `mail` command must be installed and configured:

```bash
# Test email
echo "Test" | mail -s "Test" your-email@example.com
```

## Logs

View the activity log:

```bash
tail -f logs/runpod.log
```

Log format:
```
[2024-01-22 08:30:00] [INFO] [python] Starting pod xyz123...
[2024-01-22 08:30:05] [INFO] [python] Pod started successfully
```

## Automation

### Multi-Instance Cron Setup

Each pod instance is identified by its own pod ID file (`--pod-id-file`). This allows multiple
independent pods to be managed from the same host without conflict.

**File layout** (one file per instance):
```
logs/
├── tr-gpu-01-prod.pod_id   # pod ID for instance 1
├── tr-gpu-02-prod.pod_id   # pod ID for instance 2
└── runpod.log
```

**Example crontab** (edit with `crontab -e`):

```cron
# ── Instance 1: tr-gpu-01-prod ────────────────────────────────────────────────
# Start
30 15 * * 1-5 /root/mgmt/runpod/runpod_costsaving.py restart-or-recreate \
  --pod-id-file /root/mgmt/runpod/logs/tr-gpu-01-prod.pod_id \
  --fallback-template-id 02rh6eeawc \
  --fallback-network-volume-id y4aw9smcev \
  --fallback-image-name "docker.io/parantido/riva-speech-runpod:2.19.0" \
  --fallback-gpu "NVIDIA H200" \
  --fallback-name "tr-gpu-01-prod" \
  --max-price 5.00 --json >> /root/mgmt/runpod/logs/cron.log 2>&1

# Update webservice
55 15 * * 1-5 /root/mgmt/runpod/update-webservice.sh \
  --pod-id-file /root/mgmt/runpod/logs/tr-gpu-01-prod.pod_id \
  --gpu-machine-id 3 >> /root/mgmt/runpod/logs/cron.log 2>&1

# Stop
20 23 * * * /root/mgmt/runpod/runpod_costsaving.py --json --pod-id-file /root/mgmt/runpod/logs/tr-gpu-01-prod.pod_id stop >> /root/mgmt/runpod/logs/cron.log 2>&1

# ── Instance 2: tr-gpu-02-prod ────────────────────────────────────────────────
# Start
30 15 * * 1-5 /root/mgmt/runpod/runpod_costsaving.py restart-or-recreate \
  --pod-id-file /root/mgmt/runpod/logs/tr-gpu-02-prod.pod_id \
  --fallback-template-id 02rh6eeawc \
  --fallback-network-volume-id y4aw9smcev \
  --fallback-image-name "docker.io/parantido/riva-speech-runpod:2.19.0" \
  --fallback-gpu "NVIDIA H200" \
  --fallback-name "tr-gpu-02-prod" \
  --max-price 9.00 --json >> /root/mgmt/runpod/logs/cron.log 2>&1

# Update webservice
55 15 * * 1-5 /root/mgmt/runpod/update-webservice.sh \
  --pod-id-file /root/mgmt/runpod/logs/tr-gpu-02-prod.pod_id \
  --gpu-machine-id 4 >> /root/mgmt/runpod/logs/cron.log 2>&1

# Stop
20 23 * * * /root/mgmt/runpod/runpod_costsaving.py --json --pod-id-file /root/mgmt/runpod/logs/tr-gpu-02-prod.pod_id stop >> /root/mgmt/runpod/logs/cron.log 2>&1
```

> **Note on `stop`**: the legacy `stop` action accepts `--pod-id-file` *before* the action verb
> because it is parsed in legacy mode. The pattern is:
> `runpod_costsaving.py [--json] [--pod-id-file FILE] stop`

### File Structure for Multiple Instances

```
logs/
├── tr-gpu-01-prod.pod_id   # Written by restart-or-recreate for instance 1
├── tr-gpu-02-prod.pod_id   # Written by restart-or-recreate for instance 2
└── runpod.log
```

`restart-or-recreate` automatically writes the new pod ID to `--pod-id-file` after a successful
start or recreation, so the subsequent `update-webservice.sh` and `stop` calls will always use
the correct current pod ID.

### update-webservice.sh options

| Option | Description | Default |
|--------|-------------|---------|
| `--pod-id-file FILE` | Path to file containing the pod ID | `$POD_ID_FILE` from config.sh |
| `--gpu-machine-id ID` | GPU machine ID sent to the API | `$GPU_MACHINE_ID` from config.sh |
| `--api-url URL` | Webservice API URL | `$API_URL` from config.sh |
| `--telegram-token TOKEN` | Override Telegram bot token | from config.sh |
| `--telegram-chat-id ID` | Override Telegram chat ID | from config.sh |
| `--no-telegram` | Disable Telegram notifications | `false` |

### Systemd service

Create `/etc/systemd/system/runpod-manager.service`:

```ini
[Unit]
Description=RunPod Pod Manager
After=network.target

[Service]
Type=oneshot
ExecStart=/root/mgmt/runpod/start.sh
ExecStop=/root/mgmt/runpod/stop.sh
RemainAfterExit=yes
User=root
WorkingDirectory=/root/mgmt/runpod

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
systemctl enable runpod-manager
systemctl start runpod-manager
```

## API Reference

### GraphQL Queries Used

The tool uses these RunPod GraphQL operations:

- `podFindAndDeployOnDemand` - Create new pods
- `podTerminate` - Terminate pods
- `pod` (query) - Get pod details
- `gpuTypes` - Query GPU availability
- `myself.networkVolumes` - Get network volume info

### Output Formats

All commands support `--json` for machine-readable output:

```json
{
  "success": true,
  "action": "recreated",
  "old_pod_id": "abc123",
  "new_pod_id": "xyz789",
  "original_gpu": "NVIDIA H200",
  "new_gpu": "NVIDIA H200",
  "new_gpu_price": 3.59,
  "datacenter": "US-GA-2",
  "retry_attempts": 1,
  "public_ip": "1.2.3.4",
  "port_mappings": [
    {
      "public_ip": "1.2.3.4",
      "public_port": 12345,
      "container_port": 50051,
      "protocol": "tcp"
    }
  ]
}
```

### Python Classes

The `runpod_costsaving.py` module exports these classes for programmatic use:

```python
from runpod_costsaving import (
    TelegramNotifier,   # Send Telegram alerts
    LokiLogger,         # Ship logs to Loki
    CloudManager,       # RunPod API operations
    RunPodManager,      # Single pod management
    PodConfig,          # Pod configuration dataclass
    CloudGPU,           # GPU information dataclass
)

# Example: Send a custom alert
telegram = TelegramNotifier()
telegram.send_alert("Custom message", "WARNING")

# Example: Log to Loki
loki = LokiLogger()
loki.log("Custom log", "INFO", operation="custom", status="success")

# Example: Query GPU availability
cloud = CloudManager()
gpus = cloud.find_similar_gpus("NVIDIA H200", 140, "US-GA-2", max_price=5.0)
```

## License

Internal use only. Not for redistribution.

## Support

For issues, check:
1. `logs/runpod.log` for error messages
2. RunPod console for pod status
3. Network volume datacenter constraints
4. Run `./test_alerts.sh` to verify alerting
5. Run `./dry_run_restart.py` to simulate scenarios
