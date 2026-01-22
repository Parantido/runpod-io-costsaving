# RunPod Cost-Saving Pod Manager

A comprehensive tool for managing RunPod GPU pods with automatic failover, GPU availability handling, and cost optimization.

## Features

- **Smart Pod Restart**: Automatically restart pods or recreate them if the GPU is unavailable
- **GPU Failover**: Find similar GPUs when the original is unavailable
- **Network Volume Support**: Properly handles network volumes with datacenter constraints
- **GraphQL API Integration**: Uses RunPod's GraphQL API for reliable pod management
- **Automatic Pod ID Tracking**: Maintains pod ID across restarts and recreations
- **Email Notifications**: Optional email alerts for pod status changes
- **Cost Control**: Set maximum price limits for GPU selection

## Prerequisites

- Python 3.8+
- `runpodctl` CLI tool (for some legacy operations)
- `jq` (for shell script JSON parsing)
- `curl` (for API calls)
- A RunPod account with API key

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

Edit `config.sh` to set your preferences:

```bash
# API Configuration (for your external service updates)
API_URL="https://your-api.com/endpoint"
VALIDATION_TOKEN="your-token"
GPU_MACHINE_ID="your-machine-id"

# Email Configuration
EMAIL_RECIPIENT="your-email@example.com"

# GPU Requirements for failover
FAILOVER_MIN_GPU_MEM=140          # Minimum GPU memory in GB
FAILOVER_MIN_VCPU=20              # Minimum vCPU count
FAILOVER_MAX_ONDEMAND_PRICE=4.00  # Maximum $/hour

# Preferred GPU (will be tried first if available)
FAILOVER_PREFERRED_GPU="NVIDIA H200"

# RunPod Resources
FAILOVER_NETWORK_VOLUME_ID="your-volume-id"
FAILOVER_TEMPLATE_ID="your-template-id"
FAILOVER_IMAGE_NAME="your-docker-image:tag"
FAILOVER_POD_NAME="your-pod-name"

# Debug mode (set to "true" for verbose email notifications)
DEBUGGING="false"
```

### 5. Make scripts executable

```bash
chmod +x runpod_costsaving.py start.sh stop.sh info.sh update-webservice.sh
```

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

The main command that handles all scenarios intelligently:

```bash
# Restart existing pod (or recreate if GPU unavailable)
./runpod_costsaving.py restart-or-recreate <pod_id> --json

# With custom max price
./runpod_costsaving.py restart-or-recreate <pod_id> --max-price 4.00 --json

# Create new pod from scratch (no existing pod)
./runpod_costsaving.py restart-or-recreate \
  --fallback-template-id your-template-id \
  --fallback-network-volume-id your-volume-id \
  --fallback-image-name "your-image:tag" \
  --fallback-gpu "NVIDIA H200" \
  --max-price 5.00 \
  --json
```

**What it does:**
1. Tries to restart the existing pod
2. If GPU unavailable, finds a similar GPU in the same datacenter
3. Creates a new pod with the same configuration
4. Terminates the old pod
5. Updates the pod ID file

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

#### Failover Command

Manual failover to create a new pod with best available GPU:

```bash
./runpod_costsaving.py failover \
  --old-pod-id <old_pod_id> \
  --min-mem 140 \
  --min-vcpu 20 \
  --max-price 4.00 \
  --preferred-gpu "NVIDIA H200" \
  --template-id your-template-id \
  --network-volume-id your-volume-id \
  --image-name "your-image:tag" \
  --name "failover-pod" \
  --json
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

**Workflow:**
1. Reads pod ID from argument or `logs/current_pod_id`
2. Checks if pod is already running
3. If not running, uses smart restart/recreate
4. Waits for pod to get IP and port
5. Updates external webservice with pod endpoint

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

## Configuration Reference

### config.sh Options

| Variable | Description | Example |
|----------|-------------|---------|
| `API_URL` | External API endpoint to update with pod info | `https://api.example.com/update` |
| `VALIDATION_TOKEN` | Token for external API authentication | `abc123...` |
| `GPU_MACHINE_ID` | ID for external service | `3` |
| `EMAIL_RECIPIENT` | Email for notifications | `admin@example.com` |
| `LOG_DIR` | Directory for log files | `/root/mgmt/runpod/logs` |
| `MAX_WAIT_SECONDS` | Max time to wait for pod ready | `600` |
| `POLL_INTERVAL` | Seconds between status checks | `5` |
| `GPU_RETRY_INTERVAL` | Seconds between GPU retry attempts | `30` |
| `GPU_RETRY_MAX_TIME` | Max time for GPU retries | `600` |
| `FAILOVER_MIN_GPU_MEM` | Minimum GPU memory (GB) | `140` |
| `FAILOVER_MIN_VCPU` | Minimum vCPU count | `20` |
| `FAILOVER_MAX_ONDEMAND_PRICE` | Maximum hourly price | `4.00` |
| `FAILOVER_PREFERRED_GPU` | Preferred GPU type | `NVIDIA H200` |
| `FAILOVER_NETWORK_VOLUME_ID` | Network volume to attach | `abc123xyz` |
| `FAILOVER_TEMPLATE_ID` | Pod template ID | `xyz789abc` |
| `FAILOVER_IMAGE_NAME` | Docker image | `user/image:tag` |
| `FAILOVER_POD_NAME` | Name for new pods | `my-gpu-pod` |
| `DEBUGGING` | Enable debug emails | `true` or `false` |

## File Structure

```
/root/mgmt/runpod/
├── runpod_costsaving.py    # Main Python CLI tool
├── config.sh               # Configuration file
├── start.sh                # Pod start orchestration
├── stop.sh                 # Pod stop script
├── info.sh                 # Pod info script
├── update-webservice.sh    # External service update
├── .apiKey                 # RunPod API key (DO NOT COMMIT)
├── logs/
│   ├── runpod.log          # Activity log
│   └── current_pod_id      # Current active pod ID
└── README.md               # This file
```

## GPU Selection Logic

When selecting a GPU, the tool prioritizes:

1. **Exact GPU match** - Same GPU type as the original
2. **Stock availability** - High > Medium > Low > Unknown
3. **Memory similarity** - Closest to original GPU memory
4. **Price** - Lower is better (within max price limit)

## Troubleshooting

### Pod won't start - "not enough free GPUs"

This means the specific GPU type is unavailable on the host. The tool will:
1. Look for the same GPU type in other datacenters
2. If not found, look for similar GPUs (same memory class)
3. Create a new pod with the best available option

### Network volume not attaching

Network volumes are datacenter-specific. The tool automatically:
1. Detects the network volume's datacenter
2. Only searches for GPUs in that datacenter
3. Creates pods in the correct datacenter

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
[2024-01-22 08:30:00] [INFO] [start] Starting pod xyz123...
[2024-01-22 08:30:05] [INFO] [start] Pod started successfully
```

## Automation

### Cron job for automatic restart

Add to crontab to automatically start pods:

```bash
# Start pod every day at 8 AM
0 8 * * * /root/mgmt/runpod/start.sh >> /root/mgmt/runpod/logs/cron.log 2>&1

# Stop pod every day at 10 PM
0 22 * * * /root/mgmt/runpod/stop.sh >> /root/mgmt/runpod/logs/cron.log 2>&1
```

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

## License

Internal use only. Not for redistribution.

## Support

For issues, check:
1. `logs/runpod.log` for error messages
2. RunPod console for pod status
3. Network volume datacenter constraints
