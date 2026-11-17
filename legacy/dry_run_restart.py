#!/usr/bin/env python3
"""
Dry-run simulation for RunPod restart-or-recreate command.

This script simulates what would happen if the current pod couldn't start,
showing real GPU availability and the order in which GPUs would be tried.

Usage:
    ./dry_run_restart.py [--verbose]
    
    Or with custom parameters:
    ./dry_run_restart.py --max-price 5.00 --verbose
"""

import sys
import os
import json
import argparse
from datetime import datetime

# Add script directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from runpod_costsaving import (
    CloudManager, RunPodManager, TelegramNotifier, LokiLogger,
    read_config_value
)


def print_header(text):
    """Print a section header"""
    print(f"\n{'='*60}")
    print(f" {text}")
    print(f"{'='*60}")


def print_step(num, text):
    """Print a step indicator"""
    print(f"\n[Step {num}] {text}")
    print("-" * 50)


def format_price(price):
    """Format price with color indication"""
    if price is None:
        return "N/A"
    return f"${price:.2f}/hr"


def format_stock(status):
    """Format stock status with indicator"""
    indicators = {
        "High": "🟢 High",
        "Medium": "🟡 Medium", 
        "Low": "🔴 Low",
        None: "⚪ Unknown"
    }
    return indicators.get(status, f"⚪ {status}")


def main():
    parser = argparse.ArgumentParser(description="Dry-run simulation for restart-or-recreate")
    parser.add_argument('--pod-id', help='Pod ID to simulate (default: from current_pod_id file)')
    parser.add_argument('--max-price', type=float, default=5.0, help='Maximum price per hour (default: 5.0)')
    parser.add_argument('--search-timeout', type=int, default=1200, help='Search deadline in seconds (default: 1200)')
    parser.add_argument('--poll-interval', type=int, default=15, help='Seconds between polls (default: 15)')
    parser.add_argument('--fallback-gpu', help='Preferred GPU type (as passed to restart-or-recreate)')
    parser.add_argument('--fallback-min-mem', type=int, default=80, help='Min GPU memory in GB (default: 80)')
    parser.add_argument('--fallback-min-vcpu', type=int, default=16, help='Min vCPU count (default: 16)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show verbose output')
    parser.add_argument('--send-test-alert', action='store_true', help='Send a test alert showing what would be sent on failure')
    args = parser.parse_args()

    print_header("RunPod Restart-or-Recreate DRY RUN Simulation")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Max Price: ${args.max_price:.2f}/hr")
    print(f"Min GPU Mem: {args.fallback_min_mem} GB | Min vCPU: {args.fallback_min_vcpu}")
    if args.fallback_gpu:
        print(f"Preferred GPU: {args.fallback_gpu}")
    print(f"Search Timeout: {args.search_timeout}s ({args.search_timeout//60} minutes)")
    print(f"Poll Interval:  {args.poll_interval}s")

    # Initialize managers
    cloud_mgr = CloudManager()

    # Step 1: Get current pod ID
    print_step(1, "Loading Current Pod Configuration")

    pod_id = args.pod_id
    if not pod_id:
        pod_id_file = os.path.join(SCRIPT_DIR, 'logs', 'current_pod_id')
        try:
            with open(pod_id_file, 'r') as f:
                pod_id = f.read().strip()
            print(f"Pod ID (from file): {pod_id}")
        except FileNotFoundError:
            print("WARNING: No pod ID found - will simulate pure fallback config")
            pod_id = None
    else:
        print(f"Pod ID (from args): {pod_id}")

    # Step 2: Get pod configuration
    print_step(2, "Fetching Pod Configuration from RunPod API")

    pod_config = None
    if pod_id:
        try:
            pod_config = cloud_mgr.get_pod_config(pod_id)
            print(f"Pod Name:          {pod_config.name}")
            print(f"GPU Type:          {pod_config.gpu_type_id}")
            print(f"GPU Count:         {pod_config.gpu_count}")
            print(f"Template ID:       {pod_config.template_id}")
            print(f"Network Volume:    {pod_config.network_volume_id}")
            print(f"Container Disk:    {pod_config.container_disk_in_gb} GB")
            print(f"Memory:            {pod_config.memory_in_gb} GB")
            print(f"vCPU:              {pod_config.vcpu_count}")
            if pod_config.cost_per_hr:
                print(f"Current Cost:      ${pod_config.cost_per_hr:.2f}/hr")
        except Exception as e:
            print(f"WARNING: Failed to get pod config: {e} - using fallback config only")
            pod_config = None

    if pod_config is None and not args.fallback_gpu:
        print("ERROR: No pod config and no --fallback-gpu given; nothing to simulate")
        sys.exit(1)

    # Step 3: Get network volume datacenter
    print_step(3, "Identifying Datacenter from Network Volume")
    
    datacenter_id = None
    datacenter_name = None
    
    if pod_config and pod_config.network_volume_id:
        try:
            vol_info = cloud_mgr.get_network_volume_info(pod_config.network_volume_id)
            datacenter_id = vol_info.get('datacenter_id')
            datacenter_name = vol_info.get('datacenter_name', datacenter_id)
            print(f"Network Volume:    {vol_info.get('name')} ({vol_info.get('size')} GB)")
            print(f"Datacenter ID:     {datacenter_id}")
            print(f"Datacenter Name:   {datacenter_name}")
            print(f"Global Network:    {vol_info.get('global_network', False)}")
        except Exception as e:
            print(f"WARNING: Could not get volume info: {e}")
    else:
        print("No network volume attached - can use any datacenter")

    # Step 4: Simulate pod start failure
    print_step(4, "SIMULATING: Pod Start Failure")
    print(">>> Simulating scenario where pod cannot start due to 'Not enough free GPUs on host'")
    print(">>> In real scenario, this would trigger the recreation flow...")

    # Step 5: Query GPU availability
    print_step(5, "Querying Real GPU Availability")
    
    if datacenter_id:
        print(f"Searching in datacenter: {datacenter_id} ({datacenter_name})")
        print("(Network volume constraint: must stay in same datacenter)")
    else:
        print("Searching globally (no datacenter constraint)")
    
    target_gpu = (pod_config.gpu_type_id if pod_config else None) or args.fallback_gpu

    print(f"\nTarget GPU: {target_gpu}")
    print(f"Max Price: ${args.max_price:.2f}/hr")
    print(f"Min GPU Mem: {args.fallback_min_mem} GB | Min vCPU: {args.fallback_min_vcpu}")
    print("\nQuerying available GPUs...")

    similar_gpus = cloud_mgr.find_booking_candidates(
        min_mem=args.fallback_min_mem,
        min_vcpu=args.fallback_min_vcpu,
        max_price=args.max_price,
        preferred_gpu=args.fallback_gpu or target_gpu,
    )

    # Step 6: Display results
    print_step(6, "GPU Availability Results")
    
    if not similar_gpus:
        print("\n⚠️  NO GPUs AVAILABLE!")
        print(f"   No GPUs found matching requirements in datacenter {datacenter_id or 'any'}")
        print(f"   Max price filter: ${args.max_price:.2f}/hr")
        print("\n   In real scenario, this would trigger:")
        print(f"   - Re-poll availability every {args.poll_interval}s (fresh list each round)")
        print(f"   - Keep trying for up to {args.search_timeout//60} minutes")
        print(f"   - Deadline reached: CRITICAL alert sent, script exits")

        if args.send_test_alert:
            print("\n   Sending test CRITICAL alert...")
            telegram = TelegramNotifier()
            telegram.alert_no_gpu_final(
                datacenter=datacenter_id or "any",
                gpu_type=target_gpu,
                max_price=args.max_price,
                attempts=args.search_timeout // args.poll_interval,
                total_time_minutes=args.search_timeout // 60
            )
            print("   ✓ Test alert sent!")
    else:
        print(f"\n✅ Found {len(similar_gpus)} available GPU(s):\n")
        print(f"{'#':<3} {'GPU Type':<25} {'Memory':<10} {'Price':<12} {'Stock':<15} {'Match'}")
        print("-" * 85)
        
        for i, gpu in enumerate(similar_gpus, 1):
            is_exact = "✓ PREFERRED" if gpu.gpu_type.lower() == target_gpu.lower() else ""
            print(f"{i:<3} {gpu.gpu_type:<25} {gpu.mem_gb:<10} GB {format_price(gpu.ondemand_price):<12} {format_stock(gpu.stock_status):<15} {is_exact}")
        
        print("\n" + "-" * 85)
        print("\nSimulated Recreation Order:")
        print("The script would try GPUs in the order shown above until one succeeds.\n")
        
        # Show what would happen
        best_gpu = similar_gpus[0]
        print(f"PREDICTION: Would likely use #{1}: {best_gpu.gpu_type}")
        print(f"            Price: {format_price(best_gpu.ondemand_price)}")
        print(f"            Stock: {format_stock(best_gpu.stock_status)}")
        
        if best_gpu.gpu_type.lower() != target_gpu.lower():
            print(f"\n⚠️  Note: This is NOT the same GPU type as target ({target_gpu})")
            if best_gpu.ondemand_price and pod_config and pod_config.cost_per_hr:
                diff = best_gpu.ondemand_price - pod_config.cost_per_hr
                if diff > 0:
                    print(f"   Cost increase: +${diff:.2f}/hr")
                elif diff < 0:
                    print(f"   Cost savings: -${abs(diff):.2f}/hr")

        if args.send_test_alert:
            print("\n   Sending test SUCCESS alert (simulating successful recreation)...")
            telegram = TelegramNotifier()
            telegram.alert_pod_recreated(
                old_pod_id=pod_id or "dry-run",
                new_pod_id="dry-run-test-id",
                pod_name=pod_config.name if pod_config else "fallback-pod",
                gpu_type=best_gpu.gpu_type,
                datacenter=datacenter_id or "auto",
                cost_per_hr=best_gpu.ondemand_price or 0
            )
            print("   ✓ Test alert sent!")

    # Summary
    print_header("Dry Run Summary")
    print(f"Current Pod:       {pod_id or 'none (fallback config)'}")
    print(f"Pod Name:          {pod_config.name if pod_config else 'fallback-pod'}")
    print(f"Target GPU:        {target_gpu}")
    print(f"Datacenter:        {datacenter_name or datacenter_id or 'Any'}")
    print(f"Available GPUs:    {len(similar_gpus)}")
    print(f"Max Price:         ${args.max_price:.2f}/hr")
    print(f"Search Timeout:   {args.search_timeout}s ({args.search_timeout//60} minutes)")
    print(f"Poll Interval:    {args.poll_interval}s")
    
    if similar_gpus:
        print(f"\n✅ RESULT: Recreation would SUCCEED")
        print(f"   Best option: {similar_gpus[0].gpu_type} @ {format_price(similar_gpus[0].ondemand_price)}")
    else:
        print(f"\n❌ RESULT: Recreation would FAIL after {args.search_timeout//60} minutes of polling")
        print(f"   Polls every {args.poll_interval}s with a fresh GPU list each round")
        print(f"   A CRITICAL alert would be sent to Telegram and logged to Loki")
    
    print("\n" + "=" * 60)
    

if __name__ == '__main__':
    main()
