#!/usr/bin/env python3
"""Tests for CloudManager.find_booking_candidates."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_costsaving import CloudManager, CloudGPU


def _gpu(gpu_type, mem_gb, vcpu, price, cloud="SECURE"):
    return CloudGPU(
        gpu_type=gpu_type, display_name=gpu_type, mem_gb=mem_gb,
        vcpu=vcpu, spot_price=None, ondemand_price=price,
        stock_status="Low", cloud=cloud,
    )


def _graphql_response(gpus):
    """Build gpuTypes payload as returned by get_available_gpus query."""
    return {
        'data': {
            'gpuTypes': [
                {
                    'id': g.gpu_type,
                    'displayName': g.display_name,
                    'memoryInGb': g.mem_gb,
                    'lowestPrice': {
                        'uninterruptablePrice': g.ondemand_price,
                        'minimumBidPrice': None,
                        'stockStatus': g.stock_status,
                        'availableGpuCounts': None,
                        'minVcpu': g.vcpu,
                    },
                }
                for g in gpus
            ]
        }
    }


class TestFindBookingCandidates(unittest.TestCase):
    def setUp(self):
        self.mgr = CloudManager()

    def _run(self, gpus, **kwargs):
        with patch.object(CloudManager, '_graphql_query',
                          return_value=_graphql_response(gpus)):
            return self.mgr.find_booking_candidates(**kwargs)

    def test_preferred_gpu_sorted_first_despite_higher_price(self):
        gpus = [
            _gpu('NVIDIA RTX 5090', 32, 16, 1.19),
            _gpu('NVIDIA A100-SXM4-80GB', 80, 32, 1.79),
            _gpu('NVIDIA RTX PRO 6000 Blackwell Server Edition', 96, 16, 2.49),
        ]
        result = self._run(gpus, min_mem=80, min_vcpu=16, max_price=3.00,
                           preferred_gpu='NVIDIA A100-SXM4-80GB')
        self.assertEqual(result[0].gpu_type, 'NVIDIA A100-SXM4-80GB')
        self.assertEqual(len(result), 2)  # 5090 excluded: mem < 80

    def test_hard_min_mem_excludes_smaller_skus(self):
        gpus = [
            _gpu('NVIDIA RTX 4090', 24, 16, 2.00),
            _gpu('NVIDIA RTX PRO 6000 Blackwell Server Edition', 96, 16, 2.49),
        ]
        result = self._run(gpus, min_mem=80, min_vcpu=16, max_price=3.00,
                           preferred_gpu='NVIDIA RTX 4090')
        self.assertEqual([g.gpu_type for g in result],
                         ['NVIDIA RTX PRO 6000 Blackwell Server Edition'])

    def test_hard_min_vcpu_excludes_smaller_skus(self):
        gpus = [
            _gpu('NVIDIA RTX A6000', 48, 9, 0.59),
            _gpu('NVIDIA Big', 96, 16, 2.00),
        ]
        result = self._run(gpus, min_mem=48, min_vcpu=16, max_price=3.00,
                           preferred_gpu='NVIDIA RTX A6000')
        self.assertEqual([g.gpu_type for g in result], ['NVIDIA Big'])

    def test_max_price_ceiling(self):
        gpus = [
            _gpu('NVIDIA H100 80GB HBM3', 80, 24, 3.99),
            _gpu('NVIDIA L4', 24, 4, 0.80),
        ]
        result = self._run(gpus, min_mem=24, min_vcpu=4, max_price=3.00,
                           preferred_gpu='NVIDIA H100 80GB HBM3')
        self.assertEqual([g.gpu_type for g in result], ['NVIDIA L4'])

    def test_no_vendor_cross(self):
        gpus = [
            _gpu('AMD Instinct MI300X', 192, 64, 2.50),
            _gpu('NVIDIA RTX PRO 6000 Blackwell Server Edition', 96, 16, 2.49),
        ]
        result = self._run(gpus, min_mem=80, min_vcpu=16, max_price=3.00,
                           preferred_gpu='NVIDIA A100-SXM4-80GB')
        self.assertEqual([g.gpu_type for g in result],
                         ['NVIDIA RTX PRO 6000 Blackwell Server Edition'])

    def test_rest_sorted_cheapest_first(self):
        gpus = [
            _gpu('GPU-C', 96, 16, 2.50),
            _gpu('GPU-B', 96, 16, 1.50),
            _gpu('GPU-A', 96, 16, 2.00),
        ]
        result = self._run(gpus, min_mem=80, min_vcpu=16, max_price=3.00,
                           preferred_gpu=None)
        self.assertEqual([g.gpu_type for g in result],
                         ['GPU-B', 'GPU-A', 'GPU-C'])

    def test_none_price_excluded(self):
        gpus = [_gpu('Reserved', 96, 16, None)]
        result = self._run(gpus, min_mem=80, min_vcpu=16, max_price=3.00,
                           preferred_gpu=None)
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
