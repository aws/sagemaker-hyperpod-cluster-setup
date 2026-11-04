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
    
def install_rig_dependencies():
    """
    Install RIG specific Helm chart from GitHub repository
    """
    try:
        print("Installing RIG specific Helm chart...")

        required_env_vars = [
            'RIG_SCRIPT_PATH',
        ]

        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        script_dir = os.path.dirname(os.environ['RIG_SCRIPT_PATH'])
        script_filename = os.path.basename(os.environ['RIG_SCRIPT_PATH'])
        # Install the Helm chart RIG dependencies
        os.chdir(f'/tmp/helm-charts/{script_dir}')
        # Make script executable
        os.chmod(script_filename, 0o700)
        # Run script and pass y to confirm install rig dependencies
        subprocess.run([f'./{script_filename}'], input='y\n', text=True, check=True)

        print("RIG specific Helm chart installed successfully")
    
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to install RIG specific Helm chart: {e.cmd}. Return code: {e.returncode}")


MODELEXPRESS_ENABLED = 'modelexpress.enabled=true'

# Client profile ConfigMaps for ModelExpress inference pods (envFrom). Values come from
# the HyperPod POC; the gdr profile is for instances with GPUDirect RDMA (p5, p5e, p5en,
# p6, g7), non-gdr for instances without it (g5, g6, g6e), where P2P weights are slower
# than S3 and only the compiled-kernel cache is transferred.
MODELEXPRESS_PROFILE_COMMON = {
    'VLLM_PLUGINS': 'modelexpress',
    'VLLM_ENGINE_READY_TIMEOUT_S': '3600',
    'VLLM_RPC_TIMEOUT': '7200000',
    'MX_P2P_METADATA': '1',
    'MX_METADATA_PORT': '5555',
    'MX_WORKER_GRPC_PORT': '6555',
    'MX_ARTIFACT_TRANSFER': '1',
    'MX_MS_DISTRIBUTED': '1',
    'MX_INSTANT_TENSOR': '0',
    'RUNAI_STREAMER_CONCURRENCY': '8',
    'RUNAI_STREAMER_S3_REQUEST_TIMEOUT_MS': '10000',
    'MX_NIXL_BACKEND': 'LIBFABRIC',
    'MX_RDMA_NIC_PIN': 'auto',
    'FI_PROVIDER': 'efa',
    'FI_EFA_USE_DEVICE_RDMA': '1',
    'FI_EFA_ENABLE_SHM_TRANSFER': '0',
    'FI_LOG_LEVEL': 'warn',
    'NIXL_LOG_LEVEL': 'WARN',
    'MODEL_EXPRESS_LOG_LEVEL': 'INFO',
}
MODELEXPRESS_PROFILES = {
    'gdr': {'MX_LOAD_STRATEGY_CHAIN': 'INFERENCE'},
    'non-gdr': {'MX_LOAD_STRATEGY_CHAIN': 'RL'},
}


def modelexpress_enabled():
    """
    Return True if OPERATORS enables ModelExpress (installed from its own chart).
    """
    return MODELEXPRESS_ENABLED in os.environ.get('OPERATORS', '').split(',')


def modelexpress_profiles_manifest(namespace):
    """
    Render the client profile ConfigMaps for one namespace.
    """
    server_address = (
        f"{os.environ['MODELEXPRESS_RELEASE']}.{os.environ['MODELEXPRESS_NAMESPACE']}"
        f".svc.cluster.local:8001"
    )
    configmaps = []
    for profile, settings in MODELEXPRESS_PROFILES.items():
        configmaps.append({
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'metadata': {
                'name': f'modelexpress-{profile}',
                'namespace': namespace,
                'labels': {
                    'app.kubernetes.io/name': 'modelexpress',
                    'app.kubernetes.io/component': 'client-profile',
                    'app.kubernetes.io/managed-by': 'sagemaker-hyperpod-cluster-setup',
                },
            },
            'data': {'MX_SERVER_ADDRESS': server_address, **MODELEXPRESS_PROFILE_COMMON, **settings},
        })
    return yaml.dump_all(configmaps, default_flow_style=False)


def install_modelexpress():
    """
    Install or upgrade the ModelExpress server from the upstream ai-dynamo/modelexpress
    chart, plus the client profile ConfigMaps.
    """
    try:
        print("Installing ModelExpress...")

        required_env_vars = [
            'MODELEXPRESS_REPO_URL',
            'MODELEXPRESS_REPO_REVISION',
            'MODELEXPRESS_NAMESPACE',
            'MODELEXPRESS_RELEASE',
            'MODELEXPRESS_PROFILE_NAMESPACE',
        ]

        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        repo_dir = '/tmp/modelexpress'
        subprocess.run(['rm', '-rf', repo_dir], check=True)
        subprocess.run(['git', 'clone', os.environ['MODELEXPRESS_REPO_URL'], repo_dir], check=True)
        subprocess.run(['git', '-C', repo_dir, 'checkout', os.environ['MODELEXPRESS_REPO_REVISION']], check=True)

        # Kubernetes metadata backend needs no Redis; the server keeps no state on disk,
        # so the chart's default PVC is not needed.
        subprocess.run([
            'helm', 'upgrade', '--install',
            os.environ['MODELEXPRESS_RELEASE'],
            f'{repo_dir}/helm',
            '--namespace', os.environ['MODELEXPRESS_NAMESPACE'],
            '--create-namespace',
            '--set', 'env.MX_METADATA_BACKEND=kubernetes',
            '--set', 'serviceAccount.rbac.enabled=true',
            '--set', 'persistence.enabled=false',
        ], check=True)

        subprocess.run(
            ['kubectl', 'apply', '-f', '-'],
            input=modelexpress_profiles_manifest(os.environ['MODELEXPRESS_PROFILE_NAMESPACE']),
            text=True,
            check=True,
        )

        subprocess.run(['rm', '-rf', repo_dir], check=True)

        print("ModelExpress installed successfully")

    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to install ModelExpress: {e.cmd}. Return code: {e.returncode}")


def modelexpress_installed():
    """
    Return True if the ModelExpress release exists.
    """
    result = subprocess.run(
        ['helm', 'status', os.environ['MODELEXPRESS_RELEASE'], '--namespace', os.environ['MODELEXPRESS_NAMESPACE']],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def uninstall_modelexpress():
    """
    Remove the ModelExpress release and client profiles. The CRDs are left in place,
    as Helm does on uninstall.
    """
    print("Uninstalling ModelExpress...")
    subprocess.run(
        ['helm', 'uninstall', os.environ['MODELEXPRESS_RELEASE'], '--namespace', os.environ['MODELEXPRESS_NAMESPACE']],
        check=True,
    )
    subprocess.run([
        'kubectl', 'delete', 'configmap',
        '--namespace', os.environ['MODELEXPRESS_PROFILE_NAMESPACE'],
        '--selector', 'app.kubernetes.io/component=client-profile,app.kubernetes.io/name=modelexpress',
        '--ignore-not-found',
    ], check=True)
    print("ModelExpress uninstalled successfully")


def install_helm_chart():
    """
    Install custom Helm chart from GitHub repository
    """
    try:
        print("Installing custom Helm chart...")
        
        # Ensure required environment variables are set
        required_env_vars = [
            'GITHUB_REPO_URL',
            'GITHUB_REPO_REVISION',
            'CHART_PATH',
            'NAMESPACE',
            'RELEASE_NAME',
            'OPERATORS',
            'CREATE_RIG',
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        
        # Add required Helm repositories
        subprocess.run(['helm', 'repo', 'add', 'nvidia', 'https://nvidia.github.io/k8s-device-plugin'], check=True)
        subprocess.run(['helm', 'repo', 'add', 'eks', 'https://aws.github.io/eks-charts/'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)

        # Clone the GitHub repository
        clone_cmd = ['git', 'clone', os.environ['GITHUB_REPO_URL'], '/tmp/helm-charts']
        subprocess.run(clone_cmd, check=True)

        # Specify revision
        subprocess.run(['git', '-C', '/tmp/helm-charts', 'checkout', os.environ['GITHUB_REPO_REVISION']], check=True)

        # Update the gpu operator dependencies
        gpu_operator_path = f"/tmp/helm-charts/{os.environ['CHART_PATH']}/charts/gpu-operator"
        if os.path.exists(gpu_operator_path):
            subprocess.run(['helm', 'dependency', 'update', gpu_operator_path], check=True)
        else:
            print(f"Warning: Gpu operator folder {gpu_operator_path} not found. Proceeding without it.")
        
        

        # Update dependencies
        subprocess.run(['helm', 'dependency', 'update', f"/tmp/helm-charts/{os.environ['CHART_PATH']}"], check=True)

        

        # Process OPERATORS environment variable
        operators = os.environ['OPERATORS']
        
        # Check if gpu-operator.enabled=true is present
        if "gpu-operator.enabled=true" in operators:
            # Add nvidia-device-plugin.devicePlugin.enabled=false if not already present
            if "nvidia-device-plugin.devicePlugin.enabled=" not in operators:
                operators = operators + ",nvidia-device-plugin.devicePlugin.enabled=false"
            else:
                # Replace existing nvidia-device-plugin setting with enabled=false
                parts = operators.split(',')
                for i, part in enumerate(parts):
                    if part.startswith("nvidia-device-plugin.devicePlugin.enabled="):
                        parts[i] = "nvidia-device-plugin.devicePlugin.enabled=false"
                operators = ",".join(parts)
            
            print("Detected gpu-operator.enabled=true, setting nvidia-device-plugin.devicePlugin.enabled=false")

        # Define the regional values file path
        regional_values_file = f"/tmp/helm-charts/{os.environ['CHART_PATH']}/regional-values/values-{os.environ['AWS_REGION']}.yaml"
        
        # Install the Helm chart
        install_cmd = [
            'helm', 'install',
            os.environ['RELEASE_NAME'],
            f"/tmp/helm-charts/{os.environ['CHART_PATH']}",
            '--namespace', os.environ['NAMESPACE'],
            '--set', f'health-monitoring-agent.region={os.environ['AWS_REGION']}',
            '--set', operators
        ]
        
        # Check if the regional values file exists and include it in the command if it does
        if os.path.exists(regional_values_file):
            install_cmd.insert(4, '-f')
            install_cmd.insert(5, regional_values_file)
        else:
            print(f"Warning: Regional values file {regional_values_file} not found. Proceeding without it.")
        subprocess.run(install_cmd, check=True)

        if os.environ['CREATE_RIG'] == 'true':
            install_rig_dependencies()

        if modelexpress_enabled():
            install_modelexpress()

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

        # Install custom Helm chart
        install_helm_chart()
        response_data["CustomChartInstalled"] = True

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
            'GITHUB_REPO_REVISION',
            'CHART_PATH',
            'RELEASE_NAME',
            'CREATE_RIG',
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Add required Helm repositories
        subprocess.run(['helm', 'repo', 'add', 'nvidia', 'https://nvidia.github.io/k8s-device-plugin'], check=True)
        subprocess.run(['helm', 'repo', 'add', 'eks', 'https://aws.github.io/eks-charts/'], check=True)
        subprocess.run(['helm', 'repo', 'update'], check=True)

        # Clone the updated chart
        clone_cmd = ['git', 'clone', os.environ['GITHUB_REPO_URL'], '/tmp/helm-charts']
        subprocess.run(clone_cmd, check=True)

        # Specify revision
        subprocess.run(['git', '-C', '/tmp/helm-charts', 'checkout', os.environ['GITHUB_REPO_REVISION']], check=True)
        
        # Update the gpu operator dependencies
        gpu_operator_path = f"/tmp/helm-charts/{os.environ['CHART_PATH']}/charts/gpu-operator"
        if os.path.exists(gpu_operator_path):
            subprocess.run(['helm', 'dependency', 'update', gpu_operator_path], check=True)
        else:
            print(f"Warning: Gpu operator folder {gpu_operator_path} not found. Proceeding without it.")
        
        # Update dependencies if any
        subprocess.run(['helm', 'dependency', 'update', f"/tmp/helm-charts/{os.environ['CHART_PATH']}"], check=True)

        # Define the regional values file path
        regional_values_file = f"/tmp/helm-charts/{os.environ['CHART_PATH']}/regional-values/values-{os.environ['AWS_REGION']}.yaml"
        
        # Upgrade the release
        upgrade_cmd = [
            'helm', 'upgrade', '--install',
            os.environ['RELEASE_NAME'],
            f"/tmp/helm-charts/{os.environ['CHART_PATH']}",
            '--namespace', os.environ['NAMESPACE'],
        ]
        
        # Check if the regional values file exists and include it in the command if it does
        if os.path.exists(regional_values_file):
            upgrade_cmd.insert(4, '-f')
            upgrade_cmd.insert(5, regional_values_file)
        else:
            print(f"Warning: Regional values file {regional_values_file} not found. Proceeding without it.")
        subprocess.run(upgrade_cmd, check=True)

        if os.environ['CREATE_RIG'] == 'true':
            result = subprocess.run(
                ['helm','status','rig-dependencies', '--namespace', os.environ['NAMESPACE']], 
                check=True,
                capture_output=True,
                text=True
            )
            if 'rig-dependencies' in result.stdout:
                print("rig-dependencies is present, skip RIG installation")
            else:
                install_rig_dependencies()

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

        # Update custom Helm chart
        update_helm_chart()
        response_data["CustomChartUpdated"] = True

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
            'CREATE_RIG'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Uninstall custom Helm chart if it was installed
        if 'RELEASE_NAME' in os.environ:
            try:
                print(f"Uninstalling custom Helm chart: {os.environ['RELEASE_NAME']}")
                uninstall_cmd = [
                    'helm', 'uninstall',
                    os.environ['RELEASE_NAME'],
                    '--namespace', os.environ['NAMESPACE']
                ]
                subprocess.run(uninstall_cmd, check=True)
                if os.environ['CREATE_RIG'] == 'true':
                    subprocess.run(['helm', 'uninstall', 'rig-dependencies', '--namespace', os.environ['NAMESPACE']], check=True)
                print("Custom Helm chart uninstalled successfully")
                response_data["CustomChartUninstalled"] = True
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to uninstall custom chart: {e}")
                response_data["CustomChartUninstalled"] = False

        if 'MODELEXPRESS_RELEASE' in os.environ:
            try:
                if modelexpress_installed():
                    uninstall_modelexpress()
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to uninstall ModelExpress: {e}")


        return response_data

    except Exception as e:
        print(f"Error during deletion: {str(e)}")
        # Return SUCCESS anyway to allow stack deletion to proceed
        return {
            "Status": "SUCCESS",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
