"""Starts the Slurm observability setup stack (slurm-observability-setup-template.yaml) for a new cluster.

The cluster's stack creates this custom resource once the cluster exists. On Create it starts the setup stack as a
separate top-level stack and returns at once, so observability failing (a Grafana or Prometheus workspace quota, a VPC
endpoint, a role) rolls back only that stack (OnFailure=DELETE) and never the cluster. The customer can then install the
observability extension from the cluster's Extensions tab.

On Delete it deletes the setup stack and waits until it is gone: its VPC endpoints are in the cluster's VPC, which the
cluster's stack deletes after this resource. A wait longer than one invocation continues in the next one (the function
invokes itself with the same request), up to MAX_DELETE_INVOCATIONS, within the custom resource's ServiceTimeout.
"""
import json
import logging
import os
import re
import time
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The cluster tag naming the setup stack, which the console reads while the stack runs (the cluster extensions function
# in that stack sets it too, once it gets that far).
SETUP_STACK_TAG = 'hyperpod-cluster-extensions-stack'
# The tag's value when the setup stack couldn't be started, instead of 'pending'.
NOT_CREATED_TAG_VALUE = 'not-created'
CAPABILITIES = ['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
STACK_ID_PATTERN = re.compile(r'arn:aws[a-z-]*:cloudformation:[a-z0-9-]+:\d{12}:stack/[^/]+/.+')
# Every call gives up quickly, so a slow API never keeps the response to CloudFormation waiting past the timeout.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 2})
POLL_SECONDS = 10
# Kept, when the deletion takes longer than the function has left, for the last describe and the response to
# CloudFormation with all its retries.
RESPONSE_MARGIN_SECONDS = 150
RESPONSE_ATTEMPTS = 5
RESPONSE_TIMEOUT_SECONDS = 15
BACKOFF_MAX_SECONDS = 8
# CloudFormation rejects a response body over 4096 bytes.
MAX_REASON_LENGTH = 1000
# Invocations a Delete can take: about 12 minutes each, within the custom resource's ServiceTimeout of an hour.
MAX_DELETE_INVOCATIONS = 4
# The request's key counting the invocations a Delete has taken.
INVOCATION_KEY = 'SetupStackDeleteInvocation'
# CreateStack's limit for ClientRequestToken.
CLIENT_REQUEST_TOKEN_LENGTH = 128
TAG_ATTEMPTS = 3
TAG_BACKOFF_SECONDS = 2


def client(service):
    return boto3.client(service, config=CLIENT_CONFIG)


def stack_parameters(values):
    """CreateStack's parameters from the resource's Parameters map. A list (a CommaDelimitedList) is joined."""
    return [
        {'ParameterKey': key, 'ParameterValue': ','.join(value) if isinstance(value, list) else str(value)}
        for key, value in values.items()
    ]


def tag_cluster(cluster_arn, stack_id):
    """Replaces the cluster's pending tag with the setup stack (or not-created), retried. Best effort: a pending tag
    left behind only makes the console wait a few minutes after the cluster is InService before offering Install."""
    for attempt in range(1, TAG_ATTEMPTS + 1):
        try:
            client('sagemaker').add_tags(ResourceArn=cluster_arn, Tags=[{'Key': SETUP_STACK_TAG, 'Value': stack_id}])
            return
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Could not tag the cluster with the setup stack (attempt {attempt}): {e}')
            if attempt < TAG_ATTEMPTS:
                time.sleep(TAG_BACKOFF_SECONDS * attempt)


def existing_setup_stack(cloudformation, stack_name, cluster_arn):
    """The ID of the stack with this name if it is this cluster's setup stack (a create that succeeded though its
    response was lost), else None."""
    stack = cloudformation.describe_stacks(StackName=stack_name)['Stacks'][0]
    parameters = {p['ParameterKey']: p.get('ParameterValue') for p in stack.get('Parameters', [])}
    return stack['StackId'] if parameters.get('HyperPodClusterArn') == cluster_arn else None


def not_created_id(event):
    """The physical ID when there is no setup stack: not a stack ID, so a Delete deletes nothing."""
    return f'{event.get("LogicalResourceId")}-not-created'


def own_stack_tags(cloudformation, stack_id):
    """The tags of the cluster's stack, which a nested stack would inherit, without the aws: ones CloudFormation sets.
    Best effort: without them, the setup stack is created untagged."""
    try:
        tags = cloudformation.describe_stacks(StackName=stack_id)['Stacks'][0].get('Tags', [])
    except (ClientError, BotoCoreError) as e:
        logger.warning(f'Could not read the tags of the stack {stack_id}; the setup stack is created without them: {e}')
        return []
    return [tag for tag in tags if not tag['Key'].startswith('aws:')]


def start_setup_stack(event, stack_name, cluster_arn):
    """Starts the setup stack, or finds this cluster's one a lost response created. Returns its ID."""
    props = event['ResourceProperties']
    cloudformation = client('cloudformation')
    try:
        stack_id = cloudformation.create_stack(
            StackName=stack_name,
            # Set by the cluster's stack on the function, so a request can't start another template with its
            # permissions.
            TemplateURL=os.environ['SETUP_TEMPLATE_URL'],
            Parameters=stack_parameters(props.get('Parameters', {})),
            Capabilities=CAPABILITIES,
            OnFailure='DELETE',
            Tags=own_stack_tags(cloudformation, event['StackId']),
            # The same for a retry of this request (by the SDK or CloudFormation), so it doesn't create another.
            ClientRequestToken=f'setup-{event["RequestId"]}'[:CLIENT_REQUEST_TOKEN_LENGTH],
        )['StackId']
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'AlreadyExistsException':
            raise
        stack_id = existing_setup_stack(cloudformation, stack_name, cluster_arn)
        if not stack_id:
            raise
        logger.info(f'The observability setup stack {stack_id} already exists for this cluster')
    logger.info(f'Started the observability setup stack {stack_id}')
    return stack_id


def on_create(event):
    """Starts the setup stack without waiting for it. Returns the physical resource ID (the stack's ID) and the data.

    When the stack can't be started, it succeeds without a stack, with the error in the log, so the cluster is kept:
    the customer installs the extension from the cluster's Extensions tab. Only a malformed request fails."""
    props = event['ResourceProperties']
    stack_name, cluster_arn = props['StackName'], props['ClusterArn']
    try:
        stack_id = start_setup_stack(event, stack_name, cluster_arn)
    except Exception:  # pylint: disable=broad-except
        logger.exception(f'Could not start the observability setup stack {stack_name}; the cluster is kept')
        # The create may have started before the error, a timeout for example.
        try:
            stack_id = existing_setup_stack(client('cloudformation'), stack_name, cluster_arn)
        except Exception as lookup_error:  # pylint: disable=broad-except
            logger.warning(f'Could not look up the observability setup stack {stack_name}: {lookup_error}')
            stack_id = None
        if not stack_id:
            tag_cluster(cluster_arn, NOT_CREATED_TAG_VALUE)
            return not_created_id(event), {}
        logger.info(f'The observability setup stack {stack_id} was started for this cluster')
    tag_cluster(cluster_arn, stack_id)
    return stack_id, {'StackId': stack_id}


def describe_stack(cloudformation, stack_id):
    """The stack, or None when it doesn't exist. By ID, a deleted stack is still found, as DELETE_COMPLETE."""
    try:
        return cloudformation.describe_stacks(StackName=stack_id)['Stacks'][0]
    except ClientError as e:
        if 'does not exist' in str(e):
            return None
        raise


def is_gone(stack):
    return stack is None or stack['StackStatus'] == 'DELETE_COMPLETE'


def continue_delete(event, context):
    """Hands the wait to a new invocation of this function, with the same request. Returns whether it did."""
    invocation = int(event.get(INVOCATION_KEY, 1))
    if invocation >= MAX_DELETE_INVOCATIONS:
        return False
    try:
        client('lambda').invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType='Event',
            Payload=json.dumps({**event, INVOCATION_KEY: invocation + 1}).encode('utf-8'),
        )
    except (ClientError, BotoCoreError) as e:
        # This invocation then answers that the stack is still being deleted, rather than with the invoke's error.
        logger.warning(f'Could not continue the wait in another invocation: {e}')
        return False
    logger.info(f'The setup stack is still being deleted: continuing in invocation {invocation + 1}')
    return True


def on_delete(event, context):
    """Deletes the setup stack and waits until it is gone. Returns False when the wait continues in another
    invocation, which answers CloudFormation."""
    stack_id = event.get('PhysicalResourceId', '')
    if not STACK_ID_PATTERN.fullmatch(stack_id):
        logger.info(f'No setup stack to delete ({stack_id}): it was not created')
        return True
    cloudformation = client('cloudformation')
    delete_requested = False
    while True:
        try:
            stack = describe_stack(cloudformation, stack_id)
            if is_gone(stack):
                logger.info(f'Deleted the setup stack {stack_id}' if delete_requested
                            else f'The setup stack {stack_id} is already deleted')
                return True
            status = stack['StackStatus']
            if delete_requested:
                logger.info(f'The setup stack {stack_id} is {status}')
                if status == 'DELETE_FAILED':
                    raise RuntimeError(
                        f'The setup stack {stack_id} failed to delete: {stack.get("StackStatusReason", "")}'
                    )
            else:
                if status != 'DELETE_IN_PROGRESS':
                    # Also after DELETE_FAILED: deleting the cluster's stack again retries the setup stack's deletion.
                    cloudformation.delete_stack(StackName=stack_id)
                delete_requested = True
        except (ClientError, BotoCoreError) as e:
            # Throttling, a server error or a timeout: the next poll tries again, within the time left.
            logger.warning(f'Could not describe or delete the setup stack {stack_id}: {e}')
        if context.get_remaining_time_in_millis() / 1000 < RESPONSE_MARGIN_SECONDS + POLL_SECONDS:
            if continue_delete(event, context):
                return False
            raise TimeoutError(f'The setup stack {stack_id} is still being deleted. Delete this stack again.')
        time.sleep(POLL_SECONDS)


def respond(event, context, status, physical_id, data, reason=''):
    """Sends the response to CloudFormation, retried: a response that never arrives holds the stack until its timeout."""
    body = json.dumps({
        'Status': status,
        'Reason': reason or f'See the details in CloudWatch Log Stream: {context.log_stream_name}',
        'PhysicalResourceId': physical_id,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data,
    }).encode('utf-8')
    for attempt in range(1, RESPONSE_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                event['ResponseURL'], data=body, method='PUT', headers={'Content-Type': '', 'Content-Length': str(len(body))}
            )
            with urllib.request.urlopen(request, timeout=RESPONSE_TIMEOUT_SECONDS):
                return
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Sending the response failed (attempt {attempt}): {e}')
            if attempt < RESPONSE_ATTEMPTS:
                time.sleep(min(BACKOFF_MAX_SECONDS, 2 ** attempt))
    logger.error('The response to CloudFormation was not sent')


def lambda_handler(event, context):
    logger.info(f'Received event: {json.dumps({k: v for k, v in event.items() if k != "ResponseURL"})}')
    request_type = event.get('RequestType')
    # A Create that fails has no stack: an ID that isn't a stack ID, so the rollback's Delete deletes nothing.
    physical_id = event.get('PhysicalResourceId') or not_created_id(event)
    status, data, reason = 'SUCCESS', {}, ''
    try:
        if request_type == 'Create':
            physical_id, data = on_create(event)
        elif request_type == 'Delete':
            if not on_delete(event, context):
                # The next invocation answers.
                return
        elif request_type == 'Update':
            # The setup stack is set up once, with the cluster; the customer changes extensions from the cluster's
            # Extensions tab.
            logger.info('Update: the setup stack is left as it is')
            old_cluster_arn = event.get('OldResourceProperties', {}).get('ClusterArn')
            if old_cluster_arn != event['ResourceProperties'].get('ClusterArn'):
                setup_stack = physical_id if STACK_ID_PATTERN.fullmatch(physical_id) else 'none'
                logger.warning(
                    f'The cluster was replaced: the setup stack ({setup_stack}) still targets {old_cluster_arn}'
                )
        else:
            raise ValueError(f'Invalid request type: {request_type}')
    except Exception as e:  # pylint: disable=broad-except
        logger.exception(f'{request_type} failed')
        status, reason = 'FAILED', str(e)[:MAX_REASON_LENGTH]
    respond(event, context, status, physical_id, data, reason)
