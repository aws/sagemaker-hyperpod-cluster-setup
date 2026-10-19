"""
Unit tests for Grafana 12+/13 compatibility in the observability Grafana resources Lambda function
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402

ENV = {
    'GRAFANA_WORKSPACE_ID': 'g-abc',
    'PROMETHEUS_WORKSPACE_ID': 'ws-123',
    'GRAFANA_WORKSPACE_TOKEN_KEY': 'token',
    'REGION': 'us-west-2',
    'S3_BUCKET': 'bucket',
    'DASHBOARD_PREFIX_S3_KEY': 'prefix',
}


def response(status, body=None):
    r = MagicMock()
    r.status = status
    r.data = json.dumps(body or {}).encode('utf-8')
    return r


class TestPrometheusDatasourceType(unittest.TestCase):
    def _created_type(self, plugin_status):
        calls = []

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            calls.append((endpoint, method, payload))
            if endpoint.startswith('plugins/'):
                return response(plugin_status)
            return response(200, {'id': 1})

        with patch.dict(os.environ, ENV, clear=True), patch.object(lambda_function, 'make_grafana_request', fake_request):
            lambda_function.create_prometheus_datasource()
        return next(p for e, m, p in calls if e == 'datasources')['type']

    def test_uses_amazon_prometheus_plugin_when_installed(self):
        self.assertEqual(self._created_type(200), 'grafana-amazonprometheus-datasource')

    def test_falls_back_to_core_prometheus_when_plugin_missing(self):
        self.assertEqual(self._created_type(404), 'prometheus')

    def test_plugin_check_failure_raises_before_creating_datasource(self):
        calls = []

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            calls.append(endpoint)
            return response(503) if endpoint.startswith('plugins/') else response(200, {'id': 1})

        with patch.dict(os.environ, ENV, clear=True), patch.object(lambda_function, 'make_grafana_request', fake_request):
            with self.assertRaisesRegex(Exception, 'grafana-amazonprometheus-datasource.*503'):
                lambda_function.create_prometheus_datasource()

        self.assertNotIn('datasources', calls)


class TestPluginCheckFailureFailsStack(unittest.TestCase):
    def test_on_create_fails_with_serializable_reason_and_skips_dashboards(self):
        def fake_request(endpoint, method, payload=None, additional_headers=None):
            return response(503) if endpoint.startswith('plugins/') else response(200, {'id': 1})

        send = lambda_function.cfnresponse.send
        send.reset_mock()
        with patch.dict(os.environ, ENV, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', fake_request), \
                patch.object(lambda_function, 'cleanup_service_account'), \
                patch.object(lambda_function, 'create_dashboard') as create_dashboard, \
                patch.object(lambda_function, 'create_alert_rules') as create_alert_rules:
            lambda_function.lambda_handler({'RequestType': 'Create'}, None)

        _, _, status, data = send.call_args[0]
        self.assertIs(status, lambda_function.cfnresponse.FAILED)
        self.assertIn('503', json.loads(json.dumps(data))['Reason'])
        create_dashboard.assert_not_called()
        create_alert_rules.assert_not_called()


class TestGrafanaHttpClient(unittest.TestCase):
    def test_requests_are_bounded_and_retry_transient_statuses(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(lambda_function.urllib3, 'PoolManager') as pool:
            pool.return_value.request.return_value = response(200)
            lambda_function.make_grafana_request('health', 'GET')

        kwargs = pool.call_args.kwargs
        self.assertEqual(kwargs['timeout'].read_timeout, 30)
        self.assertIn(503, kwargs['retries'].status_forcelist)
        self.assertFalse(kwargs['retries'].raise_on_status)


class TestAlertRuleGroup(unittest.TestCase):
    def test_rules_carry_the_yaml_group_name(self):
        tmp = os.path.join(os.path.dirname(__file__), '_assets')
        os.makedirs(os.path.join(tmp, 'alerts'), exist_ok=True)
        with open(os.path.join(tmp, 'alerts', 'alert-rules.yaml'), 'w') as f:
            f.write('groups:\n- name: sagemaker_hyperpod_alerts\n  rules:\n  - alert: A\n    expr: up == 0\n')
        try:
            with patch.dict(os.environ, {'PROMETHEUS_WORKSPACE_ID': 'ws-123'}), \
                    patch.object(lambda_function, 'download_and_extract_assets', return_value=tmp):
                rules = lambda_function.convert_rules_to_json()
        finally:
            os.remove(os.path.join(tmp, 'alerts', 'alert-rules.yaml'))
            os.rmdir(os.path.join(tmp, 'alerts'))
            os.rmdir(tmp)

        self.assertEqual([r['ruleGroup'] for r in rules], ['sagemaker_hyperpod_alerts'])


if __name__ == '__main__':
    unittest.main()
