import sys
import os
import boto3
import botocore
import cfnresponse
import json
from botocore.exceptions import ClientError

def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for updating EKS addon configuration
    """
    try: 
        print(f'boto3 version: {boto3.__version__}')
        print(f'botocore version: {botocore.__version__}')
        
        request_type = event['RequestType']
 
        response_data = {"Status": "SUCCESS"}
        
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
 
def on_create():
    """
    Handle Create request to update EKS addon configuration
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "EKS AddOn updated successfully"
        }
        
        cluster_name = os.environ.get('CLUSTER_NAME')
        workspace_name = os.environ.get('GRAFANA_WORKSPACE_NAME')
        workspace_arn = os.environ.get('GRAFANA_WORKSPACE_ARN')
        
        if not all([cluster_name, workspace_name, workspace_arn]):
            raise ValueError("Missing required environment variables")
        
        print(f"Updating addon for cluster: {cluster_name}")
        print(f"AMG workspace: {workspace_name}")
        
        eks = boto3.client('eks')
        
        # Get current addon configuration for update
        try:
            response = eks.describe_addon(
                clusterName=cluster_name,
                addonName='amazon-sagemaker-hyperpod-observability'
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                print(f"Addon not found for cluster {cluster_name}")
                return {
                    "Status": "FAILED",
                    "Reason": f"Addon not found for cluster {cluster_name}"
                }
            print(f"Error getting addon configuration: {str(e)}")
            return {
                "Status": "FAILED",
                "Reason": f"Error getting addon configuration: {str(e)}"
            }
        
        current_config = json.loads(response['addon']['configurationValues']) if response['addon'].get('configurationValues') else {}
        print(f"Current config: {current_config}")
        
        # Add AMG workspace configuration
        current_config['amgWorkspace'] = {
            'workspaceName': workspace_name,
            'arn': workspace_arn
        }
        
        print(f"Updated config: {current_config}")
        
        # Update addon
        try:
            eks.update_addon(
                clusterName=cluster_name,
                addonName='amazon-sagemaker-hyperpod-observability',
                configurationValues=json.dumps(current_config),
                resolveConflicts='OVERWRITE'
            )
            print("AddOn updated successfully")
        except Exception as e:
            print(f"Error updating addon: {str(e)}")
            return {
                "Status": "FAILED",
                "Reason": f"AddOn update failed: {str(e)}"
            }
        
        return response_data
         
    except Exception as e:
        print(f"Failed to update EKS addon: {str(e)}")
        return {
            "Status": "FAILED",
            "Reason": f"AddOn update failed: {str(e)}"
        }
 
def on_update():
    """
    Handle Update request
    """
    return on_create()

def on_delete():
    """
    Handle Delete request
    """
    print("Delete request - no cleanup required")
    return {
        "Status": "SUCCESS",
        "Reason": "No cleanup required"
    }