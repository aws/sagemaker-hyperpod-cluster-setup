import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml

# env vars
CLUSTER_NAME = 'CLUSTER_NAME'
AWS_REGION = 'AWS_REGION'
ACCOUNT_ID = 'ACCOUNT_ID'

# env vars for SA creation
EKS_CLUSTER_NAME = "EKS_CLUSTER_NAME"
ALB_CONTROLLER_SA_NAME = "aws-load-balancer-controller"
ALB_CONTROLLER_IAM_POLICY_ARN = "ALB_CONTROLLER_IAM_POLICY_ARN"
S3_CSI_SA_NAME = "s3-csi-driver-sa"
S3_CSI_IAM_POLICY_ARN = "S3_CSI_IAM_POLICY_ARN"
S3_CSI_IAM_ROLE_NAME = "S3_CSI_IAM_ROLE_NAME"



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


def create_service_accounts(service_account_name, policy_arn, role_name=None):
    """
    Install custom Helm chart from GitHub repository
    """
    try:
        print(f"Creating IAM role and Kubernetes service accounts for {service_account_name}...")

        # Ensure required environment variables are set
        required_env_vars = [
            AWS_REGION,
            ACCOUNT_ID,
            EKS_CLUSTER_NAME,
            ALB_CONTROLLER_IAM_POLICY_ARN,
            S3_CSI_IAM_POLICY_ARN,
            S3_CSI_IAM_ROLE_NAME,
        ]

        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Create IAM role and SA
        creation_cmd = [
            'eksctl', 'create', 'iamserviceaccount',
            '--name', service_account_name,
            '--namespace', 'kube-system',
            '--override-existing-serviceaccounts',
            '--cluster', os.environ[EKS_CLUSTER_NAME],
            '--attach-policy-arn', policy_arn,
            '--approve',
            '--region', os.environ['AWS_REGION']
        ]

        if role_name:
            creation_cmd.extend(['--role-name', role_name])

        # Execute the SA creation
        subprocess.run(creation_cmd, check=True)

        # Verify proper annotation of the service account with the IAM role ARN
        try:
            result = subprocess.run(['kubectl', 'get', 'sa', service_account_name, '-n', 'kube-system', '-oyaml'],
                                    check=True, capture_output=True, text=True)
            print(f"Service account verification:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify service account: {e}")


    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to create service account: {e.cmd}. Return code: {e.returncode}")


def on_create():
    """
    Handle Create request to create service accounts
    """

    response_data = {
        "Status": "SUCCESS",
        "Reason": "Inference operator service accounts created successfully"
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



        # Configure kubectl using boto3
        write_kubeconfig(os.environ[CLUSTER_NAME], os.environ[AWS_REGION])

        # Create SA for ALB Controller
        create_service_accounts(ALB_CONTROLLER_SA_NAME, os.environ[ALB_CONTROLLER_IAM_POLICY_ARN])

        # Create SA for S3 CSI Driver
        create_service_accounts(S3_CSI_SA_NAME, os.environ[S3_CSI_IAM_POLICY_ARN], role_name=os.environ[S3_CSI_IAM_ROLE_NAME])

        # Label S3 CSI Driver SA
        label_cmd = [
            'kubectl', 'label', 'serviceaccount', 's3-csi-driver-sa',
            'app.kubernetes.io/component=csi-driver',
            'app.kubernetes.io/instance=aws-mountpoint-s3-csi-driver',
            'app.kubernetes.io/managed-by=EKS',
            'app.kubernetes.io/name=aws-mountpoint-s3-csi-driver',
            '-n', 'kube-system',
            '--overwrite'
        ]

        subprocess.run(label_cmd, check=True)

        response_data["CustomInferenceOperatorServiceAccountsCreated"] = True

        return response_data

    except subprocess.CalledProcessError as e:
        response_data["Reason"] = f"Command failed: {e.cmd}. Return code: {e.returncode}"
        response_data["CustomInferenceOperatorServiceAccountsCreated"] = False
        return response_data
    except Exception as e:
        response_data["Reason"] = f"Failed to create service accounts: {str(e)}"
        response_data["CustomInferenceOperatorServiceAccountsCreated"] = False
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
    Handle Delete request to clean up service accounts and IAM roles
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Inference operator service accounts deleted successfully"
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
            # Configure kubectl using boto3
            write_kubeconfig(os.environ[CLUSTER_NAME], os.environ[AWS_REGION])
        except Exception as e:
            print(f"Warning: Failed to configure kubectl, cluster may already be deleted: {str(e)}")
            return response_data

        # Delete service accounts and associated IAM roles
        service_accounts_to_delete = [
            {
                'name': ALB_CONTROLLER_SA_NAME,
                'namespace': 'kube-system'
            },
            {
                'name': S3_CSI_SA_NAME,
                'namespace': 'kube-system'
            }
        ]

        for sa in service_accounts_to_delete:
            try:
                # Delete the IAM service account (this also deletes the associated IAM role)
                delete_cmd = [
                    'eksctl', 'delete', 'iamserviceaccount',
                    '--name', sa['name'],
                    '--namespace', sa['namespace'],
                    '--cluster', os.environ.get(EKS_CLUSTER_NAME, os.environ['CLUSTER_NAME']),
                    '--region', os.environ['AWS_REGION'],
                    '--wait'
                ]

                print(f"Deleting service account: {sa['name']} in namespace: {sa['namespace']}")
                subprocess.run(delete_cmd, check=True, capture_output=True, text=True)
                print(f"Successfully deleted service account: {sa['name']}")

            except subprocess.CalledProcessError as e:
                # Log the error but don't fail the entire deletion process
                print(f"Warning: Failed to delete service account {sa['name']}: {e}")
                print(f"Command output: {e.stdout if e.stdout else 'No stdout'}")
                print(f"Command error: {e.stderr if e.stderr else 'No stderr'}")
                
                # Try to delete just the Kubernetes service account if eksctl fails
                try:
                    kubectl_delete_cmd = [
                        'kubectl', 'delete', 'serviceaccount', sa['name'],
                        '-n', sa['namespace'],
                        '--ignore-not-found=true'
                    ]
                    subprocess.run(kubectl_delete_cmd, check=True, capture_output=True, text=True)
                    print(f"Successfully deleted Kubernetes service account: {sa['name']}")
                except subprocess.CalledProcessError as kubectl_error:
                    print(f"Warning: Failed to delete Kubernetes service account {sa['name']}: {kubectl_error}")

        response_data["CustomInferenceOperatorServiceAccountsDeleted"] = True
        return response_data

    except Exception as e:
        # For delete operations, we generally want to succeed even if cleanup fails
        # to avoid blocking stack deletion
        print(f"Warning: Error during service account cleanup: {str(e)}")
        return {
            "Status": "SUCCESS",
            "Reason": f"Service account cleanup completed with warnings: {str(e)}"
        }
