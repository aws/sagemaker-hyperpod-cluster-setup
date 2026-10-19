import os
import sys
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402
from lambda_function import lambda_handler  # noqa: E402


class TestPhysicalResourceId(unittest.TestCase):
    def setUp(self):
        lambda_function.cfnresponse.send.reset_mock()
        self.context = Mock(log_stream_name='2026/10/13/[$LATEST]new-stream')
        self.cloudformation = Mock()
        self.cloudformation.create_stack.return_value = {'StackId': 'new-stack-id'}
        self.cloudformation.update_stack.return_value = {'StackId': 'stack-id'}
        self.cloudformation.describe_stacks.return_value = {'Stacks': [{'StackId': 'stack-id'}]}
        self.cloudformation.get_template_summary.return_value = {'Parameters': []}
        patcher = patch('lambda_function.boto3.client', return_value=self.cloudformation)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _event(self, request_type, **fields):
        return {
            'RequestType': request_type,
            'ResourceProperties': {
                'ResourceNamePrefix': 'test',
                'StackTemplateUrl': 'https://example.com/observability-template.yaml',
                'PrivateSubnetIds': 'subnet-1',
            },
            **fields,
        }

    def _sent(self):
        args = lambda_function.cfnresponse.send.call_args.args
        return args[2], args[3], args[4]

    def test_create_uses_the_stack_id(self):
        lambda_handler(self._event('Create'), self.context)

        status, _, physical_id = self._sent()
        self.assertEqual((status, physical_id), (lambda_function.cfnresponse.SUCCESS, 'new-stack-id'))

    def test_update_keeps_the_existing_id_so_it_is_not_a_replacement(self):
        # Stacks created before this fix have a log stream name as their id
        lambda_handler(self._event('Update', PhysicalResourceId='2026/01/01/[$LATEST]old-stream'), self.context)

        self.assertEqual(self._sent()[2], '2026/01/01/[$LATEST]old-stream')

    def test_update_with_nothing_to_change_keeps_the_id_and_returns_the_stack_id(self):
        self.cloudformation.update_stack.side_effect = ClientError(
            {'Error': {'Code': 'ValidationError', 'Message': 'No updates are to be performed.'}}, 'UpdateStack'
        )

        lambda_handler(self._event('Update', PhysicalResourceId='old-id'), self.context)

        status, data, physical_id = self._sent()
        self.assertEqual(status, lambda_function.cfnresponse.SUCCESS)
        self.assertEqual((data['StackId'], physical_id), ('stack-id', 'old-id'))

    def test_failed_update_keeps_the_existing_id(self):
        self.cloudformation.update_stack.side_effect = Exception('boom')

        lambda_handler(self._event('Update', PhysicalResourceId='old-id'), self.context)

        status, _, physical_id = self._sent()
        self.assertEqual((status, physical_id), (lambda_function.cfnresponse.FAILED, 'old-id'))


if __name__ == '__main__':
    unittest.main()
