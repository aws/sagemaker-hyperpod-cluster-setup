"""
Unit tests for Grafana workspace creation in the observability Grafana creator Lambda function
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

from botocore.exceptions import ClientError

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


class TestFailureReason(unittest.TestCase):
    def failed_reason(self, error):
        client = MagicMock()
        client.create_workspace.side_effect = error
        send = lambda_function.cfnresponse.send
        send.reset_mock()
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(lambda_function.boto3, 'client', return_value=client):
            lambda_function.lambda_handler({'RequestType': 'Create'}, Mock(log_stream_name='2026/11/03/[$LATEST]abc'))
        self.assertIs(send.call_args[0][2], lambda_function.cfnresponse.FAILED)
        return send.call_args.kwargs['reason']

    def test_reports_the_service_error_in_the_stack_event(self):
        reason = self.failed_reason(ClientError(
            {'Error': {'Code': 'ServiceQuotaExceededException',
                       'Message': 'Account has exceeded workspace quota in region us-west-2'}},
            'CreateWorkspace',
        ))

        self.assertEqual(
            reason,
            'Failed to create Grafana workspace: ServiceQuotaExceededException: Account has exceeded workspace quota '
            'in region us-west-2 (log stream: 2026/11/03/[$LATEST]abc)',
        )

    def test_keeps_a_long_error_within_the_response_limit_and_the_log_stream(self):
        reason = self.failed_reason(RuntimeError('x' * 5000))

        self.assertEqual(len(reason), lambda_function.MAX_REASON_LENGTH)
        self.assertTrue(reason.startswith('Failed to create Grafana workspace: RuntimeError: xxx'))
        self.assertTrue(reason.endswith('(log stream: 2026/11/03/[$LATEST]abc)'))


if __name__ == '__main__':
    unittest.main()
