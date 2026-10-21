import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402
from lambda_function import alert_rule_uid, convert_rules_to_json  # noqa: E402

AMP_RULES = """
groups:
  - name: test
    rules:
      - alert: GPUHighTemperature
        expr: DCGM_FI_DEV_GPU_TEMP > 80
        labels:
          severity: warning
        annotations:
          description: "GPU {{ $labels.gpu }}"
"""

CLOUDWATCH_RULES = """
groups:
  - name: test
    rules:
      - alert: GPUHighTemperature
        expr: 'DCGM_FI_DEV_GPU_TEMP{"@resource.aws.sagemaker.hyperpod.cluster.name"="__HYPERPOD_CLUSTER_NAME__"} > 80'
        annotations:
          description: 'on {{ index $labels "@resource.host.name" }}'
"""

ENV = {
    'GRAFANA_WORKSPACE_ID': 'g-abc',
    'PROMETHEUS_WORKSPACE_ID': 'ws-123',
    'GRAFANA_WORKSPACE_TOKEN_KEY': 'token',
    'REGION': 'us-west-2',
    'S3_BUCKET': 'bucket',
    'DASHBOARD_PREFIX_S3_KEY': 'prefix',
}


FOLDER = 'aws-sm-hp-observability-rules'


def listed_rule(uid, title='GPUHighTemperature', datasource='prometheus', folder=FOLDER):
    return {'uid': uid, 'title': title, 'folderUID': folder, 'data': [{'datasourceUid': datasource}]}


def response(status, body=None):
    r = MagicMock()
    r.status = status
    r.data = json.dumps(body or {}).encode('utf-8')
    return r


class TestAlertRules(unittest.TestCase):
    def setUp(self):
        self.assets = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.assets.name, 'alerts'))
        self._write('alert-rules.yaml', AMP_RULES)
        self._write('alert-rules-cloudwatch.yaml', CLOUDWATCH_RULES)
        self.download = patch.object(lambda_function, 'download_and_extract_assets', return_value=self.assets.name)
        self.download.start()

    def tearDown(self):
        self.download.stop()
        self.assets.cleanup()

    def _write(self, name, content):
        with open(os.path.join(self.assets.name, 'alerts', name), 'w', encoding='utf-8') as f:
            f.write(content)

    def _rules(self, **env):
        with patch.dict(os.environ, {**ENV, **env}, clear=True):
            return convert_rules_to_json()

    def test_amp_rules_query_prometheus(self):
        [rule] = self._rules()

        self.assertEqual(rule['data'][0]['datasourceUid'], 'prometheus')
        self.assertEqual(rule['data'][0]['model']['expr'], 'DCGM_FI_DEV_GPU_TEMP > 80')
        self.assertEqual(rule['ruleGroup'], 'test')
        self.assertEqual(rule['uid'], 'hp-gpu-high-temperature')
        self.assertEqual(rule['labels'], {'severity': 'warning'})
        self.assertEqual(rule['annotations'], {'description': 'GPU {{ $labels.gpu }}'})

    def test_without_amp_the_cloudwatch_rules_are_scoped_to_the_cluster(self):
        [rule] = self._rules(PROMETHEUS_WORKSPACE_ID='', HYPERPOD_CLUSTER_NAME='hp-cluster-1')

        # Per cluster, since several clusters can share a workspace
        self.assertEqual(rule['uid'], 'hp-gpu-high-temperature-0b9d912c')
        self.assertEqual(rule['title'], 'GPUHighTemperature (hp-cluster-1)')
        self.assertEqual(rule['data'][0]['datasourceUid'], 'cloudwatch-opentelemetry')
        self.assertEqual(
            rule['data'][0]['model']['expr'],
            'DCGM_FI_DEV_GPU_TEMP{"@resource.aws.sagemaker.hyperpod.cluster.name"="hp-cluster-1"} > 80',
        )
        self.assertEqual(rule['annotations'], {'description': 'on {{ index $labels "@resource.host.name" }}'})

    def test_without_amp_or_cluster_name_no_rules_are_created(self):
        self.assertEqual(self._rules(PROMETHEUS_WORKSPACE_ID=''), [])

    def test_without_amp_or_cloudwatch_rules_file_no_rules_are_created(self):
        os.remove(os.path.join(self.assets.name, 'alerts', 'alert-rules-cloudwatch.yaml'))

        self.assertEqual(self._rules(PROMETHEUS_WORKSPACE_ID='', HYPERPOD_CLUSTER_NAME='hp-cluster-1'), [])

    def _create_rules(self, existing_rules, **env):
        requests = []

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            requests.append((endpoint, method, payload))
            if method == 'GET':
                return response(200, existing_rules)
            return response(201 if method == 'POST' else 200, {'id': 1})

        with patch.dict(os.environ, {**ENV, **env}, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', fake_request):
            results = lambda_function.create_alert_rules()
        return requests, results

    def test_rule_created_before_fixed_uids_is_updated_in_place(self):
        requests, [result] = self._create_rules([
            listed_rule('random-uid'),
        ])

        endpoint, method, payload = requests[1]
        self.assertEqual((endpoint, method), ('v1/provisioning/alert-rules/random-uid', 'PUT'))
        self.assertEqual(payload['uid'], 'random-uid')
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['status'], 'updated')

    def test_one_copy_is_kept_and_updated_and_the_others_deleted(self):
        requests, [result] = self._create_rules([
            listed_rule('copy-1'),
            listed_rule('hp-gpu-high-temperature'),
            listed_rule('copy-2'),
        ])

        # The fixed uid is kept
        self.assertEqual(
            [(e, m) for e, m, _ in requests[1:]],
            [
                ('v1/provisioning/alert-rules/hp-gpu-high-temperature', 'PUT'),
                ('v1/provisioning/alert-rules/copy-1', 'DELETE'),
                ('v1/provisioning/alert-rules/copy-2', 'DELETE'),
            ],
        )
        self.assertEqual(result['status'], 'updated')

    def test_cloudwatch_rule_replaces_the_unscoped_cloudwatch_rule_for_the_same_alert(self):
        requests, _ = self._create_rules(
            [listed_rule('unscoped-uid', datasource='cloudwatch-opentelemetry')],
            PROMETHEUS_WORKSPACE_ID='', HYPERPOD_CLUSTER_NAME='hp-cluster-1',
        )

        self.assertEqual(
            [(e, m) for e, m, _ in requests[1:]],
            [('v1/provisioning/alert-rules', 'POST'), ('v1/provisioning/alert-rules/unscoped-uid', 'DELETE')],
        )
        self.assertEqual(requests[1][2]['title'], 'GPUHighTemperature (hp-cluster-1)')

    def test_cloudwatch_cleanup_keeps_an_amp_clusters_rule_with_the_same_title(self):
        # An AMP cluster sharing the workspace
        requests, _ = self._create_rules(
            [listed_rule('hp-gpu-high-temperature', datasource='prometheus')],
            PROMETHEUS_WORKSPACE_ID='', HYPERPOD_CLUSTER_NAME='hp-cluster-1',
        )

        self.assertEqual([(e, m) for e, m, _ in requests[1:]], [('v1/provisioning/alert-rules', 'POST')])

    def test_amp_upsert_ignores_a_cloudwatch_rule_with_the_same_title(self):
        requests, _ = self._create_rules([listed_rule('cloudwatch-uid', datasource='cloudwatch-opentelemetry')])

        self.assertEqual([(e, m) for e, m, _ in requests[1:]], [('v1/provisioning/alert-rules', 'POST')])
        self.assertEqual(requests[1][2]['uid'], 'hp-gpu-high-temperature')

    def test_rules_are_still_created_when_listing_fails(self):
        requests = []

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            requests.append((endpoint, method))
            if method == 'GET':
                raise Exception('connection reset')
            return response(201, {'id': 1})

        with patch.dict(os.environ, ENV, clear=True), patch.object(lambda_function, 'make_grafana_request', fake_request):
            [result] = lambda_function.create_alert_rules()

        self.assertEqual([m for _, m in requests], ['GET', 'POST'])
        self.assertEqual(result['status'], 'created')

    def test_replaced_rule_is_kept_when_creating_the_new_rule_fails(self):
        requests = []

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            requests.append((endpoint, method))
            if method == 'GET':
                return response(200, [listed_rule('unscoped-uid', datasource='cloudwatch-opentelemetry')])
            return response(500)

        with patch.dict(os.environ, {**ENV, 'PROMETHEUS_WORKSPACE_ID': '', 'HYPERPOD_CLUSTER_NAME': 'hp-cluster-1'},
                        clear=True), patch.object(lambda_function, 'make_grafana_request', fake_request):
            [result] = lambda_function.create_alert_rules()

        self.assertEqual([m for _, m in requests], ['GET', 'POST'])
        self.assertEqual(result['status'], 500)

    def test_another_clusters_cloudwatch_rule_is_left_alone(self):
        requests, _ = self._create_rules(
            [listed_rule('hp-gpu-high-temperature-other', title='GPUHighTemperature (hp-cluster-2)',
                         datasource='cloudwatch-opentelemetry')],
            PROMETHEUS_WORKSPACE_ID='', HYPERPOD_CLUSTER_NAME='hp-cluster-1',
        )

        self.assertEqual([(e, m) for e, m, _ in requests[1:]], [('v1/provisioning/alert-rules', 'POST')])

    def test_rule_with_the_same_title_in_another_folder_is_ignored(self):
        requests, _ = self._create_rules([
            listed_rule('other-uid', folder='customer-folder'),
        ])

        self.assertEqual([(e, m) for e, m, _ in requests[1:]], [('v1/provisioning/alert-rules', 'POST')])
        self.assertEqual(requests[1][2]['uid'], 'hp-gpu-high-temperature')

    def test_unreadable_rules_are_reported_in_the_results(self):
        self._write('alert-rules.yaml', 'groups: [')

        requests, [result] = self._create_rules([])

        self.assertEqual(requests, [])
        self.assertIn('Alert rules processing failed', result['message'])

    def test_existing_rule_is_reported_as_existing(self):
        with patch.dict(os.environ, ENV, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', return_value=response(409)):
            [result] = lambda_function.create_alert_rules()

        self.assertEqual(result['status'], 'existing')


class TestAlertRuleUid(unittest.TestCase):
    def test_cloudwatch_uids_carry_a_cluster_hash_within_the_grafana_uid_limit(self):
        self.assertEqual(alert_rule_uid('GPUHighTemperature', 'hp-cluster-1'), 'hp-gpu-high-temperature-0b9d912c')
        self.assertLessEqual(len(alert_rule_uid('FsxLustreStorageWarning', 'x' * 63)), 40)

    def test_matches_the_console_uids(self):
        self.assertEqual(
            [alert_rule_uid(a) for a in [
                'GPUHighTemperature', 'GPUCriticalTemperature', 'GPUMemoryErrorDetected', 'NVLinkErrorsDetected',
                'GPUThermalViolation', 'MIGConfigFailure', 'GPUXidError', 'NodeDiskSpaceWarning',
                'FsxLustreStorageWarning',
            ]],
            [
                'hp-gpu-high-temperature', 'hp-gpu-critical-temperature', 'hp-gpu-memory-error-detected',
                'hp-nv-link-errors-detected', 'hp-gpu-thermal-violation', 'hp-mig-config-failure',
                'hp-gpu-xid-error', 'hp-node-disk-space-warning', 'hp-fsx-lustre-storage-warning',
            ],
        )


class TestDatasources(unittest.TestCase):
    def _created_datasources(self, **env):
        requests = []

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            requests.append((endpoint, method, payload))
            return response(200, {'id': 1})

        with patch.dict(os.environ, {**ENV, **env}, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', fake_request), \
                patch.object(lambda_function, 'create_folder', return_value={}), \
                patch.object(lambda_function, 'create_dashboard', return_value={}), \
                patch.object(lambda_function, 'create_alert_rules', return_value=[]), \
                patch.object(lambda_function, 'cleanup_service_account'):
            lambda_function.on_create()

        return [payload for endpoint, method, payload in requests if endpoint == 'datasources']

    def test_amp_creates_prometheus_and_not_cloudwatch_opentelemetry(self):
        self.assertEqual([ds['uid'] for ds in self._created_datasources()], ['cloudwatch', 'prometheus'])

    def test_without_amp_creates_cloudwatch_opentelemetry_instead_of_prometheus(self):
        datasources = self._created_datasources(PROMETHEUS_WORKSPACE_ID='')

        self.assertEqual([ds['uid'] for ds in datasources], ['cloudwatch', 'cloudwatch-opentelemetry'])
        cloudwatch_opentelemetry = datasources[1]
        self.assertEqual(cloudwatch_opentelemetry['type'], 'grafana-amazonprometheus-datasource')
        self.assertEqual(cloudwatch_opentelemetry['url'], 'https://monitoring.us-west-2.amazonaws.com')
        self.assertEqual(cloudwatch_opentelemetry['jsonData']['sigv4Service'], 'monitoring')
        self.assertFalse(cloudwatch_opentelemetry['isDefault'])


if __name__ == '__main__':
    unittest.main()
