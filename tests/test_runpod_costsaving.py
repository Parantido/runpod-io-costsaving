#!/usr/bin/env python3
"""Tests for runpod_costsaving.py: fallback validation + GPU selection order."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_costsaving import (
    CloudGPU,
    CloudManager,
    PodInfo,
    PortMapping,
    RunPodManager,
    validate_fallback_config,
    gpu_vendor,
)


def _gpu(gpu_type, mem_gb=24, vcpu=16, price=1.0, stock=None):
    return CloudGPU(
        gpu_type=gpu_type,
        display_name=gpu_type,
        mem_gb=mem_gb,
        vcpu=vcpu,
        spot_price=None,
        ondemand_price=price,
        stock_status=stock,
    )


class TestValidateFallbackConfig(unittest.TestCase):
    def test_template_only_is_sufficient(self):
        validate_fallback_config('otppspsd58')

    def test_template_with_volume_ok(self):
        validate_fallback_config('otppspsd58', 'vol123')

    def test_missing_template_raises(self):
        with self.assertRaises(ValueError) as ctx:
            validate_fallback_config(None, None)
        self.assertIn('--fallback-template-id', str(ctx.exception))

    def test_missing_template_with_volume_raises(self):
        with self.assertRaises(ValueError):
            validate_fallback_config('', 'vol123')

    def test_error_does_not_mention_network_volume_as_required(self):
        with self.assertRaises(ValueError) as ctx:
            validate_fallback_config(None)
        msg = str(ctx.exception)
        self.assertNotIn('and --fallback-network-volume-id', msg)


class TestCloudGPUScore(unittest.TestCase):
    def test_cheaper_wins_regardless_of_stock(self):
        expensive_high_stock = _gpu('Expensive', price=2.50, stock='High')
        cheap_low_stock = _gpu('Cheap', price=0.80, stock='Low')
        self.assertLess(cheap_low_stock.score(), expensive_high_stock.score())

    def test_price_primary_vram_tiebreak(self):
        cheap_24 = _gpu('A', mem_gb=24, price=1.00, stock='High')
        cheap_48 = _gpu('B', mem_gb=48, price=1.00, stock='Low')
        self.assertLess(cheap_48.score(), cheap_24.score())

    def test_reserved_last(self):
        reserved = _gpu('Reserved', price=None)
        any_paid = _gpu('Paid', price=9.99)
        self.assertLess(any_paid.score(), reserved.score())

    def test_baseline_vram_filter_via_matches_requirements(self):
        under = _gpu('Small', mem_gb=16, price=0.50)
        over = _gpu('Big', mem_gb=48, price=2.00)
        self.assertFalse(under.matches_requirements(min_mem=24, min_vcpu=8, max_price=3.0))
        self.assertTrue(over.matches_requirements(min_mem=24, min_vcpu=8, max_price=3.0))


class TestFindMatchingGPUSOrder(unittest.TestCase):
    def test_sorts_cheapest_meeting_baseline_first(self):
        gpus = [
            _gpu('HighStock pricey', mem_gb=48, price=2.80, stock='High'),
            _gpu('Mid', mem_gb=24, price=1.40, stock='Medium'),
            _gpu('Cheapest', mem_gb=24, price=0.90, stock='Low'),
            _gpu('Too small', mem_gb=8, price=0.20, stock='High'),
            _gpu('Over budget', mem_gb=80, price=4.00, stock='High'),
        ]
        with patch.object(CloudManager, 'get_available_gpus', return_value=gpus):
            mgr = CloudManager()
            result = mgr.find_matching_gpus(min_mem=24, min_vcpu=16, max_price=3.00)

        names = [g.gpu_type for g in result]
        self.assertEqual(names, ['Cheapest', 'Mid', 'HighStock pricey'])
        self.assertEqual(result[0].gpu_type, 'Cheapest')


class TestFindSimilarGPUSOrder(unittest.TestCase):
    def test_cheaper_with_baseline_vram_first_not_exact_match(self):
        # Recreating an RTX 4090 (24GB): exact match is pricey;
        # cheaper GPU with same baseline VRAM must win.
        gpus = [
            _gpu('NVIDIA GeForce RTX 4090', mem_gb=24, price=2.20, stock='High'),
            _gpu('NVIDIA RTX A5000', mem_gb=24, price=1.10, stock='Low'),
            _gpu('NVIDIA RTX A6000', mem_gb=48, price=1.80, stock='High'),
            _gpu('NVIDIA Tesla T4', mem_gb=16, price=0.35, stock='High'),
        ]
        with patch.object(CloudManager, 'get_available_gpus', return_value=gpus):
            mgr = CloudManager()
            result = mgr.find_similar_gpus(
                target_gpu_type='NVIDIA GeForce RTX 4090',
                target_mem_gb=24,
                datacenter_id=None,
                max_price=3.00,
            )

        names = [g.gpu_type for g in result]
        self.assertEqual(names[0], 'NVIDIA RTX A5000')
        # T4 below 80% of target VRAM (19.2GB) excluded
        self.assertNotIn('NVIDIA Tesla T4', names)
        # price order among baseline-qualified: A5000 1.10, A6000 1.80, 4090 2.20
        self.assertEqual(
            [g.ondemand_price for g in result],
            sorted(g.ondemand_price for g in result),
        )

    def test_higher_vram_preferred_on_equal_price(self):
        gpus = [
            _gpu('GPU-24', mem_gb=24, price=1.00, stock='High'),
            _gpu('GPU-48', mem_gb=48, price=1.00, stock='High'),
        ]
        with patch.object(CloudManager, 'get_available_gpus', return_value=gpus):
            mgr = CloudManager()
            result = mgr.find_similar_gpus(
                target_gpu_type='GPU-24',
                target_mem_gb=24,
                datacenter_id=None,
                max_price=3.00,
            )
        self.assertEqual(result[0].gpu_type, 'GPU-48')


class TestGPUVendor(unittest.TestCase):
    def test_nvidia(self):
        self.assertEqual(gpu_vendor('NVIDIA GeForce RTX 4090'), 'nvidia')

    def test_amd(self):
        self.assertEqual(gpu_vendor('AMD Instinct MI300X OAM'), 'amd')

    def test_unknown(self):
        self.assertEqual(gpu_vendor('Something Else'), 'other')


class TestGetAvailableGpusGraphQL(unittest.TestCase):
    """Global listing must use GraphQL (real prices/IDs), not runpodctl."""

    def _graphql_response(self):
        return {
            'data': {
                'gpuTypes': [
                    {
                        'id': 'NVIDIA GeForce RTX 4090',
                        'displayName': 'RTX 4090',
                        'memoryInGb': 24,
                        'lowestPrice': {
                            'uninterruptablePrice': 0.74,
                            'minimumBidPrice': 0.2,
                            'stockStatus': 'Low',
                            'availableGpuCounts': None,
                            'minVcpu': 16,
                        },
                    },
                    {
                        'id': 'AMD Instinct MI300X OAM',
                        'displayName': 'MI300X',
                        'memoryInGb': 192,
                        'lowestPrice': {
                            'uninterruptablePrice': 2.39,
                            'minimumBidPrice': None,
                            'stockStatus': 'Low',
                            'availableGpuCounts': None,
                            'minVcpu': 24,
                        },
                    },
                    {
                        'id': 'NVIDIA GeForce RTX 5090',
                        'displayName': 'RTX 5090',
                        'memoryInGb': 32,
                        'lowestPrice': {
                            'uninterruptablePrice': None,
                            'minimumBidPrice': None,
                            'stockStatus': None,
                            'availableGpuCounts': None,
                            'minVcpu': 0,
                        },
                    },
                ]
            }
        }

    def test_uses_graphql_not_runpodctl(self):
        mgr = CloudManager()
        with patch.object(CloudManager, '_graphql_query', return_value=self._graphql_response()) as gq, \
             patch.object(CloudManager, '_run_command') as rc:
            gpus = mgr.get_available_gpus()
        rc.assert_not_called()
        gq.assert_called_once()
        names = [g.gpu_type for g in gpus]
        self.assertIn('NVIDIA GeForce RTX 4090', names)
        self.assertIn('AMD Instinct MI300X OAM', names)
        # 5090 has no price/stock -> excluded
        self.assertNotIn('NVIDIA GeForce RTX 5090', names)

    def test_real_prices_from_graphql(self):
        mgr = CloudManager()
        with patch.object(CloudManager, '_graphql_query', return_value=self._graphql_response()):
            gpus = mgr.get_available_gpus()
        by_name = {g.gpu_type: g for g in gpus}
        # MI300X must be $2.39 (real), not runpodctl's stale $0.50
        self.assertEqual(by_name['AMD Instinct MI300X OAM'].ondemand_price, 2.39)
        self.assertEqual(by_name['NVIDIA GeForce RTX 4090'].ondemand_price, 0.74)

    def test_excludes_gpu_without_price(self):
        mgr = CloudManager()
        with patch.object(CloudManager, '_graphql_query', return_value=self._graphql_response()):
            gpus = mgr.get_available_gpus()
        self.assertTrue(all(g.ondemand_price is not None for g in gpus))


class TestVendorFilterOnSimilar(unittest.TestCase):
    def test_amd_excluded_when_target_nvidia(self):
        gpus = [
            _gpu('NVIDIA GeForce RTX 4090', mem_gb=24, price=2.00, stock='High'),
            _gpu('AMD Instinct MI300X OAM', mem_gb=192, price=0.50, stock='Low'),
            _gpu('NVIDIA RTX A4500', mem_gb=20, price=1.50, stock='Low'),
        ]
        with patch.object(CloudManager, 'get_available_gpus', return_value=gpus):
            mgr = CloudManager()
            result = mgr.find_similar_gpus(
                target_gpu_type='NVIDIA GeForce RTX 4090',
                target_mem_gb=24,
                datacenter_id=None,
                max_price=3.00,
            )
        names = [g.gpu_type for g in result]
        self.assertNotIn('AMD Instinct MI300X OAM', names)
        self.assertIn('NVIDIA GeForce RTX 4090', names)

    def test_amd_kept_when_target_amd(self):
        gpus = [
            _gpu('AMD Instinct MI300X OAM', mem_gb=192, price=2.39, stock='Low'),
            _gpu('NVIDIA GeForce RTX 4090', mem_gb=24, price=0.74, stock='Low'),
        ]
        with patch.object(CloudManager, 'get_available_gpus', return_value=gpus):
            mgr = CloudManager()
            result = mgr.find_similar_gpus(
                target_gpu_type='AMD Instinct MI300X OAM',
                target_mem_gb=192,
                datacenter_id=None,
                max_price=3.00,
            )
        names = [g.gpu_type for g in result]
        self.assertIn('AMD Instinct MI300X OAM', names)
        self.assertNotIn('NVIDIA GeForce RTX 4090', names)


class TestDatacenterStockFilter(unittest.TestCase):
    """availableGpuCounts may be null while stockStatus+price present — keep those."""

    def test_keeps_gpu_when_counts_null_but_stock_set(self):
        mgr = CloudManager()
        resp = {
            'data': {
                'gpuTypes': [
                    {
                        'id': 'NVIDIA GeForce RTX 4090',
                        'displayName': 'RTX 4090',
                        'memoryInGb': 24,
                        'lowestPrice': {
                            'uninterruptablePrice': 0.74,
                            'minimumBidPrice': None,
                            'stockStatus': 'Low',
                            'availableGpuCounts': None,
                            'minVcpu': 16,
                        },
                    },
                    {
                        'id': 'NVIDIA H200',
                        'displayName': 'H200',
                        'memoryInGb': 141,
                        'lowestPrice': {
                            'uninterruptablePrice': None,
                            'minimumBidPrice': None,
                            'stockStatus': None,
                            'availableGpuCounts': None,
                            'minVcpu': 0,
                        },
                    },
                ]
            }
        }

        # all_gpu_types query + per-gpu queries: return same shape for both
        with patch.object(CloudManager, '_graphql_query', return_value=resp):
            gpus = mgr.get_available_gpus_for_datacenter('US-GA-2')

        names = [g.gpu_type for g in gpus]
        self.assertIn('NVIDIA GeForce RTX 4090', names)
        self.assertNotIn('NVIDIA H200', names)


class TestWaitForServicePort(unittest.TestCase):
    """RUNNING alone must not count as started: crash-looping container has no ports."""

    def _pod(self, status, mappings=None):
        return PodInfo(
            id='p1', name='tr-gpu-02-prod', status=status,
            public_ip=(mappings[0].public_ip if mappings else None),
            port_mappings=mappings or [],
        )

    def test_returns_mapping_when_port_exposed(self):
        mgr = RunPodManager('p1')
        mapping = PortMapping('1.2.3.4', 37853, 50051, 'tcp', 'pub')
        with patch.object(mgr, 'get_pod_info', return_value=self._pod('RUNNING', [mapping])):
            got = mgr.wait_for_service_port(container_port=50051, timeout=1)
        self.assertEqual(got.public_port, 37853)

    def test_running_without_port_raises(self):
        mgr = RunPodManager('p1')
        with patch.object(mgr, 'get_pod_info', return_value=self._pod('RUNNING', [])):
            with patch('runpod_costsaving.time.sleep', return_value=None):
                with self.assertRaises(TimeoutError) as ctx:
                    mgr.wait_for_service_port(container_port=50051, timeout=1)
        self.assertIn('crash-looping', str(ctx.exception))

    def test_ignores_non_matching_container_port(self):
        mgr = RunPodManager('p1')
        other = PortMapping('1.2.3.4', 16746, 8888, 'tcp', 'pub')
        with patch.object(mgr, 'get_pod_info', return_value=self._pod('RUNNING', [other])):
            with patch('runpod_costsaving.time.sleep', return_value=None):
                with self.assertRaises(TimeoutError):
                    mgr.wait_for_service_port(container_port=50051, timeout=1)


if __name__ == '__main__':
    unittest.main()
