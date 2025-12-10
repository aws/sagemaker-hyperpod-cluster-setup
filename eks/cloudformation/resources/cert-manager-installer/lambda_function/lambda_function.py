import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import time
import json

# Environment variables
EKS_CLUSTER_NAME = 'EKS_CLUSTER_NAME'
CERT_MANAGER_ADDON_VERSION = 'CERT_MANAGER_ADDON_VERSION'
AWS_REGION = 'AWS_REGION'

# Constants
CERT_MANAGER_ADDON_NAME = "cert-manager"
CERT_MANAGER_NAMESPACE = "cert-manager"


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing cert-manager EKS add-on
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
            {
                "Status": "FAILED",
                "Reason": str(e)
            }
        )


def write_kubeconfig(cluster_name, region):
    """
    Generate kubeconfig using boto3
    """
    # Initialize EKS client
    eks = boto3.client('eks', region_name=region)
    
    try:
        # Get cluster info
        cluster = eks.describe_cluster(name=cluster_name)['cluster']
        cluster_arn = cluster['arn']
        
        # Generate kubeconfig content
        kubeconfig = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'clusters': [{
                'cluster': {
                    'server': cluster['endpoint'],
                    'certificate-authority-data': cluster['certificateAuthority']['data']
                },
                'name': cluster_name
            }],
            'contexts': [{
                'context': {
                    'cluster': cluster_name,
                    'user': cluster_name
                },
                'name': cluster_arn
            }],
            # rig script get region from current-context value, expected to be cluster arn
            'current-context': cluster_arn, 
            'preferences': {},
            'users': [{
                'name': cluster_name,
                'user': {
                    'exec': {
                        'apiVersion': 'client.authentication.k8s.io/v1beta1',
                        'command': 'aws-iam-authenticator',
                        'args': [
                            'token',
                            '-i',
                            cluster_name
                        ]
                    }
                }
            }]
        }
        
        # Use /tmp instead of ~/.kube
        kubeconfig_dir = '/tmp/.kube'
        os.makedirs(kubeconfig_dir, exist_ok=True)
        kubeconfig_path = os.path.join(kubeconfig_dir, 'config')
        
        with open(kubeconfig_path, 'w') as f:
            yaml.dump(kubeconfig, f, default_flow_style=False)
        
        # Make sure kubectl can read it
        os.chmod(kubeconfig_path, 0o600)
        
        # Set KUBECONFIG environment variable
        os.environ['KUBECONFIG'] = kubeconfig_path
        
        return True
        
    except ClientError as e:
        print(f"Error getting cluster info: {str(e)}")
        raise


def check_cert_manager_exists():
    """
    Cert-manager existence detection based on deployments
    Returns True if cert-manager deployments exist, regardless of pod status
    """
    try:
        result = subprocess.run([
            'kubectl', 'get', 'deployments', '-n', 'cert-manager',
            '-o', 'jsonpath={.items[*].metadata.name}'
        ], capture_output=True, text=True, timeout=5)
        
        if result.returncode == 0 and result.stdout.strip():
            deployment_names = result.stdout.strip()
            print(f"cert-manager deployments found: {deployment_names}")
            return True
        
        return False
        
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Error checking cert-manager existence: {e}")
        return False


def get_addon_status(eks_client, cluster_name):
    """
    Get current status of cert-manager add-on if it exists
    Returns: tuple (addon_arn, addon_status) or (None, None) if not found
    """
    try:
        addon_info = eks_client.describe_addon(
            clusterName=cluster_name,
            addonName=CERT_MANAGER_ADDON_NAME
        )
        return addon_info['addon']['addonArn'], addon_info['addon']['status']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return None, None
        raise


def wait_for_addon_terminal_state(eks_client, cluster_name, max_wait_time=300):
    """
    Wait for cert-manager add-on to reach a terminal state
    Returns: tuple (addon_arn, addon_status)
    """
    start_time = time.time()
    
    while time.time() - start_time < max_wait_time:
        addon_arn, status = get_addon_status(eks_client, cluster_name)
        
        if not addon_arn:
            # Add-on was deleted during wait
            print("Add-on no longer exists")
            return None, "DELETED"
        
        print(f"Cert-manager add-on status: {status}")
        
        # Terminal states - stop waiting
        if status in ['ACTIVE', 'CREATE_FAILED', 'DEGRADED', 'UPDATE_FAILED']:
            print(f"Cert-manager add-on reached terminal state: {status}")
            return addon_arn, status
        
        time.sleep(30)
    
    # Timeout reached - get final status
    addon_arn, status = get_addon_status(eks_client, cluster_name)
    if addon_arn:
        print(f"Timeout waiting for terminal state, current status: {status}")
        return addon_arn, status
    else:
        print("Timeout reached and add-on no longer exists")
        return None, "DELETED"


def get_addon_configuration_values():
    """
    Returns standard cert-manager configuration with tolerations for all node taints
    """
    return {
        # Set single replica for cert-manager controller
        "replicaCount": 1,
        # Global tolerations for cert-manager controller
        "tolerations": [
            {
                "operator": "Exists",
                "effect": "NoSchedule",
            },
            {
                "operator": "Exists",
                "effect": "NoExecute",
            },
            {
                "operator": "Exists",
                "effect": "PreferNoSchedule",
            },
        ],
        # Webhook configuration
        "webhook": {
            "replicaCount": 1,
            "tolerations": [
                {
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
                {
                    "operator": "Exists",
                    "effect": "NoExecute",
                },
                {
                    "operator": "Exists",
                    "effect": "PreferNoSchedule",
                },
            ],
        },
        # Cainjector configuration
        "cainjector": {
            "replicaCount": 1,
            "tolerations": [
                {
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
                {
                    "operator": "Exists",
                    "effect": "NoExecute",
                },
                {
                    "operator": "Exists",
                    "effect": "PreferNoSchedule",
                },
            ],
        },
    }


def remove_addon(eks_client, cluster_name):
    """
    Remove cert-manager EKS add-on and wait for deletion to complete
    Returns: (success: bool, message: str)
    """
    try:
        print("Removing cert-manager add-on...")
        eks_client.delete_addon(
            clusterName=cluster_name,
            addonName=CERT_MANAGER_ADDON_NAME
        )
        
        # Wait for deletion
        start_time = time.time()
        while time.time() - start_time < 300:
            check_arn, _ = get_addon_status(eks_client, cluster_name)
            if not check_arn:
                print("Add-on removed successfully")
                return True, "Add-on deleted"
            time.sleep(10)
        
        return True, "Add-on deletion initiated (may still be in progress)"
        
    except Exception as e:
        print(f"Error removing add-on: {str(e)}")
        return False, str(e)


def delete_self_managed_cert_manager():
    """
    Delete self-managed cert-manager installation including webhooks and CRDs
    Returns: (success: bool, message: str)
    """
    try:
        print("Attempting to delete self-managed cert-manager...")
        
        # Step 1: Delete cert-manager namespace and all its resources
        print("Deleting cert-manager namespace...")
        namespace_result = subprocess.run([
            'kubectl', 'delete', 'namespace', CERT_MANAGER_NAMESPACE, '--wait=true'
        ], capture_output=True, text=True, timeout=180)
        
        if namespace_result.returncode != 0:
            print(f"Failed to delete namespace: {namespace_result.stderr}")
            return False, f"Namespace deletion failed: {namespace_result.stderr}"
        
        print("Namespace deleted successfully")
        
        # Step 2: Delete webhook configurations (cluster-scoped)
        print("Deleting cert-manager webhook configurations...")
        webhook_result = subprocess.run([
            'kubectl', 'delete', 'mutatingwebhookconfiguration,validatingwebhookconfiguration',
            'cert-manager-webhook', '--ignore-not-found=true'
        ], capture_output=True, text=True, timeout=30)
        
        if webhook_result.returncode == 0:
            print("Webhook configurations deleted")
        else:
            print(f"Warning: Failed to delete webhooks: {webhook_result.stderr}")
        
        # Step 3: Get and delete cert-manager CRDs
        print("Getting cert-manager CRDs...")
        crd_list_result = subprocess.run(
            'kubectl get crd | grep cert-manager | awk \'{print $1}\'',
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if crd_list_result.returncode == 0 and crd_list_result.stdout.strip():
            crds = crd_list_result.stdout.strip().split('\n')
            crds = [crd.strip() for crd in crds if crd.strip()]
            print(f"Found {len(crds)} cert-manager CRDs: {crds}")
            
            # Delete each CRD
            print("Deleting cert-manager CRDs...")
            for crd in crds:
                crd_result = subprocess.run([
                    'kubectl', 'delete', 'crd', crd, '--ignore-not-found=true'
                ], capture_output=True, text=True, timeout=30)
                
                if crd_result.returncode == 0:
                    print(f"CRD {crd} deleted")
                else:
                    print(f"Warning: Failed to delete CRD {crd}: {crd_result.stderr}")
        else:
            print("No cert-manager CRDs found")
        
        # Wait for all resources to fully clean up
        print("Waiting for cleanup to complete...")
        time.sleep(30)
        
        return True, "Self-managed cert-manager fully deleted"
            
    except Exception as e:
        print(f"Error deleting self-managed cert-manager: {str(e)}")
        return False, str(e)


def create_cert_manager_addon(cluster_name, addon_version):
    """
    Create NEW cert-manager EKS add-on
    Assumes: EKS add-on does not exist, but self-managed may have been cleaned up
    Returns: tuple (addon_arn, addon_status)
    """
    try:
        eks = boto3.client('eks')
        configuration_values = get_addon_configuration_values()
        
        print(f"Creating cert-manager EKS add-on on cluster {cluster_name} with version {addon_version}...")
        response = eks.create_addon(
            clusterName=cluster_name,
            addonName=CERT_MANAGER_ADDON_NAME,
            addonVersion=addon_version,
            configurationValues=json.dumps(configuration_values),
            resolveConflicts='OVERWRITE'
        )
        
        print(f"Cert-manager add-on creation initiated: {response['addon']['addonArn']}")
        
        # Wait for terminal state
        return wait_for_addon_terminal_state(eks, cluster_name, max_wait_time=300)
        
    except Exception as e:
        print(f"Error creating cert-manager add-on: {str(e)}")
        raise


def update_cert_manager_addon(cluster_name, addon_version):
    """
    Update EXISTING cert-manager EKS add-on
    Assumes: EKS add-on already exists
    Returns: tuple (addon_arn, addon_status)
    """
    try:
        eks = boto3.client('eks')
        configuration_values = get_addon_configuration_values()
        
        print(f"Updating cert-manager EKS add-on to version {addon_version} with tolerations...")
        eks.update_addon(
            clusterName=cluster_name,
            addonName=CERT_MANAGER_ADDON_NAME,
            addonVersion=addon_version,
            configurationValues=json.dumps(configuration_values),
            resolveConflicts='OVERWRITE'
        )
        
        print("Add-on update initiated")
        # Wait for update to complete
        return wait_for_addon_terminal_state(eks, cluster_name, max_wait_time=300)
        
    except Exception as e:
        print(f"Error updating cert-manager add-on: {str(e)}")
        raise


def check_cert_manager_pods_ready():
    """
    Check if cert-manager pods are ready
    Returns True if all cert-manager deployments have ready replicas
    """
    try:
        deployments = [
            'cert-manager',
            'cert-manager-cainjector',
            'cert-manager-webhook'
        ]
        
        for deployment in deployments:
            result = subprocess.run([
                'kubectl', 'get', 'deployment', deployment,
                '-n', CERT_MANAGER_NAMESPACE,
                '-o', 'jsonpath={.status.readyReplicas}'
            ], capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                print(f"Failed to check deployment {deployment}: {result.stderr}")
                return False
            
            ready_replicas = result.stdout.strip()
            if not ready_replicas or int(ready_replicas) == 0:
                print(f"Deployment {deployment} has no ready replicas")
                return False
        
        print("All cert-manager deployments are ready")
        return True
        
    except Exception as e:
        print(f"Error checking cert-manager pods: {str(e)}")
        return False


def on_create():
    """
    Handle Create request to install cert-manager EKS add-on
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "Cert-manager add-on successfully installed"
    }

    try:
        # Ensure required environment variables are set
        required_env_vars = [
            'EKS_CLUSTER_NAME',
            'CERT_MANAGER_ADDON_VERSION',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        cluster_name = os.environ['EKS_CLUSTER_NAME']
        addon_version = os.environ['CERT_MANAGER_ADDON_VERSION']
        region = os.environ['AWS_REGION']

        # Configure kubectl
        write_kubeconfig(cluster_name, region)

        try:
            eks = boto3.client('eks')
            
            # Check if EKS add-on already exists
            addon_arn, addon_status = get_addon_status(eks, cluster_name)
            if addon_arn:
                # Check if add-on is in a failed or stuck state that requires deletion
                if addon_status in ['CREATE_FAILED', 'UPDATE_FAILED', 'CREATING']:
                    print(f"Add-on in failed/stuck state ({addon_status}), deleting before recreating...")
                    remove_addon(eks, cluster_name)
                    # Create new add-on after deletion
                    addon_arn, addon_status = create_cert_manager_addon(cluster_name, addon_version)
                else:
                    # Update if in healthy state
                    print(f"Cert-manager EKS add-on already exists with status {addon_status}, updating...")
                    addon_arn, addon_status = update_cert_manager_addon(cluster_name, addon_version)
            else:
                # Check if self-managed cert-manager exists
                if check_cert_manager_exists():
                    print("Self-managed cert-manager detected, cleaning up...")
                    success, msg = delete_self_managed_cert_manager()
                    if not success:
                        raise Exception(f"Self-managed cleanup failed: {msg}")
                
                # Create new EKS add-on
                addon_arn, addon_status = create_cert_manager_addon(cluster_name, addon_version)
            
            # CertManagerInstalled is True only if addon is in a successful state
            is_installed = addon_status in ['ACTIVE', 'DEGRADED']
            
            response_data["AddonArn"] = addon_arn
            response_data["CertManagerInstalled"] = is_installed
            response_data["AddonStatus"] = addon_status
            response_data["Reason"] = f"Cert-manager add-on installation attempted, status: {addon_status}"
            
        except Exception as e:
            print(f"Failed to install cert-manager add-on: {str(e)}")
            response_data["CertManagerInstalled"] = False
            response_data["AddonStatus"] = "N/A"
            response_data["AddonArn"] = "N/A"
            response_data["Reason"] = f"Failed to install cert-manager add-on: {str(e)}"

        return response_data

    except Exception as e:
        print(f"Error in on_create: {str(e)}")
        response_data["AddonStatus"] = "N/A"
        response_data["AddonArn"] = "N/A"
        response_data["Reason"] = str(e)
        response_data["CertManagerInstalled"] = False
        return response_data


def on_update():
    """
    Handle Update request for cert-manager EKS add-on
    Orchestrates: Check if exists → Update or Create
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "Cert-manager add-on update completed"
    }

    try:
        # Ensure required environment variables are set
        required_env_vars = [
            'EKS_CLUSTER_NAME',
            'CERT_MANAGER_ADDON_VERSION',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        cluster_name = os.environ['EKS_CLUSTER_NAME']
        addon_version = os.environ['CERT_MANAGER_ADDON_VERSION']
        region = os.environ['AWS_REGION']

        # Configure kubectl
        write_kubeconfig(cluster_name, region)

        try:
            eks = boto3.client('eks')
            
            # Check if EKS add-on exists
            addon_arn, addon_status = get_addon_status(eks, cluster_name)
            if addon_arn:
                # Check if add-on is in a failed or stuck state that requires deletion
                if addon_status in ['CREATE_FAILED', 'UPDATE_FAILED', 'CREATING']:
                    print(f"Add-on in failed/stuck state ({addon_status}), deleting before recreating...")
                    remove_addon(eks, cluster_name)
                    # Create new add-on after deletion
                    addon_arn, addon_status = create_cert_manager_addon(cluster_name, addon_version)
                else:
                    # Update if in healthy state
                    print(f"Cert-manager EKS add-on exists with status {addon_status}, updating...")
                    addon_arn, addon_status = update_cert_manager_addon(cluster_name, addon_version)
            else:
                print("Cert-manager EKS add-on not found, creating...")
                
                # Check if self-managed cert-manager exists
                if check_cert_manager_exists():
                    print("Self-managed cert-manager detected, cleaning up...")
                    success, msg = delete_self_managed_cert_manager()
                    if not success:
                        raise Exception(f"Self-managed cleanup failed: {msg}")
                
                # Create new EKS add-on
                addon_arn, addon_status = create_cert_manager_addon(cluster_name, addon_version)
            
            is_installed = addon_status in ['ACTIVE', 'DEGRADED']
            
            response_data["AddonArn"] = addon_arn
            response_data["CertManagerInstalled"] = is_installed
            response_data["AddonStatus"] = addon_status
            response_data["Reason"] = f"Cert-manager add-on update attempted, status: {addon_status}"
            
        except Exception as e:
            print(f"Failed to update cert-manager add-on: {str(e)}")
            response_data["CertManagerInstalled"] = False
            response_data["AddonStatus"] = "N/A"
            response_data["AddonArn"] = "N/A"
            response_data["Reason"] = f"Failed to update cert-manager add-on: {str(e)}"

        return response_data

    except Exception as e:
        print(f"Error in on_update: {str(e)}")
        response_data["AddonArn"] = "N/A"
        response_data["AddonStatus"] = "N/A"
        response_data["Reason"] = str(e)
        response_data["CertManagerInstalled"] = False
        return response_data


def on_delete():
    """
    Handle Delete request to uninstall cert-manager EKS add-on
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Cert-manager add-on uninstall completed"
        }

        cluster_name = os.environ.get(EKS_CLUSTER_NAME)
        if not cluster_name:
            print("Cluster name not found, skipping cleanup")
            response_data["AddonArn"] = "N/A"
            response_data["AddonStatus"] = "N/A"
            return response_data

        eks = boto3.client('eks')

        # Check if add-on exists before attempting deletion
        addon_arn, addon_status = get_addon_status(eks, cluster_name)
        if not addon_arn:
            print("Cert-manager add-on not found, already deleted")
            response_data["CertManagerUninstalled"] = True
            response_data["AddonArn"] = "N/A"
            response_data["AddonStatus"] = "DELETED"
            return response_data

        # Delete add-on
        print(f"Deleting cert-manager add-on from cluster {cluster_name}...")
        remove_addon(eks, cluster_name)

        response_data["CertManagerUninstalled"] = True
        response_data["AddonArn"] = "N/A"
        response_data["AddonStatus"] = "DELETED"
        return response_data

    except Exception as e:
        print(f"Error in on_delete: {str(e)}")
        # Return SUCCESS anyway to not block stack deletion
        return {
            "Status": "SUCCESS",
            "AddonArn": "N/A",
            "AddonStatus": "N/A",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
