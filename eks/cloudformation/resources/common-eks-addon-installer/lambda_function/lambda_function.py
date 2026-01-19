import boto3
import os
import cfnresponse
from botocore.exceptions import ClientError
import time
import json

# Environment variables
EKS_CLUSTER_NAME = 'EKS_CLUSTER_NAME'
ADDON_NAME = 'ADDON_NAME'
ADDON_VERSION = 'ADDON_VERSION'
ADDON_CONFIGURATION = 'ADDON_CONFIGURATION'
SERVICE_ACCOUNT_ROLE_ARN = 'SERVICE_ACCOUNT_ROLE_ARN'
ADDON_INSTALLATION_TIMEOUT = 'ADDON_INSTALLATION_TIMEOUT'
REQUIRED_ENV_VARS = [
    'EKS_CLUSTER_NAME',
    'ADDON_NAME'
]

# Tags
CREATED_BY_TAG_KEY = 'CreatedBy'
CREATED_BY_HYPERPOD_INFERENCE_TAG_VALUE = 'HyperPodInference'

# Addon status
ADDON_FAILED_STATUSES = ['CREATE_FAILED', 'UPDATE_FAILED', 'DELETE_FAILED']
ADDON_SUCCESS_STATUSES = ['ACTIVE', 'DEGRADED']

# Default timeout for addon installation in seconds (5 minutes), pass in a higher value if
# addon installation takes longer
DEFAULT_ADDON_INSTALLATION_TIMEOUT_IN_SECONDS = 300

def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing EKS addons
    """
    try:
        request_type = event['RequestType']

        if request_type == 'Create':
            response_data = on_create()
        elif request_type == 'Update':
            response_data = on_update()
        elif request_type == 'Delete':
            response_data = on_delete()
        else:
            raise ValueError(f"Invalid request type: {request_type}")

        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            response_data
        )

    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {},
            reason=str(e)
        )


def get_addon_status(eks_client, cluster_name, addon_name):
    """
    Get current status of addon if it exists
    Returns: tuple (addon_arn, addon_status, addon_version) or (None, None, None) if not found
    """
    try:
        addon_info = eks_client.describe_addon(
            clusterName=cluster_name,
            addonName=addon_name
        )
        return (
            addon_info['addon']['addonArn'],
            addon_info['addon']['status'],
            addon_info['addon']['addonVersion']
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return None, None, None
        raise


def compare_versions(version1, version2):
    """
    Compare two semantic versions, handling EKS addon versions with suffixes like 'eksbuild'
    Returns: 1 if version1 > version2, -1 if version1 < version2, 0 if equal
    """
    def extract_numeric_parts(version):
        """Extract numeric parts from version string, handling suffixes"""
        # Remove 'v' prefix if present
        v = version.lstrip('v')
        
        # Split by dots
        parts = v.split('.')
        numeric_parts = []
        
        for part in parts:
            # Extract only the numeric portion before any hyphen or non-numeric characters
            numeric_part = ''
            for char in part:
                if char.isdigit():
                    numeric_part += char
                else:
                    break
            
            # If we found numeric digits, convert to int, otherwise use 0
            if numeric_part:
                numeric_parts.append(int(numeric_part))
            else:
                numeric_parts.append(0)
        
        return numeric_parts
    
    try:
        parts1 = extract_numeric_parts(version1)
        parts2 = extract_numeric_parts(version2)
        
        # Pad shorter version with zeros
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))
        
        # Compare each part
        for p1, p2 in zip(parts1, parts2):
            if p1 > p2:
                return 1
            elif p1 < p2:
                return -1
        
        return 0
        
    except Exception as e:
        print(f"Warning: Error comparing versions '{version1}' and '{version2}': {e}")
        # If comparison fails, assume versions are equal to avoid update attempts
        return 0


def get_addon_health_issues(eks_client, cluster_name, addon_name):
    """
    Get health issues for an addon
    Returns: list of formatted error messages
    """
    addon_info = eks_client.describe_addon(
        clusterName=cluster_name,
        addonName=addon_name
    )
    health_issues = addon_info.get('addon', {}).get('health', {}).get('issues', [])
    return [f"{issue.get('code', 'Unknown')}: {issue.get('message', 'Unknown Error')}" for issue in health_issues]


def wait_for_addon_terminal_state(eks_client, cluster_name, addon_name):
    """
    Wait for addon to reach a terminal state
    """
    timeout = int(os.environ.get(ADDON_INSTALLATION_TIMEOUT, DEFAULT_ADDON_INSTALLATION_TIMEOUT_IN_SECONDS))
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        addon_arn, status, version = get_addon_status(eks_client, cluster_name, addon_name)
        
        if not addon_arn:
            print("Addon no longer exists")
            return
        
        print(f"Addon {addon_name} status: {status}")
        
        # Check for failed states
        if status in ADDON_FAILED_STATUSES:
            error_messages = get_addon_health_issues(eks_client, cluster_name, addon_name)
            raise RuntimeError(f"Addon {addon_name} failed with status {status}. Issues: {'; '.join(error_messages)}")
        
        # Success terminal states
        if status in ADDON_SUCCESS_STATUSES:
            return
        
        time.sleep(30)
    
    # Timeout reached
    raise TimeoutError(f"Timeout waiting for addon {addon_name} moving to terminal state")


def parse_addon_configuration(config_string):
    """
    Parse addon configuration from string to dict
    Returns: dict or None if empty/invalid
    """
    if not config_string or config_string.strip() == '':
        return None
    
    try:
        return json.loads(config_string)
    except json.JSONDecodeError as e:
        print(f"Warning: Failed to parse addon configuration: {e}")
        return None


def is_addon_managed_by_inference(eks_client, addon_arn):
    """
    Check if addon was created by this stack by verifying the prerequisite tag
    Returns: bool
    """
    try:
        response = eks_client.list_tags_for_resource(resourceArn=addon_arn)
        tags = response.get('tags', {})
        
        # Check if our management tag exists
        has_tag = tags.get(CREATED_BY_TAG_KEY) == CREATED_BY_HYPERPOD_INFERENCE_TAG_VALUE
        return has_tag
    except Exception as e:
        print(f"Error checking addon tags: {str(e)}")
        # If we can't check tags, assume NOT managed by us for safety
        return False


def remove_addon(eks_client, cluster_name, addon_name):
    """
    Remove EKS addon and wait for deletion to complete
    """
    try:
        timeout = int(os.environ.get(ADDON_INSTALLATION_TIMEOUT, DEFAULT_ADDON_INSTALLATION_TIMEOUT_IN_SECONDS))
        print(f"Removing addon {addon_name}...")
        eks_client.delete_addon(
            clusterName=cluster_name,
            addonName=addon_name
        )
        
        # Wait for deletion
        start_time = time.time()
        while time.time() - start_time < timeout:
            check_arn, _, _ = get_addon_status(eks_client, cluster_name, addon_name)
            if not check_arn:
                print("Addon removed successfully")
                return
            time.sleep(10)
        
        raise TimeoutError(f"Timeout waiting for addon {addon_name} deletion to complete")
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(f"Addon {addon_name} not found during deletion, already deleted")
            return
        raise


def create_addon(cluster_name, addon_name, addon_version, configuration_values=None, service_account_role_arn=None):
    """
    Create new EKS addon
    """
    try:
        eks = boto3.client('eks')
        
        create_params = {
            'clusterName': cluster_name,
            'addonName': addon_name,
            'addonVersion': addon_version,
            'resolveConflicts': 'OVERWRITE',
            'tags': {
                CREATED_BY_TAG_KEY: CREATED_BY_HYPERPOD_INFERENCE_TAG_VALUE
            },
        }
        
        if configuration_values:
            create_params['configurationValues'] = json.dumps(configuration_values)
        
        if service_account_role_arn:
            create_params['serviceAccountRoleArn'] = service_account_role_arn
        
        response = eks.create_addon(**create_params)
        
        print(f"Addon creation initiated: {response['addon']['addonArn']}")
        
        # Wait for terminal state
        wait_for_addon_terminal_state(eks, cluster_name, addon_name)
        
    except Exception as e:
        print(f"Error creating addon: {str(e)}")
        raise


def update_addon(cluster_name, addon_name, addon_version, configuration_values=None, service_account_role_arn=None):
    """
    Update existing EKS addon
    """
    try:
        eks = boto3.client('eks')
        
        print(f"Updating addon {addon_name} to version {addon_version}...")
        
        update_params = {
            'clusterName': cluster_name,
            'addonName': addon_name,
            'addonVersion': addon_version,
            'resolveConflicts': 'OVERWRITE'
        }
        
        if configuration_values:
            update_params['configurationValues'] = json.dumps(configuration_values)
        
        if service_account_role_arn:
            update_params['serviceAccountRoleArn'] = service_account_role_arn
        
        eks.update_addon(**update_params)
        
        print("Addon update initiated")
        # Wait for update to complete
        wait_for_addon_terminal_state(eks, cluster_name, addon_name)
        
    except Exception as e:
        print(f"Error updating addon: {str(e)}")
        raise


def update_addon_version(cluster_name, addon_name, addon_version, current_version, configuration_values, service_account_role_arn=None):
    """
    Handle version comparison and update logic for existing addons
    """
    if addon_version and addon_version.strip():
        # Version specified - compare with current version
        version_comparison = compare_versions(addon_version, current_version)
        if version_comparison > 0:
            # New version is higher - update
            print(f"Addon {addon_name} already exists with version {current_version}, upgrading to {addon_version}...")
            update_addon(cluster_name, addon_name, addon_version, configuration_values, service_account_role_arn)
        else:
            # Same or lower version - skip update
            print(f"Addon {addon_name} already exists with version {current_version} (requested: {addon_version}), skipping update")
    else:
        # No version specified - keep existing version
        print(f"Addon {addon_name} already exists with version {current_version}, no version specified, keeping current version")


def on_create():
    """
    Handle Create request to install EKS addon
    """
    # Ensure required environment variables are set
    for var in REQUIRED_ENV_VARS:
        if var not in os.environ:
            raise ValueError(f"Missing required environment variable: {var}")

    cluster_name = os.environ['EKS_CLUSTER_NAME']
    addon_name = os.environ['ADDON_NAME']
    addon_version = os.environ.get('ADDON_VERSION', '')
    service_account_role_arn = os.environ.get('SERVICE_ACCOUNT_ROLE_ARN', '')
    
    # Parse configuration
    configuration_values = parse_addon_configuration(os.environ.get('ADDON_CONFIGURATION', ''))
    
    # Only pass service_account_role_arn if it's not empty
    sa_role_arn = service_account_role_arn if service_account_role_arn else None

    eks = boto3.client('eks')
    
    # Check if EKS addon already exists
    addon_arn, _, current_version = get_addon_status(eks, cluster_name, addon_name)
    if addon_arn:
        # Addon exists - handle version update
        update_addon_version(
            cluster_name, addon_name, addon_version, current_version, configuration_values, sa_role_arn
        )
    else:
        # Create new EKS addon
        print(f"Addon {addon_name} not found, creating...")
        create_addon(cluster_name, addon_name, addon_version, configuration_values, sa_role_arn)
    
    final_arn, final_status, _ = get_addon_status(eks, cluster_name, addon_name)
    
    return {
        'AddonArn': final_arn or '',
        'AddonStatus': final_status or 'UNKNOWN'
    }


def on_update():
    """
    Handle Update request for EKS addon
    """
    # Ensure required environment variables are set
    for var in REQUIRED_ENV_VARS:
        if var not in os.environ:
            raise ValueError(f"Missing required environment variable: {var}")

    cluster_name = os.environ['EKS_CLUSTER_NAME']
    addon_name = os.environ['ADDON_NAME']
    addon_version = os.environ.get('ADDON_VERSION', '')
    service_account_role_arn = os.environ.get('SERVICE_ACCOUNT_ROLE_ARN', '')
    
    # Parse configuration
    configuration_values = parse_addon_configuration(os.environ.get('ADDON_CONFIGURATION', ''))
    
    # Only pass service_account_role_arn if it's not empty
    sa_role_arn = service_account_role_arn if service_account_role_arn else None

    eks = boto3.client('eks')
    
    # Check if EKS addon exists
    addon_arn, addon_status, current_version = get_addon_status(eks, cluster_name, addon_name)
    if addon_arn:
        # Throw error if addon is in unhealthy state
        if addon_status not in ADDON_SUCCESS_STATUSES:
            error_msg = (
                f"Addon {addon_name} is in {addon_status} state. "
                f"Please ensure addon is in {ADDON_SUCCESS_STATUSES} or remove the addon in FAILED state and retry."
            )
            raise RuntimeError(error_msg)
        
        # Addon exists and healthy - attempt version update
        update_addon_version(
            cluster_name, addon_name, addon_version, current_version, configuration_values, sa_role_arn
        )
    else:
        print(f"Addon {addon_name} not found, creating...")
        # Create new EKS addon
        create_addon(cluster_name, addon_name, addon_version, configuration_values, sa_role_arn)
    
    final_arn, final_status, _ = get_addon_status(eks, cluster_name, addon_name)
    
    return {
        'AddonArn': final_arn or '',
        'AddonStatus': final_status or 'UNKNOWN'
    }


def on_delete():
    """
    Handle Delete request to uninstall EKS addon
    """
    cluster_name = os.environ.get(EKS_CLUSTER_NAME)
    addon_name = os.environ.get(ADDON_NAME)
    
    if not cluster_name or not addon_name:
        print("Cluster name or addon name not found, skipping cleanup")
        return {}
    
    eks = boto3.client('eks')

    # Check if addon exists before attempting deletion
    addon_arn, _, _ = get_addon_status(eks, cluster_name, addon_name)
    
    if addon_arn and is_addon_managed_by_inference(eks, addon_arn):
        # Delete addon only if it exists and is managed by us
        print(f"Deleting addon {addon_name} from cluster {cluster_name}...")
        remove_addon(eks, cluster_name, addon_name)
    elif addon_arn:
        print(f"Addon {addon_name} was NOT created by hyperpod inference addon - skipping deletion")
    else:
        print(f"Addon {addon_name} not found, already deleted")

    return {}
