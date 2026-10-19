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
