"""Unit tests for the Slurm observability launcher Lambda function."""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402

SETUP_STACK_ID = 'arn:aws:cloudformation:us-west-2:123456789012:stack/sagemaker-c1-abcd1234-observability/uuid-1'
CLUSTER_ARN = 'arn:aws:sagemaker:us-west-2:123456789012:cluster/abc123'
TEMPLATE_URL = 'https://bucket.s3.us-west-2.amazonaws.com/v1/templates-slurm/slurm-observability-setup-template.yaml'


FUNCTION_ARN = 'arn:aws:lambda:us-west-2:123456789012:function:sagemaker-c1-abcd1234-ObsSetup'


def context(remaining_ms=900000):
    return Mock(
        log_stream_name='log-stream',
        invoked_function_arn=FUNCTION_ARN,
        get_remaining_time_in_millis=Mock(return_value=remaining_ms),
    )


def event(request_type='Create', physical_id=None):
    e = {
        'RequestType': request_type,
        'ResponseURL': 'https://cfn.example/response',
        'StackId': 'arn:aws:cloudformation:us-west-2:123456789012:stack/sagemaker-c1-abcd1234/uuid-0',
        'RequestId': 'request-1',
        'LogicalResourceId': 'ObservabilitySetupStack',
        'ResourceProperties': {
            'ServiceToken': 'arn:aws:lambda:us-west-2:123456789012:function:launcher',
            'StackName': 'sagemaker-c1-abcd1234-observability',
            'ClusterArn': CLUSTER_ARN,
            'Parameters': {'VpcId': 'vpc-1', 'PrivateSubnetIds': ['subnet-1', 'subnet-2'], 'GrafanaRole': ''},
        },
    }
    if physical_id:
        e['PhysicalResourceId'] = physical_id
    return e


def stack(status, reason=None):
    s = {'StackId': SETUP_STACK_ID, 'StackStatus': status}
    if reason:
        s['StackStatusReason'] = reason
    return {'Stacks': [s]}


def not_found():
    return ClientError(
        {'Error': {'Code': 'ValidationError', 'Message': f'Stack with id {SETUP_STACK_ID} does not exist'}},
        'DescribeStacks',
    )


@patch.dict(os.environ, {'SETUP_TEMPLATE_URL': TEMPLATE_URL})
@patch.object(lambda_function.time, 'sleep')
class TestLauncher(unittest.TestCase):
    def setUp(self):
        self.clients = {'cloudformation': MagicMock(), 'sagemaker': MagicMock(), 'lambda': MagicMock()}
        self.cloudformation = self.clients['cloudformation']
        self.cloudformation.create_stack.return_value = {'StackId': SETUP_STACK_ID}
        boto3_patch = patch.object(lambda_function.boto3, 'client', side_effect=lambda name, **_: self.clients[name])
        boto3_patch.start()
        self.addCleanup(boto3_patch.stop)
        respond_patch = patch.object(lambda_function, 'respond')
        self.respond = respond_patch.start()
        self.addCleanup(respond_patch.stop)

    def run_handler(self, e, remaining_ms=900000):
        lambda_function.lambda_handler(e, context(remaining_ms))
        self.respond.assert_called_once()
        _event, _context, status, physical_id, data, reason = self.respond.call_args.args
        return status, physical_id, data, reason

    # --- Create ---

    def test_create_starts_the_setup_stack_without_waiting(self, sleep):
        self.cloudformation.describe_stacks.return_value = {'Stacks': [{'StackId': event()['StackId'], 'Tags': [
            {'Key': 'CostCenter', 'Value': '1234'},
            {'Key': 'aws:cloudformation:stack-name', 'Value': 'sagemaker-c1-abcd1234'},
        ]}]}
        status, physical_id, data, _ = self.run_handler(event())
        self.assertEqual((status, physical_id, data), ('SUCCESS', SETUP_STACK_ID, {'StackId': SETUP_STACK_ID}))
        self.cloudformation.create_stack.assert_called_once_with(
            StackName='sagemaker-c1-abcd1234-observability',
            TemplateURL=TEMPLATE_URL,
            Parameters=[
                {'ParameterKey': 'VpcId', 'ParameterValue': 'vpc-1'},
                {'ParameterKey': 'PrivateSubnetIds', 'ParameterValue': 'subnet-1,subnet-2'},
                {'ParameterKey': 'GrafanaRole', 'ParameterValue': ''},
            ],
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
            OnFailure='DELETE',
            # The cluster's stack's tags, as a nested stack would inherit them, without the ones CloudFormation sets.
            Tags=[{'Key': 'CostCenter', 'Value': '1234'}],
            ClientRequestToken='setup-request-1',
        )
        self.cloudformation.describe_stacks.assert_called_once_with(StackName=event()['StackId'])
        sleep.assert_not_called()

    def test_create_starts_the_setup_stack_untagged_when_its_own_tags_cannot_be_read(self, _sleep):
        for error in (
            ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'not authorized'}}, 'DescribeStacks'),
            ReadTimeoutError(endpoint_url='https://cloudformation.example'),
        ):
            with self.subTest(error=type(error).__name__):
                self.respond.reset_mock()
                self.cloudformation.reset_mock()
                self.cloudformation.create_stack.return_value = {'StackId': SETUP_STACK_ID}
                self.cloudformation.describe_stacks.side_effect = error
                status, physical_id, _, _ = self.run_handler(event())
                self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
                self.assertEqual(self.cloudformation.create_stack.call_args.kwargs['Tags'], [])

    def test_create_ignores_a_template_url_in_the_request(self, _sleep):
        e = event()
        e['ResourceProperties']['TemplateURL'] = 'https://elsewhere.example/template.yaml'
        self.run_handler(e)
        self.assertEqual(self.cloudformation.create_stack.call_args.kwargs['TemplateURL'], TEMPLATE_URL)

    def test_create_tags_the_cluster_with_the_setup_stack(self, _sleep):
        self.run_handler(event())
        self.clients['sagemaker'].add_tags.assert_called_once_with(
            ResourceArn=CLUSTER_ARN, Tags=[{'Key': 'hyperpod-cluster-extensions-stack', 'Value': SETUP_STACK_ID}]
        )

    def test_create_succeeds_when_the_tag_fails(self, _sleep):
        self.clients['sagemaker'].add_tags.side_effect = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': 'denied'}}, 'AddTags'
        )
        status, physical_id, _, _ = self.run_handler(event())
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.assertEqual(self.clients['sagemaker'].add_tags.call_count, lambda_function.TAG_ATTEMPTS)

    def test_create_retries_the_tag(self, sleep):
        throttled = ClientError({'Error': {'Code': 'ThrottlingException', 'Message': 'slow down'}}, 'AddTags')
        self.clients['sagemaker'].add_tags.side_effect = [throttled, throttled, {}]
        status, _, _, _ = self.run_handler(event())
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(self.clients['sagemaker'].add_tags.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])

    def already_exists(self, cluster_arn):
        self.cloudformation.create_stack.side_effect = ClientError(
            {'Error': {'Code': 'AlreadyExistsException', 'Message': 'Stack already exists'}}, 'CreateStack'
        )
        self.cloudformation.describe_stacks.return_value = {'Stacks': [{
            'StackId': SETUP_STACK_ID,
            'Parameters': [{'ParameterKey': 'VpcId', 'ParameterValue': 'vpc-1'},
                           {'ParameterKey': 'HyperPodClusterArn', 'ParameterValue': cluster_arn}],
        }]}
        return self.run_handler(event())

    def test_create_adopts_this_clusters_stack_a_lost_response_created(self, _sleep):
        status, physical_id, data, _ = self.already_exists(CLUSTER_ARN)
        self.assertEqual((status, physical_id, data), ('SUCCESS', SETUP_STACK_ID, {'StackId': SETUP_STACK_ID}))
        self.cloudformation.describe_stacks.assert_called_with(StackName='sagemaker-c1-abcd1234-observability')
        self.clients['sagemaker'].add_tags.assert_called_once_with(
            ResourceArn=CLUSTER_ARN, Tags=[{'Key': 'hyperpod-cluster-extensions-stack', 'Value': SETUP_STACK_ID}]
        )

    def test_create_succeeds_without_a_stack_for_another_clusters_stack_with_the_name(self, _sleep):
        with self.assertLogs(lambda_function.logger, level='ERROR') as logs:
            status, physical_id, data, _ = self.already_exists('arn:aws:sagemaker:us-west-2:123456789012:cluster/other')
        self.assertEqual((status, physical_id, data), ('SUCCESS', 'ObservabilitySetupStack-not-created', {}))
        self.assertIn('Stack already exists', '\n'.join(logs.output))
        self.clients['sagemaker'].add_tags.assert_called_once_with(
            ResourceArn=CLUSTER_ARN, Tags=[{'Key': 'hyperpod-cluster-extensions-stack', 'Value': 'not-created'}]
        )

    def test_the_request_token_fits_cloudformations_limit(self, _sleep):
        e = event()
        e['RequestId'] = 'x' * 200
        self.run_handler(e)
        self.assertEqual(len(self.cloudformation.create_stack.call_args.kwargs['ClientRequestToken']), 128)

    def test_create_succeeds_without_a_stack_when_the_stack_cannot_be_started(self, _sleep):
        for error in (
            ClientError({'Error': {'Code': 'AlreadyExistsException', 'Message': 'Stack already exists'}}, 'CreateStack'),
            ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'explicit deny in an SCP'}}, 'CreateStack'),
            ClientError({'Error': {'Code': 'LimitExceededException', 'Message': 'stack limit'}}, 'CreateStack'),
            ClientError({'Error': {'Code': 'ValidationError', 'Message': 'TemplateURL must be'}}, 'CreateStack'),
        ):
            with self.subTest(error=error.response['Error']['Code']):
                self.respond.reset_mock()
                self.clients['sagemaker'].reset_mock()
                self.cloudformation.create_stack.side_effect = error
                status, physical_id, data, _ = self.run_handler(event())
                self.assertEqual((status, physical_id), ('SUCCESS', 'ObservabilitySetupStack-not-created'))
                self.assertFalse(lambda_function.STACK_ID_PATTERN.fullmatch(physical_id))
                self.assertEqual(data, {})
                # Replaces the pending tag, so the console doesn't wait for a setup stack.
                self.clients['sagemaker'].add_tags.assert_called_once_with(
                    ResourceArn=CLUSTER_ARN, Tags=[{'Key': 'hyperpod-cluster-extensions-stack', 'Value': 'not-created'}]
                )

    def test_create_adopts_this_clusters_stack_started_before_a_timeout(self, _sleep):
        self.cloudformation.create_stack.side_effect = ReadTimeoutError(endpoint_url='https://cloudformation.example')
        self.cloudformation.describe_stacks.return_value = {'Stacks': [{
            'StackId': SETUP_STACK_ID,
            'Parameters': [{'ParameterKey': 'HyperPodClusterArn', 'ParameterValue': CLUSTER_ARN}],
        }]}
        status, physical_id, data, _ = self.run_handler(event())
        self.assertEqual((status, physical_id, data), ('SUCCESS', SETUP_STACK_ID, {'StackId': SETUP_STACK_ID}))
        self.cloudformation.describe_stacks.assert_called_with(StackName='sagemaker-c1-abcd1234-observability')
        self.clients['sagemaker'].add_tags.assert_called_once_with(
            ResourceArn=CLUSTER_ARN, Tags=[{'Key': 'hyperpod-cluster-extensions-stack', 'Value': SETUP_STACK_ID}]
        )

    def test_create_succeeds_without_a_stack_when_the_lookup_after_a_timeout_fails(self, _sleep):
        timeout = ReadTimeoutError(endpoint_url='https://cloudformation.example')
        self.cloudformation.create_stack.side_effect = timeout
        self.cloudformation.describe_stacks.side_effect = timeout
        status, physical_id, data, _ = self.run_handler(event())
        self.assertEqual((status, physical_id), ('SUCCESS', 'ObservabilitySetupStack-not-created'))
        self.assertEqual(data, {})
        self.clients['sagemaker'].add_tags.assert_called_once_with(
            ResourceArn=CLUSTER_ARN, Tags=[{'Key': 'hyperpod-cluster-extensions-stack', 'Value': 'not-created'}]
        )

    def test_create_succeeds_without_a_stack_when_the_not_created_tag_fails(self, _sleep):
        self.cloudformation.create_stack.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'explicit deny in an SCP'}}, 'CreateStack'
        )
        self.clients['sagemaker'].add_tags.side_effect = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': 'denied'}}, 'AddTags'
        )
        status, physical_id, data, _ = self.run_handler(event())
        self.assertEqual((status, physical_id, data), ('SUCCESS', 'ObservabilitySetupStack-not-created', {}))
        self.assertEqual(self.clients['sagemaker'].add_tags.call_count, lambda_function.TAG_ATTEMPTS)

    def test_a_delete_after_a_create_without_a_stack_deletes_nothing(self, _sleep):
        self.cloudformation.create_stack.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'explicit deny in an SCP'}}, 'CreateStack'
        )
        _, physical_id, _, _ = self.run_handler(event())
        self.respond.reset_mock()
        self.cloudformation.reset_mock()
        status, deleted_id, _, _ = self.run_handler(event('Delete', physical_id))
        self.assertEqual((status, deleted_id), ('SUCCESS', physical_id))
        self.assertEqual(self.cloudformation.method_calls, [])

    # --- Update ---

    def test_update_leaves_the_setup_stack_as_it_is(self, _sleep):
        e = event('Update', SETUP_STACK_ID)
        e['OldResourceProperties'] = dict(e['ResourceProperties'])
        with self.assertNoLogs(lambda_function.logger, level='WARNING'):
            status, physical_id, _, _ = self.run_handler(e)
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.assertEqual(self.cloudformation.method_calls, [])

    def test_update_warns_when_the_cluster_was_replaced(self, _sleep):
        e = event('Update', SETUP_STACK_ID)
        old_cluster_arn = 'arn:aws:sagemaker:us-west-2:123456789012:cluster/old123'
        e['OldResourceProperties'] = {**e['ResourceProperties'], 'ClusterArn': old_cluster_arn}
        with self.assertLogs(lambda_function.logger, level='WARNING') as logs:
            status, physical_id, _, _ = self.run_handler(e)
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.assertIn(f'still targets {old_cluster_arn}', logs.output[0])
        self.assertEqual(self.cloudformation.method_calls, [])

    # --- Delete ---

    def test_delete_deletes_the_setup_stack_and_waits_until_it_is_gone(self, sleep):
        self.cloudformation.describe_stacks.side_effect = [
            stack('CREATE_COMPLETE'),
            stack('DELETE_IN_PROGRESS'),
            stack('DELETE_COMPLETE'),
        ]
        status, physical_id, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.cloudformation.delete_stack.assert_called_once_with(StackName=SETUP_STACK_ID)
        self.assertEqual(self.cloudformation.describe_stacks.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_delete_deletes_a_stack_still_being_created(self, sleep):
        self.cloudformation.describe_stacks.side_effect = [
            stack('CREATE_IN_PROGRESS'),
            stack('DELETE_IN_PROGRESS'),
            stack('DELETE_COMPLETE'),
        ]
        status, _, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual(status, 'SUCCESS')
        self.cloudformation.delete_stack.assert_called_once_with(StackName=SETUP_STACK_ID)
        self.assertEqual(sleep.call_count, 2)

    def test_delete_waits_without_deleting_again_a_stack_being_deleted(self, _sleep):
        self.cloudformation.describe_stacks.side_effect = [stack('DELETE_IN_PROGRESS'), not_found()]
        status, _, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual(status, 'SUCCESS')
        self.cloudformation.delete_stack.assert_not_called()

    def test_delete_retries_a_stack_that_failed_to_delete_before(self, _sleep):
        self.cloudformation.describe_stacks.side_effect = [stack('DELETE_FAILED'), stack('DELETE_COMPLETE')]
        status, _, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual(status, 'SUCCESS')
        self.cloudformation.delete_stack.assert_called_once_with(StackName=SETUP_STACK_ID)

    def test_delete_succeeds_for_a_stack_already_gone(self, _sleep):
        for already_gone in (not_found(), stack('DELETE_COMPLETE')):
            with self.subTest(already_gone=already_gone):
                self.respond.reset_mock()
                self.cloudformation.reset_mock()
                self.cloudformation.describe_stacks.side_effect = [already_gone]
                status, _, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
                self.assertEqual(status, 'SUCCESS')
                self.cloudformation.delete_stack.assert_not_called()

    def test_delete_succeeds_without_a_stack_for_a_create_that_failed(self, _sleep):
        status, physical_id, _, _ = self.run_handler(event('Delete', 'ObservabilitySetupStack-not-created'))
        self.assertEqual((status, physical_id), ('SUCCESS', 'ObservabilitySetupStack-not-created'))
        self.assertEqual(self.cloudformation.method_calls, [])

    def test_delete_keeps_polling_after_a_transient_describe_error(self, sleep):
        self.cloudformation.describe_stacks.side_effect = [
            stack('CREATE_COMPLETE'),
            ClientError({'Error': {'Code': 'Throttling', 'Message': 'Rate exceeded'}}, 'DescribeStacks'),
            ClientError({'Error': {'Code': 'InternalFailure', 'Message': 'internal error'}}, 'DescribeStacks'),
            stack('DELETE_COMPLETE'),
        ]
        status, physical_id, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.cloudformation.delete_stack.assert_called_once_with(StackName=SETUP_STACK_ID)
        self.assertEqual(self.cloudformation.describe_stacks.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_delete_keeps_polling_after_a_describe_timeout_or_connection_error(self, sleep):
        self.cloudformation.describe_stacks.side_effect = [
            stack('CREATE_COMPLETE'),
            ReadTimeoutError(endpoint_url='https://cloudformation.example'),
            EndpointConnectionError(endpoint_url='https://cloudformation.example'),
            stack('DELETE_COMPLETE'),
        ]
        status, physical_id, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.assertEqual(self.cloudformation.describe_stacks.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_delete_retries_a_first_describe_or_delete_that_fails_transiently(self, sleep):
        self.cloudformation.describe_stacks.side_effect = [
            ClientError({'Error': {'Code': 'Throttling', 'Message': 'Rate exceeded'}}, 'DescribeStacks'),
            stack('CREATE_COMPLETE'),
            stack('CREATE_COMPLETE'),
            stack('DELETE_IN_PROGRESS'),
            stack('DELETE_COMPLETE'),
        ]
        self.cloudformation.delete_stack.side_effect = [
            ReadTimeoutError(endpoint_url='https://cloudformation.example'),
            {},
        ]
        status, physical_id, _, _ = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual((status, physical_id), ('SUCCESS', SETUP_STACK_ID))
        self.assertEqual(self.cloudformation.delete_stack.call_count, 2)
        self.assertEqual(self.cloudformation.describe_stacks.call_count, 5)
        self.assertEqual(sleep.call_count, 4)

    def test_delete_fails_with_the_reason_when_the_stack_fails_to_delete(self, _sleep):
        self.cloudformation.describe_stacks.side_effect = [
            stack('CREATE_COMPLETE'),
            stack('DELETE_FAILED', 'The following resource(s) failed to delete: [GrafanaVPCEndpoint].'),
        ]
        status, physical_id, _, reason = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual((status, physical_id), ('FAILED', SETUP_STACK_ID))
        self.assertIn('failed to delete: [GrafanaVPCEndpoint]', reason)

    def test_delete_continues_in_another_invocation_before_the_function_times_out(self, sleep):
        self.cloudformation.describe_stacks.return_value = stack('CREATE_COMPLETE')
        lambda_function.lambda_handler(event('Delete', SETUP_STACK_ID), context(150000))
        # The next invocation answers CloudFormation, with the same request.
        self.respond.assert_not_called()
        self.cloudformation.delete_stack.assert_called_once()
        sleep.assert_not_called()
        invoke = self.clients['lambda'].invoke.call_args.kwargs
        self.assertEqual((invoke['FunctionName'], invoke['InvocationType']), (FUNCTION_ARN, 'Event'))
        payload = json.loads(invoke['Payload'])
        self.assertEqual(payload['SetupStackDeleteInvocation'], 2)
        self.assertEqual(payload['PhysicalResourceId'], SETUP_STACK_ID)

    def test_delete_says_it_is_still_deleting_when_it_cannot_continue_in_another_invocation(self, sleep):
        self.cloudformation.describe_stacks.return_value = stack('DELETE_IN_PROGRESS')
        self.clients['lambda'].invoke.side_effect = ClientError(
            {'Error': {'Code': 'TooManyRequestsException', 'Message': 'Rate exceeded'}}, 'Invoke'
        )
        status, _, _, reason = self.run_handler(event('Delete', SETUP_STACK_ID), remaining_ms=150000)
        self.assertEqual(status, 'FAILED')
        self.assertIn('still being deleted. Delete this stack again.', reason)
        self.assertNotIn('Rate exceeded', reason)

    def test_a_continued_delete_waits_without_deleting_again(self, sleep):
        self.cloudformation.describe_stacks.side_effect = [stack('DELETE_IN_PROGRESS'), stack('DELETE_COMPLETE')]
        e = {**event('Delete', SETUP_STACK_ID), 'SetupStackDeleteInvocation': 2}
        status, _, _, _ = self.run_handler(e)
        self.assertEqual(status, 'SUCCESS')
        self.cloudformation.delete_stack.assert_not_called()

    def test_delete_fails_after_its_last_invocation(self, sleep):
        self.cloudformation.describe_stacks.return_value = stack('DELETE_IN_PROGRESS')
        e = {**event('Delete', SETUP_STACK_ID), 'SetupStackDeleteInvocation': lambda_function.MAX_DELETE_INVOCATIONS}
        status, _, _, reason = self.run_handler(e, remaining_ms=150000)
        self.assertEqual(status, 'FAILED')
        self.assertIn('still being deleted', reason)
        self.clients['lambda'].invoke.assert_not_called()

    # --- Always responds ---

    def test_responds_when_an_unexpected_error_happens(self, _sleep):
        self.cloudformation.describe_stacks.side_effect = RuntimeError('boom')
        status, _, _, reason = self.run_handler(event('Delete', SETUP_STACK_ID))
        self.assertEqual((status, reason), ('FAILED', 'boom'))

    def test_responds_to_a_create_with_missing_properties(self, _sleep):
        for name in ('StackName', 'ClusterArn'):
            with self.subTest(missing=name):
                self.respond.reset_mock()
                e = event()
                del e['ResourceProperties'][name]
                status, physical_id, _, _ = self.run_handler(e)
                self.assertEqual((status, physical_id), ('FAILED', 'ObservabilitySetupStack-not-created'))
        self.cloudformation.create_stack.assert_not_called()

    def test_responds_to_an_invalid_request_type(self, _sleep):
        status, _, _, reason = self.run_handler(event('Bogus', SETUP_STACK_ID))
        self.assertEqual(status, 'FAILED')
        self.assertIn('Invalid request type', reason)


@patch.object(lambda_function.time, 'sleep')
class TestRespond(unittest.TestCase):
    def test_puts_the_response_to_cloudformation(self, _sleep):
        with patch.object(lambda_function.urllib.request, 'urlopen') as urlopen:
            lambda_function.respond(event(), context(), 'SUCCESS', SETUP_STACK_ID, {'StackId': SETUP_STACK_ID})
        request = urlopen.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), ('https://cfn.example/response', 'PUT'))
        self.assertEqual(
            json.loads(request.data),
            {
                'Status': 'SUCCESS',
                'Reason': 'See the details in CloudWatch Log Stream: log-stream',
                'PhysicalResourceId': SETUP_STACK_ID,
                'StackId': event()['StackId'],
                'RequestId': 'request-1',
                'LogicalResourceId': 'ObservabilitySetupStack',
                'Data': {'StackId': SETUP_STACK_ID},
            },
        )

    def test_retries_a_response_that_fails(self, sleep):
        with patch.object(lambda_function.urllib.request, 'urlopen', side_effect=[OSError('reset'), MagicMock()]) as urlopen:
            lambda_function.respond(event(), context(), 'FAILED', 'id', {}, 'why')
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)['Reason'], 'why')
        sleep.assert_called_once()

    def test_gives_up_after_the_last_attempt(self, _sleep):
        with patch.object(lambda_function.urllib.request, 'urlopen', side_effect=OSError('reset')) as urlopen:
            lambda_function.respond(event(), context(), 'SUCCESS', 'id', {})
        self.assertEqual(urlopen.call_count, lambda_function.RESPONSE_ATTEMPTS)


if __name__ == '__main__':
    unittest.main()
