#!/usr/bin/env python3
"""Tests for llm_lifecycle.py"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_costsaving import CloudGPU, PodInfo
from llm_lifecycle import (
    choose_cheapest_gpu,
    runpod_proxy_hostname,
    upsert_hetzner_cname_record,
    wait_for_public_ip,
    _request,
    start,
    update_dns,
)


class TestChooseCheapestGPU(unittest.TestCase):
    def _gpu(self, gpu_type='NVIDIA RTX A5000', mem_gb=24, vcpu=16, ondemand_price=0.50):
        return CloudGPU(
            gpu_type=gpu_type, display_name=gpu_type, mem_gb=mem_gb,
            vcpu=vcpu, spot_price=None, ondemand_price=ondemand_price,
        )

    def test_cheapest_qualifying_wins(self):
        gpus = [
            self._gpu('NVIDIA RTX A5000', mem_gb=48, ondemand_price=0.90),
            self._gpu('NVIDIA RTX A4000', mem_gb=48, ondemand_price=0.70),
            self._gpu('NVIDIA RTX A6000', mem_gb=48, ondemand_price=1.20),
        ]
        result = choose_cheapest_gpu(gpus, min_mem=48, min_vcpu=16, max_price=1.50)
        self.assertEqual(result.gpu_type, 'NVIDIA RTX A4000')

    def test_rejects_under_min_mem(self):
        gpus = [self._gpu('Small GPU', mem_gb=40, ondemand_price=0.30)]
        with self.assertRaises(RuntimeError) as ctx:
            choose_cheapest_gpu(gpus, min_mem=48, min_vcpu=16, max_price=1.50)
        self.assertIn('No eligible GPU', str(ctx.exception))

    def test_rejects_over_price_ceiling(self):
        gpus = [self._gpu('Expensive GPU', mem_gb=48, ondemand_price=2.00)]
        with self.assertRaises(RuntimeError) as ctx:
            choose_cheapest_gpu(gpus, min_mem=48, min_vcpu=16, max_price=1.50)
        self.assertIn('No eligible GPU', str(ctx.exception))

    def test_rejects_none_price(self):
        gpus = [self._gpu('Reserved GPU', mem_gb=48, ondemand_price=None)]
        with self.assertRaises(RuntimeError) as ctx:
            choose_cheapest_gpu(gpus, min_mem=48, min_vcpu=16, max_price=1.50)
        self.assertIn('No eligible GPU', str(ctx.exception))

    def test_empty_list_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            choose_cheapest_gpu([], min_mem=48, min_vcpu=16, max_price=1.50)
        self.assertIn('No eligible GPU', str(ctx.exception))

    def test_tiebreak_by_memory_desc(self):
        gpus = [
            self._gpu('GPU-A', mem_gb=48, ondemand_price=0.50),
            self._gpu('GPU-B', mem_gb=80, ondemand_price=0.50),
        ]
        result = choose_cheapest_gpu(gpus, min_mem=48, min_vcpu=16, max_price=1.50)
        self.assertEqual(result.gpu_type, 'GPU-B')

    def test_rejects_under_min_vcpu(self):
        gpus = [self._gpu('Low CPU', mem_gb=48, vcpu=8, ondemand_price=0.50)]
        with self.assertRaises(RuntimeError):
            choose_cheapest_gpu(gpus, min_mem=48, min_vcpu=16, max_price=1.50)


class TestHetznerDNS(unittest.TestCase):
    def test_upsert_creates_cname_when_absent(self):
        responses = [
            {'json': {'zones': [{'id': 1, 'name': 'techfusion.it'}]}},
            {'json': {'rrsets': []}},
            {'json': {'rrset': {'id': 'runpod/A'}}},
        ]
        calls = []
        i = [0]

        def mock_request(url, headers=None, data=None, method='GET'):
            r = responses[i[0]]
            i[0] += 1
            calls.append({'url': url, 'headers': headers, 'data': json.loads(data) if data else None, 'method': method})
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps(r['json']).encode()
            return resp

        with patch('llm_lifecycle._request', side_effect=mock_request):
            upsert_hetzner_cname_record(
                'fake-token', 'techfusion.it', 'runpod.techfusion.it',
                'pod123-8080.proxy.runpod.net.', 60
            )

        self.assertEqual(len(calls), 3)
        post_data = calls[2]['data']
        self.assertEqual(post_data['type'], 'CNAME')
        self.assertEqual(post_data['name'], 'runpod')
        self.assertEqual(post_data['records'], [{'value': 'pod123-8080.proxy.runpod.net.'}])
        self.assertEqual(post_data['ttl'], 60)
        self.assertEqual(calls[0]['headers']['Authorization'], 'Bearer fake-token')
        self.assertIn('/zones/1/rrsets', calls[1]['url'])
        self.assertEqual(calls[2]['method'], 'POST')

    def test_upsert_updates_cname_when_present(self):
        responses = [
            {'json': {'zones': [{'id': 1, 'name': 'techfusion.it'}]}},
            {'json': {'rrsets': [{'id': 'runpod/CNAME', 'type': 'CNAME', 'name': 'runpod'}]}},
            {'json': {'rrset': {'id': 'runpod/CNAME'}}},
        ]
        calls = []
        i = [0]

        def mock_request(url, headers=None, data=None, method='GET'):
            r = responses[i[0]]
            i[0] += 1
            calls.append({'url': url, 'data': json.loads(data) if data else None, 'method': method})
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps(r['json']).encode()
            return resp

        with patch('llm_lifecycle._request', side_effect=mock_request):
            upsert_hetzner_cname_record(
                'fake-token', 'techfusion.it', 'runpod.techfusion.it',
                'pod123-8080.proxy.runpod.net.', 60
            )

        self.assertEqual(len(calls), 3)
        put_data = calls[2]['data']
        self.assertEqual(put_data, {'records': [{'value': 'pod123-8080.proxy.runpod.net.'}]})
        self.assertIn('/zones/1/rrsets/runpod/CNAME/actions/set_records', calls[2]['url'])
        self.assertEqual(calls[2]['method'], 'POST')

    def test_rejects_cname_without_trailing_dot(self):
        with self.assertRaises(ValueError):
            upsert_hetzner_cname_record(
                'tok', 'techfusion.it', 'runpod.techfusion.it',
                'pod123-8080.proxy.runpod.net', 60
            )

    def test_http_error_raises(self):
        call_count = [0]

        def mock_request(url, headers=None, data=None, method='GET'):
            call_count[0] += 1
            resp = MagicMock()
            if call_count[0] == 1:
                # Zone lookup succeeds
                resp.status = 200
                resp.read.return_value = json.dumps({'zones': [{'id': 'z1'}]}).encode()
            else:
                # Record lookup or update fails
                resp.status = 401
                resp.read.return_value = b'Unauthorized'
            return resp

        with patch('llm_lifecycle._request', side_effect=mock_request):
            with self.assertRaises(RuntimeError) as ctx:
                upsert_hetzner_cname_record(
                    'tok', 'techfusion.it', 'runpod.techfusion.it',
                    'pod123-8080.proxy.runpod.net.', 60
                )
            self.assertIn('401', str(ctx.exception))

    def test_fqdn_outside_zone_raises(self):
        with self.assertRaises(ValueError):
            upsert_hetzner_cname_record(
                'tok', 'techfusion.it', 'runpod.example.com',
                'pod123-8080.proxy.runpod.net.', 60
            )

    def test_builds_proxy_hostname_with_trailing_dot(self):
        self.assertEqual(
            runpod_proxy_hostname('pod123', 8080),
            'pod123-8080.proxy.runpod.net.'
        )

    def test_rejects_invalid_proxy_port(self):
        with self.assertRaises(ValueError):
            runpod_proxy_hostname('pod123', 0)


class TestWaitForPublicIP(unittest.TestCase):
    def test_returns_when_running_with_ip(self):
        mock_cloud = MagicMock()
        pi = MagicMock()
        pi.status = 'RUNNING'
        pi.public_ip = '1.2.3.4'
        mock_cloud.get_pod_info_graphql.return_value = pi

        result = wait_for_public_ip(mock_cloud, 'pod123', timeout_seconds=10, poll_interval=0)
        self.assertEqual(result.public_ip, '1.2.3.4')

    def test_polls_until_running(self):
        mock_cloud = MagicMock()
        starting = MagicMock(); starting.status = 'STARTING'; starting.public_ip = None
        running = MagicMock(); running.status = 'RUNNING'; running.public_ip = '5.6.7.8'
        mock_cloud.get_pod_info_graphql.side_effect = [starting, running]

        result = wait_for_public_ip(mock_cloud, 'pod123', timeout_seconds=10, poll_interval=0)
        self.assertEqual(result.public_ip, '5.6.7.8')
        self.assertEqual(mock_cloud.get_pod_info_graphql.call_count, 2)

    def test_timeout_raises(self):
        mock_cloud = MagicMock()
        starting = MagicMock(); starting.status = 'STARTING'; starting.public_ip = None
        mock_cloud.get_pod_info_graphql.return_value = starting

        with self.assertRaises(TimeoutError) as ctx:
            wait_for_public_ip(mock_cloud, 'pod123', timeout_seconds=0, poll_interval=0)
        self.assertIn('pod123', str(ctx.exception))


class TestStart(unittest.TestCase):
    def _config(self, pod_id_file):
        return {
            'LLM_TEMPLATE_ID': 'template-1',
            'LLM_POD_NAME': 'llm-runpod',
            'LLM_POD_ID_FILE': pod_id_file,
            'LLM_MIN_GPU_MEM': 48,
            'LLM_MIN_VCPU': 16,
            'LLM_MAX_ONDEMAND_PRICE': 1.50,
            'LLM_START_TIMEOUT_SECONDS': 600,
            'LLM_CREATE_TIMEOUT_SECONDS': 1,
            'LLM_HTTP_PORT': 8080,
            'HETZNER_DNS_ZONE_NAME': 'techfusion.it',
            'HETZNER_DNS_RECORD_NAME': 'runpod.techfusion.it',
            'HETZNER_DNS_TTL': 60,
            'HETZNER_DNS_API_TOKEN': 'token',
            'HETZNER_DNS_API_BASE_URL': 'https://dns.example.test/api/v1',
        }

    def _gpu(self, gpu_type, price):
        return CloudGPU(gpu_type, gpu_type, 48, 16, None, price)

    def test_running_pod_dns_failure_does_not_create_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            pod_id_file = os.path.join(directory, 'pod_id')
            with open(pod_id_file, 'w') as f:
                f.write('running-pod\n')
            cloud = MagicMock()
            cloud.get_pod_info_graphql.return_value = PodInfo(
                id='running-pod', name='llm-runpod', status='RUNNING',
                public_ip='1.2.3.4', port_mappings=[]
            )

            with patch('llm_lifecycle._load_config', return_value=self._config(pod_id_file)), \
                 patch('llm_lifecycle.CloudManager', return_value=cloud), \
                 patch('llm_lifecycle.upsert_hetzner_cname_record', side_effect=RuntimeError('DNS unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'DNS unavailable'):
                    start()

            cloud.create_pod_graphql.assert_not_called()

    def test_capacity_failure_tries_next_cheapest_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            pod_id_file = os.path.join(directory, 'pod_id')
            cloud = MagicMock()
            cloud.get_available_gpus.return_value = [
                self._gpu('cheap-gpu', 0.20),
                self._gpu('fallback-gpu', 0.30),
            ]
            cloud.create_pod_graphql.side_effect = [
                Exception('GraphQL error: There are no longer any instances available'),
                'new-pod',
            ]
            running = PodInfo(
                id='new-pod', name='llm-runpod', status='RUNNING',
                public_ip='5.6.7.8', port_mappings=[]
            )

            with patch('llm_lifecycle._load_config', return_value=self._config(pod_id_file)), \
                 patch('llm_lifecycle.CloudManager', return_value=cloud), \
                 patch('llm_lifecycle.wait_for_public_ip', return_value=running), \
                  patch('llm_lifecycle.upsert_hetzner_cname_record'):
                start()

            self.assertEqual(cloud.create_pod_graphql.call_count, 2)
            self.assertEqual(cloud.create_pod_graphql.call_args_list[0].kwargs['gpu_type_id'], 'cheap-gpu')
            self.assertEqual(cloud.create_pod_graphql.call_args_list[1].kwargs['gpu_type_id'], 'fallback-gpu')
            with open(pod_id_file) as f:
                self.assertEqual(f.read(), 'new-pod\n')


class TestUpdateDNS(unittest.TestCase):
    def test_updates_dns_for_tracked_running_pod_without_creating_one(self):
        with tempfile.TemporaryDirectory() as directory:
            pod_id_file = os.path.join(directory, 'pod_id')
            with open(pod_id_file, 'w') as f:
                f.write('running-pod\n')
            config = TestStart()._config(pod_id_file)
            cloud = MagicMock()
            cloud.get_pod_info_graphql.return_value = PodInfo(
                id='running-pod', name='llm-runpod', status='RUNNING',
                public_ip=None, port_mappings=[]
            )

            with patch('llm_lifecycle._load_config', return_value=config), \
                 patch('llm_lifecycle.CloudManager', return_value=cloud), \
                 patch('llm_lifecycle.upsert_hetzner_cname_record') as upsert:
                update_dns()

            upsert.assert_called_once_with(
                'token', 'techfusion.it', 'runpod.techfusion.it',
                'running-pod-8080.proxy.runpod.net.', 60,
                'https://dns.example.test/api/v1'
            )
            cloud.create_pod_graphql.assert_not_called()

    def test_rejects_missing_tracked_pod(self):
        with tempfile.TemporaryDirectory() as directory:
            config = TestStart()._config(os.path.join(directory, 'missing'))
            with patch('llm_lifecycle._load_config', return_value=config):
                with self.assertRaisesRegex(RuntimeError, 'No tracked pod'):
                    update_dns()


class TestTerminate(unittest.TestCase):
    def test_missing_id_file_is_success(self):
        pass  # Full terminate test after implementation exists


if __name__ == '__main__':
    unittest.main()
