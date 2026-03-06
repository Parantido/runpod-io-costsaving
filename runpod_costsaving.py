#!/usr/bin/env python3
"""
RunPod Pod Management Script
Automates starting/stopping pods and extracts public IP/port information
Includes failover logic to create new pods when GPU unavailable
"""

import subprocess
import re
import time
import json
import sys
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# Global debug logger
_debug_logger = None
_debug_enabled = False

# Script directory for finding config files
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'config.sh')

def read_config_value(key: str, default: str = None) -> Optional[str]:
    """Read a value from config.sh file"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    # Parse KEY="value" or KEY=value
                    if line.startswith(f'{key}='):
                        value = line.split('=', 1)[1]
                        # Remove quotes if present
                        value = value.strip('"\'')
                        return value
    except Exception:
        pass
    return default

def setup_debug_logging(enabled: bool = None, log_file: str = None):
    """Setup debug logging if DEBUGGING environment variable is true or explicitly enabled"""
    global _debug_logger, _debug_enabled

    # Check if debugging should be enabled - check env first, then config.sh
    if enabled is None:
        env_debug = os.environ.get('DEBUGGING', '').lower()
        if env_debug:
            enabled = env_debug == 'true'
        else:
            # Read from config.sh
            config_debug = read_config_value('DEBUGGING', 'false')
            enabled = config_debug.lower() == 'true'

    _debug_enabled = enabled

    if not enabled:
        return

    # Determine log file path - check env first, then config.sh
    if log_file is None:
        log_dir = os.environ.get('LOG_DIR', '')
        if not log_dir:
            log_dir = read_config_value('LOG_DIR', '/root/mgmt/runpod/logs')
        log_file = os.path.join(log_dir, 'runpod.log')

    # Ensure log directory exists
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Setup logger
    _debug_logger = logging.getLogger('runpod_debug')
    _debug_logger.setLevel(logging.DEBUG)

    # Remove existing handlers
    _debug_logger.handlers = []

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] [python] %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    file_handler.setFormatter(formatter)
    _debug_logger.addHandler(file_handler)

    debug_log("INFO", f"Debug logging initialized. Log file: {log_file}")

def debug_log(level: str, message: str):
    """Log a debug message if debugging is enabled"""
    global _debug_logger, _debug_enabled

    if not _debug_enabled or _debug_logger is None:
        return

    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARN': logging.WARNING,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
    }

    log_level = level_map.get(level.upper(), logging.INFO)
    _debug_logger.log(log_level, message)


@dataclass
class PortMapping:
    """Represents a port mapping from the pod"""
    public_ip: str
    public_port: int
    container_port: int
    protocol: str
    visibility: str  # 'pub' or 'prv'


@dataclass
class PodInfo:
    """Represents pod information"""
    id: str
    name: str
    status: str
    public_ip: Optional[str]
    port_mappings: List[PortMapping]


@dataclass
class PodConfig:
    """Stores pod configuration for cloning/recreating a pod"""
    id: str
    name: str
    image_name: str
    gpu_type_id: str
    gpu_count: int
    vcpu_count: int
    memory_in_gb: int
    container_disk_in_gb: int
    volume_in_gb: int
    volume_mount_path: str
    template_id: Optional[str]
    network_volume_id: Optional[str]
    datacenter_id: Optional[str]
    ports: str
    env: List[str] = field(default_factory=list)
    docker_args: Optional[str] = None
    cost_per_hr: Optional[float] = None


@dataclass
class CloudGPU:
    """Represents an available GPU type from the cloud"""
    gpu_type: str  # API ID (e.g., "NVIDIA H100 NVL")
    display_name: str  # Human-readable name (e.g., "H100 NVL")
    mem_gb: int
    vcpu: int
    spot_price: Optional[float]
    ondemand_price: Optional[float]
    stock_status: Optional[str] = None  # "High", "Medium", "Low", or None
    datacenter_id: Optional[str] = None  # Datacenter where available

    def matches_requirements(self, min_mem: int, min_vcpu: int, max_price: float) -> bool:
        """Check if this GPU meets the minimum requirements"""
        if self.mem_gb < min_mem:
            return False
        if self.vcpu < min_vcpu:
            return False
        if self.ondemand_price is None:
            return False  # Reserved GPUs
        if self.ondemand_price > max_price:
            return False
        return True

    def stock_score(self) -> int:
        """Return a score for stock status (lower is better = more available)"""
        if self.stock_status == "High":
            return 0
        elif self.stock_status == "Medium":
            return 1
        elif self.stock_status == "Low":
            return 2
        else:
            return 3  # Unknown or None

    def score(self) -> tuple:
        """
        Calculate a score for this GPU (lower is better).
        Prioritizes: stock availability, then lowest price, then highest memory
        """
        if self.ondemand_price is None:
            return (999, float('inf'), 0)
        # Primary: stock status, Secondary: price, Tertiary: inverse of memory
        return (self.stock_score(), self.ondemand_price, -self.mem_gb)


class TelegramNotifier:
    """Sends alerts to Telegram via Bot API"""
    
    LEVEL_EMOJI = {
        "SUCCESS": "✅",
        "INFO": "ℹ️",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "CRITICAL": "🚨"
    }
    
    def __init__(self, bot_token: str = None, chat_id: str = None, enabled: bool = True):
        self.bot_token = bot_token or read_config_value("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or read_config_value("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and self.bot_token and self.chat_id
        if self.enabled:
            self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        else:
            self.api_url = None
            if not bot_token and not self.bot_token:
                debug_log("WARNING", "Telegram notifications disabled: no bot token configured")
    
    def send_alert(self, message: str, level: str = "INFO") -> bool:
        """Send alert to Telegram with emoji indicator"""
        if not self.enabled:
            debug_log("DEBUG", f"Telegram disabled, would send [{level}]: {message}")
            return False
        
        emoji = self.LEVEL_EMOJI.get(level, "ℹ️")
        formatted_message = f"{emoji} *RunPod Alert* [{level}]\n\n{message}"
        
        try:
            import urllib.request
            import urllib.parse
            
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": formatted_message,
                "parse_mode": "Markdown"
            }).encode("utf-8")
            
            req = urllib.request.Request(self.api_url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    debug_log("DEBUG", f"Telegram alert sent: {level}")
                    return True
                else:
                    debug_log("ERROR", f"Telegram API error: {result}")
                    return False
        except Exception as e:
            debug_log("ERROR", f"Failed to send Telegram alert: {e}")
            return False
    
    def alert_pod_start_failed(self, pod_id: str, pod_name: str, error: str) -> bool:
        """Alert when pod start fails"""
        message = (
            f"*Pod start failed*\n"
            f"Pod ID: `{pod_id}`\n"
            f"Name: {pod_name}\n"
            f"Error: {error}\n\n"
            f"Attempting to find alternative GPU..."
        )
        return self.send_alert(message, "WARNING")
    
    def alert_no_gpu_retry(self, datacenter: str, gpu_type: str, retry: int, max_retries: int, 
                           next_retry_seconds: int) -> bool:
        """Alert when no GPU available, will retry"""
        message = (
            f"*No GPU available*\n"
            f"Datacenter: {datacenter}\n"
            f"GPU Type: {gpu_type}\n"
            f"Retry: {retry}/{max_retries}\n\n"
            f"Next attempt in {next_retry_seconds // 60} minutes..."
        )
        return self.send_alert(message, "WARNING")
    
    def alert_no_gpu_final(self, datacenter: str, gpu_type: str, max_price: float, 
                           attempts: int, total_time_minutes: int) -> bool:
        """Alert when all GPU retries exhausted"""
        message = (
            f"*CRITICAL: No GPU available after all retries*\n"
            f"Datacenter: {datacenter}\n"
            f"Required GPU: {gpu_type}\n"
            f"Max Price: ${max_price:.2f}/hr\n"
            f"Attempts: {attempts}\n"
            f"Total wait time: {total_time_minutes} minutes\n\n"
            f"*Manual intervention required!*"
        )
        return self.send_alert(message, "CRITICAL")
    
    def alert_pod_recreated(self, old_pod_id: str, new_pod_id: str, pod_name: str,
                            gpu_type: str, datacenter: str, cost_per_hr: float) -> bool:
        """Alert when pod successfully recreated"""
        message = (
            f"*Pod recreated successfully*\n"
            f"Old Pod: `{old_pod_id}`\n"
            f"New Pod: `{new_pod_id}`\n"
            f"Name: {pod_name}\n"
            f"GPU: {gpu_type}\n"
            f"Datacenter: {datacenter}\n"
            f"Cost: ${cost_per_hr:.2f}/hr"
        )
        return self.send_alert(message, "SUCCESS")
    
    def alert_pod_started(self, pod_id: str, pod_name: str) -> bool:
        """Alert when existing pod started successfully"""
        message = (
            f"*Pod started successfully*\n"
            f"Pod ID: `{pod_id}`\n"
            f"Name: {pod_name}"
        )
        return self.send_alert(message, "SUCCESS")
    
    def alert_creation_failed(self, gpu_type: str, datacenter: str, error: str, 
                              attempts: int, total_gpus: int) -> bool:
        """Alert when pod creation fails for a specific GPU"""
        message = (
            f"*Pod creation failed*\n"
            f"GPU: {gpu_type}\n"
            f"Datacenter: {datacenter}\n"
            f"Error: {error}\n"
            f"Attempt: {attempts}/{total_gpus}"
        )
        return self.send_alert(message, "ERROR")


class LokiLogger:
    """Ships structured logs to Loki"""
    
    def __init__(self, loki_url: str = None, enabled: bool = True):
        base_url = loki_url or read_config_value("LOKI_URL", "http://69.167.170.111:3100")
        self.push_url = f"{base_url}/loki/api/v1/push"
        self.enabled = enabled and bool(base_url)
        self.default_labels = {
            "job": "runpod-costsaving",
            "host": os.uname().nodename
        }
    
    def log(self, message: str, level: str = "INFO", **extra_labels) -> bool:
        """Push a log entry to Loki"""
        if not self.enabled:
            debug_log("DEBUG", f"Loki disabled, would log [{level}]: {message}")
            return False
        
        # Merge labels
        labels = {**self.default_labels, "level": level, **extra_labels}
        
        # Format labels as Loki expects: {key="value", key2="value2"}
        label_str = ", ".join(f'{k}="{v}"' for k, v in labels.items())
        
        # Timestamp in nanoseconds
        timestamp_ns = str(int(time.time() * 1_000_000_000))
        
        payload = {
            "streams": [
                {
                    "stream": labels,
                    "values": [[timestamp_ns, message]]
                }
            ]
        }
        
        try:
            import urllib.request
            
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(self.push_url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status in (200, 204):
                    debug_log("DEBUG", f"Loki log shipped: [{level}] {message[:50]}...")
                    return True
                else:
                    debug_log("WARNING", f"Loki returned status {response.status}")
                    return False
        except Exception as e:
            debug_log("WARNING", f"Failed to ship log to Loki: {e}")
            return False
    
    def log_pod_start_attempted(self, pod_id: str, pod_name: str) -> bool:
        """Log pod start attempt"""
        return self.log(
            f"Attempting to start pod {pod_id} ({pod_name})",
            level="INFO",
            operation="start",
            pod_id=pod_id,
            status="attempting"
        )
    
    def log_pod_start_failed(self, pod_id: str, pod_name: str, error: str) -> bool:
        """Log pod start failure"""
        return self.log(
            f"Pod start failed: {pod_id} ({pod_name}) - {error}",
            level="ERROR",
            operation="start",
            pod_id=pod_id,
            status="failed"
        )
    
    def log_pod_start_success(self, pod_id: str, pod_name: str) -> bool:
        """Log pod start success"""
        return self.log(
            f"Pod started successfully: {pod_id} ({pod_name})",
            level="INFO",
            operation="start",
            pod_id=pod_id,
            status="success"
        )
    
    def log_gpu_search(self, datacenter: str, gpu_type: str, retry: int = 0) -> bool:
        """Log GPU search attempt"""
        return self.log(
            f"Searching for GPU in {datacenter}: {gpu_type} (retry {retry})",
            level="INFO",
            operation="gpu_search",
            datacenter=datacenter,
            retry=str(retry)
        )
    
    def log_no_gpu_available(self, datacenter: str, gpu_type: str, retry: int, max_retries: int) -> bool:
        """Log no GPU available"""
        level = "CRITICAL" if retry >= max_retries else "WARNING"
        status = "no_availability_final" if retry >= max_retries else "no_availability"
        return self.log(
            f"No GPU available in {datacenter} for {gpu_type} (retry {retry}/{max_retries})",
            level=level,
            operation="gpu_search",
            datacenter=datacenter,
            status=status,
            retry=str(retry)
        )
    
    def log_pod_creation_attempted(self, gpu_type: str, datacenter: str) -> bool:
        """Log pod creation attempt"""
        return self.log(
            f"Attempting to create pod with {gpu_type} in {datacenter}",
            level="INFO",
            operation="create",
            gpu_type=gpu_type,
            datacenter=datacenter,
            status="attempting"
        )
    
    def log_pod_creation_failed(self, gpu_type: str, datacenter: str, error: str) -> bool:
        """Log pod creation failure"""
        return self.log(
            f"Pod creation failed with {gpu_type} in {datacenter}: {error}",
            level="ERROR",
            operation="create",
            gpu_type=gpu_type,
            datacenter=datacenter,
            status="failed"
        )
    
    def log_pod_created(self, pod_id: str, pod_name: str, gpu_type: str, datacenter: str) -> bool:
        """Log pod created successfully"""
        return self.log(
            f"Pod created: {pod_id} ({pod_name}) with {gpu_type} in {datacenter}",
            level="INFO",
            operation="create",
            pod_id=pod_id,
            gpu_type=gpu_type,
            datacenter=datacenter,
            status="success"
        )
    
    def log_pod_recreated(self, old_pod_id: str, new_pod_id: str, gpu_type: str, datacenter: str) -> bool:
        """Log pod recreation complete"""
        return self.log(
            f"Pod recreated: {old_pod_id} -> {new_pod_id} with {gpu_type} in {datacenter}",
            level="INFO",
            operation="recreate",
            old_pod_id=old_pod_id,
            new_pod_id=new_pod_id,
            gpu_type=gpu_type,
            datacenter=datacenter,
            status="success"
        )
    
    def log_operation_failed(self, operation: str, error: str, **extra) -> bool:
        """Log operation failure"""
        return self.log(
            f"Operation {operation} failed: {error}",
            level="CRITICAL",
            operation=operation,
            status="failed",
            **extra
        )


class RunPodManager:
    """Manages RunPod operations"""
    
    def __init__(self, pod_id: str):
        self.pod_id = pod_id
    
    def _run_command(self, command: List[str]) -> str:
        """Execute a runpodctl command and return output"""
        cmd_str = ' '.join(command)
        debug_log("DEBUG", f"Executing command: {cmd_str}")
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True
            )
            debug_log("DEBUG", f"Command output: {result.stdout[:500] if result.stdout else '(empty)'}")
            return result.stdout
        except subprocess.CalledProcessError as e:
            debug_log("ERROR", f"Command failed: {cmd_str}")
            debug_log("ERROR", f"Stderr: {e.stderr}")
            raise Exception(f"Command failed: {e.stderr}")
    
    def get_pod_info(self) -> PodInfo:
        """Get detailed pod information"""
        output = self._run_command(['runpodctl', 'get', 'pod', self.pod_id, '-a'])
        return self._parse_pod_output(output)
    
    def _parse_pod_output(self, output: str) -> PodInfo:
        """Parse runpodctl output to extract pod information"""
        
        # Just for debugging
        # print(f"Pod infos: {output} ...")

        lines = output.strip().split('\n')

        # Skip header line, get data line
        if len(lines) < 2:
            raise Exception("Unexpected output format from runpodctl")

        data_line = lines[1]

        # Split by whitespace, but need to handle the PORTS column specially
        # since it contains spaces
        parts = data_line.split()

        pod_id = parts[0]
        pod_name = parts[1]

        # Find STATUS (looking for RUNNING, STOPPED, etc.)
        status_idx = None
        for i, part in enumerate(parts):
            if part in ['RUNNING', 'STOPPED', 'STARTING', 'STOPPING', 'EXITED']:
                status_idx = i
                break

        if status_idx is None:
            raise Exception("Could not find pod status")

        status = parts[status_idx]

        # Extract ports section (find first IP address pattern)
        ip_pattern = re.search(r'\d+\.\d+\.\d+\.\d+:\d+->', data_line)
        if not ip_pattern:
            # No public ports found
            return PodInfo(
                id=pod_id,
                name=pod_name,
                status=status,
                public_ip=None,
                port_mappings=[]
            )

        ports_section = data_line[ip_pattern.start():]

        # Parse port mappings
        port_mappings = self._parse_ports(ports_section)

        # Extract public IP from first mapping
        public_ip = port_mappings[0].public_ip if port_mappings else None

        return PodInfo(
            id=pod_id,
            name=pod_name,
            status=status,
            public_ip=public_ip,
            port_mappings=port_mappings
        )
    
    def _parse_ports(self, ports_section: str) -> List[PortMapping]:
        """Parse port mappings from the PORTS section"""
        # Pattern: IP:PORT->CONTAINER_PORT (visibility,protocol)
        pattern = r'(\d+\.\d+\.\d+\.\d+):(\d+)->(\d+)\s*\((\w+),(\w+)\)'

        mappings = []
        for match in re.finditer(pattern, ports_section):
            ip, public_port, container_port, visibility, protocol = match.groups()

            # Only include public ports
            if visibility == 'pub':
                mappings.append(PortMapping(
                    public_ip=ip,
                    public_port=int(public_port),
                    container_port=int(container_port),
                    protocol=protocol,
                    visibility=visibility
                ))

        return mappings
    
    def start_pod(self, wait_for_ready: bool = True, timeout: int = 300) -> PodInfo:
        """
        Start the pod and optionally wait for it to be ready

        Args:
            wait_for_ready: If True, wait for pod to reach RUNNING status
            timeout: Maximum seconds to wait for pod to start

        Returns:
            PodInfo with updated pod information
        """
        debug_log("INFO", f"Starting pod {self.pod_id}...")
        print(f"Starting pod {self.pod_id}...")
        self._run_command(['runpodctl', 'start', 'pod', self.pod_id])

        if wait_for_ready:
            debug_log("DEBUG", f"Waiting for pod to reach RUNNING status (timeout: {timeout}s)")
            return self._wait_for_status('RUNNING', timeout)

        return self.get_pod_info()

    def stop_pod(self, wait_for_stopped: bool = True, timeout: int = 120) -> PodInfo:
        """
        Stop the pod and optionally wait for it to be stopped

        Args:
            wait_for_stopped: If True, wait for pod to reach STOPPED status
            timeout: Maximum seconds to wait for pod to stop

        Returns:
            PodInfo with updated pod information
        """
        debug_log("INFO", f"Stopping pod {self.pod_id}...")
        print(f"Stopping pod {self.pod_id}...")
        self._run_command(['runpodctl', 'stop', 'pod', self.pod_id])

        if wait_for_stopped:
            debug_log("DEBUG", f"Waiting for pod to reach EXITED status (timeout: {timeout}s)")
            return self._wait_for_status('EXITED', timeout)

        return self.get_pod_info()

    def _wait_for_status(self, target_status: str, timeout: int) -> PodInfo:
        """Wait for pod to reach target status"""
        start_time = time.time()
        debug_log("DEBUG", f"Waiting for status: {target_status}")

        while True:
            if time.time() - start_time > timeout:
                debug_log("ERROR", f"Timeout: Pod did not reach {target_status} status within {timeout} seconds")
                raise TimeoutError(f"Pod did not reach {target_status} status within {timeout} seconds")

            pod_info = self.get_pod_info()
            debug_log("DEBUG", f"Current status: {pod_info.status}")
            print(f"Current status: {pod_info.status}")

            if pod_info.status == target_status:
                debug_log("INFO", f"Pod reached target status: {target_status}")
                return pod_info
            
            time.sleep(5)
    
    def get_port_by_container_port(self, container_port: int) -> Optional[PortMapping]:
        """Get port mapping for a specific container port"""
        pod_info = self.get_pod_info()

        for mapping in pod_info.port_mappings:
            if mapping.container_port == container_port:
                return mapping

        return None

    def remove_pod(self) -> bool:
        """
        Remove/terminate the pod permanently

        Returns:
            True if pod was removed successfully
        """
        print(f"Removing pod {self.pod_id}...")
        self._run_command(['runpodctl', 'remove', 'pod', self.pod_id])
        return True


class CloudManager:
    """Manages cloud resources and pod creation"""

    # Path to RunPod API key file
    API_KEY_FILE = "/root/mgmt/runpod/.apiKey"
    RUNPOD_API_URL = "https://api.runpod.io/graphql"

    @staticmethod
    def _run_command(command: List[str]) -> str:
        """Execute a runpodctl command and return output"""
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise Exception(f"Command failed: {e.stderr}")

    def _get_api_key(self) -> str:
        """Read the RunPod API key from file"""
        try:
            with open(self.API_KEY_FILE, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            raise Exception(f"API key file not found: {self.API_KEY_FILE}")

    def _graphql_query(self, query: str, variables: dict = None, operation_name: str = None, max_retries: int = 3) -> dict:
        """Execute a GraphQL query against the RunPod API with browser-like headers and retry logic"""
        import urllib.request
        import urllib.error
        import time

        api_key = self._get_api_key()

        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name

        debug_log("DEBUG", f"GraphQL request - operation: {operation_name or 'unnamed'}")
        debug_log("DEBUG", f"GraphQL variables: {json.dumps(variables) if variables else 'none'}")

        data = json.dumps(payload).encode('utf-8')

        # Build URL with operation name in query string (like the browser does)
        url = self.RUNPOD_API_URL
        if operation_name:
            url = f"{self.RUNPOD_API_URL}?operation={operation_name}"

        # Browser-like headers to avoid Cloudflare bot detection (error 1010)
        # Note: We omit Accept-Encoding because urllib doesn't auto-decompress gzip/br
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://console.runpod.io',
            'Referer': 'https://console.runpod.io/',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }

        last_error = None
        last_error_body = None
        for attempt in range(max_retries):
            try:
                # Create a fresh request for each attempt
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    debug_log("DEBUG", f"GraphQL response: {json.dumps(result)[:1000]}")
                    return result
            except urllib.error.HTTPError as e:
                error_body = e.read().decode('utf-8')
                last_error = e
                last_error_body = error_body

                # Check if it's a Cloudflare rate limit / bot detection error (403 with code 1010)
                if e.code == 403 and '1010' in error_body:
                    if attempt < max_retries - 1:
                        delay = (attempt + 1) * 2  # Exponential backoff: 2s, 4s, 6s
                        debug_log("DEBUG", f"Cloudflare rate limit detected, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(delay)
                        continue

                debug_log("ERROR", f"GraphQL API error: {e.code} - {error_body}")
                raise Exception(f"GraphQL API error: {e.code} - {error_body}")

        # If we exhausted all retries
        if last_error:
            debug_log("ERROR", f"GraphQL API error: {last_error.code} - Max retries exceeded. Last error: {last_error_body}")
            raise Exception(f"GraphQL API error: {last_error.code} - Max retries exceeded")

    def get_network_volume_datacenter(self, network_volume_id: str) -> str:
        """
        Get the datacenter ID for a network volume.

        Args:
            network_volume_id: The network volume ID

        Returns:
            The datacenter ID where the network volume is located
        """
        query = '{ myself { networkVolumes { id name dataCenterId } } }'
        result = self._graphql_query(query)

        volumes = result.get('data', {}).get('myself', {}).get('networkVolumes', [])

        for volume in volumes:
            if volume.get('id') == network_volume_id:
                datacenter_id = volume.get('dataCenterId')
                if datacenter_id:
                    return datacenter_id
                raise Exception(f"Network volume {network_volume_id} has no datacenter ID")

        raise Exception(f"Network volume {network_volume_id} not found")

    def get_network_volume_info(self, network_volume_id: str) -> dict:
        """
        Get detailed information about a network volume including its datacenter.

        Args:
            network_volume_id: The network volume ID

        Returns:
            Dict with volume info including id, name, size, and datacenter
        """
        query = '''
        query getMyVolumes {
            myself {
                networkVolumes {
                    id
                    size
                    name
                    dataCenter {
                        id
                        name
                        globalNetwork
                    }
                }
            }
        }
        '''
        result = self._graphql_query(query, operation_name="getMyVolumes")
        volumes = result.get('data', {}).get('myself', {}).get('networkVolumes', [])

        for volume in volumes:
            if volume.get('id') == network_volume_id:
                return {
                    'id': volume.get('id'),
                    'name': volume.get('name'),
                    'size': volume.get('size'),
                    'datacenter_id': volume.get('dataCenter', {}).get('id'),
                    'datacenter_name': volume.get('dataCenter', {}).get('name'),
                    'global_network': volume.get('dataCenter', {}).get('globalNetwork', False)
                }

        raise Exception(f"Network volume {network_volume_id} not found")

    def get_pod_config(self, pod_id: str) -> PodConfig:
        """
        Get the configuration of an existing pod for cloning/recreation.

        Args:
            pod_id: The pod ID to get configuration from

        Returns:
            PodConfig object with all pod settings
        """
        query = '''
        query podDetailedInspector($input: PodFilter) {
            pod(input: $input) {
                id
                name
                imageName
                gpuCount
                vcpuCount
                memoryInGb
                containerDiskInGb
                volumeInGb
                volumeMountPath
                templateId
                ports
                env
                dockerArgs
                costPerHr
                networkVolume {
                    id
                    name
                    size
                }
                machine {
                    gpuTypeId
                    dataCenterId
                    gpuType {
                        memoryInGb
                    }
                }
            }
        }
        '''
        variables = {"input": {"podId": pod_id}}
        result = self._graphql_query(query, variables, "podDetailedInspector")

        pod = result.get('data', {}).get('pod')
        if not pod:
            raise Exception(f"Pod {pod_id} not found")

        machine = pod.get('machine') or {}
        network_volume = pod.get('networkVolume')

        return PodConfig(
            id=pod.get('id'),
            name=pod.get('name'),
            image_name=pod.get('imageName'),
            gpu_type_id=machine.get('gpuTypeId'),
            gpu_count=pod.get('gpuCount', 1),
            vcpu_count=pod.get('vcpuCount', 0),
            memory_in_gb=pod.get('memoryInGb', 0),
            container_disk_in_gb=pod.get('containerDiskInGb', 10),
            volume_in_gb=pod.get('volumeInGb', 0),
            volume_mount_path=pod.get('volumeMountPath', '/workspace'),
            template_id=pod.get('templateId'),
            network_volume_id=network_volume.get('id') if network_volume else None,
            datacenter_id=machine.get('dataCenterId'),
            ports=pod.get('ports', ''),
            env=pod.get('env', []),
            docker_args=pod.get('dockerArgs'),
            cost_per_hr=pod.get('costPerHr')
        )

    def get_available_gpus_for_datacenter(self, datacenter_id: str, min_mem: int = 8, min_vcpu: int = 2) -> List[CloudGPU]:
        """
        Get list of available GPU types for a specific datacenter using GraphQL API.

        Args:
            datacenter_id: The datacenter ID to query
            min_mem: Minimum memory in GB for the query filter
            min_vcpu: Minimum vCPU count for the query filter

        Returns:
            List of CloudGPU objects with availability info for the datacenter
        """
        # Query all GPU types first to get the list of IDs
        all_gpus_query = '''
        {
            gpuTypes {
                id
                displayName
                memoryInGb
            }
        }
        '''
        all_result = self._graphql_query(all_gpus_query)
        all_gpu_types = all_result.get('data', {}).get('gpuTypes', [])

        gpus = []

        # Query each GPU type with datacenter-specific availability
        for gpu_info in all_gpu_types:
            gpu_id = gpu_info.get('id')
            if not gpu_id:
                continue

            query = '''
            query SecureGpuTypes($lowestPriceInput: GpuLowestPriceInput, $gpuTypesInput: GpuTypeFilter) {
                gpuTypes(input: $gpuTypesInput) {
                    lowestPrice(input: $lowestPriceInput) {
                        minimumBidPrice
                        uninterruptablePrice
                        minVcpu
                        minMemory
                        stockStatus
                        maxUnreservedGpuCount
                        availableGpuCounts
                    }
                    id
                    displayName
                    memoryInGb
                }
            }
            '''

            variables = {
                "gpuTypesInput": {"id": gpu_id},
                "lowestPriceInput": {
                    "gpuCount": 1,
                    "minDisk": 0,
                    "minMemoryInGb": min_mem,
                    "minVcpuCount": min_vcpu,
                    "secureCloud": True,
                    "dataCenterId": datacenter_id,
                    "globalNetwork": True
                }
            }

            try:
                result = self._graphql_query(query, variables, "SecureGpuTypes")
                gpu_types = result.get('data', {}).get('gpuTypes', [])

                for gpu in gpu_types:
                    lowest_price = gpu.get('lowestPrice') or {}
                    ondemand_price = lowest_price.get('uninterruptablePrice')
                    spot_price = lowest_price.get('minimumBidPrice')
                    stock_status = lowest_price.get('stockStatus')
                    available_counts = lowest_price.get('availableGpuCounts') or []
                    min_vcpu_result = lowest_price.get('minVcpu') or 0

                    # Skip GPUs with no availability in this datacenter
                    if not available_counts or len(available_counts) == 0:
                        continue

                    # Skip if no on-demand price available
                    if ondemand_price is None:
                        continue

                    gpu_id = gpu.get('id')
                    display_name = gpu.get('displayName', gpu_id)
                    gpus.append(CloudGPU(
                        gpu_type=gpu_id,  # Use raw ID for API calls
                        display_name=display_name,
                        mem_gb=gpu.get('memoryInGb', 0),
                        vcpu=min_vcpu_result,
                        spot_price=spot_price,
                        ondemand_price=ondemand_price,
                        stock_status=stock_status
                    ))
            except Exception as e:
                print(f"Warning: Failed to query GPU {gpu_id}: {e}", file=sys.stderr)
                continue

            # Small delay between GPU queries to avoid Cloudflare rate limiting
            import time
            time.sleep(0.3)

        return gpus

    def get_available_gpus(self) -> List[CloudGPU]:
        """Get list of available GPU types from the cloud (global, not datacenter-specific)"""
        output = self._run_command(['runpodctl', 'get', 'cloud'])
        return self._parse_cloud_output(output)

    def _parse_cloud_output(self, output: str) -> List[CloudGPU]:
        """Parse runpodctl get cloud output to extract GPU information"""
        lines = output.strip().split('\n')
        gpus = []

        # Skip header line
        if len(lines) < 2:
            return gpus

        current_gpu_name = ""

        for line in lines[1:]:
            # Skip empty lines
            if not line.strip():
                continue

            # Check if line starts with a GPU type (starts with "1x" or similar)
            if line.strip().startswith(('1x', '2x', '4x', '8x')):
                # This is a new GPU entry or continuation
                # Try to parse as a complete entry first
                gpu = self._parse_gpu_line(line)
                if gpu:
                    gpus.append(gpu)
                    current_gpu_name = ""
                else:
                    # Incomplete line, store the name for continuation
                    current_gpu_name = line.strip()
            elif current_gpu_name:
                # This might be a continuation of a multi-line GPU name
                # Combine with previous line and try to parse
                combined = current_gpu_name + " " + line.strip()
                gpu = self._parse_gpu_line_combined(combined, line)
                if gpu:
                    gpus.append(gpu)
                current_gpu_name = ""

        return gpus

    def _parse_gpu_line(self, line: str) -> Optional[CloudGPU]:
        """Parse a single line GPU entry"""
        # Pattern for lines with all data: GPU_TYPE MEM VCPU SPOT ONDEMAND
        # The line is tab-separated with values
        parts = line.split('\t')
        if len(parts) < 5:
            # Try splitting by multiple spaces
            parts = re.split(r'\s{2,}', line.strip())

        if len(parts) < 5:
            return None

        try:
            gpu_type = parts[0].strip()
            mem_gb = int(parts[1].strip())
            vcpu = int(parts[2].strip())

            # Parse spot price
            spot_str = parts[3].strip()
            spot_price = None if spot_str == 'Reserved' else float(spot_str)

            # Parse ondemand price
            ondemand_str = parts[4].strip()
            ondemand_price = None if ondemand_str == 'Reserved' else float(ondemand_str)

            return CloudGPU(
                gpu_type=gpu_type,
                display_name=gpu_type,  # Use same as gpu_type for runpodctl output
                mem_gb=mem_gb,
                vcpu=vcpu,
                spot_price=spot_price,
                ondemand_price=ondemand_price
            )
        except (ValueError, IndexError):
            return None

    def _parse_gpu_line_combined(self, gpu_name: str, data_line: str) -> Optional[CloudGPU]:
        """Parse a GPU entry that spans multiple lines"""
        # The data_line contains: (continuation of name) MEM VCPU SPOT ONDEMAND
        parts = re.split(r'\s{2,}', data_line.strip())

        # Find where the numbers start
        data_parts = []
        name_parts = []
        for part in parts:
            if part and (part.isdigit() or part == 'Reserved' or re.match(r'^\d+\.\d+$', part)):
                data_parts.append(part)
            elif not data_parts:  # Still in name portion
                name_parts.append(part)

        if len(data_parts) < 4:
            return None

        try:
            # Combine name parts with the first part of gpu_name
            full_name = gpu_name.split()[0:]
            if name_parts:
                full_name = gpu_name.rstrip()
                for np in name_parts:
                    if np not in full_name:
                        full_name += " " + np
            else:
                full_name = gpu_name

            # Clean up the name
            full_name = ' '.join(full_name.split())

            mem_gb = int(data_parts[0])
            vcpu = int(data_parts[1])

            spot_str = data_parts[2]
            spot_price = None if spot_str == 'Reserved' else float(spot_str)

            ondemand_str = data_parts[3]
            ondemand_price = None if ondemand_str == 'Reserved' else float(ondemand_str)

            return CloudGPU(
                gpu_type=full_name,
                display_name=full_name,  # Use same as gpu_type for runpodctl output
                mem_gb=mem_gb,
                vcpu=vcpu,
                spot_price=spot_price,
                ondemand_price=ondemand_price
            )
        except (ValueError, IndexError):
            return None

    def find_best_gpu(self, min_mem: int, min_vcpu: int, max_price: float) -> Optional[CloudGPU]:
        """
        Find the best available GPU that meets the requirements.

        Args:
            min_mem: Minimum GPU memory in GB
            min_vcpu: Minimum vCPU count
            max_price: Maximum ondemand price per hour

        Returns:
            The best matching GPU or None if no GPU meets requirements
        """
        matching = self.find_matching_gpus(min_mem, min_vcpu, max_price)
        return matching[0] if matching else None

    def find_matching_gpus(
        self,
        min_mem: int,
        min_vcpu: int,
        max_price: float,
        datacenter_id: Optional[str] = None
    ) -> List[CloudGPU]:
        """
        Find all GPUs that meet the requirements, sorted by preference.

        Args:
            min_mem: Minimum GPU memory in GB
            min_vcpu: Minimum vCPU count (ignored if datacenter_id is provided)
            max_price: Maximum ondemand price per hour
            datacenter_id: Optional datacenter ID for location-specific availability

        Returns:
            List of matching GPUs sorted by availability and price
        """
        if datacenter_id:
            # Use GraphQL API for datacenter-specific availability
            print(f"Querying GPU availability for datacenter {datacenter_id}...", file=sys.stderr)
            gpus = self.get_available_gpus_for_datacenter(datacenter_id, min_mem, min_vcpu)
            # Filter by GPU memory and price (API min_mem is for host memory, not GPU memory)
            matching = [
                g for g in gpus
                if g.mem_gb >= min_mem
                and g.ondemand_price is not None
                and g.ondemand_price <= max_price
            ]
        else:
            # Fall back to runpodctl for global availability
            gpus = self.get_available_gpus()
            matching = [g for g in gpus if g.matches_requirements(min_mem, min_vcpu, max_price)]

        # Sort by score (stock availability first, then price, then specs)
        matching.sort(key=lambda g: g.score())

        # Log available GPUs for debugging
        if matching:
            print(f"Found {len(matching)} matching GPUs:", file=sys.stderr)
            for g in matching:
                stock = f"[{g.stock_status}]" if g.stock_status else ""
                display = f" ({g.display_name})" if g.display_name != g.gpu_type else ""
                print(f"  - {g.gpu_type}{display}: {g.mem_gb}GB, ${g.ondemand_price}/hr {stock}", file=sys.stderr)
        else:
            print("No matching GPUs found", file=sys.stderr)

        return matching

    def find_similar_gpus(
        self,
        target_gpu_type: str,
        target_mem_gb: int,
        datacenter_id: Optional[str] = None,
        max_price: float = float('inf'),
        tolerance_percent: int = 20
    ) -> List[CloudGPU]:
        """
        Find GPUs similar to a target GPU, sorted by how close they are in specs.

        This is useful when the original GPU is unavailable and we need a replacement.

        Args:
            target_gpu_type: The original GPU type ID
            target_mem_gb: The original GPU memory in GB
            datacenter_id: Optional datacenter ID for location-specific availability
            max_price: Maximum ondemand price per hour
            tolerance_percent: Percentage tolerance for memory difference (default 20%)

        Returns:
            List of GPUs sorted by similarity (closest match first)
        """
        # Calculate acceptable memory range
        min_mem = int(target_mem_gb * (1 - tolerance_percent / 100))

        # Get all available GPUs
        if datacenter_id:
            gpus = self.get_available_gpus_for_datacenter(datacenter_id, min_mem=8, min_vcpu=2)
        else:
            gpus = self.get_available_gpus()

        # Filter by minimum memory and max price
        matching = [
            g for g in gpus
            if g.mem_gb >= min_mem
            and g.ondemand_price is not None
            and g.ondemand_price <= max_price
        ]

        # Calculate similarity score for each GPU
        def similarity_score(gpu: CloudGPU) -> tuple:
            """
            Calculate similarity score (lower is better/more similar).
            Prioritizes:
            1. Exact match of GPU type
            2. Stock availability
            3. Closest memory to target
            4. Lowest price
            """
            # Exact GPU type match gets priority
            is_exact_match = 0 if gpu.gpu_type.lower() == target_gpu_type.lower() else 1

            # Stock availability
            stock = gpu.stock_score()

            # Memory difference (absolute)
            mem_diff = abs(gpu.mem_gb - target_mem_gb)

            # Price
            price = gpu.ondemand_price or float('inf')

            return (is_exact_match, stock, mem_diff, price)

        # Sort by similarity
        matching.sort(key=similarity_score)

        # Log available similar GPUs
        if matching:
            print(f"Found {len(matching)} similar GPUs to {target_gpu_type} ({target_mem_gb}GB):", file=sys.stderr)
            for g in matching[:5]:  # Show top 5
                stock = f"[{g.stock_status}]" if g.stock_status else ""
                display = f" ({g.display_name})" if g.display_name != g.gpu_type else ""
                exact = " [EXACT MATCH]" if g.gpu_type.lower() == target_gpu_type.lower() else ""
                print(f"  - {g.gpu_type}{display}: {g.mem_gb}GB, ${g.ondemand_price}/hr {stock}{exact}", file=sys.stderr)
        else:
            print(f"No similar GPUs found for {target_gpu_type}", file=sys.stderr)

        return matching

    def create_pod(
        self,
        gpu_type: str,
        template_id: str,
        network_volume_id: str,
        image_name: str,
        pod_name: str = "failover-pod",
        gpu_count: int = 1
    ) -> str:
        """
        Create a new pod with the specified configuration.

        Args:
            gpu_type: GPU type string (e.g., 'NVIDIA GeForce RTX 3090')
            template_id: Template ID to use
            network_volume_id: Network volume ID to attach
            image_name: Container image name (required by runpodctl)
            pod_name: Name for the pod
            gpu_count: Number of GPUs

        Returns:
            The new pod ID
        """
        # Extract just the GPU model name without the count prefix
        gpu_name = gpu_type
        if gpu_type.startswith(('1x ', '2x ', '4x ', '8x ')):
            gpu_name = gpu_type[3:]

#        # Get the datacenter ID from the network volume
#        # N.B.: adding a network volume seems to always lead to a creation failure - REMOVING TEMPORARY
#        print(f"Looking up datacenter for network volume {network_volume_id}...", file=sys.stderr)
#        datacenter_id = self.get_network_volume_datacenter(network_volume_id)
#        print(f"Network volume is in datacenter: {datacenter_id}", file=sys.stderr)

        cmd = [
            'runpodctl', 'create', 'pod',
            '--gpuType', gpu_name,
            '--gpuCount', str(gpu_count),
            '--templateId', template_id,
#            '--networkVolumeId', network_volume_id,
#            '--dataCenterId', datacenter_id,
            '--imageName', image_name,
            '--name', pod_name
        ]

        print(f"Creating pod with command: {' '.join(cmd)}", file=sys.stderr)
        output = self._run_command(cmd)

        # Parse the output to get the pod ID
        # Expected output format varies, try to extract pod ID
        pod_id = self._extract_pod_id(output)

        if not pod_id:
            raise Exception(f"Could not extract pod ID from output: {output}")

        return pod_id

    def create_pod_graphql(
        self,
        gpu_type_id: str,
        template_id: str,
        network_volume_id: Optional[str] = None,
        datacenter_id: Optional[str] = None,
        pod_name: str = "failover-pod",
        gpu_count: int = 1,
        container_disk_in_gb: int = 10,
        volume_in_gb: int = 0,
        min_memory_in_gb: int = 251,
        min_vcpu_count: int = 24,
        cloud_type: str = "SECURE"
    ) -> str:
        """
        Create a new pod using the GraphQL API (podFindAndDeployOnDemand).

        This method properly supports network volumes and datacenter selection.

        Args:
            gpu_type_id: GPU type ID (e.g., 'NVIDIA H200')
            template_id: Template ID to use
            network_volume_id: Optional network volume ID to attach
            datacenter_id: Optional datacenter ID (required if using network volume)
            pod_name: Name for the pod
            gpu_count: Number of GPUs
            container_disk_in_gb: Container disk size in GB
            volume_in_gb: Volume size in GB (0 if using network volume)
            min_memory_in_gb: Minimum host memory in GB
            min_vcpu_count: Minimum vCPU count
            ports: Port configuration string
            cloud_type: Cloud type ('SECURE' or 'COMMUNITY')

        Returns:
            The new pod ID
        """
        # Clean GPU type ID - remove "1x ", "2x " etc. prefixes from runpodctl format
        if re.match(r'^\d+x\s+', gpu_type_id):
            gpu_type_id = re.sub(r'^\d+x\s+', '', gpu_type_id)

        # If network volume provided, get its datacenter
        if network_volume_id and not datacenter_id:
            volume_info = self.get_network_volume_info(network_volume_id)
            datacenter_id = volume_info.get('datacenter_id')
            print(f"Network volume is in datacenter: {datacenter_id}", file=sys.stderr)

        # Get GPU price for deployCost
        deploy_cost = None
        try:
            query = '''
            query SecureGpuTypes($lowestPriceInput: GpuLowestPriceInput, $gpuTypesInput: GpuTypeFilter) {
                gpuTypes(input: $gpuTypesInput) {
                    lowestPrice(input: $lowestPriceInput) {
                        uninterruptablePrice
                    }
                }
            }
            '''
            variables = {
                "gpuTypesInput": {"id": gpu_type_id},
                "lowestPriceInput": {
                    "gpuCount": gpu_count,
                    "secureCloud": cloud_type == "SECURE",
                    "dataCenterId": datacenter_id,
                    "globalNetwork": True
                }
            }
            result = self._graphql_query(query, variables, "SecureGpuTypes")
            gpu_types = result.get('data', {}).get('gpuTypes', [])
            if gpu_types:
                deploy_cost = gpu_types[0].get('lowestPrice', {}).get('uninterruptablePrice')
        except Exception as e:
            print(f"Warning: Could not get deploy cost: {e}", file=sys.stderr)

        # Build the mutation
        mutation = '''
        mutation Mutation($input: PodFindAndDeployOnDemandInput) {
            podFindAndDeployOnDemand(input: $input) {
                id
                imageName
                env
                machineId
                machine {
                    podHostId
                }
            }
        }
        '''

        # Build input variables
        # Note: We explicitly set startJupyter and startSsh to False to ensure
        # the template's port configuration is used instead of RunPod defaults
        input_vars = {
            "cloudType": cloud_type,
            "containerDiskInGb": container_disk_in_gb,
            "volumeInGb": volume_in_gb,
            "gpuCount": gpu_count,
            "gpuTypeId": gpu_type_id,
            "minMemoryInGb": min_memory_in_gb,
            "minVcpuCount": min_vcpu_count,
            "templateId": template_id,
            "name": pod_name,
            "startJupyter": False,
            "startSsh": False
        }

        if deploy_cost is not None:
            input_vars["deployCost"] = deploy_cost

        if datacenter_id:
            input_vars["dataCenterId"] = datacenter_id

        if network_volume_id:
            input_vars["networkVolumeId"] = network_volume_id
            input_vars["volumeKey"] = None  # Required when using network volume

        variables = {"input": input_vars}

        debug_log("INFO", f"Creating pod via GraphQL with GPU {gpu_type_id}")
        debug_log("INFO", f"Template ID: {template_id}")
        debug_log("DEBUG", f"Pod creation input_vars: {json.dumps(input_vars)}")
        print(f"Creating pod via GraphQL with GPU {gpu_type_id}...", file=sys.stderr)
        result = self._graphql_query(mutation, variables, "Mutation")

        # Check for errors
        if 'errors' in result:
            error_msg = result['errors'][0].get('message', 'Unknown error')
            debug_log("ERROR", f"GraphQL error creating pod: {error_msg}")
            raise Exception(f"GraphQL error: {error_msg}")

        pod_data = result.get('data', {}).get('podFindAndDeployOnDemand')
        if not pod_data:
            debug_log("ERROR", f"No pod returned from creation. Response: {result}")
            raise Exception(f"No pod returned from creation. Response: {result}")

        pod_id = pod_data.get('id')
        if not pod_id:
            debug_log("ERROR", f"No pod ID in response: {result}")
            raise Exception(f"No pod ID in response: {result}")

        debug_log("INFO", f"Pod created successfully: {pod_id}")
        print(f"Pod created successfully: {pod_id}", file=sys.stderr)
        return pod_id

    def terminate_pod(self, pod_id: str) -> bool:
        """
        Terminate/remove a pod using GraphQL API.

        Args:
            pod_id: The pod ID to terminate

        Returns:
            True if successful
        """
        mutation = '''
        mutation terminatePod($input: PodTerminateInput!) {
            podTerminate(input: $input)
        }
        '''
        variables = {"input": {"podId": pod_id}}

        print(f"Terminating pod {pod_id} via GraphQL...", file=sys.stderr)
        result = self._graphql_query(mutation, variables, "terminatePod")

        if 'errors' in result:
            error_msg = result['errors'][0].get('message', 'Unknown error')
            raise Exception(f"GraphQL error: {error_msg}")

        return True

    def _extract_pod_id(self, output: str) -> Optional[str]:
        """Extract pod ID from create pod output"""
        # Look for pod ID pattern (alphanumeric string)
        # Common format: 'pod "pod_id" created' or 'pod pod_id created' or just the ID

        # Try common patterns
        patterns = [
            r'pod\s+"([a-zA-Z0-9]+)"\s+created',  # pod "id" created (quoted)
            r'pod\s+([a-zA-Z0-9]+)\s+created',     # pod id created (unquoted)
            r'id:\s*"?([a-zA-Z0-9]+)"?',           # id: "id" or id: id
            r'^([a-zA-Z0-9]{10,})$',               # Just an ID on its own line
        ]

        for pattern in patterns:
            match = re.search(pattern, output, re.MULTILINE | re.IGNORECASE)
            if match:
                return match.group(1)

        # If no pattern matched, try to find any alphanumeric string that looks like an ID
        lines = output.strip().split('\n')
        for line in lines:
            line = line.strip()
            # Pod IDs are typically 10+ alphanumeric characters
            if re.match(r'^[a-zA-Z0-9]{10,}$', line):
                return line

        return None

    def create_pod_with_retry(
        self,
        min_mem: int,
        min_vcpu: int,
        max_price: float,
        template_id: str,
        network_volume_id: str,
        image_name: str,
        pod_name: str = "failover-pod",
        preferred_gpu: Optional[str] = None
    ) -> Tuple[str, CloudGPU]:
        """
        Create a new pod, trying multiple GPU types if needed.

        Args:
            min_mem: Minimum GPU memory in GB
            min_vcpu: Minimum vCPU count
            max_price: Maximum ondemand price per hour
            template_id: Template ID to use
            network_volume_id: Network volume ID to attach
            image_name: Container image name
            pod_name: Name for the pod
            preferred_gpu: Preferred GPU type (will be tried first if available)

        Returns:
            Tuple of (pod_id, gpu) for the successfully created pod

        Raises:
            Exception if no GPU could be used to create a pod
        """

#        # First, get the datacenter ID from the network volume
#        # N.B.: adding a network volume seems to always lead to a creation failure - REMOVING TEMPORARY
#        print(f"Looking up datacenter for network volume {network_volume_id}...", file=sys.stderr)
#        datacenter_id = self.get_network_volume_datacenter(network_volume_id)
#        print(f"Network volume is in datacenter: {datacenter_id}", file=sys.stderr)

        # Find matching GPUs with datacenter-specific availability
        #matching_gpus = self.find_matching_gpus(min_mem, min_vcpu, max_price, datacenter_id)
        matching_gpus = self.find_matching_gpus(min_mem, min_vcpu, max_price)

        if not matching_gpus:
            raise Exception(f"No GPU found matching criteria in datacenter")

        # If a preferred GPU is specified, move it to the front of the list
        if preferred_gpu:
            preferred_gpu_lower = preferred_gpu.lower()
            # Find GPUs that match the preferred type (partial match)
            preferred_matches = []
            other_gpus = []
            for gpu in matching_gpus:
                # Check if preferred GPU name is contained in the gpu_type or display_name
                if (preferred_gpu_lower in gpu.gpu_type.lower() or 
                    preferred_gpu_lower in gpu.display_name.lower()):
                    preferred_matches.append(gpu)
                else:
                    other_gpus.append(gpu)
            
            if preferred_matches:
                print(f"Preferred GPU '{preferred_gpu}' found in available list, prioritizing it", file=sys.stderr)
                matching_gpus = preferred_matches + other_gpus
            else:
                print(f"Preferred GPU '{preferred_gpu}' not available, using best alternative", file=sys.stderr)

        errors = []
        for gpu in matching_gpus:
            stock_info = f" [{gpu.stock_status}]" if gpu.stock_status else ""
            display = f" ({gpu.display_name})" if gpu.display_name != gpu.gpu_type else ""
            print(f"Trying GPU: {gpu.gpu_type}{display} (${gpu.ondemand_price}/hr){stock_info}...", file=sys.stderr)
            try:
                pod_id = self.create_pod(
                    gpu_type=gpu.gpu_type,
                    template_id=template_id,
                    network_volume_id=network_volume_id,
                    image_name=image_name,
                    pod_name=pod_name
                )
                print(f"Successfully created pod with {gpu.gpu_type}{display}", file=sys.stderr)
                return pod_id, gpu
            except Exception as e:
                error_msg = str(e)
                print(f"Failed with {gpu.gpu_type}{display}: {error_msg}", file=sys.stderr)
                errors.append(f"{gpu.gpu_type}{display}: {error_msg}")
                # Check if the error indicates no availability - try next GPU
                if "no longer any instances" in error_msg.lower() or "not available" in error_msg.lower():
                    continue
                # For other errors, also try next GPU
                continue

        # All GPUs failed
        raise Exception(f"Failed to create pod with any available GPU. Errors:\n" + "\n".join(errors))


def main():
    """Main entry point with CLI argument parsing"""
    import argparse

    # Ensure PATH includes common binary locations (important for crontab)
    path_additions = ['/usr/local/bin', '/usr/bin', '/bin', '/usr/local/sbin', '/usr/sbin', '/sbin']
    current_path = os.environ.get('PATH', '')
    for p in path_additions:
        if p not in current_path:
            current_path = f"{p}:{current_path}" if current_path else p
    os.environ['PATH'] = current_path

    # Initialize logging early (before argument parsing) to catch any startup issues
    # This uses config.sh values if environment variables are not set
    setup_debug_logging()
    debug_log("INFO", f"Script started. PATH={os.environ.get('PATH', 'NOT SET')}")
    debug_log("DEBUG", f"Arguments: {sys.argv}")

    # Check if first argument is a known subcommand
    subcommands = ['cloud', 'failover', 'create', 'restart-or-recreate', 'clone-pod']
    use_subcommand = len(sys.argv) > 1 and sys.argv[1] in subcommands

    if use_subcommand:
        # Use subcommand-based parsing
        parser = argparse.ArgumentParser(description='Manage RunPod pods and cloud resources')
        parser.add_argument('--debug', action='store_true',
                           help='Enable debug logging (also enabled via DEBUGGING=true env var)')
        subparsers = parser.add_subparsers(dest='command', help='Commands')

        # Cloud commands
        cloud_parser = subparsers.add_parser('cloud', help='Cloud resource operations')
        cloud_subparsers = cloud_parser.add_subparsers(dest='cloud_action')

        # cloud list - list available GPUs
        cloud_list = cloud_subparsers.add_parser('list', help='List available GPU types')
        cloud_list.add_argument('--json', action='store_true', help='Output in JSON format')
        cloud_list.add_argument('--min-mem', type=int, default=0, help='Minimum GPU memory in GB')
        cloud_list.add_argument('--min-vcpu', type=int, default=0, help='Minimum vCPU count')
        cloud_list.add_argument('--max-price', type=float, default=float('inf'), help='Maximum ondemand $/HR')

        # cloud find-best - find best GPU matching criteria
        cloud_best = cloud_subparsers.add_parser('find-best', help='Find best GPU matching criteria')
        cloud_best.add_argument('--min-mem', type=int, required=True, help='Minimum GPU memory in GB')
        cloud_best.add_argument('--min-vcpu', type=int, required=True, help='Minimum vCPU count')
        cloud_best.add_argument('--max-price', type=float, required=True, help='Maximum ondemand $/HR')
        cloud_best.add_argument('--json', action='store_true', help='Output in JSON format')

        # cloud create-pod - create a new pod
        cloud_create = cloud_subparsers.add_parser('create-pod', help='Create a new pod')
        cloud_create.add_argument('--gpu-type', required=True, help='GPU type')
        cloud_create.add_argument('--template-id', required=True, help='Template ID')
        cloud_create.add_argument('--network-volume-id', required=True, help='Network volume ID')
        cloud_create.add_argument('--image-name', required=True, help='Container image name')
        cloud_create.add_argument('--name', default='failover-pod', help='Pod name')
        cloud_create.add_argument('--gpu-count', type=int, default=1, help='Number of GPUs')
        cloud_create.add_argument('--json', action='store_true', help='Output in JSON format')

        # Failover command - complete failover workflow
        failover_parser = subparsers.add_parser('failover', help='Execute failover to new GPU')
        failover_parser.add_argument('--old-pod-id', help='Old pod ID to replace (optional)')
        failover_parser.add_argument('--min-mem', type=int, required=True, help='Minimum GPU memory in GB')
        failover_parser.add_argument('--min-vcpu', type=int, required=True, help='Minimum vCPU count')
        failover_parser.add_argument('--max-price', type=float, required=True, help='Maximum ondemand $/HR')
        failover_parser.add_argument('--preferred-gpu', help='Preferred GPU type (will be tried first if available)')
        failover_parser.add_argument('--template-id', required=True, help='Template ID')
        failover_parser.add_argument('--network-volume-id', required=True, help='Network volume ID')
        failover_parser.add_argument('--image-name', required=True, help='Container image name')
        failover_parser.add_argument('--name', default='failover-pod', help='Pod name')
        failover_parser.add_argument('--json', action='store_true', help='Output in JSON format')

        # Create command - create a new pod (simpler interface)
        create_parser = subparsers.add_parser('create', help='Create a new pod with best available GPU')
        create_parser.add_argument('--min-mem', type=int, required=True, help='Minimum GPU memory in GB')
        create_parser.add_argument('--min-vcpu', type=int, required=True, help='Minimum vCPU count')
        create_parser.add_argument('--max-price', type=float, required=True, help='Maximum ondemand $/HR')
        create_parser.add_argument('--preferred-gpu', help='Preferred GPU type (will be tried first if available)')
        create_parser.add_argument('--template-id', required=True, help='Template ID')
        create_parser.add_argument('--network-volume-id', required=True, help='Network volume ID')
        create_parser.add_argument('--image-name', required=True, help='Container image name')
        create_parser.add_argument('--name', default='riva-pod', help='Pod name')
        create_parser.add_argument('--json', action='store_true', help='Output in JSON format')

        # Restart-or-recreate command - the main workflow
        restart_parser = subparsers.add_parser('restart-or-recreate',
            help='Restart a pod, or recreate it with same/similar GPU if unavailable')
        restart_parser.add_argument('pod_id', nargs='?', help='Pod ID to restart or recreate (optional if using fallback config)')
        restart_parser.add_argument('--max-price', type=float, default=5.00,
            help='Maximum ondemand $/HR for fallback GPU (default: 5.00)')
        restart_parser.add_argument('--pod-id-file', default='/root/mgmt/runpod/logs/current_pod_id',
            help='File to read/write pod ID (default: /root/mgmt/runpod/logs/current_pod_id)')
        restart_parser.add_argument('--json', action='store_true', help='Output in JSON format')
        # Fallback config for when pod doesn't exist or no pod ID provided
        restart_parser.add_argument('--fallback-template-id', help='Template ID for new pod creation')
        restart_parser.add_argument('--fallback-network-volume-id', help='Network volume ID for new pod')
        restart_parser.add_argument('--fallback-image-name', help='Container image for new pod')
        restart_parser.add_argument('--fallback-gpu', help='Preferred GPU type for new pod')
        restart_parser.add_argument('--fallback-name', default='failover-pod', help='Name for new pod')
        restart_parser.add_argument('--fallback-min-mem', type=int, default=80, help='Min GPU memory for new pod')
        restart_parser.add_argument('--fallback-min-vcpu', type=int, default=16, help='Min vCPU for new pod')
        # Retry configuration
        restart_parser.add_argument('--max-retries', type=int, default=3,
            help='Maximum number of retry attempts when no GPU available (default: 3)')
        restart_parser.add_argument('--retry-interval', type=int, default=300,
            help='Seconds to wait between retry attempts (default: 300 = 5 minutes)')
        # Telegram alerting configuration
        restart_parser.add_argument('--telegram-token', help='Telegram bot token (overrides config)')
        restart_parser.add_argument('--telegram-chat-id', help='Telegram chat ID (overrides config)')
        restart_parser.add_argument('--no-telegram', action='store_true', help='Disable Telegram alerts')
        # Loki logging configuration
        restart_parser.add_argument('--loki-url', help='Loki push URL (overrides config)')
        restart_parser.add_argument('--no-loki', action='store_true', help='Disable Loki logging')

        # Clone-pod command - create a new pod with same config as existing pod
        clone_parser = subparsers.add_parser('clone-pod',
            help='Create a new pod cloning config from an existing pod')
        clone_parser.add_argument('source_pod_id', help='Source pod ID to clone from')
        clone_parser.add_argument('--gpu-type', help='Override GPU type (default: same as source)')
        clone_parser.add_argument('--name', help='New pod name (default: same as source)')
        clone_parser.add_argument('--max-price', type=float, default=5.00,
            help='Maximum ondemand $/HR (default: 5.00)')
        clone_parser.add_argument('--json', action='store_true', help='Output in JSON format')

        args = parser.parse_args()
    else:
        # Legacy interface: <action> [pod_id] OR <pod_id> <action>
        # We detect which format based on whether first positional matches an action
        parser = argparse.ArgumentParser(description='Manage RunPod pods')
        parser.add_argument('action', choices=['start', 'stop', 'info', 'restart', 'remove'],
                            help='Action to perform on pod')
        parser.add_argument('pod_id', nargs='?', help='Pod ID (or read from --pod-id-file)')
        parser.add_argument('--container-port', type=int,
                            help='Container port to get mapping for')
        parser.add_argument('--json', action='store_true',
                            help='Output in JSON format')
        parser.add_argument('--debug', action='store_true',
                            help='Enable debug logging (also enabled via DEBUGGING=true env var)')
        parser.add_argument('--pod-id-file', default='/root/mgmt/runpod/logs/current_pod_id',
                            help='File to read pod ID from if not provided')
        args = parser.parse_args()
        args.command = None  # Mark as legacy mode

    # If --debug flag was explicitly passed, ensure logging is enabled
    if getattr(args, 'debug', False):
        setup_debug_logging(enabled=True)

    # Log the parsed arguments (logging was already initialized at script start)
    debug_log("DEBUG", f"Parsed args: {args}")

    try:
        # Handle cloud commands
        if args.command == 'cloud':
            cloud_mgr = CloudManager()

            if args.cloud_action == 'list':
                gpus = cloud_mgr.get_available_gpus()

                # Filter if criteria specified
                if args.min_mem > 0 or args.min_vcpu > 0 or args.max_price < float('inf'):
                    gpus = [g for g in gpus if g.matches_requirements(
                        args.min_mem, args.min_vcpu, args.max_price
                    )]

                if args.json:
                    output = [
                        {
                            'gpu_type': g.gpu_type,
                            'mem_gb': g.mem_gb,
                            'vcpu': g.vcpu,
                            'spot_price': g.spot_price,
                            'ondemand_price': g.ondemand_price
                        }
                        for g in gpus
                    ]
                    print(json.dumps(output, indent=2))
                else:
                    print(f"{'GPU Type':<40} {'MEM GB':<8} {'vCPU':<6} {'Spot $/HR':<12} {'Ondemand $/HR':<14}")
                    print("-" * 80)
                    for g in gpus:
                        spot = f"{g.spot_price:.3f}" if g.spot_price else "Reserved"
                        ondemand = f"{g.ondemand_price:.3f}" if g.ondemand_price else "Reserved"
                        print(f"{g.gpu_type:<40} {g.mem_gb:<8} {g.vcpu:<6} {spot:<12} {ondemand:<14}")

            elif args.cloud_action == 'find-best':
                gpu = cloud_mgr.find_best_gpu(args.min_mem, args.min_vcpu, args.max_price)

                if gpu is None:
                    print("Error: No GPU found matching criteria", file=sys.stderr)
                    exit(1)

                if args.json:
                    output = {
                        'gpu_type': gpu.gpu_type,
                        'mem_gb': gpu.mem_gb,
                        'vcpu': gpu.vcpu,
                        'spot_price': gpu.spot_price,
                        'ondemand_price': gpu.ondemand_price
                    }
                    print(json.dumps(output, indent=2))
                else:
                    print(f"Best matching GPU: {gpu.gpu_type}")
                    print(f"  Memory: {gpu.mem_gb} GB")
                    print(f"  vCPU: {gpu.vcpu}")
                    print(f"  Ondemand Price: ${gpu.ondemand_price}/hr")

            elif args.cloud_action == 'create-pod':
                pod_id = cloud_mgr.create_pod(
                    gpu_type=args.gpu_type,
                    template_id=args.template_id,
                    network_volume_id=args.network_volume_id,
                    image_name=args.image_name,
                    pod_name=args.name,
                    gpu_count=args.gpu_count
                )

                if args.json:
                    print(json.dumps({'pod_id': pod_id}))
                else:
                    print(f"Pod created successfully: {pod_id}")

        # Handle failover command
        elif args.command == 'failover':
            cloud_mgr = CloudManager()

            # Step 1 & 2: Find available GPU and create pod (with retry)
            print("Finding available GPU and creating pod...", file=sys.stderr)
            try:
                new_pod_id, gpu = cloud_mgr.create_pod_with_retry(
                    min_mem=args.min_mem,
                    min_vcpu=args.min_vcpu,
                    max_price=args.max_price,
                    template_id=args.template_id,
                    network_volume_id=args.network_volume_id,
                    image_name=args.image_name,
                    pod_name=args.name
                )
            except Exception as e:
                result = {
                    'success': False,
                    'error': str(e),
                    'old_pod_id': args.old_pod_id
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)
            print(f"New pod created: {new_pod_id}", file=sys.stderr)

            # Step 3: Wait for new pod to be ready
            print("Waiting for new pod to start...", file=sys.stderr)
            new_pod_mgr = RunPodManager(new_pod_id)
            try:
                pod_info = new_pod_mgr._wait_for_status('RUNNING', timeout=300)
            except TimeoutError as e:
                # Clean up the new pod if it didn't start
                print(f"New pod failed to start, cleaning up...", file=sys.stderr)
                try:
                    new_pod_mgr.remove_pod()
                except Exception:
                    pass
                result = {
                    'success': False,
                    'error': f'New pod failed to start: {str(e)}',
                    'old_pod_id': args.old_pod_id,
                    'new_pod_id': new_pod_id
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)

            # Step 4: Remove old pod (if provided)
            if args.old_pod_id:
                print(f"Removing old pod {args.old_pod_id}...", file=sys.stderr)
                old_pod_mgr = RunPodManager(args.old_pod_id)
                try:
                    old_pod_mgr.remove_pod()
                    print("Old pod removed successfully", file=sys.stderr)
                except Exception as e:
                    print(f"Warning: Failed to remove old pod: {e}", file=sys.stderr)

            # Step 5: Return results
            result = {
                'success': True,
                'old_pod_id': args.old_pod_id,
                'new_pod_id': new_pod_id,
                'gpu_type': gpu.gpu_type,
                'ondemand_price': gpu.ondemand_price,
                'public_ip': pod_info.public_ip,
                'port_mappings': [
                    {
                        'public_ip': pm.public_ip,
                        'public_port': pm.public_port,
                        'container_port': pm.container_port,
                        'protocol': pm.protocol
                    }
                    for pm in pod_info.port_mappings
                ]
            }

            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"\nFailover completed successfully!", file=sys.stderr)
                if args.old_pod_id:
                    print(f"  Old Pod: {args.old_pod_id} (removed)", file=sys.stderr)
                print(f"  New Pod: {new_pod_id}", file=sys.stderr)
                print(f"  GPU: {gpu.gpu_type}", file=sys.stderr)
                print(f"  Price: ${gpu.ondemand_price}/hr", file=sys.stderr)
                print(f"  Public IP: {pod_info.public_ip}", file=sys.stderr)

        # Handle create command (create new pod with best available GPU)
        elif args.command == 'create':
            cloud_mgr = CloudManager()

            # Step 1 & 2: Find available GPU and create pod (with retry)
            print("Finding available GPU and creating pod...", file=sys.stderr)
            try:
                new_pod_id, gpu = cloud_mgr.create_pod_with_retry(
                    min_mem=args.min_mem,
                    min_vcpu=args.min_vcpu,
                    max_price=args.max_price,
                    template_id=args.template_id,
                    network_volume_id=args.network_volume_id,
                    image_name=args.image_name,
                    pod_name=args.name,
                    preferred_gpu=args.preferred_gpu
                )
            except Exception as e:
                result = {
                    'success': False,
                    'error': str(e)
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)

            print(f"Pod created with {gpu.gpu_type} (${gpu.ondemand_price}/hr)", file=sys.stderr)

            # Step 3: Wait for new pod to be ready
            print("Waiting for new pod to start...", file=sys.stderr)
            new_pod_mgr = RunPodManager(new_pod_id)
            try:
                pod_info = new_pod_mgr._wait_for_status('RUNNING', timeout=300)
            except TimeoutError as e:
                # Clean up the new pod if it didn't start
                print(f"New pod failed to start, cleaning up...", file=sys.stderr)
                try:
                    new_pod_mgr.remove_pod()
                except Exception:
                    pass
                result = {
                    'success': False,
                    'error': f'New pod failed to start: {str(e)}',
                    'new_pod_id': new_pod_id
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)

            # Step 4: Return results
            result = {
                'success': True,
                'new_pod_id': new_pod_id,
                'gpu_type': gpu.gpu_type,
                'ondemand_price': gpu.ondemand_price,
                'public_ip': pod_info.public_ip,
                'port_mappings': [
                    {
                        'public_ip': pm.public_ip,
                        'public_port': pm.public_port,
                        'container_port': pm.container_port,
                        'protocol': pm.protocol
                    }
                    for pm in pod_info.port_mappings
                ]
            }

            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"\nPod created successfully!", file=sys.stderr)
                print(f"  Pod ID: {new_pod_id}", file=sys.stderr)
                print(f"  GPU: {gpu.gpu_type}", file=sys.stderr)
                print(f"  Price: ${gpu.ondemand_price}/hr", file=sys.stderr)
                print(f"  Public IP: {pod_info.public_ip}", file=sys.stderr)

        # Handle restart-or-recreate command
        elif args.command == 'restart-or-recreate':
            cloud_mgr = CloudManager()
            
            # Initialize alerting and logging
            telegram = TelegramNotifier(
                bot_token=getattr(args, 'telegram_token', None),
                chat_id=getattr(args, 'telegram_chat_id', None),
                enabled=not getattr(args, 'no_telegram', False)
            )
            loki = LokiLogger(
                loki_url=getattr(args, 'loki_url', None),
                enabled=not getattr(args, 'no_loki', False)
            )
            
            # Retry configuration
            max_retries = getattr(args, 'max_retries', 3)
            retry_interval = getattr(args, 'retry_interval', 300)  # 5 minutes

            # Step 0: Determine pod ID (from arg, file, or create new)
            pod_id = args.pod_id

            # If no pod ID provided, try to read from file
            if not pod_id and args.pod_id_file:
                try:
                    with open(args.pod_id_file, 'r') as f:
                        pod_id = f.read().strip()
                        if pod_id:
                            print(f"Read pod ID from file: {pod_id}", file=sys.stderr)
                except FileNotFoundError:
                    print(f"Pod ID file not found: {args.pod_id_file}", file=sys.stderr)

            # Step 1: Get current pod configuration (or use fallback)
            pod_config = None
            pod_exists = False

            if pod_id:
                print(f"Getting configuration for pod {pod_id}...", file=sys.stderr)
                try:
                    pod_config = cloud_mgr.get_pod_config(pod_id)
                    pod_exists = True
                    print(f"Pod config: GPU={pod_config.gpu_type_id}, Network Volume={pod_config.network_volume_id}", file=sys.stderr)
                except Exception as e:
                    print(f"Could not get pod config: {e}", file=sys.stderr)
                    print("Pod may not exist. Will create new pod with fallback config.", file=sys.stderr)

            # If no pod config, we need fallback parameters
            if not pod_config:
                if not args.fallback_template_id or not args.fallback_network_volume_id:
                    result = {
                        'success': False,
                        'error': 'No pod ID or pod not found. Fallback requires --fallback-template-id and --fallback-network-volume-id',
                        'pod_id': pod_id
                    }
                    if args.json:
                        print(json.dumps(result))
                    else:
                        print(f"Error: {result['error']}", file=sys.stderr)
                    exit(1)

                # Create a synthetic PodConfig from fallback args
                print("Using fallback configuration for new pod creation...", file=sys.stderr)
                pod_config = PodConfig(
                    id=None,
                    name=args.fallback_name,
                    image_name=args.fallback_image_name or "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04",
                    gpu_type_id=args.fallback_gpu or "NVIDIA H200",
                    gpu_count=1,
                    vcpu_count=args.fallback_min_vcpu,
                    memory_in_gb=251,
                    container_disk_in_gb=10,
                    volume_in_gb=0,
                    volume_mount_path="/workspace",
                    template_id=args.fallback_template_id,
                    network_volume_id=args.fallback_network_volume_id,
                    datacenter_id=None,
                    ports="8888/http,22/tcp",
                    env=[],
                    docker_args=None,
                    cost_per_hr=None
                )
                pod_exists = False

            pod_mgr = RunPodManager(pod_id) if pod_id else None

            # Step 2: Try to start the existing pod (if it exists)
            restart_success = False
            start_error = None

            if pod_exists and pod_mgr:
                print(f"Attempting to start pod {pod_id}...", file=sys.stderr)
                loki.log_pod_start_attempted(pod_id, pod_config.name)
                try:
                    pod_info = pod_mgr.start_pod(wait_for_ready=True, timeout=120)
                    restart_success = True
                    loki.log_pod_start_success(pod_id, pod_config.name)
                    telegram.alert_pod_started(pod_id, pod_config.name)
                except Exception as e:
                    start_error = str(e)
                    print(f"Start failed: {start_error}", file=sys.stderr)
                    loki.log_pod_start_failed(pod_id, pod_config.name, start_error)
                    telegram.alert_pod_start_failed(pod_id, pod_config.name, start_error)
            else:
                print("No existing pod to restart. Will create new pod.", file=sys.stderr)

            if restart_success:
                # Pod started successfully
                result = {
                    'success': True,
                    'action': 'restarted',
                    'pod_id': pod_id,
                    'gpu_type': pod_config.gpu_type_id,
                    'public_ip': pod_info.public_ip,
                    'port_mappings': [
                        {
                            'public_ip': pm.public_ip,
                            'public_port': pm.public_port,
                            'container_port': pm.container_port,
                            'protocol': pm.protocol
                        }
                        for pm in pod_info.port_mappings
                    ]
                }
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    print(f"\nPod restarted successfully!", file=sys.stderr)
                    print(f"  Pod ID: {pod_id}", file=sys.stderr)
                    print(f"  GPU: {pod_config.gpu_type_id}", file=sys.stderr)
                    print(f"  Public IP: {pod_info.public_ip}", file=sys.stderr)
            else:
                # Step 3: Pod start failed, need to recreate
                print("Pod restart failed. Attempting to recreate...", file=sys.stderr)

                # Get network volume datacenter for GPU availability query
                datacenter_id = None
                if pod_config.network_volume_id:
                    try:
                        vol_info = cloud_mgr.get_network_volume_info(pod_config.network_volume_id)
                        datacenter_id = vol_info.get('datacenter_id')
                        print(f"Network volume datacenter: {datacenter_id}", file=sys.stderr)
                    except Exception as e:
                        print(f"Warning: Could not get volume datacenter: {e}", file=sys.stderr)

                # Step 4: Find similar GPUs (prioritize same GPU, then similar specs)
                gpu_mem_gb = 80  # Default estimate for high-end GPU
                if pod_config.gpu_type_id:
                    # Try to get actual GPU memory from the API
                    try:
                        query = '{ gpuTypes { id memoryInGb } }'
                        result_data = cloud_mgr._graphql_query(query)
                        for gpu in result_data.get('data', {}).get('gpuTypes', []):
                            if gpu.get('id') == pod_config.gpu_type_id:
                                gpu_mem_gb = gpu.get('memoryInGb', gpu_mem_gb)
                                break
                    except:
                        pass

                print(f"Looking for GPUs similar to {pod_config.gpu_type_id} ({gpu_mem_gb}GB)...", file=sys.stderr)
                
                # Retry loop for GPU search and pod creation
                retry_start_time = time.time()
                new_pod_id = None
                used_gpu = None
                final_errors = []
                
                for retry_attempt in range(1, max_retries + 1):
                    loki.log_gpu_search(datacenter_id or "any", pod_config.gpu_type_id, retry_attempt)
                    
                    similar_gpus = cloud_mgr.find_similar_gpus(
                        target_gpu_type=pod_config.gpu_type_id,
                        target_mem_gb=gpu_mem_gb,
                        datacenter_id=datacenter_id,
                        max_price=args.max_price
                    )

                    if not similar_gpus:
                        elapsed_minutes = int((time.time() - retry_start_time) / 60)
                        loki.log_no_gpu_available(
                            datacenter_id or "any", 
                            pod_config.gpu_type_id, 
                            retry_attempt, 
                            max_retries
                        )
                        
                        if retry_attempt < max_retries:
                            # Not final attempt - alert and wait
                            telegram.alert_no_gpu_retry(
                                datacenter=datacenter_id or "any",
                                gpu_type=pod_config.gpu_type_id,
                                retry=retry_attempt,
                                max_retries=max_retries,
                                next_retry_seconds=retry_interval
                            )
                            print(f"No GPUs available. Retry {retry_attempt}/{max_retries}. "
                                  f"Waiting {retry_interval // 60} minutes...", file=sys.stderr)
                            time.sleep(retry_interval)
                            continue
                        else:
                            # Final attempt failed - send critical alert
                            total_time_minutes = int((time.time() - retry_start_time) / 60)
                            telegram.alert_no_gpu_final(
                                datacenter=datacenter_id or "any",
                                gpu_type=pod_config.gpu_type_id,
                                max_price=args.max_price,
                                attempts=max_retries,
                                total_time_minutes=total_time_minutes
                            )
                            loki.log_operation_failed(
                                "recreate",
                                f"No GPU available after {max_retries} attempts",
                                datacenter=datacenter_id or "any"
                            )
                            result = {
                                'success': False,
                                'error': f'No suitable GPU found after {max_retries} attempts (max price: ${args.max_price}/hr)',
                                'pod_id': pod_id,
                                'original_gpu': pod_config.gpu_type_id,
                                'datacenter': datacenter_id,
                                'retry_attempts': max_retries,
                                'total_wait_minutes': total_time_minutes
                            }
                            if args.json:
                                print(json.dumps(result))
                            else:
                                print(f"Error: {result['error']}", file=sys.stderr)
                            exit(1)

                    # Step 5: Try to create new pod with each GPU until one works
                    errors = []
                    gpu_attempt = 0
                    total_gpus = len(similar_gpus)

                    for gpu in similar_gpus:
                        gpu_attempt += 1
                        stock_info = f" [{gpu.stock_status}]" if gpu.stock_status else ""
                        print(f"Trying GPU: {gpu.gpu_type} ({gpu.mem_gb}GB, ${gpu.ondemand_price}/hr){stock_info}...", file=sys.stderr)
                        loki.log_pod_creation_attempted(gpu.gpu_type, datacenter_id or "auto")
                        try:
                            new_pod_id = cloud_mgr.create_pod_graphql(
                                gpu_type_id=gpu.gpu_type,
                                template_id=pod_config.template_id,
                                network_volume_id=pod_config.network_volume_id,
                                datacenter_id=datacenter_id,
                                pod_name=pod_config.name,
                                gpu_count=pod_config.gpu_count,
                                container_disk_in_gb=pod_config.container_disk_in_gb,
                                volume_in_gb=pod_config.volume_in_gb,
                                min_memory_in_gb=pod_config.memory_in_gb or 251,
                                min_vcpu_count=pod_config.vcpu_count or 24
                            )
                            used_gpu = gpu
                            loki.log_pod_created(new_pod_id, pod_config.name, gpu.gpu_type, datacenter_id or "auto")
                            print(f"Successfully created pod {new_pod_id} with {gpu.gpu_type}", file=sys.stderr)
                            break
                        except Exception as e:
                            error_msg = str(e)
                            print(f"Failed: {error_msg}", file=sys.stderr)
                            loki.log_pod_creation_failed(gpu.gpu_type, datacenter_id or "auto", error_msg)
                            errors.append(f"{gpu.gpu_type}: {error_msg}")
                    
                    if new_pod_id:
                        # Successfully created pod, exit retry loop
                        break
                    
                    # All GPUs in this attempt failed
                    final_errors.extend(errors)
                    
                    if retry_attempt < max_retries:
                        print(f"All {total_gpus} GPUs failed. Retry {retry_attempt}/{max_retries}. "
                              f"Waiting {retry_interval // 60} minutes...", file=sys.stderr)
                        telegram.alert_no_gpu_retry(
                            datacenter=datacenter_id or "any",
                            gpu_type=pod_config.gpu_type_id,
                            retry=retry_attempt,
                            max_retries=max_retries,
                            next_retry_seconds=retry_interval
                        )
                        time.sleep(retry_interval)
                    else:
                        # Final attempt - all GPUs failed
                        total_time_minutes = int((time.time() - retry_start_time) / 60)
                        telegram.alert_no_gpu_final(
                            datacenter=datacenter_id or "any",
                            gpu_type=pod_config.gpu_type_id,
                            max_price=args.max_price,
                            attempts=max_retries,
                            total_time_minutes=total_time_minutes
                        )
                        loki.log_operation_failed(
                            "recreate",
                            f"Failed to create pod after {max_retries} attempts: {'; '.join(final_errors[-3:])}",
                            datacenter=datacenter_id or "any"
                        )
                        result = {
                            'success': False,
                            'error': f'Failed to create pod with any available GPU after {max_retries} attempts. Errors: {"; ".join(final_errors[-5:])}',
                            'pod_id': pod_id,
                            'retry_attempts': max_retries,
                            'total_wait_minutes': total_time_minutes
                        }
                        if args.json:
                            print(json.dumps(result))
                        else:
                            print(f"Error: {result['error']}", file=sys.stderr)
                        exit(1)

                if not new_pod_id:
                    # This should not be reached due to retry loop, but just in case
                    loki.log_operation_failed("recreate", "No pod created after retry loop")
                    result = {
                        'success': False,
                        'error': 'Failed to create pod (unexpected state)',
                        'pod_id': pod_id
                    }
                    if args.json:
                        print(json.dumps(result))
                    else:
                        print(f"Error: {result['error']}", file=sys.stderr)
                    exit(1)

                # Step 6: Wait for new pod to be ready
                print("Waiting for new pod to start...", file=sys.stderr)
                new_pod_mgr = RunPodManager(new_pod_id)
                try:
                    new_pod_info = new_pod_mgr._wait_for_status('RUNNING', timeout=300)
                except TimeoutError as e:
                    print(f"New pod failed to start, cleaning up...", file=sys.stderr)
                    loki.log_operation_failed("recreate", f"Pod {new_pod_id} failed to reach RUNNING state")
                    telegram.alert_creation_failed(
                        used_gpu.gpu_type if used_gpu else "unknown",
                        datacenter_id or "auto",
                        f"Timeout waiting for RUNNING state: {e}",
                        1, 1
                    )
                    try:
                        cloud_mgr.terminate_pod(new_pod_id)
                    except:
                        pass
                    result = {
                        'success': False,
                        'error': f'New pod failed to start: {str(e)}',
                        'pod_id': pod_id,
                        'new_pod_id': new_pod_id
                    }
                    if args.json:
                        print(json.dumps(result))
                    else:
                        print(f"Error: {result['error']}", file=sys.stderr)
                    exit(1)

                # Step 7: Terminate old pod (only if it existed)
                if pod_exists and pod_id:
                    print(f"Terminating old pod {pod_id}...", file=sys.stderr)
                    try:
                        cloud_mgr.terminate_pod(pod_id)
                        print("Old pod terminated successfully", file=sys.stderr)
                    except Exception as e:
                        print(f"Warning: Failed to terminate old pod: {e}", file=sys.stderr)

                # Step 8: Write new pod ID to file if specified
                if args.pod_id_file:
                    try:
                        with open(args.pod_id_file, 'w') as f:
                            f.write(new_pod_id)
                        print(f"New pod ID written to {args.pod_id_file}", file=sys.stderr)
                    except Exception as e:
                        print(f"Warning: Failed to write pod ID file: {e}", file=sys.stderr)

                # Send success alerts
                loki.log_pod_recreated(
                    pod_id or "none",
                    new_pod_id,
                    used_gpu.gpu_type if used_gpu else "unknown",
                    datacenter_id or "auto"
                )
                telegram.alert_pod_recreated(
                    old_pod_id=pod_id or "none",
                    new_pod_id=new_pod_id,
                    pod_name=pod_config.name,
                    gpu_type=used_gpu.gpu_type if used_gpu else "unknown",
                    datacenter=datacenter_id or "auto",
                    cost_per_hr=used_gpu.ondemand_price if used_gpu else 0.0
                )

                # Return results - action is 'created' if no old pod, 'recreated' if replacing
                action = 'recreated' if pod_exists else 'created'
                result = {
                    'success': True,
                    'action': action,
                    'old_pod_id': pod_id if pod_exists else None,
                    'new_pod_id': new_pod_id,
                    'original_gpu': pod_config.gpu_type_id,
                    'new_gpu': used_gpu.gpu_type,
                    'new_gpu_price': used_gpu.ondemand_price,
                    'public_ip': new_pod_info.public_ip,
                    'port_mappings': [
                        {
                            'public_ip': pm.public_ip,
                            'public_port': pm.public_port,
                            'container_port': pm.container_port,
                            'protocol': pm.protocol
                        }
                        for pm in new_pod_info.port_mappings
                    ]
                }
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    if pod_exists:
                        print(f"\nPod recreated successfully!", file=sys.stderr)
                        print(f"  Old Pod: {pod_id} (terminated)", file=sys.stderr)
                    else:
                        print(f"\nPod created successfully!", file=sys.stderr)
                    print(f"  New Pod: {new_pod_id}", file=sys.stderr)
                    print(f"  GPU: {used_gpu.gpu_type} (${used_gpu.ondemand_price}/hr)", file=sys.stderr)
                    print(f"  Public IP: {new_pod_info.public_ip}", file=sys.stderr)

        # Handle clone-pod command
        elif args.command == 'clone-pod':
            cloud_mgr = CloudManager()

            # Get source pod configuration
            print(f"Getting configuration for source pod {args.source_pod_id}...", file=sys.stderr)
            try:
                pod_config = cloud_mgr.get_pod_config(args.source_pod_id)
            except Exception as e:
                result = {
                    'success': False,
                    'error': f'Failed to get source pod configuration: {str(e)}',
                    'source_pod_id': args.source_pod_id
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)

            # Determine GPU to use
            gpu_type = args.gpu_type or pod_config.gpu_type_id
            pod_name = args.name or pod_config.name

            # Get datacenter from network volume
            datacenter_id = None
            if pod_config.network_volume_id:
                try:
                    vol_info = cloud_mgr.get_network_volume_info(pod_config.network_volume_id)
                    datacenter_id = vol_info.get('datacenter_id')
                except:
                    pass

            print(f"Creating new pod with GPU {gpu_type}...", file=sys.stderr)
            try:
                new_pod_id = cloud_mgr.create_pod_graphql(
                    gpu_type_id=gpu_type,
                    template_id=pod_config.template_id,
                    network_volume_id=pod_config.network_volume_id,
                    datacenter_id=datacenter_id,
                    pod_name=pod_name,
                    gpu_count=pod_config.gpu_count,
                    container_disk_in_gb=pod_config.container_disk_in_gb,
                    volume_in_gb=pod_config.volume_in_gb,
                    min_memory_in_gb=pod_config.memory_in_gb or 251,
                    min_vcpu_count=pod_config.vcpu_count or 24
                )
            except Exception as e:
                result = {
                    'success': False,
                    'error': f'Failed to create pod: {str(e)}',
                    'source_pod_id': args.source_pod_id,
                    'gpu_type': gpu_type
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)

            # Wait for new pod to start
            print("Waiting for new pod to start...", file=sys.stderr)
            new_pod_mgr = RunPodManager(new_pod_id)
            try:
                pod_info = new_pod_mgr._wait_for_status('RUNNING', timeout=300)
            except TimeoutError as e:
                print(f"New pod failed to start, cleaning up...", file=sys.stderr)
                try:
                    cloud_mgr.terminate_pod(new_pod_id)
                except:
                    pass
                result = {
                    'success': False,
                    'error': f'New pod failed to start: {str(e)}',
                    'new_pod_id': new_pod_id
                }
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f"Error: {result['error']}", file=sys.stderr)
                exit(1)

            result = {
                'success': True,
                'source_pod_id': args.source_pod_id,
                'new_pod_id': new_pod_id,
                'gpu_type': gpu_type,
                'public_ip': pod_info.public_ip,
                'port_mappings': [
                    {
                        'public_ip': pm.public_ip,
                        'public_port': pm.public_port,
                        'container_port': pm.container_port,
                        'protocol': pm.protocol
                    }
                    for pm in pod_info.port_mappings
                ]
            }
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"\nPod cloned successfully!", file=sys.stderr)
                print(f"  Source Pod: {args.source_pod_id}", file=sys.stderr)
                print(f"  New Pod: {new_pod_id}", file=sys.stderr)
                print(f"  GPU: {gpu_type}", file=sys.stderr)
                print(f"  Public IP: {pod_info.public_ip}", file=sys.stderr)

        # Handle pod operations (legacy interface)
        elif args.command is None:
            debug_log("INFO", f"Legacy interface: action={args.action}")

            # Get pod_id_file path
            pod_id_file = getattr(args, 'pod_id_file', '/root/mgmt/runpod/logs/current_pod_id')

            # Retrieve the pod_id from the command line
            pod_id = args.pod_id
            debug_log("DEBUG", f"Pod ID from args: {pod_id}")

            # If no pod ID provided, try to read from file
            if not pod_id and pod_id_file:
                debug_log("DEBUG", f"Attempting to read pod ID from file: {pod_id_file}")
                try:
                    with open(pod_id_file, 'r') as f:
                        pod_id = f.read().strip()
                        if pod_id:
                            debug_log("INFO", f"Read pod ID from file: {pod_id}")
                            print(f"Read pod ID from file: {pod_id}", file=sys.stderr)
                except FileNotFoundError:
                    debug_log("ERROR", f"Pod ID file not found: {pod_id_file}")
                    print(f"Pod ID file not found: {pod_id_file}", file=sys.stderr)

            if not pod_id:
                debug_log("ERROR", "No pod ID available")
                raise Exception("No pod ID provided and could not read from file")

            debug_log("INFO", f"Using pod ID: {pod_id}")

            # Initialise with pod id
            manager = RunPodManager(pod_id)

            if args.action == 'start':
                debug_log("INFO", f"Starting pod {pod_id}")
                pod_info = manager.start_pod()
                debug_log("INFO", f"Pod started successfully. Status: {pod_info.status}")
                if not args.json:
                    print(f"\n✓ Pod started successfully!")

            elif args.action == 'stop':
                debug_log("INFO", f"Stopping pod {pod_id}")
                pod_info = manager.stop_pod()
                debug_log("INFO", f"Pod stopped successfully. Status: {pod_info.status}")
                if not args.json:
                    print(f"\n✓ Pod stopped successfully!")

            elif args.action == 'restart':
                debug_log("INFO", f"Restarting pod {pod_id}")
                if not args.json:
                    print("Stopping pod...")
                manager.stop_pod()
                debug_log("INFO", f"Pod stopped, now starting...")
                if not args.json:
                    print("\nStarting pod...")
                pod_info = manager.start_pod()
                debug_log("INFO", f"Pod restarted successfully. Status: {pod_info.status}")
                if not args.json:
                    print(f"\n✓ Pod restarted successfully!")

            elif args.action == 'remove':
                debug_log("INFO", f"Removing pod {pod_id}")
                manager.remove_pod()
                debug_log("INFO", f"Pod removed successfully")
                if args.json:
                    print(json.dumps({'success': True, 'pod_id': pod_id}))
                else:
                    print(f"\n✓ Pod {pod_id} removed successfully!")
                return

            else:  # info
                debug_log("INFO", f"Getting info for pod {pod_id}")
                pod_info = manager.get_pod_info()
                debug_log("INFO", f"Pod info retrieved. Status: {pod_info.status}")

            # Display results
            if args.json:
                output = {
                    'id': pod_info.id,
                    'name': pod_info.name,
                    'status': pod_info.status,
                    'public_ip': pod_info.public_ip,
                    'port_mappings': [
                        {
                            'public_ip': pm.public_ip,
                            'public_port': pm.public_port,
                            'container_port': pm.container_port,
                            'protocol': pm.protocol
                        }
                        for pm in pod_info.port_mappings
                    ]
                }
                print(json.dumps(output, indent=2))
            else:
                print(f"\nPod Information:")
                print(f"  ID: {pod_info.id}")
                print(f"  Name: {pod_info.name}")
                print(f"  Status: {pod_info.status}")
                print(f"  Public IP: {pod_info.public_ip}")
                print(f"\nPort Mappings:")
                for pm in pod_info.port_mappings:
                    print(f"  {pm.public_ip}:{pm.public_port} -> {pm.container_port} ({pm.protocol})")

            # If specific container port requested
            if args.container_port:
                mapping = manager.get_port_by_container_port(args.container_port)
                if mapping:
                    if not args.json:
                        print(f"\nMapping for container port {args.container_port}:")
                        print(f"  Public endpoint: {mapping.public_ip}:{mapping.public_port}")
                else:
                    if not args.json:
                        print(f"\nNo mapping found for container port {args.container_port}")

    except Exception as e:
        import traceback
        debug_log("ERROR", f"Exception occurred: {str(e)}")
        debug_log("ERROR", f"Traceback: {traceback.format_exc()}")
        if hasattr(args, 'json') and args.json:
            print(json.dumps({'error': str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        exit(1)


if __name__ == '__main__':
    main()
