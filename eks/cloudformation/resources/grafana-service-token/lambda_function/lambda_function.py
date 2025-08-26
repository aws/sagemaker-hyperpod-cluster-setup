import boto3
import botocore
import cfnresponse
import os
import json
from botocore.exceptions import ClientError

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
            "Reason": "Grafana Workspace Service Token created successfully"
        }
        workspace_id = os.environ['GRAFANA_WORKSPACE_ID']
        service_account_name = os.environ['SERVICE_ACCOUNT_NAME']
        grafana = boto3.client('grafana')
        service_account_response = grafana.create_workspace_service_account(
            workspaceId=workspace_id,
            name=service_account_name,
            grafanaRole='ADMIN',
        )
        response_data['ServiceAccountId'] = service_account_response['id']

        service_account_token_response = grafana.create_workspace_service_account_token(
            workspaceId=workspace_id,
            serviceAccountId=response_data['ServiceAccountId'],
            name=service_account_name + "-token",
            secondsToLive=1500
        )
        response_data['ServiceAccountTokenId'] = service_account_token_response['serviceAccountToken']['id']
        response_data['ServiceAccountTokenKey'] = service_account_token_response['serviceAccountToken']['key']

        return response_data
         
    except Exception as e:
        print(f"Failed to create Grafana Workspace: {str(e)}")
        raise
 
def on_update():
    """
    Handle Update request to update an existing Grafana Workspace
    """
    on_create()

 
def on_delete():
    """
    Handle Delete request to delete a Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana Workspace Service Token deletion skipped successfully"
        }
        workspace_id = os.environ['GRAFANA_WORKSPACE_ID']
        print(f"Request received for Deletion of workspace: {workspace_id}")
        return response_data

    except Exception as e:
        print(f"Failed to delete Grafana Service token: {str(e)}")
        raise