"""Unit tests for the Slurm dashboard templates the observability Grafana resources Lambda function loads."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402


class TestSlurmDashboardTemplates(unittest.TestCase):
    def setUp(self):
        self.assets = tempfile.mkdtemp()
        for folder in ('slurmTemplates', 'slurmCloudwatchTemplates'):
            os.makedirs(os.path.join(self.assets, 'dashboards', folder))
            with open(os.path.join(self.assets, 'dashboards', folder, 'clusters.json'), 'w') as f:
                json.dump({'dashboard': {'uid': 'aws-sm-hp-obs-slurm-clusters-v1_0', 'title': folder}}, f)
        patch('lambda_function.download_and_extract_assets', return_value=self.assets).start()
        self.request = patch('lambda_function.make_grafana_request', return_value=Mock(status=200, data=b'{}')).start()

    def tearDown(self):
        patch.stopall()

    def created_title(self, prometheus_workspace_id):
        with patch.dict(os.environ, {'ORCHESTRATOR_TYPE': 'slurm', 'PROMETHEUS_WORKSPACE_ID': prometheus_workspace_id}):
            lambda_function.create_dashboard('clusters')
        return self.request.call_args[0][2]['dashboard']['title']

    def test_loads_the_amp_dashboards_with_a_prometheus_workspace(self):
        self.assertEqual(self.created_title('ws-123'), 'slurmTemplates')

    def test_loads_the_cloudwatch_dashboards_without_a_prometheus_workspace(self):
        self.assertEqual(self.created_title(''), 'slurmCloudwatchTemplates')

    def test_installs_no_dashboards_for_cloudwatch_with_assets_from_before_the_cloudwatch_ones(self):
        os.remove(os.path.join(self.assets, 'dashboards', 'slurmCloudwatchTemplates', 'clusters.json'))

        with patch.dict(os.environ, {'ORCHESTRATOR_TYPE': 'slurm', 'PROMETHEUS_WORKSPACE_ID': ''}):
            result = lambda_function.create_dashboard('clusters')

        # The AMP dashboards don't read CloudWatch; the console sets the CloudWatch ones up.
        self.assertEqual(result['status'], 'skipped')
        self.request.assert_not_called()


ENV = {
    'GRAFANA_WORKSPACE_ID': 'g-abc',
    'GRAFANA_WORKSPACE_TOKEN_KEY': 'token',
    'REGION': 'us-west-2',
    'S3_BUCKET': 'bucket',
    'DASHBOARD_PREFIX_S3_KEY': 'prefix',
    'ORCHESTRATOR_TYPE': 'slurm',
}
CLOUDWATCH_DASHBOARD = {'uid': 'aws-sm-hp-obs-slurm-clusters-v1_0', 'title': 'Clusters',
                        'panels': [{'targets': [{'expr': 'up{"@resource.cluster"="c"}'}]}]}
AMP_DASHBOARD = {'uid': 'aws-sm-hp-obs-slurm-clusters-v1_0', 'title': 'Clusters',
                 'panels': [{'targets': [{'expr': 'up{cluster="c"}'}]}]}


class TestSlurmDashboardConflict(unittest.TestCase):
    """The AMP and CloudWatch Slurm dashboards share uids: the stack never overwrites the other store's set."""

    def setUp(self):
        self.assets = tempfile.mkdtemp()
        for folder, dashboard in (('slurmTemplates', AMP_DASHBOARD), ('slurmCloudwatchTemplates', CLOUDWATCH_DASHBOARD)):
            os.makedirs(os.path.join(self.assets, 'dashboards', folder))
            with open(os.path.join(self.assets, 'dashboards', folder, 'clusters.json'), 'w') as f:
                json.dump({'dashboard': dashboard}, f)
        patch('lambda_function.download_and_extract_assets', return_value=self.assets).start()
        patch('lambda_function.cleanup_service_account').start()
        patch('lambda_function.create_alert_rules', return_value=[]).start()
        self.requests = []
        self.installed = None

    def tearDown(self):
        patch.stopall()

    def fake_request(self, endpoint, method, payload=None, additional_headers=None):
        self.requests.append((method, endpoint))
        if endpoint.startswith('dashboards/uid/'):
            if self.installed is None:
                return Mock(status=404, data=b'{}')
            return Mock(status=200, data=json.dumps({'dashboard': self.installed}).encode())
        return Mock(status=200, data=b'{"id": 1}')

    def on_create(self, prometheus_workspace_id, conflict_fails='true'):
        env = {**ENV, 'PROMETHEUS_WORKSPACE_ID': prometheus_workspace_id}
        if conflict_fails is not None:
            env['SLURM_DASHBOARD_CONFLICT_FAILS'] = conflict_fails
        with patch.dict(os.environ, env, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', self.fake_request):
            return lambda_function.on_create()

    def writes(self):
        return [r for r in self.requests if r[0] != 'GET']

    def test_fails_without_writing_anything_when_cloudwatch_finds_the_amp_dashboards(self):
        self.installed = AMP_DASHBOARD

        result = self.on_create('')

        self.assertEqual(result['Status'], 'FAILED')
        self.assertIn('Clusters (aws-sm-hp-obs-slurm-clusters-v1_0)', result['Reason'])
        self.assertIn('Amazon Managed Service for Prometheus metrics', result['Reason'])
        self.assertEqual(self.writes(), [])

    def test_fails_when_amp_finds_the_cloudwatch_dashboards(self):
        self.installed = CLOUDWATCH_DASHBOARD

        result = self.on_create('ws-123')

        self.assertEqual(result['Status'], 'FAILED')
        self.assertIn('for CloudWatch metrics', result['Reason'])
        self.assertEqual(self.writes(), [])

    def test_updates_its_own_set(self):
        self.installed = CLOUDWATCH_DASHBOARD

        with patch.object(lambda_function, 'get_prometheus_datasource_type',
                          return_value=lambda_function.AMAZON_PROMETHEUS_PLUGIN_ID):
            result = self.on_create('')

        self.assertEqual(result['Status'], 'SUCCESS')
        self.assertIn(('POST', 'dashboards/db'), self.requests)

    def test_creates_dashboards_the_workspace_does_not_have(self):
        result = self.on_create('ws-123')

        self.assertEqual(result['Status'], 'SUCCESS')
        self.assertIn(('GET', 'dashboards/uid/aws-sm-hp-obs-slurm-clusters-v1_0'), self.requests)
        self.assertIn(('POST', 'dashboards/db'), self.requests)

    def test_fails_when_the_dashboard_cannot_be_read(self):
        def fake_request(endpoint, method, payload=None, additional_headers=None):
            self.requests.append((method, endpoint))
            return Mock(status=503, data=b'{}')

        self.fake_request = fake_request
        result = self.on_create('ws-123')

        self.assertEqual(result['Status'], 'FAILED')
        self.assertIn('503', result['Reason'])
        self.assertEqual(self.writes(), [])

    def clusters_dashboard(self, result):
        [clusters] = [d for d in result['resources']['dashboards'] if d['template'] == 'clusters']
        return clusters

    def test_skips_a_conflicting_dashboard_on_the_node_agent_path(self):
        # Without the flag the stack runs alongside the cluster, so it must not fail.
        self.installed = AMP_DASHBOARD
        for conflict_fails in (None, 'false'):
            self.requests = []

            with patch.object(lambda_function, 'get_prometheus_datasource_type',
                              return_value=lambda_function.AMAZON_PROMETHEUS_PLUGIN_ID):
                result = self.on_create('', conflict_fails)

            self.assertEqual(result['Status'], 'SUCCESS')
            clusters = self.clusters_dashboard(result)
            self.assertEqual(clusters['status'], 'skipped')
            self.assertIn('Amazon Managed Service for Prometheus metrics', clusters['reason'])
            self.assertNotIn(('POST', 'dashboards/db'), self.requests)
            # The other dashboards continue (the assets here have no CloudWatch jobs dashboard, so it skips itself).
            self.assertEqual([d['template'] for d in result['resources']['dashboards']], ['clusters', 'jobs'])
            self.assertNotIn('reason', result['resources']['dashboards'][1])

    def test_skips_a_dashboard_it_cannot_read_on_the_node_agent_path(self):
        def fake_request(endpoint, method, payload=None, additional_headers=None):
            self.requests.append((method, endpoint))
            if endpoint.startswith('dashboards/uid/'):
                return Mock(status=503, data=b'{}')
            return Mock(status=200, data=b'{"id": 1}')

        self.fake_request = fake_request
        result = self.on_create('ws-123', None)

        self.assertEqual(result['Status'], 'SUCCESS')
        clusters = self.clusters_dashboard(result)
        self.assertEqual(clusters['status'], 'skipped')
        self.assertIn('503', clusters['reason'])
        self.assertNotIn(('POST', 'dashboards/db'), self.requests)

    def test_skips_a_dashboard_whose_read_raises_on_the_node_agent_path(self):
        def fake_request(endpoint, method, payload=None, additional_headers=None):
            self.requests.append((method, endpoint))
            if endpoint.startswith('dashboards/uid/'):
                raise Exception('connection reset')
            return Mock(status=200, data=b'{"id": 1}')

        self.fake_request = fake_request
        result = self.on_create('ws-123', 'false')

        self.assertEqual(result['Status'], 'SUCCESS')
        self.assertIn('connection reset', self.clusters_dashboard(result)['reason'])

    def test_does_not_check_amp_dashboards_with_assets_from_before_the_cloudwatch_ones(self):
        os.remove(os.path.join(self.assets, 'dashboards', 'slurmCloudwatchTemplates', 'clusters.json'))
        self.installed = CLOUDWATCH_DASHBOARD

        result = self.on_create('ws-123')

        # One set in the assets: overwritten as before.
        self.assertEqual(result['Status'], 'SUCCESS')
        self.assertNotIn(('GET', 'dashboards/uid/aws-sm-hp-obs-slurm-clusters-v1_0'), self.requests)

    def test_does_not_check_eks_dashboards(self):
        with patch.dict(os.environ, {**ENV, 'ORCHESTRATOR_TYPE': 'eks', 'PROMETHEUS_WORKSPACE_ID': 'ws-123'}, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', self.fake_request), \
                patch.object(lambda_function, 'create_dashboard', return_value={}):
            lambda_function.on_create()

        self.assertFalse([r for r in self.requests if r[1].startswith('dashboards/uid/')])


class TestCloudWatchDataSourcePlugin(unittest.TestCase):
    def created(self, orchestrator, plugin_status, conflict_fails=None):
        requests = []
        self.result = None

        def fake_request(endpoint, method, payload=None, additional_headers=None):
            requests.append(endpoint)
            return Mock(status=plugin_status if endpoint.startswith('plugins/') else 200, data=b'{"id": 1}')

        env = {**ENV, 'ORCHESTRATOR_TYPE': orchestrator, 'PROMETHEUS_WORKSPACE_ID': ''}
        if conflict_fails is not None:
            env['SLURM_DASHBOARD_CONFLICT_FAILS'] = conflict_fails
        with patch.dict(os.environ, env, clear=True), \
                patch.object(lambda_function, 'make_grafana_request', fake_request):
            self.result = lambda_function.create_cloudwatch_opentelemetry_datasource()
        return requests

    def test_creates_the_data_source_with_the_amazon_prometheus_plugin(self):
        self.assertEqual(
            self.created('slurm', 200), ['plugins/grafana-amazonprometheus-datasource/settings', 'datasources']
        )

    def test_fails_on_a_slurm_workspace_without_the_plugin_on_the_extension_path(self):
        with self.assertRaisesRegex(Exception, 'g-abc has no Amazon Managed Service for Prometheus plugin'):
            self.created('slurm', 404, conflict_fails='true')

    def test_fails_when_the_plugin_check_fails_on_the_extension_path(self):
        with self.assertRaisesRegex(Exception, 'Failed to check grafana-amazonprometheus-datasource plugin'):
            self.created('slurm', 500, conflict_fails='true')

    def test_skips_the_data_source_without_the_plugin_on_the_node_agent_path(self):
        for conflict_fails in (None, 'false'):
            with self.subTest(conflict_fails=conflict_fails):
                self.assertEqual(
                    self.created('slurm', 404, conflict_fails), ['plugins/grafana-amazonprometheus-datasource/settings']
                )
                self.assertEqual(self.result['status'], 'skipped')
                self.assertIn('g-abc has no Amazon Managed Service for Prometheus plugin', self.result['message'])

    def test_skips_the_data_source_when_the_plugin_check_fails_on_the_node_agent_path(self):
        self.assertEqual(self.created('slurm', 500), ['plugins/grafana-amazonprometheus-datasource/settings'])
        self.assertEqual(self.result['status'], 'skipped')

    def test_does_not_check_for_eks(self):
        self.assertEqual(self.created('eks', 404), ['datasources'])


if __name__ == '__main__':
    unittest.main()
