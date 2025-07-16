import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml

def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing Helm Charts
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
                'name': cluster_name
            }],
            'current-context': cluster_name,
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


def on_create():
    """
    Handle Set Up an FSx for Lustre File System
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx is set up successfully"
        }

        # Ensure required environment variables are set
        required_env_vars = [
            'CLUSTER_NAME',
            'PRIVATE_SUBNET_ID',
            'SECURITY_GROUP_ID',
            'PER_UNIT_STORAGE_THROUGHPUT',
            'DATA_COMPRESSION_TYPE',
            'FILE_SYSTEM_TYPE_VERSION',
            'STORAGE_CAPACITY',
            'FSX_FILE_SYSTEM_ID',
            'PATH',
            'GIT_EXEC_PATH',
            'KUBECONFIG',
            'LD_LIBRARY_PATH'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
            
         # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
        os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
        os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
        
        # Create directories
        os.makedirs('/tmp/.helm/cache', exist_ok=True)
        os.makedirs('/tmp/.helm/config', exist_ok=True)

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Associate IAM OIDC provider with the cluster
        subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', os.environ['CLUSTER_NAME'], '--approve'], check=True)

        # Create IAM service account for FSx CSI controller
        subprocess.run(['eksctl', 'create', 'iamserviceaccount',
                        '--name', 'fsx-csi-controller-sa',
                        '--namespace', 'kube-system',
                        '--cluster', os.environ['CLUSTER_NAME'],
                        '--attach-policy-arn', 'arn:aws:iam::aws:policy/AmazonFSxFullAccess',
                        '--approve',
                        '--role-name', f"FSXLCSI-{os.environ['CLUSTER_NAME']}-{os.environ['AWS_REGION']}",
                        '--region', os.environ['AWS_REGION']], check=True)

        # Verify proper annotation of the service account with the IAM role ARN
        try:
            result = subprocess.run(['kubectl', 'get', 'sa', 'fsx-csi-controller-sa', '-n', 'kube-system', '-oyaml'], 
                                   check=True, capture_output=True, text=True)
            print(f"Service account verification:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify service account: {e}")

        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to install Helm chart: {str(e)}")


def on_update():
    """
    Handle Update request to upgrade the AWS FSx CSI driver
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx CSI driver updated successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
            
        # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
        os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
        os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
        
        # Create directories
        os.makedirs('/tmp/.helm/cache', exist_ok=True)
        os.makedirs('/tmp/.helm/config', exist_ok=True)

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Associate IAM OIDC provider with the cluster (if not already done)
        try:
            subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', os.environ['CLUSTER_NAME'], '--approve'], check=True)
        except subprocess.CalledProcessError as e:
            # This might fail if already exists, which is fine
            print(f"Note: OIDC provider association: {e}")

        # Create or update IAM service account for FSx CSI controller
        subprocess.run(['eksctl', 'create', 'iamserviceaccount',
                        '--name', 'fsx-csi-controller-sa',
                        '--namespace', 'kube-system',
                        '--cluster', os.environ['CLUSTER_NAME'],
                        '--attach-policy-arn', 'arn:aws:iam::aws:policy/AmazonFSxFullAccess',
                        '--approve',
                        '--role-name', f"FSXLCSI-{os.environ['CLUSTER_NAME']}-{os.environ['AWS_REGION']}",
                        '--region', os.environ['AWS_REGION']], check=True)

        # Verify proper annotation of the service account with the IAM role ARN
        try:
            result = subprocess.run(['kubectl', 'get', 'sa', 'fsx-csi-controller-sa', '-n', 'kube-system', '-oyaml'], 
                                   check=True, capture_output=True, text=True)
            print(f"Service account verification:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify service account: {e}")

        # Add FSx CSI Driver Helm repository
        subprocess.run(['helm', 'repo', 'add', 'aws-fsx-csi-driver', 'https://kubernetes-sigs.github.io/aws-fsx-csi-driver'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)

        # Update AWS FSx CSI Driver using Helm
        subprocess.run(['helm', 'upgrade', '--install', 
                        'aws-fsx-csi-driver', 'aws-fsx-csi-driver/aws-fsx-csi-driver',
                        '--namespace', 'kube-system',
                        '--set', 'controller.serviceAccount.create=false'], check=True)

        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to update AWS FSx CSI driver: {str(e)}")

def on_delete():
    """
    Handle Delete request to uninstall the AWS FSx CSI driver
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx CSI driver uninstalled successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Uninstall the AWS FSx CSI driver
        try:
            subprocess.run(['helm', 'uninstall', 'aws-fsx-csi-driver', '--namespace', 'kube-system'], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to uninstall FSx CSI driver: {e}")

        # Delete the IAM service account
        try:
            subprocess.run(['eksctl', 'delete', 'iamserviceaccount',
                          '--name', 'fsx-csi-controller-sa',
                          '--namespace', 'kube-system',
                          '--cluster', os.environ['CLUSTER_NAME'],
                          '--region', os.environ['AWS_REGION']], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to delete service account: {e}")

        return response_data

    except Exception as e:
        print(f"Error during deletion: {str(e)}")
        # Return SUCCESS anyway to allow stack deletion to proceed
        return {
            "Status": "SUCCESS",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
