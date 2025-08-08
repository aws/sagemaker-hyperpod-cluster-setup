import boto3
import botocore
import cfnresponse
import os
import json
import yaml

from botocore.exceptions import ClientError

class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True
        
def lambda_handler(event, context):
    try:
        print(f"Event received: {json.dumps(event)}")
        request_type = event['RequestType']

        if request_type == 'Create':
            response_data = on_create(event)
        elif request_type == 'Update':
            response_data = on_create(event)
        elif request_type == 'Delete':
            response_data = on_delete(event)
        else:
            raise ValueError(f"Unsupported request type: {request_type}")

        cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data)
    except Exception as e:
        print(f"Exception: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Reason': str(e)})

def combine_settings(settings_prefix="INSTANCE_GROUP_SETTINGS"):
    """
    Combine all settings with the given prefix into a single array
    
    Parameters:
    settings_prefix (str): The prefix for environment variables to look for (e.g., "INSTANCE_GROUP_SETTINGS", "RIG_SETTINGS")
    
    Returns:
    list: Combined settings from all environment variables with the specified prefix
    """
    combined_settings = []
    
    # Get number of instance groups from environment variable or default to 20
    num_groups = int(os.environ.get('NUMBER_OF_INSTANCE_GROUPS', 20))
    
    # Parse and merge each settings object
    for i in range(1, num_groups + 1):
        setting_key = f'{settings_prefix}{i}'
        if setting_key in os.environ and os.environ[setting_key] and os.environ[setting_key] != '[]':
            try:
                settings_json = json.loads(os.environ[setting_key])
                if isinstance(settings_json, list):
                    if settings_json:
                        # Process each item in settings_json
                        for item in settings_json:
                            # If the item is itself a list, extend with its contents
                            if isinstance(item, list):
                                combined_settings.extend(item)
                            else:
                                # Otherwise, just add the item directly
                                combined_settings.append(item)
                        print(f"Added settings from {setting_key}, current length: {len(combined_settings)}")
                else:
                    print(f"Warning: Expected list format for {setting_key}, but received {type(settings_json)}")
            except json.JSONDecodeError:
                print(f"Warning: Could not parse {setting_key} as JSON")
    
    return combined_settings

def enrich_instance_groups(instance_groups, isRig=False):
    """
    Enrich instance groups with additional configuration
    
    Parameters:
    instance_groups (list): List of instance group configurations to enrich
    isRig (bool): Whether this is a restricted instance group (default: False)
    """
    sagemaker_iam_role_name = os.environ.get('SAGEMAKER_IAM_ROLE_NAME')
    
    # Parse security group IDs for potential OverrideVpcConfig
    security_group_ids_str = os.environ.get('SECURITY_GROUP_IDS', '')
    security_group_ids = security_group_ids_str.split(',') if security_group_ids_str and ',' in security_group_ids_str else [security_group_ids_str] if security_group_ids_str else []
    
    # Parse subnet IDs
    private_subnet_ids_str = os.environ.get('PRIVATE_SUBNET_IDS', '')
    private_subnet_ids = private_subnet_ids_str.split(',') if private_subnet_ids_str and ',' in private_subnet_ids_str else [private_subnet_ids_str] if private_subnet_ids_str else []
    
    # Check if any instance group has TargetAvailabilityZoneId
    has_target_az = any('TargetAvailabilityZoneId' in group for group in instance_groups)
    
    # Get subnet to AZ mapping from AWS if needed and if we have subnets
    subnet_to_az_mapping = {}
    if has_target_az and private_subnet_ids:
        try:
            ec2 = boto3.client('ec2')
            response = ec2.describe_subnets(SubnetIds=private_subnet_ids)
            for subnet in response.get('Subnets', []):
                subnet_to_az_mapping[subnet['SubnetId']] = subnet['AvailabilityZoneId']
            print(f"Retrieved subnet to AZ mapping: {subnet_to_az_mapping}")
        except Exception as e:
            print(f"Warning: Could not retrieve subnet to AZ mapping: {str(e)}")
    
    for instance_group in instance_groups:
        # Only add parameters if they are provided and not already in the configuration
        if sagemaker_iam_role_name and 'ExecutionRole' not in instance_group:
            instance_group['ExecutionRole'] = sagemaker_iam_role_name
            
        # Add lifecycle script configuration if not a RIG and not already present
        if not isRig:
            s3_bucket_name = os.environ.get('S3_BUCKET_NAME')
            on_create_path = os.environ.get('ON_CREATE_PATH')
            if s3_bucket_name and on_create_path and 'LifeCycleConfig' not in instance_group:
                # Parse the on_create_path to separate path and filename
                path_parts = on_create_path.rsplit('/', 1)
                
                # If there's a path component, add it to the SourceS3Uri
                if len(path_parts) > 1:
                    path, filename = path_parts
                    instance_group['LifeCycleConfig'] = {
                        'SourceS3Uri': f's3://{s3_bucket_name}/{path}',
                        'OnCreate': f'{filename}'
                    }
                else:
                    # No path component, just a filename
                    filename = path_parts[0]
                    instance_group['LifeCycleConfig'] = {
                        'SourceS3Uri': f's3://{s3_bucket_name}',
                        'OnCreate': f'{filename}'
                    }
        # Check if OverrideVpcConfig already exists
        if 'OverrideVpcConfig' in instance_group:
            # Only update the Subnets part, keep existing SecurityGroupIds if present
            if 'SecurityGroupIds' not in instance_group['OverrideVpcConfig']:
                instance_group['OverrideVpcConfig']['SecurityGroupIds'] = security_group_ids
        
        # Check if instance group has TargetAvailabilityZoneId
        if 'TargetAvailabilityZoneId' in instance_group:
            # Check if both subnet_to_az_mapping and security_group_ids exist
            if not subnet_to_az_mapping or not security_group_ids:
                raise ValueError("When using TargetAvailabilityZoneId, both subnet mappings and security group IDs must be provided")
            
            target_az = instance_group['TargetAvailabilityZoneId']
            print(f"Instance group has TargetAvailabilityZoneId: {target_az}")
            
            # Find the first subnet in the target AZ
            target_subnet = None
            for subnet_id, az in subnet_to_az_mapping.items():
                if az == target_az:
                    target_subnet = subnet_id
                    break
            
            if target_subnet:
                print(f"Found subnet {target_subnet} in AZ {target_az}")
                
                # Check if OverrideVpcConfig already exists
                if 'OverrideVpcConfig' in instance_group:
                    # Only update the Subnets part, keep existing SecurityGroupIds if present
                    if 'SecurityGroupIds' not in instance_group['OverrideVpcConfig']:
                        instance_group['OverrideVpcConfig']['SecurityGroupIds'] = security_group_ids
                    
                    # Update the Subnets with the target subnet
                    instance_group['OverrideVpcConfig']['Subnets'] = [target_subnet]
                    print(f"Updated Subnets in existing OverrideVpcConfig: {instance_group['OverrideVpcConfig']}")
                else:
                    # Create new OverrideVpcConfig
                    instance_group['OverrideVpcConfig'] = {
                        'SecurityGroupIds': security_group_ids,
                        'Subnets': [target_subnet]
                    }
                    print(f"Created new OverrideVpcConfig: {instance_group['OverrideVpcConfig']}")
                
                # Remove the TargetAvailabilityZoneId key after processing to prevent it from being sent to the API
                del instance_group['TargetAvailabilityZoneId']
                print(f"Removed TargetAvailabilityZoneId from instance group after processing")
            else:
                print(f"Warning: No subnet found in AZ {target_az}")
                del instance_group['TargetAvailabilityZoneId']
                print(f"Removed TargetAvailabilityZoneId from instance group after processing")
    
    return instance_groups

def get_tags_from_env():
    """
    Get tags from environment variables

    - In JSON format: `CLUSTER_TAGS=[{"Key":"Environment","Value":"Production"}]`
    - Or as key-value pairs: `CLUSTER_TAGS=Environment=Production,Team=MLOps`
    
    Returns:
    list: List of tags dictionaries with Key and Value pairs
    """
    tags = []
    tags_str = os.environ.get('CLUSTER_TAGS', '')
    
    if tags_str:
        try:
            # Try parsing as JSON
            tag_list = json.loads(tags_str)
            if isinstance(tag_list, list):
                tags = tag_list
            elif isinstance(tag_list, dict):
                # Convert dict format to list of Key/Value dictionaries
                tags = [{'Key': k, 'Value': v} for k, v in tag_list.items()]
        except json.JSONDecodeError:
            # If not valid JSON, try parsing as comma-separated key=value pairs
            try:
                tag_pairs = tags_str.split(',')
                for pair in tag_pairs:
                    if '=' in pair:
                        key, value = pair.split('=', 1)
                        tags.append({'Key': key.strip(), 'Value': value.strip()})
            except Exception as e:
                print(f"Error parsing tags string: {str(e)}")
    
    return tags

def create_hyperpod_cluster(instance_groups):
    """
    Create a SageMaker HyperPod cluster
    """    
    # Get cluster parameters from environment variables
    cluster_name = os.environ.get('HYPER_POD_CLUSTER_NAME')
    if not cluster_name:
        raise ValueError("HYPER_POD_CLUSTER_NAME environment variable is required")
    
    node_recovery = os.environ.get('NODE_RECOVERY')
    if not node_recovery:
        raise ValueError("NODE_RECOVERY environment variable is required")
    if node_recovery not in ['Automatic', 'None']:
        raise ValueError("NODE_RECOVERY must be either 'Automatic' or 'None'")
        
    # Check if we're using SLURM orchestrator
    orchestrator_type = __get_orchestrator_type()
    if orchestrator_type == 'SLURM':
        # For SLURM orchestrator, upload provisioning parameters JSON first
        upload_slurm_provisioning_parameters_json(instance_groups)
        
        # Remove InstanceGroupType from instance groups for SLURM
        if instance_groups:
            for instance_group in instance_groups:
                if 'InstanceGroupType' in instance_group:
                    del instance_group['InstanceGroupType']
    

    # Parse security group IDs more robustly
    security_group_ids_str = os.environ.get('SECURITY_GROUP_IDS', '')
    security_group_ids = security_group_ids_str.split(',') if security_group_ids_str and ',' in security_group_ids_str else [security_group_ids_str] if security_group_ids_str else []
    
    # Parse subnet IDs more robustly 
    private_subnet_ids_str = os.environ.get('PRIVATE_SUBNET_IDS', '')
    private_subnet_ids = private_subnet_ids_str.split(',') if private_subnet_ids_str and ',' in private_subnet_ids_str else [private_subnet_ids_str] if private_subnet_ids_str else []
    
    # Validate VPC configuration
    if not security_group_ids and orchestrator_type == 'EKS':
        raise ValueError("At least one security group ID is required")
    if not private_subnet_ids and orchestrator_type == 'EKS':
        raise ValueError("At least one subnet ID is required")
    
    # Create cluster using SageMaker API
    print(f"Creating HyperPod cluster: {cluster_name}")
    create_params = {
        'ClusterName': cluster_name,
        'NodeRecovery': node_recovery
    }
    
    # Only include VpcConfig if we have subnets
    if private_subnet_ids:
        vpc_config = {
            'SecurityGroupIds': security_group_ids,
            'Subnets': private_subnet_ids
        }
        create_params['VpcConfig'] = vpc_config
    
    # Only add orchestrator for EKS type (not for SLURM)
    if orchestrator_type != 'SLURM':
        eks_cluster_arn = os.environ.get('EKS_CLUSTER_ARN')
        if not eks_cluster_arn:
            raise ValueError("EKS_CLUSTER_ARN environment variable is required")
        orchestrator = {
            'Eks': {
                'ClusterArn': eks_cluster_arn
            }
        }
        create_params['Orchestrator'] = orchestrator
    
    # Only add instance groups if they exist
    if instance_groups:
        create_params['InstanceGroups'] = instance_groups
    
    # Get tags if available
    tags = get_tags_from_env()
    if tags:
        create_params['Tags'] = tags
        print(f"Adding tags to cluster: {tags}")
    
    # Get restricted instance groups if available
    rig_groups = combine_settings("RIG_SETTINGS")
    if rig_groups:
        rig_groups = enrich_instance_groups(rig_groups, isRig=True)  # Only add execution role
        create_params['RestrictedInstanceGroups'] = rig_groups

    if orchestrator_type != 'SLURM':
        node_provisioning_mode = os.environ.get('NODE_PROVISIONING_MODE')
        if node_provisioning_mode and node_provisioning_mode == 'Continuous':
            create_params['NodeProvisioningMode'] = node_provisioning_mode;
        
    print(f"Creating yaml with parameters: {create_params}")
    yaml_str = generate_cluster_template_yaml(create_params)
    template_url = upload_cluster_template_to_s3(yaml_str)        
         
    return {
        'ClusterName': cluster_name,
        'TemplateUrl': template_url,    
    }

def delete_hyperpod_cluster():
    """
    Delete a SageMaker HyperPod cluster and wait until it's fully deleted
    """
    sagemaker = boto3.client('sagemaker')
    cluster_name = os.environ.get('HYPER_POD_CLUSTER_NAME')
    
    try:
        # Check if the cluster exists
        describe_response = sagemaker.describe_cluster(ClusterName=cluster_name)
        cluster_status = describe_response.get('ClusterStatus', '')
        print(f"Current cluster status: {cluster_status}")
        
        # Delete the cluster
        print(f"Deleting HyperPod cluster: {cluster_name}")
        response = sagemaker.delete_cluster(ClusterName=cluster_name)
        print(f"Delete cluster response: {response}")
        
        # Poll until cluster is deleted
        print(f"Starting to poll for cluster deletion completion...")
        wait_time_seconds = 15  # Initial wait time between polls
        
        while True:
            try:
                describe_response = sagemaker.describe_cluster(ClusterName=cluster_name)
                cluster_status = describe_response.get('ClusterStatus', '')
                print(f"Cluster still exists. Current status: {cluster_status}")
                print(f"Waiting {wait_time_seconds} seconds before checking again...")
                import time
                time.sleep(wait_time_seconds)
            
            except ClientError as e:
                if e.response['Error']['Code'] == 'ResourceNotFound':
                    # Resource no longer exists - deletion is complete
                    print(f"Cluster {cluster_name} has been successfully deleted")
                    return {
                        'Message': f'Cluster {cluster_name} successfully deleted'
                    }
                else:
                    # For other client errors, log and re-raise
                    print(f"Client error while polling: {str(e)}")
                    raise
            except Exception as e:
                print(f"Error while polling for deletion: {str(e)}")
                raise
    
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFound':
            # Resource already doesn't exist
            print(f"Cluster {cluster_name} not found, nothing to delete")
            return {
                'Message': f'Cluster {cluster_name} not found, nothing to delete'
            }
        else:
            # For other client errors, log and re-raise
            print(f"Client error during deletion: {str(e)}")
            raise
    except Exception as e:
        print(f"Error deleting cluster: {str(e)}")
        raise


def on_delete(event):
    """
    Handle Delete request to clean up files created during cluster creation
    """
    try:
        s3 = boto3.client('s3')
        bucket = os.environ.get('S3_BUCKET_NAME')
        if not bucket:
            print("S3_BUCKET_NAME environment variable not found")
            return {'Message': 'S3 bucket name not provided, nothing to delete'}
            
        deleted_files = []
        
        # Delete the cluster template yaml file
        template_key = 'hyperpod-cluster-template.yaml'
        try:
            s3.delete_object(Bucket=bucket, Key=template_key)
            deleted_files.append(f"s3://{bucket}/{template_key}")
            print(f"Successfully deleted s3://{bucket}/{template_key}")
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                print(f"File s3://{bucket}/{template_key} not found, nothing to delete")
            else:
                print(f"Error checking/deleting s3://{bucket}/{template_key}: {str(e)}")
        
        # Delete provisioning parameters JSON file if it exists
        # For SLURM orchestrator, this file is uploaded during cluster creation
        orchestrator_type = __get_orchestrator_type()
        if orchestrator_type == 'SLURM':
            # Determine the path for the provisioning_parameters.json file
            on_create_path = os.environ.get('ON_CREATE_PATH', '')
            path_prefix = ""
            if on_create_path:
                path_parts = on_create_path.rsplit('/', 1)
                if len(path_parts) > 1:
                    path_prefix = path_parts[0]
            
            # Set the file key with path prefix if available
            if path_prefix:
                params_key = f"{path_prefix}/provisioning_parameters.json"
            else:
                params_key = "provisioning_parameters.json"
                
            try:
                s3.delete_object(Bucket=bucket, Key=params_key)
                deleted_files.append(f"s3://{bucket}/{params_key}")
                print(f"Successfully deleted s3://{bucket}/{params_key}")
            except ClientError as e:
                if e.response['Error']['Code'] == '404':
                    print(f"File s3://{bucket}/{params_key} not found, nothing to delete")
                else:
                    print(f"Error checking/deleting s3://{bucket}/{params_key}: {str(e)}")
        
        if deleted_files:
            return {'Message': f'Successfully deleted files: {", ".join(deleted_files)}'}
        else:
            return {'Message': 'No files found to delete'}
            
    except Exception as e:
        print(f"Error in on_delete: {str(e)}")
        # Don't raise the exception, just return a message about it
        # This ensures CloudFormation deletion continues even if file cleanup fails
        return {'Message': f'Error deleting files: {str(e)}'}

def on_create(event):
    """
    Handle Create request to create a new HyperPod cluster
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "HyperPod cluster creation initiated successfully"
        }
        
        # Combine instance group settings
        instance_groups = combine_settings("INSTANCE_GROUP_SETTINGS")
        
        # Enrich instance groups with additional configuration
        if instance_groups:
            instance_groups = enrich_instance_groups(instance_groups, isRig=False)
        
        # Create the HyperPod cluster
        cluster_info = create_hyperpod_cluster(instance_groups)
        
        # Update response data with cluster information
        response_data.update(cluster_info)
        
        # Add status information
        response_data.update({
            'ClusterStatus': 'Creating',
            'Message': f'Cluster {cluster_info["ClusterName"]} creation initiated'
        })
        
        return response_data
        
    except Exception as e:
        print(f"Failed to create HyperPod cluster: {str(e)}")
        raise

def __get_orchestrator_type():
    """
    Get the orchestrator type from environment variables
    
    Returns:
    str: Orchestrator type ('EKS' or 'SLURM')
    """
    return os.environ.get('ORCHESTRATOR_TYPE', 'EKS')

def __get_provisioning_parameters_file(instance_groups):
    config_data = {
        "version": "1.0.0",
        "workload_manager": "slurm",
        "login_group": "my-login-group",
        "worker_groups": []
    }
    login_group = []
    compute_group = []
    controller_group = []
    compute_group_to_type = {}
    
    for instance_group in instance_groups:
        if "Login" == instance_group.get("InstanceGroupType"):
            login_group.append(instance_group["InstanceGroupName"])
        elif "Compute" == instance_group.get("InstanceGroupType"):
            compute_group.append(instance_group["InstanceGroupName"])
            compute_group_to_type[instance_group["InstanceGroupName"]] = instance_group["InstanceType"]
        elif "Controller" == instance_group.get("InstanceGroupType"):
            controller_group.append(instance_group["InstanceGroupName"])
        else:
            raise Exception(f"invalid type for instance group {instance_group['InstanceGroupName']}")

    if len(login_group) > 1 or len(controller_group) != 1:
        raise Exception(f"wrong number of instance group for login and controller type with config {instance_groups}")
    config_data = {
        "version": "1.0.0",
        "workload_manager": "slurm",
        "controller_group": controller_group[0],
        "worker_groups": [{"instance_group_name": groupName, "partition_name": compute_group_to_type[groupName]} for groupName in compute_group]
    }
    if login_group:
        config_data["login_group"] = login_group[0]
    enabled_fsx = os.environ.get('ENABLED_FSX', 'false').lower() == 'true'
    if enabled_fsx:
        config_data["fsx_dns_name"] = os.environ.get('FSX_DNS_NAME', '')
        config_data["fsx_mountname"] = os.environ.get('FSX_MOUNT_NAME', '')
        
        # Check if FSX values are provided
        if not config_data["fsx_dns_name"] or not config_data["fsx_mountname"]:
            print("Warning: FSX is enabled but FSX_DNS_NAME or FSX_MOUNT_NAME is missing")

    return json.dumps(config_data, indent=2)


def upload_slurm_provisioning_parameters_json(instance_groups):
    """
    Upload the generated provisioning_parameters json file, this file needs to reference the instanceGroup name.
    """
    if "SLURM" != __get_orchestrator_type():
        return
    s3 = boto3.client('s3')
    s3_bucket_name = os.environ.get("S3_BUCKET_NAME", "")
    on_create_path = os.environ.get('ON_CREATE_PATH', '')
    
    # Get path prefix from ON_CREATE_PATH if available
    path_prefix = ""
    if on_create_path:
        path_parts = on_create_path.rsplit('/', 1)
        if len(path_parts) > 1:
            path_prefix = path_parts[0]
    
    # Set the file key with path prefix if available
    if path_prefix:
        s3_file_key = f"{path_prefix}/provisioning_parameters.json"
    else:
        s3_file_key = "provisioning_parameters.json"
    
    s3.put_object(
        Bucket=s3_bucket_name,
        Key=s3_file_key,
        Body=__get_provisioning_parameters_file(instance_groups)
    )
    print(f"Uploaded provisioning_parameters.json to s3://{s3_bucket_name}/{s3_file_key}")

def generate_cluster_template_yaml(create_params):
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {
            "NewHyperPodCluster": {
                "Type": "AWS::SageMaker::Cluster",
                "Properties": create_params
            }
        },
        "Outputs": {
            "HyperPodClusterArn": {
                "Description": "The ARN of the created SageMaker HyperPod cluster",
                "Value": {"Fn::GetAtt": ["NewHyperPodCluster", "ClusterArn"]}
            },
            "HyperPodClusterName": {
                "Description": "The name of the created SageMaker HyperPod cluster",
                "Value": {"Ref": "NewHyperPodCluster"}
            }
        }
    }
    return yaml.dump(template, sort_keys=False, default_flow_style=False, Dumper=NoAliasDumper)

def upload_cluster_template_to_s3(yaml_str):
    s3 = boto3.client('s3')
    bucket = os.environ['S3_BUCKET_NAME']
    key = 'hyperpod-cluster-template.yaml'

    s3.put_object(Bucket=bucket, Key=key, Body=yaml_str.encode('utf-8'), ContentType='text/yaml')
    return f"https://{bucket}.s3.amazonaws.com/{key}"
