import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import json

# Environment variables
CLUSTER_NAME = 'CLUSTER_NAME'
REGION = 'REGION'
ACCOUNT_ID = 'ACCOUNT_ID'
HYPERPOD_CLUSTER_ARN = 'HYPERPOD_CLUSTER_ARN'
EKS_CLUSTER_ARN = 'EKS_CLUSTER_ARN'

# Base names for Kubernetes groups (will be numbered per mapping)
KUBERNETES_GROUP_BASE_NAMES = [
    'hyperpod-data-scientist-namespace-level',
    'hyperpod-data-scientist-cluster-level'
]

IAM_POLICY_BASE_NAME = "HyperPodDataScientistUI"

# Policy name function for data scientist access (cluster-specific)
def get_policy_name(cluster_name: str) -> str:
    """
    Generate cluster-specific policy name.
    """
    return f'{IAM_POLICY_BASE_NAME}-{cluster_name}'


def get_kubernetes_groups_for_setup(setup_index):
    """
    Generate unique Kubernetes group names for a specific 
    data scientist setup
    """
    return [
        f'{base_name}-{setup_index}' 
        for base_name in KUBERNETES_GROUP_BASE_NAMES
    ]


def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for data scientist setup
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

        # Always return success to prevent rollback on failures.
        # response_data will contain more detailed information.
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            response_data
        )

    except Exception as e:
        print(f"Error in lambda_handler: {str(e)}")
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            {
                "Status": "FAILED",
                "Reason": f"Lambda completed with errors: {str(e)}",
                "DataScientistSetupComplete": False
            }
        )


def parse_role_namespace_mappings():
    """
    Parse multiple role-namespace mappings from individual environment variables
    Returns list of mapping dictionaries
    """
    mappings = []
    
    # Check each of the 10 possible role parameters
    for i in range(1, 11):
        role_env_var = f'DATA_SCIENTIST_ROLE_{i}'
        namespaces_env_var = f'DATA_SCIENTIST_ROLE_{i}_NAMESPACES'
        
        role_name = os.environ.get(role_env_var, '').strip()
        namespaces = os.environ.get(namespaces_env_var, '').strip()
        
        if role_name:  # Only process if role name is provided
            mapping = {
                'roleName': role_name,
                'namespaces': namespaces if namespaces else 'default'
            }
            mappings.append(mapping)
            print(f"Parsed mapping {len(mappings)}: {mapping}")
    
    # Check for domain user profile role (name-based, same as DS roles)
    domain_role_name = os.environ.get('DOMAIN_USER_PROFILE_ROLE', '').strip()
    domain_role_namespaces = os.environ.get('DOMAIN_USER_PROFILE_ROLE_NAMESPACES', '').strip()
    
    if domain_role_name:
        mapping = {
            'roleName': domain_role_name,
            'namespaces': domain_role_namespaces if domain_role_namespaces else 'default'
        }
        mappings.append(mapping)
        print(f"Parsed domain user profile mapping {len(mappings)}: {mapping}")

    print(f"Found {len(mappings)} valid role-namespace mappings")
    return mappings


def resolve_role_from_mapping(mapping):
    """
    Resolve the data scientist role ARN from a mapping configuration
    Returns tuple: (role_name, role_arn)
    """
    role_name = mapping.get('roleName', '').strip()
    
    if role_name:
        print(f"Using provided role name: {role_name}")
        try:
            iam = boto3.client('iam')
            role_response = iam.get_role(RoleName=role_name)
            role_arn = role_response['Role']['Arn']
            print(f"Resolved role ARN: {role_arn}")
            return role_name, role_arn
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchEntity':
                raise Exception(f"IAM role '{role_name}' not found")
            raise
    
    raise Exception("roleName not provided in mapping")


def attach_hyperpod_policy(
    role_name: str,
    cluster_name: str,
    eks_cluster_arn: str,
    hyperpod_cluster_arn: str
):
    """
    Attach the HyperPod UI access policy to the data scientist role
    """
    print(f"Attaching HyperPod UI access policy to role: {role_name}")

    # Get cluster-specific policy name
    policy_name = get_policy_name(cluster_name)

    # Create the policy document based on AWS documentation
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DescribeHyperpodClusterPermissions",
                "Effect": "Allow",
                "Action": ["sagemaker:DescribeCluster"],
                "Resource": hyperpod_cluster_arn
            },
            {
                "Sid": "AllowK8SMutateViaConsole",
                "Effect": "Allow",
                "Action": [
                    "eks:AccessKubernetesApi",
                    "eks:MutateViaKubernetesApi"
                ],
                "Resource": eks_cluster_arn
            },
            {
                "Sid": "ListPermission",
                "Effect": "Allow",
                "Action": [
                    "sagemaker:ListClusters"
                ],
                "Resource": f"arn:aws:sagemaker:{os.environ[REGION]}:{os.environ[ACCOUNT_ID]}:cluster/*"
            },
            {
                "Sid": "SageMakerEndpointAccess",
                "Effect": "Allow",
                "Action": [
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:InvokeEndpoint",
                    "sagemaker:ListEndpoints"
                ],
                "Resource": f"arn:aws:sagemaker:{os.environ[REGION]}:{os.environ[ACCOUNT_ID]}:endpoint/*"
            }
        ]
    }
    
    try:
        iam = boto3.client('iam')
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document)
        )
        print(f"Successfully attached policy '{policy_name}' to role '{role_name}'")
        
    except ClientError as e:
        raise Exception(f"Failed to attach policy to role '{role_name}': {str(e)}")


def create_eks_access_entry(role_arn, cluster_name, kubernetes_groups):
    """
    Create EKS access entry for the data scientist role with specific groups
    """
    print(f"Creating EKS access entry for role: {role_arn} with groups: {kubernetes_groups}")
    
    try:
        eks = boto3.client('eks')
        
        # Check if access entry already exists
        try:
            eks.describe_access_entry(
                clusterName=cluster_name,
                principalArn=role_arn
            )
            print(f"Access entry already exists for role: {role_arn}")
            return
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
        
        # Create the access entry
        eks.create_access_entry(
            clusterName=cluster_name,
            principalArn=role_arn,
            kubernetesGroups=kubernetes_groups
        )
        print(f"Successfully created EKS access entry for role: {role_arn}")
        
    except ClientError as e:
        raise Exception(f"Failed to create EKS access entry for role '{role_arn}': {str(e)}")


def setup_kubeconfig(cluster_name, region):
    """
    Generate kubeconfig using boto3
    """
    # Initialise EKS client
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
                        'args': ['token', '-i', cluster_name]
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
        
        # Make sure kubectl can read the new context
        os.chmod(kubeconfig_path, 0o600)
        os.environ['KUBECONFIG'] = kubeconfig_path
        
        return True
        
    except ClientError as e:
        print(f"Error getting cluster info: {str(e)}")
        raise


def deploy_rbac_policies(namespaces, kubernetes_groups, group_index):
    """
    Deploy Kubernetes RBAC policies for data scientist access
    Supports single namespace (string) or multiple namespaces (list)
    """
    # convert comma-separated string list into python list
    namespace_list = [ns.strip(' ,') for ns in namespaces.split(',')]
    if len(namespace_list):
        print(f"Deploying RBAC policies for namespaces: {namespace_list} with groups: {kubernetes_groups}")
    
    # Extract group names
    namespace_group = kubernetes_groups[0]  # hyperpod-data-scientist-namespace-level-{index}
    cluster_group = kubernetes_groups[1]    # hyperpod-data-scientist-cluster-level-{index}
    
    # Cluster-level RBAC
    cluster_rbac_yaml = yield_cluster_rbac_yaml(cluster_group, group_index)

    try:
        # Create namespaces if they don't exist
        for namespace in namespace_list:
            try:
                subprocess.run(
                    ["kubectl", "create", "namespace", namespace],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                print(f"Created namespace: {namespace}")
            except subprocess.CalledProcessError as e:
                if "AlreadyExists" in e.stderr:
                    print(f"Namespace '{namespace}' already exists")
                else:
                    print(f"Warning: Failed to create namespace {namespace}: {e.stderr}")

        # Apply cluster-level RBAC
        with open('/tmp/cluster-rbac.yaml', 'w') as f:
            f.write(cluster_rbac_yaml)

        subprocess.run(
            ["kubectl", "apply", "-f", "/tmp/cluster-rbac.yaml"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        print("Applied cluster-level RBAC policies")

        # Apply namespace-level RBAC
        for namespace in namespace_list:
            namespace_rbac_yaml = yield_namespace_rbac_yaml(namespace, namespace_group, group_index)
            with open('/tmp/namespace-rbac.yaml', 'w') as f:
                f.write(namespace_rbac_yaml)

            subprocess.run(
                ["kubectl", "apply", "-f", "/tmp/namespace-rbac.yaml"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            print(f"Applied namespace-level RBAC policies for namespace: {namespace}")

    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to deploy RBAC policies: {e.stderr}")

def yield_cluster_rbac_yaml(cluster_group: str, group_index: int) -> str:
    return f"""
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: hyperpod-data-scientist-cluster-role-{group_index}
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["events"]
  verbs: ["get", "list"]
- apiGroups: ["apiextensions.k8s.io"]
  resources: ["customresourcedefinitions"]
  verbs: ["get"]
- apiGroups: ["authorization.k8s.io"]
  resources: ["selfsubjectaccessreviews"]
  verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: hyperpod-data-scientist-cluster-role-binding-{group_index}
subjects:
- kind: Group
  name: {cluster_group}
  apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: hyperpod-data-scientist-cluster-role-{group_index}
  apiGroup: rbac.authorization.k8s.io
"""

def yield_namespace_rbac_yaml(
    namespace: str,
    namespace_group: str,
    group_index: int,    
) -> str:
    return f"""
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: {namespace}
  name: hyperpod-data-scientist-namespace-role-{group_index}
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["create", "get"]
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["pods/log"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["get", "create"]
- apiGroups: ["kubeflow.org"]
  resources: ["pytorchjobs", "pytorchjobs/status"]
  verbs: ["get", "list", "create", "delete", "update", "describe"]
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["create", "update", "get", "list", "delete"]
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["create", "get", "list", "delete"]
- apiGroups: ["inference.sagemaker.aws.amazon.com"]
  resources: ["inferenceendpointconfigs", "jumpstartmodels"]
  verbs: ["get", "list", "create", "delete", "update", "describe"]
- apiGroups: ["inference.sagemaker.aws.amazon.com"]
  resources: ["sagemakerendpointregistrations"]
  verbs: ["get", "list", "describe"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  namespace: {namespace}
  name: hyperpod-data-scientist-namespace-role-binding-{group_index}
subjects:
- kind: Group
  name: {namespace_group}
  apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: hyperpod-data-scientist-namespace-role-{group_index}
  apiGroup: rbac.authorization.k8s.io
"""

def process_single_setup(mapping, index, cluster_name, hyperpod_cluster_arn, eks_cluster_arn):
    """
    Process a single IAM role to Kubernetes namespace mapping
    Returns result dictionary with mapping details
    """
    try:
        role_name, role_arn = resolve_role_from_mapping(mapping)
        namespaces = mapping.get('namespaces', 'default')
        
        print(f"Processing mapping {index}: Role={role_name}, Namespaces={namespaces}")
        
        # Attach HyperPod UI access policy
        attach_hyperpod_policy(role_name, cluster_name, eks_cluster_arn, hyperpod_cluster_arn)
        
        # Get unique Kubernetes groups for this setup
        kubernetes_groups = get_kubernetes_groups_for_setup(index)
        
        # Create EKS access entry
        create_eks_access_entry(role_arn, cluster_name, kubernetes_groups)
        
        # Deploy RBAC policies for this role's namespaces
        deploy_rbac_policies(namespaces, kubernetes_groups, index)
        
        return {
            "SetupIndex": index,
            "RoleName": role_name,
            "RoleArn": role_arn,
            "Namespaces": namespaces,
            "Status": "SUCCESS"
        }
        
    except Exception as e:
        print(f"Error processing mapping {index}: {str(e)}")
        return {
            "SetupIndex": index,
            "Status": "FAILED",
            "Error": str(e)
        }


def on_create():
    """
    Handle Create request for data scientist setup
    """
    response_data = {
        "Status": "SUCCESS",
        "Reason": "Data scientist setup completed successfully",
    }
    
    try:
        # Parse role-namespace mappings
        mappings = parse_role_namespace_mappings()
        
        # If no mappings specified, return success (no-op)
        if not mappings:
            response_data["Reason"] = "No data scientist mappings specified. Skipping setup."
            response_data["DataScientistSetupSkipped"] = True
            return response_data
        
        # Get required environment variables
        cluster_name = os.environ[CLUSTER_NAME]
        region = os.environ[REGION]
        hyperpod_cluster_arn = os.environ[HYPERPOD_CLUSTER_ARN]
        eks_cluster_arn = os.environ[EKS_CLUSTER_ARN]
        
        # Configure kubectl
        setup_kubeconfig(cluster_name, region)
        
        # Process each mapping
        successful_setups = 0
        for i, role_ns_mapping in enumerate(mappings):
            result = process_single_setup(
                role_ns_mapping, i+1, cluster_name, 
                hyperpod_cluster_arn, eks_cluster_arn
            )

            if result["Status"] == "SUCCESS":
                successful_setups += 1
        
        # Update response based on results
        if successful_setups == len(mappings):
            response_data["Reason"] = f"Successfully completed all {len(mappings)} data scientist mappings"
        elif successful_setups > 0:
            response_data["Status"] = "FAILED"
            response_data["Reason"] = f"Failed to complete {len(mappings) - successful_setups} of {len(mappings)} data scientist setups"
        else:
            response_data["Status"] = "FAILED"
            response_data["Reason"] = f"Failed to complete any of the {len(mappings)} data scientist setups"
        
        response_data["DataScientistSetupComplete"] = successful_setups == len(mappings)
        response_data["TotalSetups"] = len(mappings)
        response_data["SuccessfulSetups"] = successful_setups
        
        return response_data
        
    except Exception as e:
        response_data["Status"] = "FAILED"
        response_data["Reason"] = f"Failed to setup data scientist access: {str(e)}"
        response_data["DataScientistSetupComplete"] = False
        return response_data


def on_update():
    """
    Handle Update request to data scientist role
    """
    raise NotImplementedError


def cleanup_resources(role_name: str, cluster_name: str):
    """
    Clean up data scientist setup resources
    """
    print(f"Cleaning up data scientist setup for role: {role_name}")
    
    # Get cluster-specific policy name
    policy_name = get_policy_name(cluster_name)

    # Remove IAM policy
    try:
        iam = boto3.client('iam')
        iam.delete_role_policy(
            RoleName=role_name,
            PolicyName=policy_name
        )
        print(f"Removed policy '{policy_name}' from role '{role_name}'")
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchEntity':
            print(f"Policy '{policy_name}' not found on role '{role_name}'")
        else:
            print(f"Warning: Failed to remove policy from role '{role_name}': {str(e)}")
    
    # Remove EKS access entry
    try:
        eks = boto3.client('eks')
        
        # Get role ARN
        iam = boto3.client('iam')
        role_response = iam.get_role(RoleName=role_name)
        role_arn = role_response['Role']['Arn']
        
        eks.delete_access_entry(
            clusterName=cluster_name,
            principalArn=role_arn
        )
        print(f"Removed EKS access entry for role: {role_arn}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(f"EKS access entry not found for role: {role_name}")
        else:
            print(f"Warning: Failed to remove EKS access entry for role '{role_name}': {str(e)}")


def on_delete():
    """
    Handle Delete request to clean up data scientist setup
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Data scientist setup cleanup completed successfully"
        }
        
        # Parse role-namespace mappings for cleanup
        mappings = parse_role_namespace_mappings()
        
        # If no mappings specified, return success (no-op)
        if not mappings:
            response_data["Reason"] = "No data scientist mappings specified. Nothing to clean up."
            return response_data
        
        cluster_name = os.environ[CLUSTER_NAME]
        
        # Clean up resources for each mapping
        successful_cleanups = 0
        for i, role_ns_mapping in enumerate(mappings):
            try:
                role_name, role_arn = resolve_role_from_mapping(role_ns_mapping)
                cleanup_resources(role_name, cluster_name)
                successful_cleanups += 1
                print(f"Successfully cleaned up mapping {i+1}: {role_name}")
            except Exception as e:
                print(f"Warning: Failed to clean up mapping {i+1}: {str(e)}")
        
        response_data["TotalCleanups"] = len(mappings)
        response_data["SuccessfulCleanups"] = successful_cleanups
        response_data["DataScientistCleanupComplete"] = successful_cleanups == len(mappings)
        
        if successful_cleanups == len(mappings):
            response_data["Reason"] = f"Successfully cleaned up all {len(mappings)} data scientist setups"
        elif successful_cleanups > 0:
            response_data["Status"] = "FAILED"
            response_data["Reason"] = f"Failed to clean up {len(mappings) - successful_cleanups} of {len(mappings)} data scientist setups"
        else:
            response_data["Status"] = "FAILED"
            response_data["Reason"] = f"Failed to clean up any of the {len(mappings)} data scientist setups"
        
        return response_data
        
    except Exception as e:
        # For delete operations, we generally want to succeed even if cleanup fails
        print(f"Warning: Error during data scientist setup cleanup: {str(e)}")
        return {
            "Status": "SUCCESS",
            "Reason": f"Data scientist setup cleanup completed with warnings: {str(e)}"
        }
