"""CFN Custom Resource that installs SageMaker HyperPod cluster extensions on a new Slurm cluster.

The extensions are the stack's ClusterExtensions parameter, a JSON list of {"ExtensionName", "ConfigurationValues"}.
The observability extension (sagemaker-observability) is installed when the stack collects metrics with it, from the
list's configuration or the template's metric levels, with the stack's Prometheus remote-write URL (metrics go to
CloudWatch without one) and Grafana workspace added. CloudFormation has no resource type for cluster extensions, and
the Lambda runtime's SDK has no model for the extension API yet, so the requests are signed and sent directly in the
SageMaker API's protocol (awsJson 1.1).

Extensions are add-ons that the console manages after the cluster is created, so a failed install never fails the
stack: rolling back would delete the cluster that was just built. Every outcome is SUCCESS, and the response to
CloudFormation is retried, since a response that never arrives fails the stack too. Each extension's outcome is
reported in InstallStatus and InstallReason (JSON objects keyed by extension name) and in the log.

With FailOnFailedInstall 'true' (the observability setup stack, which rolls back on its own without the cluster), the
observability extension failing to install fails the resource; other extensions are reported as before and never fail
it. Deleting the resource removes the extensions it tried that failed to install from the cluster, and after a failed
create also the observability extension in any status but Active or Degraded, so none is left pointing at deleted
workspaces.

The same function serves Custom::ObservabilityWorkspaceCleanup in that stack, which its nested observability stack
depends on, so it is deleted last. Its Delete deletes the Prometheus and Grafana workspaces the stack created when the
stack never completed its create: CloudFormation keeps the Prometheus workspace (DeletionPolicy Retain) and doesn't
manage the Grafana one. A stack that completed keeps them, with the customer's dashboards.
"""
import http.client
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

OBSERVABILITY_EXTENSION = 'sagemaker-observability'
# The cluster tag that names the stack whose outputs record the installs, so the console can tell the customer about an
# extension the stack couldn't install.
SETUP_STACK_TAG = 'hyperpod-cluster-extensions-stack'
COLLECTION_INTERVAL_SECONDS = 30
POLL_SECONDS = 15
# The installs are reported as they are after this long; Creating is still installing on the nodes.
MAX_WAIT_SECONDS = 300
# With FailOnFailedInstall, the observability setup stack waits longer for the extension's outcome (within the
# function's 15 minutes), since a failure rolls that stack back. An extension still Creating after it is not a failure:
# its outcome isn't known, so the stack is kept, and the console shows the extension's status.
SETUP_STACK_MAX_WAIT_SECONDS = 660
# An extension whose status can't be read this many times in a row is reported Unknown.
MAX_DESCRIBE_FAILURES = 4
# Stop waiting this long before the Lambda times out, to leave time to respond to CloudFormation.
RESPONSE_MARGIN_MS = 60000
REQUEST_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1
BACKOFF_MAX_SECONDS = 20
RESPONSE_ATTEMPTS = 5
# A response is a small PUT to a presigned S3 URL; each attempt gets at most this long, and never more than the time
# the Lambda has left, so the retries fit in the RESPONSE_MARGIN_MS kept for them.
RESPONSE_TIMEOUT_SECONDS = 15
# CloudFormation rejects a response body over 4096 bytes, which would lose every reason.
MAX_REASON_LENGTH = 300
MAX_RESPONSE_BYTES = 4000
# CloudFormation's limit for a physical resource ID.
MAX_PHYSICAL_ID_BYTES = 1024
REMOVED_REASON = 'Removed from the cluster after the stack installed it'
REPLACED_REASON = "Not installed on the replacement cluster; install it from the cluster's Extensions tab"
# With FailOnFailedInstall: the observability extension's final statuses that fail the resource. CreateFailed is the
# extension's own failure (the SageMaker API model's ExtensionStatus: Creating, Active, CreateFailed, Updating,
# Deleting, DeleteFailed, Degraded, UpdateFailed); NotInstalled is a create that raised, or a run that failed as a
# whole; Unknown is a status that couldn't be read.
FAILED_INSTALL_STATUSES = ('CreateFailed', 'Unknown', 'NotInstalled')
# The statuses on the cluster of an extension that failed to install, which deleting the resource removes.
REMOVED_ON_DELETE_STATUSES = ('CreateFailed',)
# After a failed create, the observability extension stays only in these.
KEPT_AFTER_FAILED_CREATE_STATUSES = ('Active', 'Degraded')
# How long deleting the resource waits for those extensions, or the cleanup for the workspaces, to go.
MAX_DELETE_WAIT_SECONDS = 300
# The third part of the physical resource ID after the two lists of extensions, set by a create that failed.
FAILED_MARKER = 'failed'
CLEANUP_RESOURCE_TYPE = 'Custom::ObservabilityWorkspaceCleanup'
# The workspaces' names, as the nested observability stack sets them: the Prometheus alias and the Grafana name.
PROMETHEUS_ALIAS_SUFFIX = '-ampws'
GRAFANA_NAME_SUFFIX = '-amgws'
# The stack's own events are read newest first back to its create; this only bounds a runaway stack.
MAX_EVENT_PAGES = 20
# A workspace counts as this stack's when created at or after the stack's create began, less this for clock skew, so
# one an earlier stack with the same ResourceNamePrefix left behind is never deleted.
CLOCK_SKEW_SECONDS = 5
AWS_CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=15, retries={'max_attempts': 3})
# The role's policy applies a few seconds after the role is created.
IAM_PROPAGATION_SECONDS = 120

# The template's metric levels, by the extension's metric type. nodeMetrics and acceleratedComputeMetrics take
# BASIC, ADVANCED or DISABLED; the others take only ENABLED or DISABLED.
LEVEL_PROPERTIES = {
    'nodeMetrics': 'NodeMetricLevel',
    'acceleratedComputeMetrics': 'AcceleratedComputeMetricLevel',
    'clusterMetrics': 'ClusterMetricLevel',
    'networkMetrics': 'NetworkMetricLevel',
    'jobMetrics': 'JobMetricLevel',
    'ncclMetrics': 'NcclMetricLevel',
}
LEVELED_METRIC_TYPES = ('nodeMetrics', 'acceleratedComputeMetrics')
RETRYABLE_ERROR_PREFIXES = ('Throttling', 'TooManyRequests', 'RequestLimitExceeded', 'InternalServer', 'InternalFailure',
                            'ServiceUnavailable')


class SageMakerError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.status = status


def budget_seconds(context):
    """The time left before the response to CloudFormation has to be sent."""
    return (context.get_remaining_time_in_millis() - RESPONSE_MARGIN_MS) / 1000


def is_retryable(error):
    if isinstance(error, SageMakerError):
        return (error.status is not None and (error.status == 429 or error.status >= 500)) or error.code.startswith(
            RETRYABLE_ERROR_PREFIXES
        )
    # Connection failures, timeouts and TLS errors (all OSError), and responses cut short (for example IncompleteRead).
    # An HTTPError is a SageMakerError by now.
    return isinstance(error, (OSError, http.client.HTTPException))


_session = None
_endpoint = None


def sagemaker_session():
    """The session and the SDK's SageMaker endpoint for this Region (which handles the partition's DNS suffix),
    created once per Lambda container."""
    global _session, _endpoint  # pylint: disable=global-statement
    if _session is None:
        _session = boto3.session.Session(region_name=os.environ['AWS_REGION'])
        _endpoint = _session.client('sagemaker').meta.endpoint_url
    return _session, _endpoint


def send(operation, body, timeout):
    """One SageMaker API request, signed with the Lambda's credentials."""
    region = os.environ['AWS_REGION']
    session, endpoint = sagemaker_session()
    data = json.dumps(body)
    request = AWSRequest(
        method='POST',
        url=f'{endpoint}/',
        data=data,
        headers={'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': f'SageMaker.{operation}'},
    )
    SigV4Auth(session.get_credentials().get_frozen_credentials(), 'sagemaker', region).add_auth(request)
    prepared = request.prepare()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(prepared.url, data=data.encode('utf-8'), headers=dict(prepared.headers), method='POST'),
            timeout=timeout,
        ) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read() or b'{}')
        except ValueError:
            # Not a service error, for example an HTML page from a load balancer.
            error = {}
        if not isinstance(error, dict):
            error = {}
        raise SageMakerError(
            error.get('__type', f'HTTP {e.code}').split('#')[-1],
            error.get('message') or error.get('Message') or e.reason,
            e.code,
        ) from e


def call(operation, body, context):
    """A SageMaker API call, retried with backoff and jitter on throttling, server errors and timeouts, within the
    time budget."""
    attempt = 1
    while True:
        timeout = max(1, min(REQUEST_TIMEOUT_SECONDS, budget_seconds(context)))
        try:
            return send(operation, body, timeout)
        except Exception as e:  # pylint: disable=broad-except
            delay = random.uniform(0, min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** attempt))
            if not is_retryable(e) or attempt >= MAX_ATTEMPTS or budget_seconds(context) < delay + 1:
                raise
            logger.warning(f'{operation} failed (attempt {attempt}), retrying in {delay:.1f}s: {e}')
            time.sleep(delay)
            attempt += 1


def observability_configuration(properties, configuration=None):
    """The observability extension's configuration: the given one (metric levels and host profiling, as the console
    builds it) or the template's metric levels, plus the stack's Prometheus remote-write URL and Grafana workspace
    when there are."""
    if configuration is None:
        metrics_provider = {}
        for metric_type, prop in LEVEL_PROPERTIES.items():
            level = properties[prop]
            if metric_type not in LEVELED_METRIC_TYPES:
                level = 'DISABLED' if level == 'DISABLED' else 'ENABLED'
            metrics_provider[metric_type] = {'level': level, 'collectionIntervalSeconds': COLLECTION_INTERVAL_SECONDS}
        # The extension's schema allows NCCL metrics only with node metrics on.
        if metrics_provider['ncclMetrics']['level'] == 'ENABLED' and metrics_provider['nodeMetrics']['level'] == 'DISABLED':
            logger.warning('NCCL metrics need node metrics, so they are off: NodeMetricLevel is DISABLED')
            metrics_provider['ncclMetrics']['level'] = 'DISABLED'
        configuration = {'metricsProvider': metrics_provider}
    configuration = dict(configuration)
    if properties.get('RemoteWriteUrl'):
        configuration['prometheus'] = {'remoteWriteUrl': properties['RemoteWriteUrl']}
    if properties.get('GrafanaWorkspaceId'):
        configuration['grafana'] = {'workspaceId': properties['GrafanaWorkspaceId']}
    return configuration


def entry_configuration(entry):
    """A ClusterExtensions entry's configuration: missing or empty is {}, a string must be a JSON object."""
    configuration = entry.get('ConfigurationValues')
    if configuration in (None, '', {}):
        return {}
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except ValueError as e:
            raise ValueError(f'ConfigurationValues is not JSON: {e}') from e
    if not isinstance(configuration, dict):
        raise ValueError('ConfigurationValues is not a JSON object')
    return configuration


def in_scope(properties, name):
    """Whether this stack installs the listed extension: with ObservabilityOnly 'true' (the observability setup stack),
    only the observability extension; with SkipObservability 'true' (the main stack, when the setup stack installs
    it), every other one."""
    if properties.get('ObservabilityOnly') == 'true':
        return name == OBSERVABILITY_EXTENSION
    if properties.get('SkipObservability') == 'true':
        return name != OBSERVABILITY_EXTENSION
    return True


def listed_extensions(properties):
    """The valid ClusterExtensions entries as (name, configuration), and why each other entry is not valid, by its
    ExtensionName, or ClusterExtensions[<index>] without one (ClusterExtensions when it isn't a JSON list). Only the
    entries in this stack's scope."""
    try:
        entries = json.loads(properties.get('Extensions') or '[]')
    except ValueError as e:
        invalid = {'ClusterExtensions': f'Not installed: ClusterExtensions is not JSON: {e}'}
        return [], {k: v for k, v in invalid.items() if in_scope(properties, k)}
    if not isinstance(entries, list):
        invalid = {'ClusterExtensions': 'Not installed: ClusterExtensions is not a JSON list'}
        return [], {k: v for k, v in invalid.items() if in_scope(properties, k)}
    listed, invalid = [], {}
    for index, entry in enumerate(entries):
        name = entry.get('ExtensionName') if isinstance(entry, dict) else None
        key = name if isinstance(name, str) and name else f'ClusterExtensions[{index}]'
        if not in_scope(properties, key):
            continue
        try:
            if not isinstance(entry, dict):
                raise ValueError('the entry is not a JSON object')
            if not isinstance(name, str) or not name:
                raise ValueError('ExtensionName is not a non-empty string')
            listed.append((name, entry_configuration(entry)))
        except ValueError as e:
            invalid[key] = f'Not installed: {e}'
    return listed, invalid


def requested_extensions(properties):
    """The extensions to install, as (name, configuration) in the stack's order. The observability extension, when the
    stack collects metrics with it, gets the stack's workspaces, and the template's metric levels if it isn't listed
    (or its entry isn't valid)."""
    extensions = []
    for name, configuration in listed_extensions(properties)[0]:
        if name == OBSERVABILITY_EXTENSION:
            if properties.get('InstallObservability') != 'true':
                continue
            configuration = observability_configuration(properties, configuration or None)
        extensions.append((name, configuration))
    if properties.get('InstallObservability') == 'true' and OBSERVABILITY_EXTENSION not in dict(extensions):
        extensions.append((OBSERVABILITY_EXTENSION, observability_configuration(properties)))
    return extensions


def requested_names(properties):
    """Every extension name the stack is asked for, in order: each ClusterExtensions entry's string ExtensionName, valid
    or not, and the observability extension when the stack installs it."""
    try:
        entries = json.loads(properties.get('Extensions') or '[]')
    except ValueError:
        entries = []
    names = [
        e['ExtensionName'] for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict) and isinstance(e.get('ExtensionName'), str) and e['ExtensionName']
    ]
    if properties.get('InstallObservability') == 'true':
        names.append(OBSERVABILITY_EXTENSION)
    return list(dict.fromkeys(name for name in names if in_scope(properties, name)))


def skipped_extensions(properties):
    """Listed extensions the stack doesn't install: the observability extension when the stack doesn't collect
    metrics with it (EnableObservabilityFeature is false)."""
    names = [name for name, _ in listed_extensions(properties)[0]]
    return [OBSERVABILITY_EXTENSION] if (OBSERVABILITY_EXTENSION in names
                                         and properties.get('InstallObservability') != 'true') else []


def rejects_host_profiling(error, name, configuration):
    """Whether the service rejected the observability configuration because the extension version it installs has no
    host profiling: a rejection of the configuration's top-level properties whose permitted ones don't include
    profilesProvider, as the console reads it ('version 0.2.0: /: has properties the schema does not permit; permitted
    are "grafana", "metricsProvider", "prometheus"'). A rejection under a nested path is about something else."""
    message = str(error)
    return (
        name == OBSERVABILITY_EXTENSION
        and 'profilesProvider' in configuration
        and isinstance(error, SageMakerError)
        and re.search(r'version \S+: /: has properties the schema does not permit', message) is not None
        and not re.search(r'permitted are [^:]*"profilesProvider"', message)
    )


def wait(context):
    """Sleeps for one poll, or returns False when the budget has no room for it."""
    if budget_seconds(context) < POLL_SECONDS:
        return False
    time.sleep(POLL_SECONDS)
    return True


def list_extensions(cluster_arn, context):
    extensions, body = {}, {'ClusterArn': cluster_arn}
    while True:
        response = call('ListClusterExtensions', body, context)
        extensions.update({e.get('ExtensionName'): e for e in response.get('Extensions', [])})
        if not response.get('NextToken'):
            return extensions
        body = {'ClusterArn': cluster_arn, 'NextToken': response['NextToken']}


def wait_for_cluster(cluster_arn, context):
    """The cluster stack completes once the cluster is created. Extensions can be created while the cluster is
    Creating too, and start on the nodes as they come up, so only the cluster's record has to be readable."""
    started = time.time()
    while True:
        try:
            status = call('DescribeCluster', {'ClusterName': cluster_arn}, context)['ClusterStatus']
        except SageMakerError as e:
            # Access denied while the role's policy applies.
            if e.code != 'AccessDeniedException' or time.time() - started > IAM_PROPAGATION_SECONDS:
                raise
            status = 'not authorized yet'
        if status in ('Creating', 'InService'):
            return
        if status in ('Failed', 'Deleting'):
            raise RuntimeError(f'Cluster {cluster_arn} is {status}')
        if not wait(context):
            raise RuntimeError(f'Cluster {cluster_arn} is still {status}')


def create_extension(cluster_arn, name, configuration, client_token, context):
    logger.info(f'Installing {name} with {json.dumps(configuration)}')
    # The client token makes a create retried after a server error return the extension it created.
    return call(
        'CreateClusterExtension',
        {
            'ClusterArn': cluster_arn,
            'ExtensionName': name,
            'ConfigurationValues': json.dumps(configuration),
            'ClientToken': client_token,
        },
        context,
    )['ExtensionArn']


def install_one(cluster_arn, name, configuration, request_id, existing, context):
    """Installs one extension, or reports the one already on the cluster (one a retried request installed), in any
    status: the stack never removes an extension. Returns its ARN and a note."""
    # A UUID of the request and the extension: 36 characters, within every SageMaker client-token shape.
    client_token = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{request_id}/{name}'))
    if existing:
        logger.info(f'{name} already on the cluster: {existing["ExtensionArn"]} is {existing.get("Status")}')
        return existing['ExtensionArn'], ''
    try:
        return create_extension(cluster_arn, name, configuration, client_token, context), ''
    except Exception as e:  # pylint: disable=broad-except
        if not rejects_host_profiling(e, name, configuration):
            raise
        # The default version has no host profiling: install the rest, and say so.
        logger.warning(f'Installing {name} without host profiling: {e}')
        configuration = {k: v for k, v in configuration.items() if k != 'profilesProvider'}
        token = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{request_id}/{name}/no-profiling'))
        return (
            create_extension(cluster_arn, name, configuration, token, context),
            'Host profiling is not on: the installed extension version does not have it. ',
        )


def tag_cluster(cluster_arn, stack_id, context):
    """Best effort: the outcome is in the stack's outputs either way."""
    try:
        call('AddTags', {'ResourceArn': cluster_arn, 'Tags': [{'Key': SETUP_STACK_TAG, 'Value': stack_id}]}, context)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(f'Could not tag the cluster with its setup stack: {e}')


def install(event, context, extensions, statuses, reasons, arns):
    """Installs every requested extension, then waits for the installs, recording each status, reason and ARN as soon
    as they are known. A failure in one extension doesn't stop the others."""
    properties = event['ResourceProperties']
    notes = {}
    for name, _ in extensions:
        statuses[name] = 'NotInstalled'
    for name in skipped_extensions(properties):
        statuses[name] = 'NotInstalled'
        reasons[name] = 'Not installed: observability is off in this stack (EnableObservabilityFeature is false)'
    for key, reason in listed_extensions(properties)[1].items():
        if key == OBSERVABILITY_EXTENSION and key in statuses:
            # Installed with the template's metric levels instead.
            notes[key] = f"{reason}; installed with the template's metric levels. "
        else:
            statuses[key] = 'NotInstalled'
            reasons[key] = reason[:MAX_REASON_LENGTH]
    cluster_arn = properties['ClusterArn']
    # First, so a cluster that can't be described is still tagged. With SkipObservability, the tag names the setup
    # stack, which installs the observability extension.
    if properties.get('SkipObservability') != 'true':
        tag_cluster(cluster_arn, event['StackId'], context)
    wait_for_cluster(cluster_arn, context)

    existing = list_extensions(cluster_arn, context)
    for name, configuration in extensions:
        try:
            arns[name], note = install_one(
                cluster_arn, name, configuration, event['RequestId'], existing.get(name), context
            )
            notes[name] = notes.get(name, '') + note
        except Exception as e:  # pylint: disable=broad-except
            reasons[name] = f'{notes.get(name, "")}{e}'[:MAX_REASON_LENGTH]

    # Active is installed. Creating when the wait or the time runs out is still installing on the nodes. Anything else
    # is reported as it is. A status that can't be read keeps the last one read and the error, until it can't be read
    # MAX_DESCRIBE_FAILURES times in a row.
    max_wait = SETUP_STACK_MAX_WAIT_SECONDS if properties.get('FailOnFailedInstall') == 'true' else MAX_WAIT_SECONDS
    pending = dict(arns)
    failures, read = {}, set()
    started = time.time()
    while pending:
        for name, extension_arn in list(pending.items()):
            try:
                statuses[name] = call(
                    'DescribeClusterExtension', {'ClusterArn': cluster_arn, 'ExtensionArn': extension_arn}, context
                )['ExtensionDetails']['Status']
            except Exception as e:  # pylint: disable=broad-except
                failures[name] = failures.get(name, 0) + 1
                reasons[name] = str(e)[:MAX_REASON_LENGTH]
                if name not in read or failures[name] >= MAX_DESCRIBE_FAILURES:
                    statuses[name] = 'Unknown'
                if failures[name] >= MAX_DESCRIBE_FAILURES:
                    del pending[name]
                continue
            failures[name] = 0
            read.add(name)
            reasons.pop(name, None)
            if statuses[name] != 'Creating':
                del pending[name]
        if pending and (time.time() - started >= max_wait or not wait(context)):
            break
    for name, extension_arn in arns.items():
        reasons.setdefault(name, f'{notes[name]}{extension_arn} is {statuses[name]}'[:MAX_REASON_LENGTH])


def outcome(statuses, reasons):
    return {'InstallStatus': json.dumps(statuses), 'InstallReason': json.dumps(reasons)}


def physical_resource_id(cluster_arn, installed, not_installed, suffix=''):
    """Records the extensions requested at create, for Update, since CloudFormation sends the ID back unchanged:
    {cluster ARN}/extensions/{installed}/{not installed}, each a comma-separated list. Names that don't fit in
    CloudFormation's limit are left out, the ones not installed first. Names are percent-encoded ('/' and ',' in an
    invalid entry's name would break the format), and created_with decodes them."""
    installed, not_installed = list(installed), list(not_installed)
    left_out = []

    def encoded(names):
        return ','.join(urllib.parse.quote(n, safe='') for n in names)

    while True:
        physical_id = f'{cluster_arn}/extensions/{encoded(installed)}/{encoded(not_installed)}{suffix}'
        if len(physical_id.encode('utf-8')) <= MAX_PHYSICAL_ID_BYTES or not (installed or not_installed):
            break
        left_out.append((not_installed or installed).pop())
    if left_out:
        logger.warning(f'Left {left_out} out of the physical resource ID to fit its limit; Update does not report them')
    return physical_id


def created_with(physical_id):
    """The extensions installed and not installed at create, from the physical resource ID. ([], []) from the form
    before it recorded them ({cluster ARN}/extensions). Anything after the two lists is ignored."""
    lists = physical_id.partition('/extensions/')[2].split('/') + ['']
    return tuple([urllib.parse.unquote(n) for n in names.split(',') if n] for names in lists[:2])


def failed_physical_id(physical_id):
    """The physical resource ID with FAILED_MARKER, which created_with ignores."""
    installed, not_installed = created_with(physical_id)
    return physical_resource_id(
        physical_id.partition('/extensions')[0], installed, not_installed, suffix=f'/{FAILED_MARKER}'
    )


def create_failed(physical_id):
    return (physical_id.partition('/extensions/')[2].split('/') + ['', '', ''])[2] == FAILED_MARKER


def on_create(event, context):
    """InstallStatus and InstallReason are always set: the stack's outputs read both. Returns them and the physical
    resource ID."""
    properties = event['ResourceProperties']
    statuses, reasons, arns = {}, {}, {}
    names = []
    try:
        names = requested_names(properties)
        install(event, context, requested_extensions(properties), statuses, reasons, arns)
    except Exception as e:  # pylint: disable=broad-except
        # The run failed as a whole: every extension asked for is not installed, not only the ones recorded so far.
        reason = str(e)[:MAX_REASON_LENGTH]
        for name in names + list(statuses):
            if name not in arns:
                statuses[name] = 'NotInstalled'
                reasons.setdefault(name, reason)
        reasons['ClusterExtensions'] = reason
    if any(status != 'Active' for status in statuses.values()) or not statuses:
        logger.warning(f'Cluster extensions not all installed: {statuses} {reasons}')
    # Invalid and skipped entries too, so Update still reports them NotInstalled.
    return outcome(statuses, reasons), physical_resource_id(
        properties.get('ClusterArn', ''), [n for n in names if n in arns], [n for n in names if n not in arns]
    )


def on_update(event, context):
    """Nothing changes, since the console manages the extensions after the cluster is created. The current status of
    the extensions requested when it was created is reported again, for the stack's outputs; an extension added to the
    list later was never installed by the stack, so it isn't reported as one the stack couldn't install. One the stack
    installed that is no longer on the cluster was removed there (Removed, not a failed install). Returns them and the
    physical resource ID, a new one when the update replaced the cluster."""
    properties = event['ResourceProperties']
    physical_id = event['PhysicalResourceId']
    if properties.get('ClusterArn') != physical_id.partition('/extensions')[0]:
        # The stack installed nothing on the new cluster.
        names = requested_names(properties)
        return outcome({n: 'NotInstalled' for n in names}, {n: REPLACED_REASON for n in names}), physical_resource_id(
            properties.get('ClusterArn', ''), [], names
        )
    installed_at_create, not_installed_at_create = created_with(physical_id)
    statuses, reasons = {}, {}
    try:
        on_cluster = list_extensions(properties['ClusterArn'], context)
        for name in sorted(installed_at_create + not_installed_at_create):
            if name in on_cluster:
                statuses[name] = on_cluster[name].get('Status', 'Unknown')
            elif name in installed_at_create:
                statuses[name], reasons[name] = 'Removed', REMOVED_REASON
            else:
                statuses[name] = 'NotInstalled'
    except Exception as e:  # pylint: disable=broad-except
        reasons['ClusterExtensions'] = str(e)[:MAX_REASON_LENGTH]
    return outcome(statuses, reasons), physical_id


def remove_failed_installs(event, context):
    """With FailOnFailedInstall: removes from the cluster the extensions this resource tried that failed to install,
    and after a failed create the observability extension unless it is Active or Degraded, then waits for them to go,
    within MAX_DELETE_WAIT_SECONDS. Other Active and Degraded extensions stay."""
    physical_id = event['PhysicalResourceId']
    cluster_arn = physical_id.partition('/extensions')[0]
    tried = created_with(physical_id)[0]
    after_failed_create = create_failed(physical_id)
    on_cluster = list_extensions(cluster_arn, context)

    def removed(name):
        status = on_cluster.get(name, {}).get('Status')
        if name == OBSERVABILITY_EXTENSION and after_failed_create:
            return status is not None and status not in KEPT_AFTER_FAILED_CREATE_STATUSES
        return name in tried and status in REMOVED_ON_DELETE_STATUSES

    removing = {name: e['ExtensionArn'] for name, e in on_cluster.items() if removed(name)}
    for name, extension_arn in removing.items():
        if on_cluster[name].get('Status') == 'Deleting':
            continue
        logger.info(f'Removing {name} ({on_cluster[name].get("Status")}): {extension_arn}')
        try:
            call('DeleteClusterExtension', {'ClusterArn': cluster_arn, 'ExtensionArn': extension_arn}, context)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Could not remove {name}: {e}')
    started = time.time()
    while removing:
        if time.time() - started >= MAX_DELETE_WAIT_SECONDS or not wait(context):
            logger.warning(f'Still removing {sorted(removing)}')
            return
        on_cluster = list_extensions(cluster_arn, context)
        removing = {name: arn for name, arn in removing.items() if name in on_cluster}


def stack_never_created(stack_id):
    """When the stack never completed its create, the time its create began: reading its own events newest first, past
    this deletion, the first one is its CREATE_IN_PROGRESS, before any CREATE_COMPLETE, UPDATE_* or IMPORT_*. None
    when it completed, or when the events don't say within MAX_EVENT_PAGES."""
    cloudformation = boto3.client('cloudformation', config=AWS_CLIENT_CONFIG)
    token = None
    for _ in range(MAX_EVENT_PAGES):
        page = cloudformation.describe_stack_events(StackName=stack_id, **({'NextToken': token} if token else {}))
        for stack_event in page['StackEvents']:
            if stack_event.get('PhysicalResourceId') != stack_id:
                continue
            status = stack_event.get('ResourceStatus', '')
            if status == 'CREATE_IN_PROGRESS':
                return stack_event['Timestamp']
            if status == 'CREATE_COMPLETE' or status.startswith(('UPDATE_', 'IMPORT_')):
                return None
        token = page.get('NextToken')
        if not token:
            return None
    return None


def is_not_found(error):
    return isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') == 'ResourceNotFoundException'


def created_workspaces(properties, create_started):
    """The workspaces the stack created, as (client, workspace ID): the alias and the name the nested stack sets, the
    SageMaker tag, and created since the stack's create began. Without CreatePrometheusWorkspace or
    CreateGrafanaWorkspace they are the customer's."""
    prefix = properties['ResourceNamePrefix']
    earliest = create_started - timedelta(seconds=CLOCK_SKEW_SECONDS)

    def ours(workspace, created_key):
        created = workspace.get(created_key)
        return (workspace.get('tags') or {}).get('SageMaker') == 'true' and created is not None and created >= earliest

    workspaces = []
    if properties.get('CreatePrometheusWorkspace') == 'true':
        alias = f'{prefix}{PROMETHEUS_ALIAS_SUFFIX}'
        pages = boto3.client('amp', config=AWS_CLIENT_CONFIG).get_paginator('list_workspaces').paginate(alias=alias)
        workspaces += [
            ('amp', w['workspaceId']) for page in pages for w in page['workspaces']
            if w.get('alias') == alias and ours(w, 'createdAt')
        ]
    if properties.get('CreateGrafanaWorkspace') == 'true':
        name = f'{prefix}{GRAFANA_NAME_SUFFIX}'
        pages = boto3.client('grafana', config=AWS_CLIENT_CONFIG).get_paginator('list_workspaces').paginate()
        workspaces += [
            ('grafana', w['id']) for page in pages for w in page['workspaces']
            if w.get('name') == name and ours(w, 'created')
        ]
    return workspaces


def delete_workspaces(workspaces, context):
    """Deletes the workspaces and waits for them to go, within MAX_DELETE_WAIT_SECONDS. Best effort; one already gone
    is deleted."""
    pending = []
    for service, workspace_id in workspaces:
        try:
            boto3.client(service, config=AWS_CLIENT_CONFIG).delete_workspace(workspaceId=workspace_id)
            logger.info(f'Deleting the {service} workspace {workspace_id}, which the create that never completed made')
            pending.append((service, workspace_id))
        except Exception as e:  # pylint: disable=broad-except
            if not is_not_found(e):
                logger.warning(f'Could not delete the {service} workspace {workspace_id}: {e}')
    started = time.time()
    while pending:
        if time.time() - started >= MAX_DELETE_WAIT_SECONDS or not wait(context):
            logger.warning(f'Still deleting {pending}')
            return
        remaining = []
        for service, workspace_id in pending:
            try:
                boto3.client(service, config=AWS_CLIENT_CONFIG).describe_workspace(workspaceId=workspace_id)
                remaining.append((service, workspace_id))
            except Exception as e:  # pylint: disable=broad-except
                if not is_not_found(e):
                    logger.warning(f'Could not read the {service} workspace {workspace_id}: {e}')
        pending = remaining


def workspace_cleanup(event, context):
    """Custom::ObservabilityWorkspaceCleanup: nothing on Create and Update. On Delete, the workspaces the stack created
    when it never completed its create. Always SUCCESS."""
    physical_id = event.get('PhysicalResourceId') or f'{event["StackId"]}/workspace-cleanup'
    if event['RequestType'] == 'Delete':
        try:
            create_started = stack_never_created(event['StackId'])
            if create_started:
                delete_workspaces(created_workspaces(event['ResourceProperties'], create_started), context)
            else:
                logger.info('The stack completed its create: keeping its workspaces')
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Could not clean up the workspaces: {e}')
    respond(event, context, 'SUCCESS', {}, physical_id)


def on_delete(event, context):
    """Removes nothing, unless FailOnFailedInstall is set (see remove_failed_installs).
    Deleting the cluster removes its extensions, and the console manages them after the cluster is created, so the
    resource going away (the stack's deletion, or an update that drops it) must not remove extensions from a cluster
    that stays."""
    if event['ResourceProperties'].get('FailOnFailedInstall') == 'true':
        try:
            remove_failed_installs(event, context)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Could not remove the extensions that failed to install: {e}')
        return
    logger.info('Leaving the cluster extensions in place: they go with the cluster')


def respond(event, context, status, data, physical_id, reason=''):
    """Sends the response to CloudFormation, retried: a response that never arrives fails the stack after an hour."""
    def encode(data):
        return json.dumps({
            'Status': status,
            'Reason': reason or f'See the details in CloudWatch Log Stream: {context.log_stream_name}',
            'PhysicalResourceId': physical_id,
            'StackId': event['StackId'],
            'RequestId': event['RequestId'],
            'LogicalResourceId': event['LogicalResourceId'],
            'Data': data,
        }).encode('utf-8')

    body = encode(data)
    # CloudFormation fails a resource whose response is over 4096 bytes, which would roll the cluster back. The
    # reasons are the part that grows (one per extension, quoted twice): shorten them, then drop them.
    if len(body) > MAX_RESPONSE_BYTES and 'InstallReason' in data:
        for length in (100, 0):
            reasons = json.loads(data['InstallReason'] or '{}')
            shortened = {k: v[:length] for k, v in reasons.items()} if length else {}
            body = encode({**data, 'InstallReason': json.dumps(shortened)})
            if len(body) <= MAX_RESPONSE_BYTES:
                break
        logger.warning('Shortened the install reasons to fit the response to CloudFormation; they are in this log')
    # Only a very long ClusterExtensions list gets here: keep the observability status first, then as many as fit.
    if len(body) > MAX_RESPONSE_BYTES and 'InstallStatus' in data:
        statuses = json.loads(data['InstallStatus'] or '{}')
        kept = {}
        for name in sorted(statuses, key=lambda n: n != OBSERVABILITY_EXTENSION):
            trial = encode({'InstallStatus': json.dumps({**kept, name: statuses[name]}), 'InstallReason': '{}'})
            if len(trial) > MAX_RESPONSE_BYTES:
                break
            kept[name] = statuses[name]
        body = encode({'InstallStatus': json.dumps(kept), 'InstallReason': '{}'})
        logger.warning(f'Reported {len(kept)} of {len(statuses)} extension statuses to fit the response; all are in this log')
    for attempt in range(1, RESPONSE_ATTEMPTS + 1):
        # One second is kept for the Lambda to return.
        remaining = context.get_remaining_time_in_millis() / 1000 - 1
        try:
            request = urllib.request.Request(
                event['ResponseURL'], data=body, method='PUT', headers={'Content-Type': '', 'Content-Length': str(len(body))}
            )
            with urllib.request.urlopen(request, timeout=max(1, min(RESPONSE_TIMEOUT_SECONDS, remaining))):
                return
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Sending the response failed (attempt {attempt}): {e}')
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** attempt)
            # Retry only while the wait leaves time for another attempt.
            if attempt == RESPONSE_ATTEMPTS or context.get_remaining_time_in_millis() / 1000 - 1 < delay + 1:
                logger.error(f'The response to CloudFormation was not sent: {e}')
                return
            time.sleep(delay)


def lambda_handler(event, context):
    logger.info(f'Received event: {json.dumps({k: v for k, v in event.items() if k != "ResponseURL"})}')
    if event.get('ResourceType') == CLEANUP_RESOURCE_TYPE:
        workspace_cleanup(event, context)
        return
    request_type = event['RequestType']
    # Set at create and stable for the stack's life, so an Update never replaces the resource.
    physical_id = event.get('PhysicalResourceId') or f'{event["ResourceProperties"].get("ClusterArn", "")}/extensions'
    fail_on_failed_install = event.get('ResourceProperties', {}).get('FailOnFailedInstall') == 'true'
    data, status, reason = {}, 'SUCCESS', ''
    try:
        if request_type == 'Create':
            data, physical_id = on_create(event, context)
            if fail_on_failed_install:
                observability = json.loads(data['InstallStatus']).get(OBSERVABILITY_EXTENSION)
                if observability in FAILED_INSTALL_STATUSES:
                    status = 'FAILED'
                    reason = f'Cluster extensions failed to install: {OBSERVABILITY_EXTENSION} is {observability}'
                    physical_id = failed_physical_id(physical_id)
        elif request_type == 'Delete':
            on_delete(event, context)
        else:
            data, physical_id = on_update(event, context)
    except Exception as e:  # pylint: disable=broad-except
        # Anything not caught above, for example a malformed event. Still SUCCESS: a failed extension install must
        # never roll back the cluster. With FailOnFailedInstall, a Create fails; a Delete never does.
        logger.error(f'Error: {e}')
        data = outcome({}, {'ClusterExtensions': str(e)[:MAX_REASON_LENGTH]})
        if fail_on_failed_install and request_type == 'Create':
            # The error can hold ARNs; it is in the log.
            status, reason = 'FAILED', f'Cluster extensions failed to install: {type(e).__name__}'
            physical_id = failed_physical_id(physical_id)
    respond(event, context, status, data, physical_id, reason=reason)
