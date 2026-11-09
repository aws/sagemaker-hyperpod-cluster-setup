"""Unit tests for the cluster extensions installer Lambda function."""
import http.client
import io
import json
import os
import ssl
import sys
import unittest
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lambda_function  # noqa: E402

CLUSTER_ARN = 'arn:aws:sagemaker:us-west-2:123456789012:cluster/abc123'
OBS = 'sagemaker-observability'
OBS_ARN = 'arn:aws:sagemaker:us-west-2:123456789012:extension/sagemaker-abc123/sagemaker-observability/x1'
OTHER = 'sagemaker-other'
OTHER_ARN = 'arn:aws:sagemaker:us-west-2:123456789012:extension/sagemaker-abc123/sagemaker-other/y1'
REMOTE_WRITE_URL = 'https://aps-workspaces.us-west-2.amazonaws.com/workspaces/ws-1/api/v1/remote_write'
LEVELS = {
    'NodeMetricLevel': 'ADVANCED',
    'AcceleratedComputeMetricLevel': 'BASIC',
    'ClusterMetricLevel': 'BASIC',
    'NetworkMetricLevel': 'DISABLED',
    'JobMetricLevel': 'BASIC',
    'NcclMetricLevel': 'DISABLED',
}
TEMPLATE_METRICS = {
    'nodeMetrics': {'level': 'ADVANCED', 'collectionIntervalSeconds': 30},
    'acceleratedComputeMetrics': {'level': 'BASIC', 'collectionIntervalSeconds': 30},
    'clusterMetrics': {'level': 'ENABLED', 'collectionIntervalSeconds': 30},
    'networkMetrics': {'level': 'DISABLED', 'collectionIntervalSeconds': 30},
    'jobMetrics': {'level': 'ENABLED', 'collectionIntervalSeconds': 30},
    'ncclMetrics': {'level': 'DISABLED', 'collectionIntervalSeconds': 30},
}
CONSOLE_OBS = {'metricsProvider': {'nodeMetrics': {'level': 'BASIC'}}, 'profilesProvider': {'hostProfiling': {'enabled': True}}}


def context(remaining=900000):
    return Mock(log_stream_name='log-stream', get_remaining_time_in_millis=Mock(return_value=remaining))


def extensions(*entries):
    return json.dumps([{'ExtensionName': name, 'ConfigurationValues': configuration} for name, configuration in entries])


def event(request_type='Create', **properties):
    return {
        'RequestType': request_type,
        'ResponseURL': 'https://cfn.example/response',
        'StackId': 'stack-1',
        'RequestId': 'request-1',
        'LogicalResourceId': 'ClusterExtensions',
        'ResourceProperties': {'ClusterArn': CLUSTER_ARN, 'InstallObservability': 'true', **LEVELS, **properties},
    }


def token(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, '/'.join(['request-1', *parts])))


class TestRequestedExtensions(unittest.TestCase):
    def test_installs_observability_with_the_template_levels_when_not_listed(self):
        self.assertEqual(
            lambda_function.requested_extensions({'InstallObservability': 'true', **LEVELS}),
            [(OBS, {'metricsProvider': TEMPLATE_METRICS})],
        )

    def test_uses_the_listed_observability_configuration_with_the_stacks_workspaces(self):
        properties = {
            'InstallObservability': 'true',
            'Extensions': extensions((OBS, CONSOLE_OBS)),
            'RemoteWriteUrl': REMOTE_WRITE_URL,
            'GrafanaWorkspaceId': 'g-0123456789',
            **LEVELS,
        }

        self.assertEqual(
            lambda_function.requested_extensions(properties),
            [(OBS, {**CONSOLE_OBS, 'prometheus': {'remoteWriteUrl': REMOTE_WRITE_URL}, 'grafana': {'workspaceId': 'g-0123456789'}})],
        )

    def test_sends_metrics_to_cloudwatch_and_no_grafana_without_workspaces(self):
        [(_, configuration)] = lambda_function.requested_extensions(
            {'InstallObservability': 'true', 'RemoteWriteUrl': '', 'GrafanaWorkspaceId': '', **LEVELS}
        )

        self.assertNotIn('prometheus', configuration)
        self.assertNotIn('grafana', configuration)

    def test_installs_other_extensions_as_listed_and_in_order(self):
        properties = {
            'InstallObservability': 'false',
            'Extensions': extensions((OTHER, {'a': 1}), ('sagemaker-third', '{"b": 2}')),
            **LEVELS,
        }

        self.assertEqual(
            lambda_function.requested_extensions(properties), [(OTHER, {'a': 1}), ('sagemaker-third', {'b': 2})]
        )

    def test_uses_the_template_levels_for_an_empty_observability_configuration(self):
        [(_, configuration)] = lambda_function.requested_extensions(
            {'InstallObservability': 'true', 'Extensions': extensions((OBS, {})), **LEVELS}
        )

        self.assertEqual(configuration, {'metricsProvider': TEMPLATE_METRICS})

    def test_skips_a_listed_observability_extension_when_the_node_agent_collects(self):
        properties = {'InstallObservability': 'false', 'Extensions': extensions((OBS, CONSOLE_OBS), (OTHER, {})), **LEVELS}

        self.assertEqual(lambda_function.requested_extensions(properties), [(OTHER, {})])

    def test_installs_only_the_observability_extension_with_observability_only(self):
        properties = {
            'InstallObservability': 'true',
            'ObservabilityOnly': 'true',
            'Extensions': json.dumps([
                {'ExtensionName': OTHER, 'ConfigurationValues': {}},
                {'ExtensionName': OBS, 'ConfigurationValues': {}},
                {'ExtensionName': 'sagemaker-bad', 'ConfigurationValues': 'not json'},
                'not-an-object',
            ]),
            **LEVELS,
        }

        self.assertEqual(lambda_function.requested_extensions(properties), [(OBS, {'metricsProvider': TEMPLATE_METRICS})])
        self.assertEqual(lambda_function.requested_names(properties), [OBS])
        self.assertEqual(lambda_function.listed_extensions(properties)[1], {})
        self.assertEqual(lambda_function.skipped_extensions(properties), [])

    def test_installs_every_extension_but_observability_with_skip_observability(self):
        properties = {
            'InstallObservability': 'false',
            'SkipObservability': 'true',
            'Extensions': json.dumps([
                {'ExtensionName': OBS, 'ConfigurationValues': CONSOLE_OBS},
                {'ExtensionName': OTHER, 'ConfigurationValues': {}},
                {'ExtensionName': 7},
            ]),
            **LEVELS,
        }

        self.assertEqual(lambda_function.requested_extensions(properties), [(OTHER, {})])
        self.assertEqual(lambda_function.requested_names(properties), [OTHER])
        self.assertEqual(list(lambda_function.listed_extensions(properties)[1]), ['ClusterExtensions[2]'])
        # The setup stack installs it, so it isn't reported as skipped here.
        self.assertEqual(lambda_function.skipped_extensions(properties), [])

    def test_reports_a_list_that_is_not_json_only_outside_observability_only(self):
        properties = {'Extensions': 'not json', **LEVELS}

        self.assertEqual(list(lambda_function.listed_extensions({**properties, 'SkipObservability': 'true'})[1]),
                         ['ClusterExtensions'])
        self.assertEqual(lambda_function.listed_extensions({**properties, 'ObservabilityOnly': 'true'})[1], {})

    def test_turns_nccl_metrics_off_when_node_metrics_are_off(self):
        # The extension's schema allows NCCL metrics only with node metrics on.
        levels = {**LEVELS, 'NodeMetricLevel': 'DISABLED', 'NcclMetricLevel': 'BASIC'}

        [(_, configuration)] = lambda_function.requested_extensions({'InstallObservability': 'true', **levels})

        self.assertEqual(configuration['metricsProvider']['nodeMetrics']['level'], 'DISABLED')
        self.assertEqual(configuration['metricsProvider']['ncclMetrics']['level'], 'DISABLED')

    def test_reports_each_entry_that_is_not_valid_and_keeps_the_others(self):
        properties = {
            'InstallObservability': 'false',
            'Extensions': json.dumps([
                {'ExtensionName': OTHER, 'ConfigurationValues': '{"a": 1}'},
                'sagemaker-not-an-object',
                {'ExtensionName': 7},
                {'ConfigurationValues': {}},
                {'ExtensionName': 'sagemaker-bad-json', 'ConfigurationValues': '{not json'},
                {'ExtensionName': 'sagemaker-list', 'ConfigurationValues': '[1]'},
                {'ExtensionName': 'sagemaker-number', 'ConfigurationValues': 5},
                {'ExtensionName': 'sagemaker-empty', 'ConfigurationValues': ''},
                {'ExtensionName': 'sagemaker-missing'},
            ]),
            **LEVELS,
        }

        listed, invalid = lambda_function.listed_extensions(properties)

        self.assertEqual(
            listed, [(OTHER, {'a': 1}), ('sagemaker-empty', {}), ('sagemaker-missing', {})]
        )
        self.assertEqual(
            sorted(invalid),
            ['ClusterExtensions[1]', 'ClusterExtensions[2]', 'ClusterExtensions[3]', 'sagemaker-bad-json',
             'sagemaker-list', 'sagemaker-number'],
        )
        self.assertIn('not a JSON object', invalid['ClusterExtensions[1]'])
        self.assertIn('ExtensionName', invalid['ClusterExtensions[2]'])
        self.assertIn('not JSON', invalid['sagemaker-bad-json'])
        self.assertIn('not a JSON object', invalid['sagemaker-list'])
        self.assertEqual(lambda_function.requested_extensions(properties), listed)

    def test_reports_a_value_that_is_not_a_list_and_still_installs_observability(self):
        properties = {'InstallObservability': 'true', 'Extensions': '{"ExtensionName": "sagemaker-other"}', **LEVELS}

        self.assertEqual(
            lambda_function.listed_extensions(properties),
            ([], {'ClusterExtensions': 'Not installed: ClusterExtensions is not a JSON list'}),
        )
        self.assertEqual(
            lambda_function.requested_extensions(properties), [(OBS, {'metricsProvider': TEMPLATE_METRICS})]
        )

    def test_installs_observability_with_the_template_levels_when_its_entry_is_not_valid(self):
        properties = {'InstallObservability': 'true', 'Extensions': extensions((OBS, '[]')), **LEVELS}

        self.assertEqual(
            lambda_function.requested_extensions(properties), [(OBS, {'metricsProvider': TEMPLATE_METRICS})]
        )

    def test_keeps_nccl_metrics_with_node_metrics_on(self):
        [(_, configuration)] = lambda_function.requested_extensions(
            {'InstallObservability': 'true', **LEVELS, 'NcclMetricLevel': 'BASIC'}
        )

        self.assertEqual(configuration['metricsProvider']['ncclMetrics']['level'], 'ENABLED')


def signed_session(session, token='TOKEN'):
    session.return_value.client.return_value.meta.endpoint_url = 'https://api.sagemaker.us-west-2.amazonaws.com'
    session.return_value.get_credentials.return_value.get_frozen_credentials.return_value = Mock(
        access_key='AKID', secret_key='SECRET', token=token
    )


def http_error(status, body):
    return urllib.error.HTTPError('url', status, 'Reason', {}, io.BytesIO(body))


@patch.dict(os.environ, {'AWS_REGION': 'us-west-2'})
@patch('lambda_function.boto3.session.Session')
@patch('lambda_function.urllib.request.urlopen')
class TestSend(unittest.TestCase):
    def setUp(self):
        # Each test gets its own mocked session, not the one the module cached.
        lambda_function._session = lambda_function._endpoint = None

    def test_signs_an_awsjson_request_to_the_regions_sagemaker_endpoint(self, urlopen, session):
        signed_session(session)
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"ExtensionArn": "arn"}'

        self.assertEqual(lambda_function.send('CreateClusterExtension', {'ClusterArn': CLUSTER_ARN}, 42), {'ExtensionArn': 'arn'})

        request = urlopen.call_args[0][0]
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(request.full_url, 'https://api.sagemaker.us-west-2.amazonaws.com/')
        self.assertEqual(headers['x-amz-target'], 'SageMaker.CreateClusterExtension')
        self.assertEqual(headers['content-type'], 'application/x-amz-json-1.1')
        self.assertIn('Credential=AKID/', headers['authorization'])
        self.assertIn('/us-west-2/sagemaker/aws4_request', headers['authorization'])
        self.assertEqual(headers['x-amz-security-token'], 'TOKEN')
        self.assertEqual(json.loads(request.data), {'ClusterArn': CLUSTER_ARN})
        self.assertEqual(urlopen.call_args[1]['timeout'], 42)

    def test_raises_the_service_error(self, urlopen, session):
        signed_session(session, token=None)
        urlopen.side_effect = http_error(400, b'{"__type": "com.amazon#ValidationException", "message": "bad"}')

        with self.assertRaises(lambda_function.SageMakerError) as raised:
            lambda_function.send('CreateClusterExtension', {}, 60)
        self.assertEqual(raised.exception.code, 'ValidationException')
        self.assertEqual(raised.exception.status, 400)
        self.assertIn('bad', str(raised.exception))

    def test_reports_the_http_status_for_a_body_that_is_not_json(self, urlopen, session):
        signed_session(session)
        urlopen.side_effect = http_error(503, b'<html>Service Unavailable</html>')

        with self.assertRaises(lambda_function.SageMakerError) as raised:
            lambda_function.send('DescribeClusterExtension', {}, 60)
        self.assertEqual(str(raised.exception), 'HTTP 503: Reason')
        self.assertEqual(raised.exception.status, 503)


class TestCall(unittest.TestCase):
    def setUp(self):
        self.send = patch('lambda_function.send').start()
        self.sleep = patch('lambda_function.time.sleep').start()

    def tearDown(self):
        patch.stopall()

    def test_retries_throttling_server_errors_and_timeouts(self):
        self.send.side_effect = [
            lambda_function.SageMakerError('ThrottlingException', 'slow down', 400),
            lambda_function.SageMakerError('HTTP 500', 'Internal Server Error', 500),
            urllib.error.URLError('timed out'),
            TimeoutError('read timed out'),
            {'Extensions': []},
        ]

        self.assertEqual(lambda_function.call('ListClusterExtensions', {}, context()), {'Extensions': []})
        self.assertEqual(self.send.call_count, 5)
        self.assertEqual(self.sleep.call_count, 4)

    def test_retries_a_response_cut_short_and_a_tls_error(self):
        self.send.side_effect = [
            http.client.IncompleteRead(b'{"Ext'),
            ssl.SSLError('record layer failure'),
            http.client.RemoteDisconnected('closed'),
            {'Extensions': []},
        ]

        self.assertEqual(lambda_function.call('ListClusterExtensions', {}, context()), {'Extensions': []})
        self.assertEqual(self.send.call_count, 4)

    def test_does_not_retry_an_error_that_is_not_transient(self):
        self.send.side_effect = KeyError('ExtensionArn')

        with self.assertRaises(KeyError):
            lambda_function.call('CreateClusterExtension', {}, context())
        self.assertEqual(self.send.call_count, 1)

    def test_does_not_retry_a_client_error(self):
        self.send.side_effect = lambda_function.SageMakerError('ValidationException', 'bad', 400)

        with self.assertRaises(lambda_function.SageMakerError):
            lambda_function.call('CreateClusterExtension', {}, context())
        self.assertEqual(self.send.call_count, 1)

    def test_gives_up_after_the_last_attempt(self):
        self.send.side_effect = lambda_function.SageMakerError('ThrottlingException', 'slow down', 400)

        with self.assertRaises(lambda_function.SageMakerError):
            lambda_function.call('ListClusterExtensions', {}, context())
        self.assertEqual(self.send.call_count, lambda_function.MAX_ATTEMPTS)

    def test_gives_up_when_the_time_budget_is_spent(self):
        self.send.side_effect = lambda_function.SageMakerError('ThrottlingException', 'slow down', 400)

        with self.assertRaises(lambda_function.SageMakerError):
            lambda_function.call('ListClusterExtensions', {}, context(remaining=lambda_function.RESPONSE_MARGIN_MS))
        self.assertEqual(self.send.call_count, 1)

    def test_bounds_each_request_by_the_time_budget(self):
        self.send.return_value = {}

        lambda_function.call('ListClusterExtensions', {}, context(remaining=lambda_function.RESPONSE_MARGIN_MS + 20000))

        # 20 s of budget left, under REQUEST_TIMEOUT_SECONDS.
        self.assertEqual(self.send.call_args[0][2], 20)


@patch.dict(os.environ, {'AWS_REGION': 'us-west-2'})
class TestHandler(unittest.TestCase):
    def setUp(self):
        self.call = patch('lambda_function.call').start()
        self.respond = patch('lambda_function.respond').start()
        patch('lambda_function.time.sleep').start()

    def tearDown(self):
        patch.stopall()

    def responses(self, **by_operation):
        by_operation.setdefault('DescribeCluster', {'ClusterStatus': 'Creating'})
        by_operation.setdefault('ListClusterExtensions', {'Extensions': []})
        by_operation.setdefault('AddTags', {})

        def respond(operation, body, context):
            value = by_operation[operation]
            if isinstance(value, Exception):
                raise value
            return value(body) if callable(value) else value

        self.call.side_effect = respond

    def sent(self):
        _, _, status, data, physical_id = self.respond.call_args[0]
        return status, {k: json.loads(v) for k, v in data.items()}, physical_id

    def calls(self, operation):
        return [c[0][1] for c in self.call.call_args_list if c[0][0] == operation]

    def test_create_installs_each_extension_while_the_cluster_is_created_and_waits(self):
        arns = {OBS: OBS_ARN, OTHER: OTHER_ARN}
        statuses = {OBS_ARN: iter(['Creating', 'Active']), OTHER_ARN: iter(['Active'])}
        self.responses(
            CreateClusterExtension=lambda body: {'ExtensionArn': arns[body['ExtensionName']]},
            DescribeClusterExtension=lambda body: {'ExtensionDetails': {'Status': next(statuses[body['ExtensionArn']])}},
        )

        lambda_function.lambda_handler(event(Extensions=extensions((OTHER, {'a': 1}))), context())

        creates = self.calls('CreateClusterExtension')
        self.assertEqual([c['ExtensionName'] for c in creates], [OTHER, OBS])
        self.assertEqual([c['ClientToken'] for c in creates], [token(OTHER), token(OBS)])
        self.assertEqual(json.loads(creates[0]['ConfigurationValues']), {'a': 1})
        self.assertEqual(self.calls('DescribeCluster'), [{'ClusterName': CLUSTER_ARN}])
        # The cluster names the stack whose outputs record the installs.
        self.assertEqual(
            self.calls('AddTags'),
            [{'ResourceArn': CLUSTER_ARN, 'Tags': [{'Key': 'hyperpod-cluster-extensions-stack', 'Value': 'stack-1'}]}],
        )
        status, data, physical_id = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'Active', OBS: 'Active'})
        self.assertEqual(data['InstallReason'][OBS], f'{OBS_ARN} is Active')
        # The extensions requested at create, for Update: installed, then not installed.
        self.assertEqual(physical_id, f'{CLUSTER_ARN}/extensions/{OTHER},{OBS}/')

    def test_create_reports_a_listed_observability_extension_the_stack_skips(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OTHER_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(
            event(InstallObservability='false', Extensions=extensions((OBS, CONSOLE_OBS), (OTHER, {}))), context()
        )

        self.assertEqual([c['ExtensionName'] for c in self.calls('CreateClusterExtension')], [OTHER])
        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'Active', OBS: 'NotInstalled'})
        self.assertIn('observability is off in this stack', data['InstallReason'][OBS])

    def test_create_continues_with_the_others_when_one_fails(self):
        def create(body):
            if body['ExtensionName'] == OTHER:
                raise lambda_function.SageMakerError('ValidationException', 'not in the catalog', 400)
            return {'ExtensionArn': OBS_ARN}

        self.responses(CreateClusterExtension=create, DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}})

        lambda_function.lambda_handler(event(Extensions=extensions((OTHER, {}))), context())

        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'NotInstalled', OBS: 'Active'})
        self.assertIn('not in the catalog', data['InstallReason'][OTHER])
        self.assertEqual(self.sent()[2], f'{CLUSTER_ARN}/extensions/{OBS}/{OTHER}')

    def test_create_installs_without_host_profiling_on_a_version_without_it(self):
        creates = []

        def create(body):
            creates.append(body)
            if len(creates) == 1:
                raise lambda_function.SageMakerError(
                    'ValidationException',
                    # The message SageMaker alpha returned for extension 0.2.0.
                    'ConfigurationValues does not match the schema for extension sagemaker-observability version 0.2.0: '
                    '/: has properties the schema does not permit; permitted are "grafana", "metricsProvider", '
                    '"prometheus"',
                    400,
                )
            return {'ExtensionArn': OBS_ARN}

        self.responses(CreateClusterExtension=create, DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}})

        lambda_function.lambda_handler(event(Extensions=extensions((OBS, CONSOLE_OBS))), context())

        self.assertNotIn('profilesProvider', json.loads(creates[1]['ConfigurationValues']))
        # A new token: the first one belongs to the rejected request.
        self.assertEqual(creates[1]['ClientToken'], token(OBS, 'no-profiling'))
        _, data, _ = self.sent()
        self.assertEqual(data['InstallStatus'], {OBS: 'Active'})
        self.assertTrue(data['InstallReason'][OBS].startswith('Host profiling is not on'))

    def test_create_does_not_drop_host_profiling_for_a_rejection_under_a_nested_path(self):
        self.responses(
            CreateClusterExtension=lambda_function.SageMakerError(
                'ValidationException',
                'ConfigurationValues does not match the schema for extension sagemaker-observability version 0.3.0: '
                '/metricsProvider: has properties the schema does not permit; permitted are "nodeMetrics", '
                '"acceleratedComputeMetrics"',
                400,
            )
        )

        lambda_function.lambda_handler(event(Extensions=extensions((OBS, CONSOLE_OBS))), context())

        # Not a version without host profiling: the request is reported as it failed, not retried without it.
        self.assertEqual(len(self.calls('CreateClusterExtension')), 1)
        _, data, _ = self.sent()
        self.assertEqual(data['InstallStatus'], {OBS: 'NotInstalled'})
        self.assertIn('/metricsProvider', data['InstallReason'][OBS])

    def test_create_does_not_drop_host_profiling_for_a_version_that_has_it(self):
        self.responses(
            CreateClusterExtension=lambda_function.SageMakerError(
                'ValidationException',
                'does not match the schema: "profilesProvider.hostProfiling" bad value; permitted are "grafana", '
                '"metricsProvider", "profilesProvider"',
                400,
            )
        )

        lambda_function.lambda_handler(event(Extensions=extensions((OBS, CONSOLE_OBS))), context())

        self.assertEqual(len(self.calls('CreateClusterExtension')), 1)
        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'NotInstalled'})

    def test_create_reuses_an_extension_a_retried_request_installed(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Creating'}]},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(event(), context())

        self.assertEqual(self.calls('CreateClusterExtension'), [])
        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'Active'})

    def test_create_reports_a_failed_install_left_on_the_cluster_as_it_is(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'CreateFailed'}]},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'CreateFailed'}},
        )

        lambda_function.lambda_handler(event(), context())

        # The stack never removes an extension.
        self.assertEqual([c[0][0] for c in self.call.call_args_list if 'Delete' in c[0][0]], [])
        self.assertEqual(self.calls('CreateClusterExtension'), [])
        _, data, _ = self.sent()
        self.assertEqual(data['InstallStatus'], {OBS: 'CreateFailed'})
        self.assertEqual(data['InstallReason'][OBS], f'{OBS_ARN} is CreateFailed')

    def test_create_with_skip_observability_installs_the_others_without_tagging_the_cluster(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OTHER_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(
            event(InstallObservability='false', SkipObservability='true',
                  Extensions=extensions((OBS, CONSOLE_OBS), (OTHER, {}))),
            context(),
        )

        self.assertEqual([c['ExtensionName'] for c in self.calls('CreateClusterExtension')], [OTHER])
        # The cluster's tag names the setup stack, which installs the observability extension.
        self.assertEqual(self.calls('AddTags'), [])
        status, data, physical_id = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'Active'})
        self.assertEqual(physical_id, f'{CLUSTER_ARN}/extensions/{OTHER}/')

    def test_create_with_skip_observability_and_only_observability_listed_installs_nothing(self):
        self.responses()

        lambda_function.lambda_handler(
            event(InstallObservability='false', SkipObservability='true', Extensions=extensions((OBS, CONSOLE_OBS))),
            context(),
        )

        self.assertEqual(self.calls('CreateClusterExtension'), [])
        status, data, _ = self.sent()
        self.assertEqual((status, data['InstallStatus']), ('SUCCESS', {}))

    def test_create_with_observability_only_installs_only_observability(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(
            event(ObservabilityOnly='true', Extensions=extensions((OTHER, {}), (OBS, CONSOLE_OBS))), context()
        )

        self.assertEqual([c['ExtensionName'] for c in self.calls('CreateClusterExtension')], [OBS])
        self.assertEqual(len(self.calls('AddTags')), 1)
        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'Active'})

    def test_create_installs_even_when_the_cluster_cannot_be_tagged(self):
        self.responses(
            AddTags=lambda_function.SageMakerError('AccessDeniedException', 'not authorized', 400),
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(event(), context())

        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'Active'})

    def test_create_tags_the_cluster_before_waiting_for_it(self):
        self.responses(DescribeCluster=lambda_function.SageMakerError('ValidationException', 'cannot describe', 400))

        lambda_function.lambda_handler(event(), context())

        self.assertEqual(len(self.calls('AddTags')), 1)
        self.assertEqual([c[0][0] for c in self.call.call_args_list][:2], ['AddTags', 'DescribeCluster'])

    def test_create_reports_still_creating_when_time_runs_out(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Creating'}},
        )

        lambda_function.lambda_handler(event(), context(remaining=lambda_function.RESPONSE_MARGIN_MS + 5000))

        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OBS: 'Creating'})

    def test_create_stops_waiting_after_the_maximum_wait(self):
        clock = [0]
        self.responses(
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Creating'}},
        )

        def sleep(seconds):
            clock[0] += seconds

        with patch('lambda_function.time.time', side_effect=lambda: clock[0]), \
                patch('lambda_function.time.sleep', side_effect=sleep):
            lambda_function.lambda_handler(event(), context())

        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'Creating'})
        self.assertEqual(clock[0], lambda_function.MAX_WAIT_SECONDS)
        self.assertEqual(
            len(self.calls('DescribeClusterExtension')),
            lambda_function.MAX_WAIT_SECONDS // lambda_function.POLL_SECONDS + 1,
        )

    def test_setup_stack_waits_longer_and_keeps_an_extension_still_creating(self):
        # The observability setup stack waits SETUP_STACK_MAX_WAIT_SECONDS for the outcome; still Creating then is not a
        # failure (the outcome isn't known), so the stack isn't rolled back.
        clock = [0]
        self.responses(
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Creating'}},
        )

        def sleep(seconds):
            clock[0] += seconds

        with patch('lambda_function.time.time', side_effect=lambda: clock[0]), \
                patch('lambda_function.time.sleep', side_effect=sleep):
            lambda_function.lambda_handler(event(FailOnFailedInstall='true'), context())

        status, data, physical_id = self.sent()
        self.assertEqual((status, data['InstallStatus']), ('SUCCESS', {OBS: 'Creating'}))
        self.assertFalse(physical_id.endswith('/failed'))
        self.assertEqual(clock[0], lambda_function.SETUP_STACK_MAX_WAIT_SECONDS)

    def test_create_keeps_polling_after_a_status_that_cannot_be_read(self):
        statuses = iter([
            {'ExtensionDetails': {'Status': 'Creating'}},
            urllib.error.URLError('reset'),
            {'ExtensionDetails': {'Status': 'Active'}},
        ])

        def describe(body):
            value = next(statuses)
            if isinstance(value, Exception):
                raise value
            return value

        self.responses(CreateClusterExtension={'ExtensionArn': OBS_ARN}, DescribeClusterExtension=describe)

        lambda_function.lambda_handler(event(), context())

        _, data, _ = self.sent()
        self.assertEqual(data['InstallStatus'], {OBS: 'Active'})
        # The error is gone once the status is read again.
        self.assertEqual(data['InstallReason'][OBS], f'{OBS_ARN} is Active')

    def test_create_keeps_the_last_status_read_when_the_time_runs_out_on_a_failed_read(self):
        # Time for one poll after the first read, and none after the failed one.
        remaining = iter([900000, lambda_function.RESPONSE_MARGIN_MS])
        statuses = iter([{'ExtensionDetails': {'Status': 'Creating'}}, urllib.error.URLError('reset')])

        def describe(body):
            value = next(statuses)
            if isinstance(value, Exception):
                raise value
            return value

        self.responses(CreateClusterExtension={'ExtensionArn': OBS_ARN}, DescribeClusterExtension=describe)
        ctx = Mock(log_stream_name='l', get_remaining_time_in_millis=Mock(side_effect=lambda: next(remaining, 0)))

        on_create_data, _ = lambda_function.on_create(event(), ctx)

        data = {k: json.loads(v) for k, v in on_create_data.items()}
        self.assertEqual(data['InstallStatus'], {OBS: 'Creating'})
        self.assertIn('reset', data['InstallReason'][OBS])

    def test_create_reports_unknown_after_the_status_cannot_be_read_repeatedly(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension=lambda_function.SageMakerError('AccessDeniedException', 'not authorized', 400),
        )

        lambda_function.lambda_handler(event(), context())

        self.assertEqual(len(self.calls('DescribeClusterExtension')), lambda_function.MAX_DESCRIBE_FAILURES)
        _, data, _ = self.sent()
        self.assertEqual(data['InstallStatus'], {OBS: 'Unknown'})
        self.assertIn('not authorized', data['InstallReason'][OBS])

    def test_create_installs_nothing_on_a_failed_cluster_without_failing_the_stack(self):
        self.responses(DescribeCluster={'ClusterStatus': 'Failed'})

        lambda_function.lambda_handler(event(), context())

        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OBS: 'NotInstalled'})
        self.assertIn('is Failed', data['InstallReason'][OBS])
        self.assertIn('is Failed', data['InstallReason']['ClusterExtensions'])
        self.assertEqual(self.calls('CreateClusterExtension'), [])

    def test_create_reports_every_requested_extension_when_the_run_fails_as_a_whole(self):
        self.responses(ListClusterExtensions=lambda_function.SageMakerError('AccessDeniedException', 'no list', 400))

        lambda_function.lambda_handler(
            event(Extensions=json.dumps([{'ExtensionName': OTHER}, {'ExtensionName': 'sagemaker-bad', 'ConfigurationValues': 5}])),
            context(),
        )

        status, data, physical_id = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'NotInstalled', OBS: 'NotInstalled', 'sagemaker-bad': 'NotInstalled'})
        for name in (OTHER, OBS, 'ClusterExtensions'):
            self.assertIn('no list', data['InstallReason'][name])
        # The invalid entry keeps its own reason.
        self.assertIn('not a JSON object', data['InstallReason']['sagemaker-bad'])
        self.assertEqual(physical_id, f'{CLUSTER_ARN}/extensions//{OTHER},sagemaker-bad,{OBS}')

    def test_create_reports_the_listed_extensions_when_they_cannot_be_prepared(self):
        properties = {k: v for k, v in event(Extensions=extensions((OTHER, {})))['ResourceProperties'].items()
                      if k != 'NodeMetricLevel'}

        lambda_function.lambda_handler({**event(), 'ResourceProperties': properties}, context())

        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'NotInstalled', OBS: 'NotInstalled'})
        self.assertIn('NodeMetricLevel', data['InstallReason']['ClusterExtensions'])
        self.assertIn('NodeMetricLevel', data['InstallReason'][OTHER])

    def test_create_waits_while_the_roles_policy_applies(self):
        clusters = iter([lambda_function.SageMakerError('AccessDeniedException', 'not authorized'), {'ClusterStatus': 'InService'}])

        def describe(body):
            value = next(clusters)
            if isinstance(value, Exception):
                raise value
            return value

        self.responses(
            DescribeCluster=describe,
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(event(), context())

        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'Active'})

    def test_create_reports_extensions_that_are_not_json_without_failing(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OBS_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(event(Extensions='{not json'), context())

        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OBS: 'Active', 'ClusterExtensions': 'NotInstalled'})
        self.assertIn('not JSON', data['InstallReason']['ClusterExtensions'])
        # The observability the stack collects with is still installed, from the template's levels.
        [create] = self.calls('CreateClusterExtension')
        self.assertEqual(json.loads(create['ConfigurationValues']), {'metricsProvider': TEMPLATE_METRICS})

    def test_create_installs_the_valid_entries_and_reports_the_others(self):
        self.responses(
            CreateClusterExtension=lambda body: {'ExtensionArn': {OBS: OBS_ARN, OTHER: OTHER_ARN}[body['ExtensionName']]},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(
            event(Extensions=json.dumps([
                {'ExtensionName': OTHER},
                {'ExtensionName': 'sagemaker-bad', 'ConfigurationValues': '{not json'},
                {'ExtensionName': OBS, 'ConfigurationValues': '"a string"'},
                ['not', 'an', 'entry'],
            ])),
            context(),
        )

        self.assertEqual([c['ExtensionName'] for c in self.calls('CreateClusterExtension')], [OTHER, OBS])
        status, data, _ = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(
            data['InstallStatus'],
            {OTHER: 'Active', OBS: 'Active', 'sagemaker-bad': 'NotInstalled', 'ClusterExtensions[3]': 'NotInstalled'},
        )
        self.assertIn('not JSON', data['InstallReason']['sagemaker-bad'])
        self.assertIn('not a JSON object', data['InstallReason']['ClusterExtensions[3]'])
        # The observability entry that isn't valid is installed with the template's levels, and says so.
        self.assertTrue(data['InstallReason'][OBS].startswith('Not installed: ConfigurationValues is not a JSON object'))
        self.assertIn("installed with the template's metric levels", data['InstallReason'][OBS])
        # The invalid entry with a name is recorded for Update; the one without isn't.
        self.assertEqual(self.sent()[2], f'{CLUSTER_ARN}/extensions/{OTHER},{OBS}/sagemaker-bad')

    def test_create_records_a_skipped_extension_for_update(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OTHER_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(
            event(InstallObservability='false', Extensions=extensions((OBS, {}), (OTHER, {}))), context()
        )
        physical_id = self.sent()[2]
        self.assertEqual(physical_id, f'{CLUSTER_ARN}/extensions/{OTHER}/{OBS}')

        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN, 'Status': 'Active'}]}
        )
        self.update(physical_id, InstallObservability='false', Extensions=extensions((OBS, {}), (OTHER, {})))

        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'NotInstalled', OTHER: 'Active'})

    def test_a_malformed_event_still_succeeds(self):
        lambda_function.lambda_handler({**event(), 'ResourceProperties': {}}, context())

        self.assertEqual(self.sent()[0], 'SUCCESS')

    def update(self, physical_id, **properties):
        """An Update as CloudFormation sends it: the physical ID from create, and the old and new properties."""
        lambda_function.lambda_handler(
            {
                **event('Update', **properties),
                'OldResourceProperties': event(Extensions=extensions((OTHER, {})))['ResourceProperties'],
                'PhysicalResourceId': physical_id,
            },
            context(),
        )

    def test_update_changes_nothing_and_reports_the_current_status(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Degraded'}]}
        )
        physical_id = f'{CLUSTER_ARN}/extensions/{OBS}/{OTHER}'

        self.update(physical_id, Extensions=extensions((OTHER, {})))

        self.assertEqual([c[0][0] for c in self.call.call_args_list], ['ListClusterExtensions'])
        _, data, sent_id = self.sent()
        # OTHER wasn't installed at create and still isn't on the cluster.
        self.assertEqual(data['InstallStatus'], {OTHER: 'NotInstalled', OBS: 'Degraded'})
        self.assertEqual(sent_id, physical_id)

    def test_update_reports_an_extension_removed_from_the_cluster_as_removed(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN, 'Status': 'Active'}]}
        )

        self.update(f'{CLUSTER_ARN}/extensions/{OBS},{OTHER}/')

        _, data, _ = self.sent()
        self.assertEqual(data['InstallStatus'], {OBS: 'Removed', OTHER: 'Active'})
        self.assertEqual(data['InstallReason'], {OBS: 'Removed from the cluster after the stack installed it'})

    def test_update_after_the_cluster_was_replaced_reports_nothing_installed_on_the_new_one(self):
        self.responses()
        new_arn = 'arn:aws:sagemaker:us-west-2:123456789012:cluster/new456'

        self.update(f'{CLUSTER_ARN}/extensions/{OBS},{OTHER}/', ClusterArn=new_arn, Extensions=extensions((OTHER, {})))

        # Not Removed: the old cluster's extensions went with it. Nothing is installed on the new one.
        self.assertEqual(self.call.call_args_list, [])
        status, data, physical_id = self.sent()
        self.assertEqual(status, 'SUCCESS')
        self.assertEqual(data['InstallStatus'], {OTHER: 'NotInstalled', OBS: 'NotInstalled'})
        self.assertEqual(data['InstallReason'][OBS], lambda_function.REPLACED_REASON)
        self.assertEqual(physical_id, f'{new_arn}/extensions//{OTHER},{OBS}')

    def test_update_reports_an_extension_installed_after_create(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN, 'Status': 'Active'}]}
        )

        self.update(f'{CLUSTER_ARN}/extensions//{OTHER}')

        # The console installed the one the stack couldn't.
        self.assertEqual(self.sent()[1]['InstallStatus'], {OTHER: 'Active'})

    def test_update_reports_nothing_for_a_resource_created_before_the_physical_id_recorded_the_names(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Active'}]}
        )

        self.update(f'{CLUSTER_ARN}/extensions')

        _, data, physical_id = self.sent()
        self.assertEqual(data['InstallStatus'], {})
        self.assertEqual(physical_id, f'{CLUSTER_ARN}/extensions')

    def test_delete_leaves_the_extensions_on_the_cluster(self):
        self.responses()

        lambda_function.lambda_handler({**event('Delete'), 'PhysicalResourceId': f'{CLUSTER_ARN}/extensions'}, context())

        # Deleting the cluster removes them; an update that drops the resource must not remove them from a live one.
        self.assertEqual(self.call.call_args_list, [])
        self.assertEqual(self.sent()[0], 'SUCCESS')

    def test_delete_for_the_old_physical_id_after_the_cluster_was_replaced_removes_nothing(self):
        self.responses()
        old_id = f'{CLUSTER_ARN}/extensions/{OBS},{OTHER}/'

        # After an update that replaced the cluster returned a new physical ID, CloudFormation deletes the old one.
        lambda_function.lambda_handler({**event('Delete'), 'PhysicalResourceId': old_id}, context())

        self.assertEqual(self.call.call_args_list, [])
        status, _, physical_id = self.sent()
        self.assertEqual((status, physical_id), ('SUCCESS', old_id))

    def test_names_with_a_slash_or_comma_survive_create_then_update(self):
        self.responses(
            CreateClusterExtension={'ExtensionArn': OTHER_ARN},
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )
        properties = dict(
            InstallObservability='false',
            Extensions=json.dumps([{'ExtensionName': 'bad/name,x', 'ConfigurationValues': 1}, {'ExtensionName': OTHER}]),
        )

        lambda_function.lambda_handler(event(**properties), context())
        physical_id = self.sent()[2]
        self.assertEqual(physical_id, f'{CLUSTER_ARN}/extensions/{OTHER}/bad%2Fname%2Cx')

        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN, 'Status': 'Active'}]}
        )
        self.update(physical_id, **properties)

        self.assertEqual(self.sent()[1]['InstallStatus'], {'bad/name,x': 'NotInstalled', OTHER: 'Active'})
        self.assertEqual(self.sent()[2], physical_id)

    def test_update_reports_only_the_extensions_requested_at_create(self):
        self.responses(
            ListClusterExtensions={'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Active'}]}
        )

        # The old properties list OTHER too, but the physical ID from create is what records the request.
        self.update(f'{CLUSTER_ARN}/extensions/{OBS}/', Extensions=extensions((OTHER, {}), ('sagemaker-third', {})))

        # OTHER and the third were added later and never installed by the stack, so they aren't failed installs.
        self.assertEqual(self.sent()[1]['InstallStatus'], {OBS: 'Active'})

    def test_lists_every_page_of_extensions(self):
        pages = iter([
            {'Extensions': [{'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN}], 'NextToken': 't1'},
            {'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Creating'}]},
        ])
        self.responses(
            ListClusterExtensions=lambda body: next(pages),
            DescribeClusterExtension={'ExtensionDetails': {'Status': 'Active'}},
        )

        lambda_function.lambda_handler(event(), context())

        self.assertEqual(self.calls('ListClusterExtensions')[1], {'ClusterArn': CLUSTER_ARN, 'NextToken': 't1'})
        # Found on the second page, so reused rather than created again.
        self.assertEqual(self.calls('CreateClusterExtension'), [])


class TestFailOnFailedInstall(unittest.TestCase):
    """The observability setup stack's install, which fails the resource when an extension fails to install."""

    setUp, tearDown = TestHandler.setUp, TestHandler.tearDown
    responses, sent, calls = TestHandler.responses, TestHandler.sent, TestHandler.calls

    def create(self, status=None, create_error=None, **properties):
        def create(body):
            if create_error:
                raise create_error
            return {'ExtensionArn': OBS_ARN}

        describe = status if isinstance(status, Exception) else {'ExtensionDetails': {'Status': status}}
        self.responses(CreateClusterExtension=create, DescribeClusterExtension=describe)
        # Time for the installs and a status read, not for waiting on Creating.
        lambda_function.lambda_handler(event(**properties), context(remaining=lambda_function.RESPONSE_MARGIN_MS + 5000))
        status, data, physical_id = self.sent()
        return status, data, physical_id, self.respond.call_args.kwargs.get('reason', '')

    def test_fails_when_an_extension_fails_to_install(self):
        validation = lambda_function.SageMakerError('ValidationException', f'{OBS_ARN}: schema', 400)
        unreadable = lambda_function.SageMakerError('AccessDeniedException', 'no describe', 400)
        for name, kwargs, expected in (
            ('CreateFailed', {'status': 'CreateFailed'}, 'CreateFailed'),
            ('NotInstalled', {'create_error': validation}, 'NotInstalled'),
            ('Unknown', {'status': unreadable}, 'Unknown'),
        ):
            with self.subTest(name):
                self.respond.reset_mock()
                status, data, physical_id, reason = self.create(FailOnFailedInstall='true', **kwargs)
                self.assertEqual(status, 'FAILED')
                self.assertEqual(reason, f'Cluster extensions failed to install: {OBS} is {expected}')
                self.assertNotIn('arn:', reason)
                # The outcome and the extensions tried stay, for the rollback's Delete, which the marker tells.
                self.assertEqual(data['InstallStatus'], {OBS: expected})
                self.assertTrue(physical_id.startswith(f'{CLUSTER_ARN}/extensions/'))
                self.assertTrue(physical_id.endswith('/failed'))
                self.assertEqual(lambda_function.created_with(physical_id)[0], [OBS] if expected != 'NotInstalled' else [])

    def test_succeeds_when_the_extensions_are_active_degraded_or_still_creating(self):
        for status in ('Active', 'Degraded', 'Creating'):
            with self.subTest(status):
                self.respond.reset_mock()
                result, _, physical_id, _ = self.create(status, FailOnFailedInstall='true')
                self.assertEqual((result, physical_id), ('SUCCESS', f'{CLUSTER_ARN}/extensions/{OBS}/'))

    def test_only_the_observability_extension_fails_the_resource(self):
        arns = {OBS: OBS_ARN, OTHER: OTHER_ARN}
        statuses = {OBS_ARN: 'Active', OTHER_ARN: 'CreateFailed'}
        self.responses(
            CreateClusterExtension=lambda body: {'ExtensionArn': arns[body['ExtensionName']]},
            DescribeClusterExtension=lambda body: {'ExtensionDetails': {'Status': statuses[body['ExtensionArn']]}},
        )
        lambda_function.lambda_handler(
            event(FailOnFailedInstall='true', Extensions=extensions((OTHER, {}))),
            context(remaining=lambda_function.RESPONSE_MARGIN_MS + 5000),
        )
        status, data, physical_id = self.sent()
        self.assertEqual(status, 'SUCCESS')
        # Reported as before.
        self.assertEqual(data['InstallStatus'], {OTHER: 'CreateFailed', OBS: 'Active'})
        self.assertFalse(lambda_function.create_failed(physical_id))

    def test_fails_when_the_run_fails_as_a_whole(self):
        self.responses(DescribeCluster={'ClusterStatus': 'Failed'})
        lambda_function.lambda_handler(event(FailOnFailedInstall='true'), context())
        self.assertEqual(self.sent()[0], 'FAILED')
        self.assertEqual(self.sent()[2], f'{CLUSTER_ARN}/extensions//{OBS}/failed')
        self.assertEqual(
            self.respond.call_args.kwargs['reason'], f'Cluster extensions failed to install: {OBS} is NotInstalled'
        )

    def test_still_succeeds_without_the_property(self):
        result, _, physical_id, reason = self.create('CreateFailed')
        self.assertEqual((result, physical_id, reason), ('SUCCESS', f'{CLUSTER_ARN}/extensions/{OBS}/', ''))

    def delete(self, on_cluster, physical_id=f'{CLUSTER_ARN}/extensions/{OBS},{OTHER}/sagemaker-never', **properties):
        listings = iter(on_cluster)
        self.responses(ListClusterExtensions=lambda body: next(listings), DeleteClusterExtension={})
        lambda_function.lambda_handler({**event('Delete', **properties), 'PhysicalResourceId': physical_id}, context())
        return self.sent()[0]

    def test_delete_removes_only_the_extensions_that_failed_to_install(self):
        listed = {'Extensions': [
            {'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'CreateFailed'},
            {'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN, 'Status': 'Degraded'},
            {'ExtensionName': 'sagemaker-console', 'ExtensionArn': 'arn-console', 'Status': 'CreateFailed'},
        ]}
        deleting = {'Extensions': [{**listed['Extensions'][0], 'Status': 'Deleting'}, listed['Extensions'][1]]}
        gone = {'Extensions': [listed['Extensions'][1]]}

        status = self.delete([listed, deleting, gone], FailOnFailedInstall='true')

        self.assertEqual(status, 'SUCCESS')
        # Not the Degraded one, nor one this resource didn't try.
        self.assertEqual(self.calls('DeleteClusterExtension'), [{'ClusterArn': CLUSTER_ARN, 'ExtensionArn': OBS_ARN}])
        self.assertEqual(len(self.calls('ListClusterExtensions')), 3)

    def test_delete_succeeds_when_the_removal_fails(self):
        self.responses(ListClusterExtensions=lambda_function.SageMakerError('AccessDeniedException', 'no list', 400))
        lambda_function.lambda_handler(
            {**event('Delete', FailOnFailedInstall='true'), 'PhysicalResourceId': f'{CLUSTER_ARN}/extensions/{OBS}/'},
            context(),
        )
        self.assertEqual(self.sent()[0], 'SUCCESS')

    def test_delete_after_a_failed_create_removes_observability_unless_active_or_degraded(self):
        for status, removed in (('Creating', True), ('Updating', True), ('Degraded', False), ('Active', False)):
            with self.subTest(status):
                self.call.reset_mock()
                self.respond.reset_mock()
                listed = {'Extensions': [
                    {'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': status},
                    {'ExtensionName': OTHER, 'ExtensionArn': OTHER_ARN, 'Status': 'Creating'},
                ]}
                gone = {'Extensions': [listed['Extensions'][1]]}
                # Counted Unknown at create (its reads failed), though it was still creating.
                self.assertEqual(
                    self.delete([listed, gone], physical_id=f'{CLUSTER_ARN}/extensions/{OBS},{OTHER}//failed',
                                FailOnFailedInstall='true'),
                    'SUCCESS',
                )
                expected = [{'ClusterArn': CLUSTER_ARN, 'ExtensionArn': OBS_ARN}] if removed else []
                # The other extension, still creating, isn't the stack's to remove.
                self.assertEqual(self.calls('DeleteClusterExtension'), expected)

    def test_delete_without_the_marker_keeps_a_creating_observability_extension(self):
        listed = {'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Creating'}]}
        self.assertEqual(self.delete([listed], physical_id=f'{CLUSTER_ARN}/extensions/{OBS}/', FailOnFailedInstall='true'),
                         'SUCCESS')
        self.assertEqual(self.calls('DeleteClusterExtension'), [])

    def test_delete_after_a_failed_create_deletes_no_workspace(self):
        listed = {'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'Active'}]}
        with patch('lambda_function.boto3.client') as client:
            self.delete([listed], physical_id=f'{CLUSTER_ARN}/extensions/{OBS}//failed', FailOnFailedInstall='true')
        # The workspace cleanup resource does.
        client.assert_not_called()

    def test_marks_a_failed_create_within_the_physical_id_limit(self):
        names = [f'sagemaker-{i:04d}-' + 'x' * 40 for i in range(30)]
        physical_id = lambda_function.failed_physical_id(lambda_function.physical_resource_id(CLUSTER_ARN, names, []))
        self.assertLessEqual(len(physical_id), lambda_function.MAX_PHYSICAL_ID_BYTES)
        self.assertTrue(lambda_function.create_failed(physical_id))
        self.assertFalse(lambda_function.create_failed(f'{CLUSTER_ARN}/extensions/{OBS}/failed'))

    def test_delete_removes_nothing_without_the_property(self):
        listed = {'Extensions': [{'ExtensionName': OBS, 'ExtensionArn': OBS_ARN, 'Status': 'CreateFailed'}]}
        self.assertEqual(self.delete([listed]), 'SUCCESS')
        self.assertEqual(self.call.call_args_list, [])


SETUP_STACK_ID = 'arn:aws:cloudformation:us-west-2:123456789012:stack/p-observability/uuid-1'


# The setup stack's create began then; workspaces created before it are an earlier stack's.
CREATE_STARTED = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
BEFORE, AFTER = CREATE_STARTED - timedelta(hours=1), CREATE_STARTED + timedelta(minutes=3)


def stack_event(status, physical_id=SETUP_STACK_ID, timestamp=CREATE_STARTED):
    return {'PhysicalResourceId': physical_id, 'ResourceStatus': status, 'Timestamp': timestamp}


def not_found(operation):
    return ClientError({'Error': {'Code': 'ResourceNotFoundException', 'Message': 'gone'}}, operation)


class TestWorkspaceCleanup(unittest.TestCase):
    """Custom::ObservabilityWorkspaceCleanup, which deletes the workspaces of a setup stack that never completed."""

    # Newest first: OnFailure DELETE after a failed create, and a delete after a completed create.
    NEVER_CREATED = [stack_event('DELETE_IN_PROGRESS'), stack_event('CREATE_FAILED', 'nested'), stack_event('CREATE_IN_PROGRESS')]
    COMPLETED = [stack_event('DELETE_IN_PROGRESS'), stack_event('CREATE_COMPLETE'), stack_event('CREATE_IN_PROGRESS')]

    def setUp(self):
        self.respond = patch('lambda_function.respond').start()
        patch('lambda_function.time.sleep').start()
        self.clients = {name: MagicMock() for name in ('cloudformation', 'amp', 'grafana')}
        patch('lambda_function.boto3.client', side_effect=lambda name, **_: self.clients[name]).start()
        tagged = {'SageMaker': 'true'}
        amp_pages = [{'workspaces': [
            {'workspaceId': 'ws-other', 'alias': 'p-ampws-old', 'tags': tagged, 'createdAt': AFTER},
            {'workspaceId': 'ws-untagged', 'alias': 'p-ampws', 'tags': {}, 'createdAt': AFTER},
            # The same alias, left by an earlier stack with the same ResourceNamePrefix.
            {'workspaceId': 'ws-earlier', 'alias': 'p-ampws', 'tags': tagged, 'createdAt': BEFORE},
        ]}, {'workspaces': [{'workspaceId': 'ws-1', 'alias': 'p-ampws', 'tags': tagged, 'createdAt': AFTER}]}]
        grafana_pages = [{'workspaces': [
            {'id': 'g-other', 'name': 'q-amgws', 'tags': tagged, 'created': AFTER},
            {'id': 'g-earlier', 'name': 'p-amgws', 'tags': tagged, 'created': BEFORE},
            {'id': 'g-1', 'name': 'p-amgws', 'tags': tagged, 'created': AFTER},
        ]}]
        self.clients['amp'].get_paginator.return_value.paginate.return_value = amp_pages
        self.clients['grafana'].get_paginator.return_value.paginate.return_value = grafana_pages
        for name in ('amp', 'grafana'):
            self.clients[name].describe_workspace.side_effect = not_found('DescribeWorkspace')

    def tearDown(self):
        patch.stopall()

    def handle(self, request_type='Delete', events=NEVER_CREATED, **properties):
        self.clients['cloudformation'].describe_stack_events.return_value = {'StackEvents': list(events)}
        e = {
            'RequestType': request_type,
            'ResourceType': 'Custom::ObservabilityWorkspaceCleanup',
            'ResponseURL': 'https://cfn.example/response',
            'StackId': SETUP_STACK_ID,
            'RequestId': 'request-1',
            'LogicalResourceId': 'WorkspaceCleanup',
            'ResourceProperties': {
                'ResourceNamePrefix': 'p', 'CreatePrometheusWorkspace': 'true', 'CreateGrafanaWorkspace': 'true', **properties,
            },
        }
        lambda_function.lambda_handler(e, context())
        self.assertEqual(self.respond.call_args[0][2], 'SUCCESS')
        return self.respond.call_args[0][4]

    def deleted(self):
        return {name: [c.kwargs['workspaceId'] for c in self.clients[name].delete_workspace.call_args_list]
                for name in ('amp', 'grafana')}

    def test_create_and_update_do_nothing(self):
        for request_type in ('Create', 'Update'):
            with self.subTest(request_type):
                physical_id = self.handle(request_type)
                self.assertEqual(physical_id, f'{SETUP_STACK_ID}/workspace-cleanup')
        self.assertEqual(self.deleted(), {'amp': [], 'grafana': []})
        self.clients['cloudformation'].describe_stack_events.assert_not_called()

    def test_deletes_the_workspaces_of_a_stack_that_never_completed_its_create(self):
        self.handle()
        # Only the exact alias and name, tagged SageMaker, and created since this stack's create began: not those of an
        # earlier stack with the same prefix.
        self.assertEqual(self.deleted(), {'amp': ['ws-1'], 'grafana': ['g-1']})
        self.clients['amp'].get_paginator.return_value.paginate.assert_called_once_with(alias='p-ampws')
        # Waited for them to go.
        self.clients['amp'].describe_workspace.assert_called_once_with(workspaceId='ws-1')

    def test_allows_for_clock_skew_but_not_an_older_workspace(self):
        skewed = CREATE_STARTED - timedelta(seconds=lambda_function.CLOCK_SKEW_SECONDS - 1)
        older = CREATE_STARTED - timedelta(seconds=lambda_function.CLOCK_SKEW_SECONDS + 1)
        self.clients['amp'].get_paginator.return_value.paginate.return_value = [{'workspaces': [
            {'workspaceId': 'ws-skewed', 'alias': 'p-ampws', 'tags': {'SageMaker': 'true'}, 'createdAt': skewed},
            {'workspaceId': 'ws-older', 'alias': 'p-ampws', 'tags': {'SageMaker': 'true'}, 'createdAt': older},
            {'workspaceId': 'ws-no-date', 'alias': 'p-ampws', 'tags': {'SageMaker': 'true'}},
        ]}]
        self.handle(CreateGrafanaWorkspace='false')
        self.assertEqual(self.deleted(), {'amp': ['ws-skewed'], 'grafana': []})

    def test_keeps_the_customers_workspaces(self):
        self.handle(CreatePrometheusWorkspace='false', CreateGrafanaWorkspace='false')
        self.assertEqual(self.deleted(), {'amp': [], 'grafana': []})
        self.handle(CreateGrafanaWorkspace='disabled')
        self.assertEqual(self.deleted(), {'amp': ['ws-1'], 'grafana': []})

    def test_keeps_the_workspaces_of_a_stack_that_completed(self):
        for events in (self.COMPLETED, [stack_event('DELETE_IN_PROGRESS'), stack_event('UPDATE_COMPLETE')], []):
            with self.subTest(events=events):
                self.handle(events=events)
        self.assertEqual(self.deleted(), {'amp': [], 'grafana': []})

    def test_a_workspace_already_gone_is_deleted(self):
        self.clients['amp'].delete_workspace.side_effect = not_found('DeleteWorkspace')
        self.handle()
        self.clients['amp'].describe_workspace.assert_not_called()

    def test_still_succeeds_on_an_error(self):
        self.clients['grafana'].get_paginator.return_value.paginate.side_effect = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': 'no'}}, 'ListWorkspaces'
        )
        self.handle()
        self.clients['cloudformation'].describe_stack_events.side_effect = RuntimeError('boom')
        self.handle()


class TestStackNeverCreated(unittest.TestCase):
    def never_created(self, *pages):
        cloudformation = MagicMock()
        cloudformation.describe_stack_events.side_effect = [
            {'StackEvents': events, **({'NextToken': f't{i}'} if i < len(pages) - 1 else {})}
            for i, events in enumerate(pages)
        ]
        with patch('lambda_function.boto3.client', return_value=cloudformation):
            return lambda_function.stack_never_created(SETUP_STACK_ID), cloudformation.describe_stack_events.call_args_list

    def test_reads_the_pages_back_to_the_create(self):
        result, calls = self.never_created(
            [stack_event('DELETE_IN_PROGRESS'), stack_event('CREATE_COMPLETE', 'nested')],
            [stack_event('CREATE_FAILED', 'nested')],
            [stack_event('CREATE_IN_PROGRESS')],
        )
        self.assertEqual(result, CREATE_STARTED)
        self.assertEqual([c.kwargs.get('NextToken') for c in calls], [None, 't0', 't1'])

    def test_a_completed_create_on_a_later_page(self):
        result, _ = self.never_created([stack_event('DELETE_IN_PROGRESS')], [stack_event('CREATE_COMPLETE')])
        self.assertIsNone(result)

    def test_a_retried_delete_after_a_failed_one(self):
        result, _ = self.never_created([
            stack_event('DELETE_IN_PROGRESS'), stack_event('DELETE_FAILED'), stack_event('DELETE_IN_PROGRESS'),
            stack_event('ROLLBACK_COMPLETE'), stack_event('CREATE_IN_PROGRESS'),
        ])
        self.assertEqual(result, CREATE_STARTED)

    def test_stops_after_the_page_limit(self):
        pages = [[stack_event('DELETE_IN_PROGRESS')]] * (lambda_function.MAX_EVENT_PAGES + 5)
        result, calls = self.never_created(*pages)
        self.assertIsNone(result)
        self.assertEqual(len(calls), lambda_function.MAX_EVENT_PAGES)


class TestPhysicalResourceId(unittest.TestCase):
    def test_round_trips_the_extensions_requested_at_create(self):
        physical_id = lambda_function.physical_resource_id(CLUSTER_ARN, [OBS], [OTHER, 'sagemaker-third'])

        self.assertEqual(lambda_function.created_with(physical_id), ([OBS], [OTHER, 'sagemaker-third']))

    def test_round_trips_names_with_a_slash_comma_or_percent(self):
        names = ['a/b', 'c,d', 'e%2Ff']

        physical_id = lambda_function.physical_resource_id(CLUSTER_ARN, [OBS], names)

        self.assertEqual(physical_id.count('/'), CLUSTER_ARN.count('/') + 3)
        self.assertEqual(lambda_function.created_with(physical_id), ([OBS], names))

    def test_ignores_anything_after_the_two_lists(self):
        self.assertEqual(lambda_function.created_with(f'{CLUSTER_ARN}/extensions/{OBS}/{OTHER}/x/y'), ([OBS], [OTHER]))

    def test_reads_none_from_the_form_without_names(self):
        self.assertEqual(lambda_function.created_with(f'{CLUSTER_ARN}/extensions'), ([], []))

    def test_stays_within_the_cloudformation_limit(self):
        installed = [f'sagemaker-installed-{i:02}' for i in range(20)]
        not_installed = [f'sagemaker-not-installed-{i:02}' for i in range(30)]

        physical_id = lambda_function.physical_resource_id(CLUSTER_ARN, installed, not_installed)

        self.assertLessEqual(len(physical_id), lambda_function.MAX_PHYSICAL_ID_BYTES)
        kept_installed, kept_not_installed = lambda_function.created_with(physical_id)
        # The ones not installed are left out first.
        self.assertEqual(kept_installed, installed)
        self.assertTrue(0 < len(kept_not_installed) < len(not_installed))
        self.assertEqual(kept_not_installed, not_installed[:len(kept_not_installed)])


class TestRespond(unittest.TestCase):
    @patch('lambda_function.time.sleep')
    @patch('lambda_function.urllib.request.urlopen')
    def test_retries_the_response_to_cloudformation(self, urlopen, _sleep):
        urlopen.side_effect = [urllib.error.URLError('reset'), Mock(__enter__=Mock(), __exit__=Mock(return_value=False))]

        lambda_function.respond(event(), context(), 'SUCCESS', {'InstallStatus': '{}'}, 'id-1')

        self.assertEqual(urlopen.call_count, 2)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), 'PUT')
        self.assertEqual(request.full_url, 'https://cfn.example/response')
        body = json.loads(request.data)
        self.assertEqual(body['Status'], 'SUCCESS')
        self.assertEqual(body['PhysicalResourceId'], 'id-1')
        self.assertEqual(body['Data'], {'InstallStatus': '{}'})

    @patch('lambda_function.time.sleep')
    @patch('lambda_function.urllib.request.urlopen', side_effect=urllib.error.URLError('down'))
    def test_bounds_each_attempt_and_the_retries_by_the_time_left(self, urlopen, sleep):
        # 12 s left: each attempt's timeout fits in it, and the retries stop when a wait would not leave time for
        # another attempt.
        left = [12000]

        def remaining():
            return left[0]

        def wait(seconds):
            left[0] -= seconds * 1000

        sleep.side_effect = wait
        lambda_function.respond(event(), Mock(log_stream_name='l', get_remaining_time_in_millis=remaining), 'SUCCESS', {}, 'id-1')

        timeouts = [c.kwargs['timeout'] for c in urlopen.call_args_list]
        self.assertTrue(all(t <= 11 for t in timeouts), timeouts)
        self.assertLess(urlopen.call_count, lambda_function.RESPONSE_ATTEMPTS)
        self.assertGreater(left[0], 0)

    @patch('lambda_function.time.sleep')
    @patch('lambda_function.urllib.request.urlopen', side_effect=urllib.error.URLError('down'))
    def test_still_tries_once_with_almost_no_time_left(self, urlopen, _sleep):
        lambda_function.respond(event(), context(remaining=500), 'SUCCESS', {}, 'id-1')

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 1)

    @patch('lambda_function.urllib.request.urlopen')
    def test_keeps_the_response_within_the_cloudformation_limit(self, urlopen):
        urlopen.return_value = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        reason = 'permitted are "grafana", "metricsProvider", "prometheus" ' * 10
        statuses = {f'sagemaker-ext-{i}': 'NotInstalled' for i in range(12)}
        data = lambda_function.outcome(statuses, {name: reason[:300] for name in statuses})

        lambda_function.respond(event(), context(), 'SUCCESS', data, 'id-1')

        body = urlopen.call_args[0][0].data
        self.assertLessEqual(len(body), lambda_function.MAX_RESPONSE_BYTES)
        # The statuses always survive; the console needs them.
        self.assertEqual(json.loads(json.loads(body)['Data']['InstallStatus']), statuses)

    @patch('lambda_function.urllib.request.urlopen')
    def test_keeps_the_response_within_the_limit_with_too_many_statuses(self, urlopen):
        urlopen.return_value = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        statuses = {f'ClusterExtensions[{i}]': 'NotInstalled' for i in range(300)}
        statuses[lambda_function.OBSERVABILITY_EXTENSION] = 'Active'
        data = lambda_function.outcome(statuses, {name: 'Not installed: not a cluster extension' for name in statuses})

        lambda_function.respond(event(), context(), 'SUCCESS', data, 'id-1')

        body = urlopen.call_args[0][0].data
        self.assertLessEqual(len(body), lambda_function.MAX_RESPONSE_BYTES)
        sent = json.loads(json.loads(body)['Data']['InstallStatus'])
        self.assertEqual(sent[lambda_function.OBSERVABILITY_EXTENSION], 'Active')
        self.assertLess(len(sent), len(statuses))

    @patch('lambda_function.time.sleep')
    @patch('lambda_function.urllib.request.urlopen', side_effect=urllib.error.URLError('down'))
    def test_gives_up_after_the_last_attempt_without_raising(self, urlopen, _sleep):
        lambda_function.respond(event(), context(), 'SUCCESS', {}, 'id-1')

        self.assertEqual(urlopen.call_count, lambda_function.RESPONSE_ATTEMPTS)
