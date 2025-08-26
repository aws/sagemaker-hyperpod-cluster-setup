import boto3
import botocore
import cfnresponse
import os
import json
from botocore.exceptions import ClientError
import time

def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing SageMaker HyperPod Observability
    """
    try: 
        print(f'boto3 version: {boto3.__version__}')
        print(f'botocore version: {botocore.__version__}')
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
 
 
def on_create():
    """
    Handle Create request to create a new HyperPod cluster
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana Workspace created successfully"
        }
        workspace_name = os.environ['WORKSPACE_NAME']
        workspace_role_arn = os.environ['WORKSPACE_ROLE_ARN']
 
        account_access_type = 'CURRENT_ACCOUNT'
        authentication_providers = ['AWS_SSO']
        permission_type = 'CUSTOMER_MANAGED'
        configuration = "{\"unifiedAlerting\":{\"enabled\":true}}"
        tags = {
            "Sagemaker": "true"
        }
        grafana = boto3.client('grafana')
        print(f"Creating Grafana Workspace: {workspace_name}")
        response = grafana.create_workspace(
            accountAccessType=account_access_type,
            authenticationProviders=authentication_providers,
            permissionType=permission_type,
            configuration=configuration,
            workspaceName=workspace_name,
            workspaceRoleArn=workspace_role_arn,
            tags=tags
        )
        response_data['WorkspaceId'] = response['workspace']['id']
        retries = 0
        MAX_RETRIES = 20
        WAIT_SECONDS = 15
        while True:
            try:
                desc = grafana.describe_workspace(workspaceId=response_data['WorkspaceId'])
                status = desc['workspace']['status']
                print(f"[Attempt {retries + 1}] Workspace status: {status}")

                if status == 'ACTIVE':
                    break

                elif status in ['FAILED', 'DELETING']:
                    print(f"Workspace reached failed status: {response_data['WorkspaceId']}")
                    raise

                retries += 1
                if retries >= MAX_RETRIES:
                    print(f"Timed out creating Grafana Workspace: {response_data['WorkspaceId']}")
                    raise
                time.sleep(WAIT_SECONDS)
            except Exception as e:
                print(f"Error checking workspace status: {str(e)}")
                retries += 1
                if retries >= MAX_RETRIES:
                    print(f"Timed out creating Grafana Workspace: {response_data['WorkspaceId']}")
                    raise
                time.sleep(WAIT_SECONDS)


        response_data['Arn'] = "arn:" + os.environ['PARTITION'] +":grafana:" + os.environ['REGION'] + ":" + os.environ['AWS_ACCOUNT_ID'] + ":/workspaces/" + response['workspace']['id']
        return response_data
         
    except Exception as e:
        print(f"Failed to create Grafana Workspace: {str(e)}")
        raise
 
def on_update():
    """
    Handle Update request to update an existing Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana Workspace updation skipped successfully"
        }
        workspace_name = os.environ['WORKSPACE_NAME']
        print(f"Request received for Updation of workspace: {workspace_name}")
        return response_data
         
    except Exception as e:
        print(f"Failed to update Grafana cluster: {str(e)}")
        raise
 
def on_delete():
    """
    Handle Delete request to delete a Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana Workspace deletion skipped successfully"
        }
        workspace_name = os.environ['WORKSPACE_NAME']
        print(f"Request received for Deletion of workspace: {workspace_name}")
        return response_data

    except Exception as e:
        print(f"Failed to delete Grafana Workspace: {str(e)}")
        raise