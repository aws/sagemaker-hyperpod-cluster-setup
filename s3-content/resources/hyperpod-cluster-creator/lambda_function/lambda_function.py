import boto3
import cfnresponse
import os
import json

def lambda_handler(event, context):
  try:
    if event['RequestType'] in ['Create', 'Update']:
      sagemaker = boto3.client('sagemaker')

      # Combine all instance settings into one JSON array
      combined_settings = []
      
      # Get number of instance groups from environment variable or default to 5
      num_groups = int(os.environ.get('NUMBER_OF_INSTANCE_GROUPS', 5))
      
      # Parse and merge each settings object
      for i in range(1, num_groups + 1):
          setting_key = f'INSTANCE_GROUP_SETTINGS{i}'
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
      
      # Get cluster parameters from environment variables
      cluster_name = os.environ.get('HYPER_POD_CLUSTER_NAME')
      node_recovery = os.environ.get('NODE_RECOVERY')
      eks_cluster_arn = os.environ.get('EKS_CLUSTER_ARN')
      security_group_ids = os.environ.get('SECURITY_GROUP_IDS').split(',') if ',' in os.environ.get('SECURITY_GROUP_IDS', '') else [os.environ.get('SECURITY_GROUP_IDS')]
      private_subnet_ids = os.environ.get('PRIVATE_SUBNET_IDS').split(',') if ',' in os.environ.get('PRIVATE_SUBNET_IDS', '') else [os.environ.get('PRIVATE_SUBNET_IDS')]
      sagemaker_iam_role_name=os.environ.get('SAGEMAKER_IAM_ROLE_NAME')
      s3_bucket_name=os.environ.get('S3_BUCKET_NAME')
      on_create_path=os.environ.get('ON_CREATE_PATH')
      # Prepare instance group configurations
      instance_groups = []
      if combined_settings:
          # Ensure each instance group has the required parameters
          for instance_group in combined_settings:
              # Only add parameters if they are provided and not already in the configuration
              if sagemaker_iam_role_name and 'ExecutionRole' not in instance_group:
                  instance_group['ExecutionRole'] = sagemaker_iam_role_name
                  
              # Add lifecycle script configuration if bucket name and script path are provided
              if s3_bucket_name and on_create_path and 'LifeCycleConfig' not in instance_group:
                  instance_group['LifeCycleConfig'] = {
                      'SourceS3Uri': f's3://{s3_bucket_name}',
                      'OnCreate': f'{on_create_path}'
                  }
          
          instance_groups = combined_settings
      
      # Create VpcConfig object according to Boto3 API requirements
      vpc_config = {
          'SecurityGroupIds': security_group_ids,
          'Subnets': private_subnet_ids
      }
      
      # Create Orchestrator config with EKS information
      orchestrator = {
          'Eks': {
              'ClusterArn': eks_cluster_arn
          }
      }
      
      # Create cluster using SageMaker API
      print(f"Creating HyperPod cluster: {cluster_name}")
      create_params = {
          'ClusterName': cluster_name,
          'NodeRecovery': node_recovery,
          'VpcConfig': vpc_config,
          'Orchestrator': orchestrator
      }
      
      # Only add instance groups if they exist
      if instance_groups:
          create_params['InstanceGroups'] = instance_groups
          
      print(f"Calling create_cluster with parameters: {create_params}")
      response = sagemaker.create_cluster(**create_params)
      
      print(f"Create cluster response: {response}")
      
      cluster_arn = response['ClusterArn']
      print(f"Cluster ARN: {cluster_arn}")
      
      cfnresponse.send(event, context, cfnresponse.SUCCESS, {
        'Message': 'Cluster creation is triggered'
      })
    elif event['RequestType'] == 'Delete':
        try:
            sagemaker = boto3.client('sagemaker')
            cluster_name = os.environ.get('HYPER_POD_CLUSTER_NAME')
            
            # Check if the cluster exists before trying to delete it
            try:
                sagemaker.describe_cluster(clusterName=cluster_name)
                # If describe_cluster doesn't throw an exception, the cluster exists
                print(f"Deleting HyperPod cluster: {cluster_name}")
                response = sagemaker.delete_cluster(clusterName=cluster_name)
                print(f"Delete cluster response: {response}")
            except sagemaker.exceptions.ResourceNotFound:
                print(f"Cluster {cluster_name} not found, nothing to delete")
            
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': 'Cluster deleted successfully or not found'
            })
        except Exception as e:
            print(f"Error during delete: {e}")
            cfnresponse.send(event, context, cfnresponse.FAILED, {
                'Error': str(e)
            })
  except Exception as e:
    print(e)
    cfnresponse.send(event, context, cfnresponse.FAILED, {
      'Error': str(e)
    })
