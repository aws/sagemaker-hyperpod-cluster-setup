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


def install_fsx_csi_driver():
    """
    Install AWS FSx CSI Driver using Helm
    """
    try:
        print("Installing AWS FSx CSI Driver...")
        
        # Add FSx CSI driver repository
        subprocess.run(['helm', 'repo', 'add', 'aws-fsx-csi-driver', 'https://kubernetes-sigs.github.io/aws-fsx-csi-driver'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)
        
        # Install FSx CSI driver
        subprocess.run(['helm', 'upgrade', '--install', 
                    'aws-fsx-csi-driver', 'aws-fsx-csi-driver/aws-fsx-csi-driver',
                    '--namespace', 'kube-system',
                    '--set', 'controller.serviceAccount.create=false'], check=True)
        
        print("AWS FSx CSI Driver installed successfully")
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to install FSx CSI driver: {e.cmd}. Return code: {e.returncode}")


def install_helm_chart():
    """
    Install custom Helm chart from GitHub repository
    """
    try:
        print("Installing custom Helm chart...")
        
        # Ensure required environment variables are set
        required_env_vars = [
            'GITHUB_REPO_URL',
            'CHART_PATH',
            'NAMESPACE',
            'RELEASE_NAME',
            'OPERATORS'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        
        # Add required Helm repositories
        subprocess.run(['helm', 'repo', 'add', 'nvidia', 'https://nvidia.github.io/k8s-device-plugin'], check=True)
        subprocess.run(['helm', 'repo', 'add', 'eks', 'https://aws.github.io/eks-charts/'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)

        # Clone the GitHub repository
        clone_cmd = [
            'git', 'clone',
            os.environ['GITHUB_REPO_URL'],
            '/tmp/helm-charts'
        ]
        subprocess.run(clone_cmd, check=True)

        # Update dependencies
        subprocess.run(['helm', 'dependency', 'update', f"/tmp/helm-charts/{os.environ['CHART_PATH']}"], check=True)

        # Install the Helm chart
        install_cmd = [
            'helm', 'install',
            os.environ['RELEASE_NAME'],
            f"/tmp/helm-charts/{os.environ['CHART_PATH']}",
            '--namespace', os.environ['NAMESPACE'],
            '--set', os.environ['OPERATORS']
        ]
        subprocess.run(install_cmd, check=True)

        # Clean up cloned repository
        subprocess.run(['rm', '-rf', '/tmp/helm-charts'], check=True)
        
        print("Custom Helm chart installed successfully")
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to install Helm chart: {e.cmd}. Return code: {e.returncode}")


def on_create():
    """
    Handle Create request to install Helm charts
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Helm charts installed successfully"
        }

        # Ensure required environment variables are set
        required_env_vars = [
            'CLUSTER_NAME',
            'CREATE_FSX',
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

        # Install FSx CSI Driver if requested
        if os.environ['CREATE_FSX'].lower() == 'true':
            install_fsx_csi_driver()
            response_data["FSxCSIDriverInstalled"] = True
        else:
            response_data["FSxCSIDriverInstalled"] = False

        # Install custom Helm chart if not FSx-only mode
        if os.environ['CREATE_FSX'].lower() != 'true':
            install_helm_chart()
            response_data["CustomChartInstalled"] = True
        else:
            response_data["CustomChartInstalled"] = False

        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to install Helm charts: {str(e)}")

def update_helm_chart():
    """
    Update custom Helm chart from GitHub repository
    """
    try:
        print("Updating custom Helm chart...")
        
        # Ensure required environment variables are set
        required_env_vars = [
            'GITHUB_REPO_URL',
            'CHART_PATH',
            'RELEASE_NAME'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Add required Helm repositories
        subprocess.run(['helm', 'repo', 'add', 'nvidia', 'https://nvidia.github.io/k8s-device-plugin'], check=True)
        subprocess.run(['helm', 'repo', 'add', 'eks', 'https://aws.github.io/eks-charts/'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)

        # Clone the updated chart
        clone_cmd = [
            'git', 'clone',
            os.environ['GITHUB_REPO_URL'],
            '/tmp/helm-charts'
        ]
        subprocess.run(clone_cmd, check=True)

        # Update dependencies if any
        subprocess.run(['helm', 'dependency', 'update', f"/tmp/helm-charts/{os.environ['CHART_PATH']}"], check=True)

        # Upgrade the release
        upgrade_cmd = [
            'helm', 'upgrade',
            os.environ['RELEASE_NAME'],
            f"/tmp/helm-charts/{os.environ['CHART_PATH']}"
        ]
        subprocess.run(upgrade_cmd, check=True)

        # Clean up
        subprocess.run(['rm', '-rf', '/tmp/helm-charts'], check=True)
        
        print("Custom Helm chart updated successfully")
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to update Helm chart: {e.cmd}. Return code: {e.returncode}")


def on_update():
    """
    Handle Update request to upgrade existing Helm releases
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Helm charts updated successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION',
            'CREATE_FSX'
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

        # Update FSx CSI Driver if requested
        if os.environ['CREATE_FSX'].lower() == 'true':
            install_fsx_csi_driver()  # This function handles both install and upgrade
            response_data["FSxCSIDriverUpdated"] = True
        else:
            response_data["FSxCSIDriverUpdated"] = False

        # Update custom Helm chart if not FSx-only mode
        if os.environ['CREATE_FSX'].lower() != 'true':
            update_helm_chart()
            response_data["CustomChartUpdated"] = True
        else:
            response_data["CustomChartUpdated"] = False

        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to update Helm charts: {str(e)}")

def on_delete():
    """
    Handle Delete request to uninstall Helm releases
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Helm charts uninstalled successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION',
            'CREATE_FSX'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Uninstall custom Helm chart if it was installed
        if os.environ['CREATE_FSX'].lower() != 'true' and 'RELEASE_NAME' in os.environ:
            try:
                print(f"Uninstalling custom Helm chart: {os.environ['RELEASE_NAME']}")
                uninstall_cmd = [
                    'helm', 'uninstall',
                    os.environ['RELEASE_NAME']
                ]
                subprocess.run(uninstall_cmd, check=True)
                print("Custom Helm chart uninstalled successfully")
                response_data["CustomChartUninstalled"] = True
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to uninstall custom chart: {e}")
                response_data["CustomChartUninstalled"] = False

        # Uninstall FSx CSI Driver if it was installed
        if os.environ['CREATE_FSX'].lower() == 'true':
            try:
                print("Uninstalling AWS FSx CSI Driver")
                subprocess.run(['helm', 'uninstall', 'aws-fsx-csi-driver', '--namespace', 'kube-system'], check=True)
                print("AWS FSx CSI Driver uninstalled successfully")
                response_data["FSxCSIDriverUninstalled"] = True
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to uninstall FSx CSI driver: {e}")
                response_data["FSxCSIDriverUninstalled"] = False

        return response_data

    except Exception as e:
        print(f"Error during deletion: {str(e)}")
        # Return SUCCESS anyway to allow stack deletion to proceed
        return {
            "Status": "SUCCESS",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
