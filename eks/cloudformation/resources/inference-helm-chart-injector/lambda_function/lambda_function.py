import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import json
import time

# env vars
HYPERPOD_CLI_GITHUB_REPO_URL = 'HYPERPOD_CLI_GITHUB_REPO_URL'
HYPERPOD_CLI_GITHUB_REPO_REVISION = 'HYPERPOD_CLI_GITHUB_REPO_REVISION'
CLUSTER_NAME = 'CLUSTER_NAME'
AWS_REGION = 'AWS_REGION'
ACCOUNT_ID = 'ACCOUNT_ID'
CHART_PATH = 'helm_chart/HyperPodHelmChart/charts/inference-operator'
CHART_LOCAL_PATH = '/tmp/inference-helm-charts'

# env vars for namespace creation
KEDA_NAMESPACE = "keda"
CERT_MANAGER_NAMESPACE = "cert-manager"

# env vars for helm install
NAMESPACE = 'NAMESPACE'
RELEASE_NAME = 'hyperpod-inference-operator'
EKS_CLUSTER_NAME = 'EKS_CLUSTER_NAME'
HP_CLUSTER_ARN = 'HP_CLUSTER_ARN'
HYPERPOD_INFERENCE_ROLE_ARN = 'HYPERPOD_INFERENCE_ROLE_ARN'
JUMPSTART_GATED_ROLE_ARN = 'JUMPSTART_GATED_ROLE_ARN'
S3_CSI_ROLE_NAME = 'S3_CSI_ROLE_NAME'
KEDA_ROLE_ARN = "KEDA_ROLE_ARN"
TLS_BUCKET_NAME = 'TLS_BUCKET_NAME'
VPC_ID = 'VPC_ID'


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


def install_helm_chart():
    """
    Install custom Helm chart from GitHub repository
    """
    try:
        print("Installing custom inference helm chart...")
        
        # Ensure required environment variables are set
        required_env_vars = [
            HYPERPOD_CLI_GITHUB_REPO_URL,
            HYPERPOD_CLI_GITHUB_REPO_REVISION,
            NAMESPACE,
            AWS_REGION,
            EKS_CLUSTER_NAME,
            HP_CLUSTER_ARN,
            HYPERPOD_INFERENCE_ROLE_ARN,
            S3_CSI_ROLE_NAME,
            KEDA_ROLE_ARN,
            TLS_BUCKET_NAME,
            VPC_ID,
            ACCOUNT_ID,
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        
        # Add required Helm repositories
        subprocess.run(['helm', 'repo', 'add', 'nvidia', 'https://nvidia.github.io/k8s-device-plugin'], check=True)
        subprocess.run(['helm', 'repo', 'add', 'eks', 'https://aws.github.io/eks-charts/'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)

        # Clone the GitHub repository
        clone_cmd = ['git', 'clone', os.environ[HYPERPOD_CLI_GITHUB_REPO_URL], CHART_LOCAL_PATH]
        subprocess.run(clone_cmd, check=True)

        # Specify revision
        subprocess.run(['git', '-C', CHART_LOCAL_PATH, 'checkout', os.environ[HYPERPOD_CLI_GITHUB_REPO_REVISION]], check=True)

        # Update dependencies
        subprocess.run(['helm', 'dependency', 'update', f"{CHART_LOCAL_PATH}/{CHART_PATH}"], check=True)

        # Install the Helm chart
        install_cmd = [
            'helm', 'install',
            RELEASE_NAME,
            f'{CHART_LOCAL_PATH}/{CHART_PATH}',
            '--namespace', os.environ[NAMESPACE],
            '--set', f"region={os.environ[AWS_REGION]}",
            '--set', f"eksClusterName={os.environ[EKS_CLUSTER_NAME]}",
            '--set', f"hyperpodClusterArn={os.environ[HP_CLUSTER_ARN]}",
            '--set', f"executionRoleArn={os.environ[HYPERPOD_INFERENCE_ROLE_ARN]}",
            '--set', f"s3.serviceAccountRoleArn=arn:aws:iam::{os.environ[ACCOUNT_ID]}:role/{os.environ[S3_CSI_ROLE_NAME]}",
            '--set', "s3.node.serviceAccount.create=false",
            '--set',
            f'keda.podIdentity.aws.irsa.roleArn={os.environ[KEDA_ROLE_ARN]}',
            '--set', f"tlsCertificateS3Bucket={os.environ[TLS_BUCKET_NAME]}",
            '--set', f"alb.region={os.environ[AWS_REGION]}",
            '--set', f"alb.clusterName={os.environ[EKS_CLUSTER_NAME]}",
            '--set', f"alb.vpcId={os.environ[VPC_ID]}",
            '--set', f"jumpstartGatedModelDownloadRoleArn={os.environ[JUMPSTART_GATED_ROLE_ARN]}",
            '--set', f"fsx.enabled=false",
        ]

        # Execute the Helm install
        subprocess.run(install_cmd, check=True)

        # Clean up cloned repository
        subprocess.run(['rm', '-rf', CHART_LOCAL_PATH], check=True)
        
        print("Custom inference helm chart installed successfully")
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to install inference helm chart: {e.cmd}. Return code: {e.returncode}")

def create_namespace(namespace):
    try:
        subprocess.run(
            ["kubectl", "create", "namespace", namespace],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        print(f"Namespace '{namespace}' created.")
    except subprocess.CalledProcessError as e:
        if "AlreadyExists" in e.stderr:
            print(f"Namespace '{namespace}' already exists. Skipping.")
        else:
            print(f"Failed to create namespace {namespace}: {str(e)}")
            return

def patch_alb_deployment():
    """
    Patch the ALB deployment to add tolerations for SageMaker node health statuses
    """
    try:
        print("Patching ALB deployment with SageMaker tolerations...")
        # Wait for ALB deployment to be created (with timeout)
        max_wait_time = 600  # 10 minutes acceptalbe with deep health check
        wait_interval = 30   # 30 seconds
        elapsed_time = 0
        
        print("Waiting for ALB deployment to be created...")
        while elapsed_time < max_wait_time:
            check_cmd = [
                'kubectl', 'get', 'deployment', 'hyperpod-inference-operator-alb',
                '-n', 'kube-system'
            ]
            
            result = subprocess.run(check_cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                print(f"ALB deployment found after {elapsed_time} seconds")
                break
            
            print(f"ALB deployment not found yet, waiting... ({elapsed_time}/{max_wait_time}s)")
            time.sleep(wait_interval)
            elapsed_time += wait_interval
        else:
            print(f"ALB deployment not found after {max_wait_time} seconds, skipping patch")
            return
        
        # Define the patch JSON for tolerations
        patch_json = {
            "spec": {
                "template": {
                    "spec": {
                        "tolerations": [
                            {
                                "key": "sagemaker.amazonaws.com/node-health-status",
                                "operator": "Equal",
                                "value": "Unschedulable",
                                "effect": "NoSchedule"
                            }
                        ]
                    }
                }
            }
        }
        
        # Convert patch to JSON string
        patch_str = json.dumps(patch_json)
        
        # Execute kubectl patch command
        patch_cmd = [
            'kubectl', 'patch', 'deployment', 'hyperpod-inference-operator-alb',
            '-n', 'kube-system',
            '-p', patch_str
        ]
        
        subprocess.run(patch_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Successfully patched ALB deployment with SageMaker tolerations")
        
    except subprocess.CalledProcessError as e:
        print(f"Warning: Failed to patch ALB deployment: {e}")
        # Don't raise exception - this is not critical for the main installation
    except Exception as e:
        print(f"Warning: Error during ALB deployment patching: {str(e)}")

def on_create():
    """
    Handle Create request to install Helm charts
    """

    response_data = {
        "Status": "SUCCESS",
        "Reason": "Inference helm charts installed successfully"
    }

    try:
        # Initialize response data

        # Ensure required environment variables are set
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                response_data["Reason"] = f"Missing required environment variable: {var}"
                return response_data

            
        # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
        os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
        os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
        
        # Create directories
        os.makedirs('/tmp/.helm/cache', exist_ok=True)
        os.makedirs('/tmp/.helm/config', exist_ok=True)

        # Configure kubectl using boto3
        write_kubeconfig(os.environ[CLUSTER_NAME], os.environ[AWS_REGION])

        # Create namespace for keda and cert manager
        create_namespace(KEDA_NAMESPACE)
        create_namespace(CERT_MANAGER_NAMESPACE)

        # Install custom Helm chart
        install_helm_chart()
        response_data["CustomInferenceChartInstalled"] = True

        return response_data

    except subprocess.CalledProcessError as e:
        response_data["CustomInferenceChartInstalled"] = False
        response_data["Reason"] = f"Command failed: {e.cmd}. Return code: {e.returncode}"
        # Try to patch ALB deployment even if subprocess command failed
        try:
            patch_alb_deployment()
            response_data["ALBPatched"] = True
        except Exception as patch_error:
            print(f"Warning: ALB patching also failed: {patch_error}")
            response_data["ALBPatched"] = False
        return response_data
    except Exception as e:
        response_data["CustomInferenceChartInstalled"] = False
        response_data["Reason"] = f"Failed to install Helm charts: {str(e)}"
        try:
            patch_alb_deployment()
            response_data["ALBPatched"] = True
        except Exception as patch_error:
            print(f"Warning: ALB patching also failed: {patch_error}")
            response_data["ALBPatched"] = False
        return response_data

def update_helm_chart():
    """
    Update custom Helm chart from GitHub repository
    """
    raise NotImplementedError


def on_update():
    """
    Handle Update request to upgrade existing Helm releases
    """
    raise NotImplementedError

def on_delete():
    """
    Handle Delete request to uninstall Helm releases and clean up resources
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Inference helm charts uninstalled successfully"
        }

        # Ensure required environment variables are set
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION'
        ]

        for var in required_env_vars:
            if var not in os.environ:
                print(f"Warning: Missing environment variable {var}, skipping cleanup")
                return response_data

        try:
            # Set HELM_CACHE_HOME and HELM_CONFIG_HOME
            os.environ['HELM_CACHE_HOME'] = '/tmp/.helm/cache'
            os.environ['HELM_CONFIG_HOME'] = '/tmp/.helm/config'
            
            # Create directories
            os.makedirs('/tmp/.helm/cache', exist_ok=True)
            os.makedirs('/tmp/.helm/config', exist_ok=True)

            # Configure kubectl using boto3
            write_kubeconfig(os.environ[CLUSTER_NAME], os.environ[AWS_REGION])
        except Exception as e:
            print(f"Warning: Failed to configure kubectl/helm, cluster may already be deleted: {str(e)}")
            return response_data

        # Uninstall the Helm release
        try:
            print(f"Uninstalling Helm release: {RELEASE_NAME}")
            
            # Check if the release exists first
            list_cmd = ['helm', 'list', '-n', os.environ.get(NAMESPACE, 'default'), '-q']
            try:
                result = subprocess.run(list_cmd, check=True, capture_output=True, text=True)
                releases = result.stdout.strip().split('\n') if result.stdout.strip() else []
                
                if RELEASE_NAME in releases:
                    # Uninstall the Helm release
                    uninstall_cmd = [
                        'helm', 'uninstall', RELEASE_NAME,
                        '--namespace', os.environ.get(NAMESPACE, 'default'),
                        '--wait'
                    ]
                    subprocess.run(uninstall_cmd, check=True, capture_output=True, text=True)
                    print(f"Successfully uninstalled Helm release: {RELEASE_NAME}")
                else:
                    print(f"Helm release {RELEASE_NAME} not found, skipping uninstall")
                    
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to uninstall Helm release {RELEASE_NAME}: {e}")
                print(f"Command output: {e.stdout if e.stdout else 'No stdout'}")
                print(f"Command error: {e.stderr if e.stderr else 'No stderr'}")

        except Exception as e:
            print(f"Warning: Error during Helm release cleanup: {str(e)}")

        # Clean up namespaces (optional - be careful with this)
        namespaces_to_check = [KEDA_NAMESPACE, CERT_MANAGER_NAMESPACE]
        
        for namespace in namespaces_to_check:
            try:
                # Check if namespace has any resources before deleting
                check_cmd = ['kubectl', 'get', 'all', '-n', namespace, '--no-headers']
                result = subprocess.run(check_cmd, capture_output=True, text=True)
                
                # Only delete if namespace is empty or only has default resources
                if result.returncode == 0 and not result.stdout.strip():
                    delete_ns_cmd = ['kubectl', 'delete', 'namespace', namespace, '--ignore-not-found=true']
                    subprocess.run(delete_ns_cmd, check=True, capture_output=True, text=True)
                    print(f"Successfully deleted empty namespace: {namespace}")
                else:
                    print(f"Namespace {namespace} contains resources, skipping deletion")
                    
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to clean up namespace {namespace}: {e}")

        # Clean up any temporary files
        try:
            if os.path.exists(CHART_LOCAL_PATH):
                subprocess.run(['rm', '-rf', CHART_LOCAL_PATH], check=True)
                print("Cleaned up temporary chart files")
        except Exception as e:
            print(f"Warning: Failed to clean up temporary files: {str(e)}")

        response_data["CustomInferenceChartUninstalled"] = True
        return response_data

    except Exception as e:
        # For delete operations, we generally want to succeed even if cleanup fails
        # to avoid blocking stack deletion
        print(f"Warning: Error during Helm chart cleanup: {str(e)}")
        return {
            "Status": "SUCCESS", 
            "Reason": f"Helm chart cleanup completed with warnings: {str(e)}"
        }
