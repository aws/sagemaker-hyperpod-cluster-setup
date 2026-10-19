"""
Unit tests for Grafana workspace creation in the observability Grafana creator Lambda function
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402

BASE_ENV = {
    'WORKSPACE_NAME': 'test-amgws',
    'WORKSPACE_ROLE_ARN': 'arn:aws:iam::123456789012:role/grafana',
    'REGION': 'us-west-2',
    'AWS_ACCOUNT_ID': '123456789012',
    'PARTITION': 'aws',
}


class TestOnCreate(unittest.TestCase):
    def _run(self, env):
        client = MagicMock()
        client.create_workspace.return_value = {'workspace': {'id': 'g-abc'}}
        client.describe_workspace.return_value = {'workspace': {'status': 'ACTIVE'}}
        with patch.dict(os.environ, env, clear=True), patch.object(lambda_function.boto3, 'client', return_value=client):
            result = lambda_function.on_create()
        return client.create_workspace.call_args.kwargs, result

    def test_passes_pinned_grafana_version(self):
        kwargs, result = self._run({**BASE_ENV, 'GRAFANA_VERSION': '13.2'})

        self.assertEqual(kwargs['grafanaVersion'], '13.2')
        self.assertEqual(result['WorkspaceId'], 'g-abc')
        self.assertEqual(result['Arn'], 'arn:aws:grafana:us-west-2:123456789012:/workspaces/g-abc')

    def test_omits_grafana_version_when_unset(self):
        kwargs, _ = self._run(BASE_ENV)

        self.assertNotIn('grafanaVersion', kwargs)
        self.assertEqual(kwargs['workspaceName'], 'test-amgws')
        self.assertEqual(kwargs['permissionType'], 'CUSTOMER_MANAGED')


if __name__ == '__main__':
    unittest.main()
