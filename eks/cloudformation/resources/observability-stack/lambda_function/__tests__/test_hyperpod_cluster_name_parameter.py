import os
import sys
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lambda_function import on_create, on_update  # noqa: E402


class TestHyperPodClusterNameParameter(unittest.TestCase):
    def _event(self, **properties):
        return {
            'ResourceProperties': {
                'ResourceNamePrefix': 'test',
                'StackTemplateUrl': 'https://example.com/observability-template.yaml',
                'PrivateSubnetIds': 'subnet-1,subnet-2',
                **properties,
            }
        }

    @staticmethod
    def _parameter(call, key):
        return next(p['ParameterValue'] for p in call.kwargs['Parameters'] if p['ParameterKey'] == key)

    @staticmethod
    def _cloudformation(declared=('HyperPodClusterName',), summary_error=None):
        cloudformation = Mock()
        cloudformation.create_stack.return_value = {'StackId': 'stack-id'}
        cloudformation.update_stack.return_value = {'StackId': 'stack-id'}
        cloudformation.describe_stacks.return_value = {'Stacks': [{'StackId': 'stack-id'}]}
        if summary_error:
            cloudformation.get_template_summary.side_effect = summary_error
        else:
            cloudformation.get_template_summary.return_value = {
                'Parameters': [{'ParameterKey': key} for key in ('ResourceNamePrefix', *declared)]
            }
        return cloudformation

    def _passed(self, cloudformation):
        return [
            [p['ParameterKey'] for p in call.kwargs['Parameters']].count('HyperPodClusterName')
            for call in (cloudformation.create_stack.call_args, cloudformation.update_stack.call_args)
        ]

    @patch('lambda_function.boto3.client')
    def test_create_and_update_pass_the_cluster_name_when_the_template_declares_it(self, mock_client):
        cloudformation = self._cloudformation()
        mock_client.return_value = cloudformation

        on_create(self._event(HyperPodClusterName='hp-cluster-1'))
        on_update(self._event(HyperPodClusterName='hp-cluster-1'))

        self.assertEqual(self._parameter(cloudformation.create_stack.call_args, 'HyperPodClusterName'), 'hp-cluster-1')
        self.assertEqual(self._parameter(cloudformation.update_stack.call_args, 'HyperPodClusterName'), 'hp-cluster-1')
        cloudformation.get_template_summary.assert_called_with(
            TemplateURL='https://example.com/observability-template.yaml'
        )

    @patch('lambda_function.boto3.client')
    def test_cluster_name_is_not_passed_to_an_older_template(self, mock_client):
        # e.g. StackTemplateUrl pointing at an older observability template in a customer bucket
        cloudformation = self._cloudformation(declared=())
        mock_client.return_value = cloudformation

        on_create(self._event(HyperPodClusterName='hp-cluster-1'))
        on_update(self._event(HyperPodClusterName='hp-cluster-1'))

        self.assertEqual(self._passed(cloudformation), [0, 0])

    @patch('lambda_function.boto3.client')
    def test_cluster_name_is_not_passed_when_the_template_cannot_be_read(self, mock_client):
        cloudformation = self._cloudformation(summary_error=Exception('AccessDenied'))
        mock_client.return_value = cloudformation

        on_create(self._event(HyperPodClusterName='hp-cluster-1'))
        on_update(self._event(HyperPodClusterName='hp-cluster-1'))

        self.assertEqual(self._passed(cloudformation), [0, 0])

    @patch('lambda_function.boto3.client')
    def test_missing_cluster_name_is_not_passed(self, mock_client):
        cloudformation = self._cloudformation()
        mock_client.return_value = cloudformation

        on_create(self._event())
        on_update(self._event(HyperPodClusterName=''))

        self.assertEqual(self._passed(cloudformation), [0, 0])
        cloudformation.get_template_summary.assert_not_called()


    @patch('lambda_function.boto3.client')
    def test_metrics_destination_is_passed_when_the_template_declares_it(self, mock_client):
        cloudformation = self._cloudformation(declared=('HyperPodClusterName', 'MetricsDestination'))
        mock_client.return_value = cloudformation

        on_create(self._event(HyperPodClusterName='hp-cluster-1', MetricsDestination='cloudwatch'))
        on_update(self._event(HyperPodClusterName='hp-cluster-1', MetricsDestination='cloudwatch'))

        for call in (cloudformation.create_stack.call_args, cloudformation.update_stack.call_args):
            self.assertEqual(self._parameter(call, 'MetricsDestination'), 'cloudwatch')
            self.assertEqual(self._parameter(call, 'HyperPodClusterName'), 'hp-cluster-1')

    @patch('lambda_function.boto3.client')
    def test_cloudwatch_fails_for_an_older_template(self, mock_client):
        # The older template's default is AMP: dropping cloudwatch would create a Prometheus workspace.
        cloudformation = self._cloudformation(declared=('HyperPodClusterName',))
        mock_client.return_value = cloudformation

        for handler in (on_create, on_update):
            with self.assertRaisesRegex(RuntimeError, "doesn't declare MetricsDestination"):
                handler(self._event(HyperPodClusterName='hp-cluster-1', MetricsDestination='cloudwatch'))
        cloudformation.create_stack.assert_not_called()
        cloudformation.update_stack.assert_not_called()

    @patch('lambda_function.boto3.client')
    def test_cloudwatch_fails_when_the_template_cannot_be_read(self, mock_client):
        cloudformation = self._cloudformation(summary_error=Exception('Rate exceeded'))
        mock_client.return_value = cloudformation

        for handler in (on_create, on_update):
            with self.assertRaisesRegex(RuntimeError, r"couldn't be read \(Rate exceeded\)"):
                handler(self._event(HyperPodClusterName='hp-cluster-1', MetricsDestination='cloudwatch'))
        cloudformation.create_stack.assert_not_called()
        cloudformation.update_stack.assert_not_called()

    def _update(self, old, **properties):
        event = self._event(HyperPodClusterName='hp-cluster-1', **properties)
        event['OldResourceProperties'] = {
            'StackTemplateUrl': 'https://example.com/observability-template.yaml',
            **old,
        }
        return event

    @patch('lambda_function.boto3.client')
    def test_an_update_that_keeps_cloudwatch_and_the_older_template_succeeds(self, mock_client):
        # e.g. the rollback of a failed update: the nested stack was already deployed without MetricsDestination
        cloudformation = self._cloudformation(declared=('HyperPodClusterName',))
        mock_client.return_value = cloudformation

        on_update(self._update({'MetricsDestination': 'cloudwatch'}, MetricsDestination='cloudwatch'))

        keys = [p['ParameterKey'] for p in cloudformation.update_stack.call_args.kwargs['Parameters']]
        self.assertNotIn('MetricsDestination', keys)

    @patch('lambda_function.boto3.client')
    def test_an_update_to_cloudwatch_or_to_an_older_template_fails(self, mock_client):
        cloudformation = self._cloudformation(declared=('HyperPodClusterName',))
        mock_client.return_value = cloudformation

        for old in (
            {'MetricsDestination': 'amp'},
            {'MetricsDestination': 'cloudwatch', 'StackTemplateUrl': 'https://example.com/newer-template.yaml'},
        ):
            with self.assertRaisesRegex(RuntimeError, "doesn't declare MetricsDestination"):
                on_update(self._update(old, MetricsDestination='cloudwatch'))
        cloudformation.update_stack.assert_not_called()

    @patch('lambda_function.boto3.client')
    def test_a_rollback_to_an_older_template_succeeds(self, mock_client):
        # The failed update tried another older template; the rollback restores the one already deployed.
        cloudformation = self._cloudformation(declared=('HyperPodClusterName',))
        mock_client.return_value = cloudformation
        failed_attempt = {'MetricsDestination': 'cloudwatch', 'StackTemplateUrl': 'https://example.com/other.yaml'}
        event = {**self._update(failed_attempt, MetricsDestination='cloudwatch'), 'StackId': 'parent-stack-id'}

        for status, succeeds in (('UPDATE_ROLLBACK_IN_PROGRESS', True), ('UPDATE_IN_PROGRESS', False)):
            cloudformation.describe_stacks.return_value = {'Stacks': [{'StackStatus': status}]}
            if succeeds:
                on_update(event)
                keys = [p['ParameterKey'] for p in cloudformation.update_stack.call_args.kwargs['Parameters']]
                self.assertNotIn('MetricsDestination', keys)
            else:
                with self.assertRaisesRegex(RuntimeError, "doesn't declare MetricsDestination"):
                    on_update(event)
        cloudformation.describe_stacks.assert_called_with(StackName='parent-stack-id')

        # If the parent stack's status can't be read, it isn't treated as a rollback: the request fails.
        cloudformation.update_stack.reset_mock()
        cloudformation.describe_stacks.side_effect = Exception('Rate exceeded')
        with self.assertRaisesRegex(RuntimeError, "doesn't declare MetricsDestination"):
            on_update(event)
        cloudformation.update_stack.assert_not_called()

    @patch('lambda_function.boto3.client')
    def test_an_unchanged_update_still_fails_when_the_template_cannot_be_read(self, mock_client):
        # The template may declare MetricsDestination; leaving it out would move the nested stack to AMP.
        cloudformation = self._cloudformation(summary_error=Exception('Rate exceeded'))
        mock_client.return_value = cloudformation

        with self.assertRaisesRegex(RuntimeError, "couldn't be read"):
            on_update(self._update({'MetricsDestination': 'cloudwatch'}, MetricsDestination='cloudwatch'))
        cloudformation.update_stack.assert_not_called()

    @patch('lambda_function.boto3.client')
    def test_amp_is_still_dropped_for_an_older_template_or_an_unreadable_one(self, mock_client):
        # amp is the older template's default, so leaving it out changes nothing.
        for cloudformation in (
            self._cloudformation(declared=('HyperPodClusterName',)),
            self._cloudformation(summary_error=Exception('AccessDenied')),
        ):
            mock_client.return_value = cloudformation

            on_create(self._event(HyperPodClusterName='hp-cluster-1', MetricsDestination='amp'))

            keys = [p['ParameterKey'] for p in cloudformation.create_stack.call_args.kwargs['Parameters']]
            self.assertNotIn('MetricsDestination', keys)

    @patch('lambda_function.boto3.client')
    def test_addon_version_is_passed_only_when_set_and_declared(self, mock_client):
        cloudformation = self._cloudformation(declared=('MetricsDestination', 'ObservabilityAddonVersion'))
        mock_client.return_value = cloudformation

        on_create(self._event(MetricsDestination='cloudwatch', ObservabilityAddonVersion='v1.4.0-eksbuild.1'))
        self.assertEqual(
            self._parameter(cloudformation.create_stack.call_args, 'ObservabilityAddonVersion'), 'v1.4.0-eksbuild.1'
        )

        # Empty keeps the template's default (EKS's default add-on version).
        on_update(self._event(MetricsDestination='cloudwatch', ObservabilityAddonVersion=''))
        keys = [p['ParameterKey'] for p in cloudformation.update_stack.call_args.kwargs['Parameters']]
        self.assertNotIn('ObservabilityAddonVersion', keys)

        # An older template doesn't declare it.
        cloudformation = self._cloudformation(declared=('MetricsDestination',))
        mock_client.return_value = cloudformation
        on_create(self._event(MetricsDestination='cloudwatch', ObservabilityAddonVersion='v1.4.0-eksbuild.1'))
        keys = [p['ParameterKey'] for p in cloudformation.create_stack.call_args.kwargs['Parameters']]
        self.assertNotIn('ObservabilityAddonVersion', keys)

    @patch('lambda_function.boto3.client')
    def test_host_profiling_is_passed_only_when_declared(self, mock_client):
        cloudformation = self._cloudformation(declared=('ObservabilityAddonVersion', 'HostProfiling'))
        mock_client.return_value = cloudformation

        on_create(self._event(ObservabilityAddonVersion='v1.4.0-eksbuild.1', HostProfiling='true'))
        on_update(self._event(ObservabilityAddonVersion='v1.4.0-eksbuild.1', HostProfiling='false'))
        self.assertEqual(self._parameter(cloudformation.create_stack.call_args, 'HostProfiling'), 'true')
        self.assertEqual(self._parameter(cloudformation.update_stack.call_args, 'HostProfiling'), 'false')

        # An older template doesn't declare it; its add-on configuration has no host profiling.
        cloudformation = self._cloudformation(declared=('ObservabilityAddonVersion',))
        mock_client.return_value = cloudformation
        on_create(self._event(ObservabilityAddonVersion='v1.4.0-eksbuild.1', HostProfiling='true'))
        keys = [p['ParameterKey'] for p in cloudformation.create_stack.call_args.kwargs['Parameters']]
        self.assertNotIn('HostProfiling', keys)
        self.assertIn('ObservabilityAddonVersion', keys)

    @patch('lambda_function.boto3.client')
    def test_update_with_nothing_to_change_succeeds(self, mock_client):
        # e.g. the HyperPodClusterName property changed but the nested stack's template and parameters didn't
        cloudformation = self._cloudformation()
        cloudformation.update_stack.side_effect = ClientError(
            {'Error': {'Code': 'ValidationError', 'Message': 'No updates are to be performed.'}}, 'UpdateStack'
        )
        mock_client.return_value = cloudformation

        self.assertEqual(on_update(self._event(HyperPodClusterName='hp-cluster-1'))['Status'], 'SUCCESS')

    @patch('lambda_function.boto3.client')
    def test_other_update_errors_still_fail(self, mock_client):
        cloudformation = self._cloudformation()
        cloudformation.update_stack.side_effect = ClientError(
            {'Error': {'Code': 'ValidationError', 'Message': 'Stack is in UPDATE_IN_PROGRESS state'}}, 'UpdateStack'
        )
        mock_client.return_value = cloudformation

        with self.assertRaises(ClientError):
            on_update(self._event(HyperPodClusterName='hp-cluster-1'))


if __name__ == '__main__':
    unittest.main()
