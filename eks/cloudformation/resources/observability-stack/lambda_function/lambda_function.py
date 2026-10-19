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
            response_data = on_create(event)
        elif request_type == 'Update':
            response_data = on_update(event)
        elif request_type == 'Delete':
            response_data = on_delete(event)
        else:
            raise ValueError(f"Invalid request type: {request_type}")
 
        # Without a physical id cfnresponse sends the log stream name, which changes between invocations, so
        # CloudFormation would treat an Update as a replacement and send a Delete for the old id, deleting the stack
        # that was just updated. Keep the id on Update and Delete; use the stack id on Create.
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            response_data,
            response_data.get('StackId') if request_type == 'Create' else event.get('PhysicalResourceId')
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
            },
            event.get('PhysicalResourceId')
        )
 
 
def optional_parameters(cfn, template_url, props):
    """
    Parameters newer than some observability templates, passed only when the template declares them: CloudFormation
    rejects an undeclared parameter, e.g. when StackTemplateUrl points at an older template in a customer bucket.
    If the template's parameters can't be read, they are left out (without the cluster name, the Grafana Lambda
    only skips the CloudWatch alert rules).
    """
    if not props.get('HyperPodClusterName'):
        return []
    try:
        declared = {p['ParameterKey'] for p in cfn.get_template_summary(TemplateURL=template_url).get('Parameters', [])}
    except Exception as e:
        print(f"Could not read the observability template's parameters: {str(e)}")
        return []
    if 'HyperPodClusterName' not in declared:
        return []
    return [{'ParameterKey': 'HyperPodClusterName', 'ParameterValue': props['HyperPodClusterName']}]

def on_create(event):
    """
    Handle Create request to create a new HyperPod cluster
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Observability Stack created successfully"
        }
        props = event.get('ResourceProperties', {})
        stack_name = props.get('ResourceNamePrefix') + "-ObservabilityStack"
        template_url = props.get('StackTemplateUrl')
        subnet_string = props.get('PrivateSubnetIds')

        cfn = boto3.client('cloudformation')
        print(f"Creating Cloudformation Stack: {stack_name}")
        response = cfn.create_stack(
            StackName=stack_name,
            TemplateURL=template_url,
            OnFailure='DELETE',
            EnableTerminationProtection=False,
            Parameters=[
                {
                'ParameterKey': 'ResourceNamePrefix',
                'ParameterValue': props.get('ResourceNamePrefix')
                },
                {
                'ParameterKey': 'CustomResourceS3Bucket',
                'ParameterValue': props.get('CustomResourceS3Bucket')
                },      
                {
                'ParameterKey': 'EKSClusterName',
                'ParameterValue': props.get('EKSClusterName')
                },                
                {
                'ParameterKey': 'TrainingMetricLevel',
                'ParameterValue': props.get('TrainingMetricLevel')
                },
                {
                'ParameterKey': 'TaskGovernanceMetricLevel',
                'ParameterValue': props.get('TaskGovernanceMetricLevel')
                },  
                {
                'ParameterKey': 'ClusterMetricLevel',
                'ParameterValue': props.get('ClusterMetricLevel')
                },
                {
                'ParameterKey': 'NodeMetricLevel',
                'ParameterValue': props.get('NodeMetricLevel')
                },
                {
                'ParameterKey': 'AcceleratedComputeMetricLevel',
                'ParameterValue': props.get('AcceleratedComputeMetricLevel')
                },
                {
                'ParameterKey': 'ScalingMetricLevel',
                'ParameterValue': props.get('ScalingMetricLevel')
                },
                {
                'ParameterKey': 'NetworkMetricLevel',
                'ParameterValue': props.get('NetworkMetricLevel')
                },
                {
                'ParameterKey': 'RayMetricLevel',
                'ParameterValue': props.get('RayMetricLevel')
                },
                {
                'ParameterKey': 'Logging',
                'ParameterValue': props.get('Logging')
                },
                {
                'ParameterKey': 'VpcId',
                'ParameterValue': props.get('VpcId')
                },
                {
                'ParameterKey': 'SecurityGroupId',
                'ParameterValue': props.get('SecurityGroupId')
                },                
                {
                'ParameterKey': 'PrivateSubnetIds',
                'ParameterValue': subnet_string
                },           
                {
                'ParameterKey': 'GrafanaWorkspaceName',
                'ParameterValue': props.get('GrafanaWorkspaceName')
                }, 
                {
                'ParameterKey': 'GrafanaWorkspaceId',
                'ParameterValue': props.get('GrafanaWorkspaceId')
                },
                {
                'ParameterKey': 'GrafanaWorkspaceArn',
                'ParameterValue': props.get('GrafanaWorkspaceArn')
                },                
                {
                'ParameterKey': 'PrometheusWorkspaceId',
                'ParameterValue': props.get('PrometheusWorkspaceId')
                },           
                {
                'ParameterKey': 'PrometheusWorkspaceArn',
                'ParameterValue': props.get('PrometheusWorkspaceArn')
                },
                {
                'ParameterKey': 'PrometheusWorkspaceEndpoint',
                'ParameterValue': props.get('PrometheusWorkspaceEndpoint')
                },           
                {
                'ParameterKey': 'HyperPodObservabilityRole',
                'ParameterValue': props.get('HyperPodObservabilityRole')
                },
                {
                'ParameterKey': 'GrafanaRole',
                'ParameterValue': props.get('GrafanaRole')
                }, 
                {
                'ParameterKey': 'CreateHyperPodObservabilityRole',
                'ParameterValue': props.get('CreateHyperPodObservabilityRole')
                },
                {
                'ParameterKey': 'CreateGrafanaRole',
                'ParameterValue': props.get('CreateGrafanaRole')
                },                
                {
                'ParameterKey': 'CreatePrometheusWorkspace',
                'ParameterValue': props.get('CreatePrometheusWorkspace')
                },                
                {
                'ParameterKey': 'CreateGrafanaWorkspace',
                'ParameterValue': props.get('CreateGrafanaWorkspace')
                },
                {
                'ParameterKey': 'IsRIG',
                'ParameterValue': props.get('IsRIG', 'false')
                },
                {
                'ParameterKey': 'ExecutionRoleName',
                'ParameterValue': props.get('ExecutionRoleName', '')
                },
            ] + optional_parameters(cfn, template_url, props),
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
        )
        print(f"Stack creation initiated: {response['StackId']}")
        response_data['StackId'] = response['StackId']
        return response_data
    except Exception as e:
        print(f"Failed to create Cloudformation Workspace: {str(e)}")
        raise
          
def on_update(event):
    """
    Handle Update request to update an existing Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Observability Stack updated successfully"
        }
        print(f"Request received for Updation of CFN")
        props = event.get('ResourceProperties', {})
        stack_name = props.get('ResourceNamePrefix') + "-ObservabilityStack"
        template_url = props.get('StackTemplateUrl')
        subnet_string = props.get('PrivateSubnetIds')

        cfn = boto3.client('cloudformation')
        print(f"Updating Cloudformation Stack: {stack_name}")
        response = cfn.update_stack(
            StackName=stack_name,
            TemplateURL=template_url,
            Parameters=[
                {
                'ParameterKey': 'ResourceNamePrefix',
                'ParameterValue': props.get('ResourceNamePrefix')
                },
                {
                'ParameterKey': 'CustomResourceS3Bucket',
                'ParameterValue': props.get('CustomResourceS3Bucket')
                },
                {
                'ParameterKey': 'EKSClusterName',
                'ParameterValue': props.get('EKSClusterName')
                },                
                {
                'ParameterKey': 'TrainingMetricLevel',
                'ParameterValue': props.get('TrainingMetricLevel')
                },
                {
                'ParameterKey': 'TaskGovernanceMetricLevel',
                'ParameterValue': props.get('TaskGovernanceMetricLevel')
                },  
                {
                'ParameterKey': 'ClusterMetricLevel',
                'ParameterValue': props.get('ClusterMetricLevel')
                },
                {
                'ParameterKey': 'NodeMetricLevel',
                'ParameterValue': props.get('NodeMetricLevel')
                },
                {
                'ParameterKey': 'AcceleratedComputeMetricLevel',
                'ParameterValue': props.get('AcceleratedComputeMetricLevel')
                },
                {
                'ParameterKey': 'ScalingMetricLevel',
                'ParameterValue': props.get('ScalingMetricLevel')
                },
                {
                'ParameterKey': 'NetworkMetricLevel',
                'ParameterValue': props.get('NetworkMetricLevel')
                },
                {
                'ParameterKey': 'RayMetricLevel',
                'ParameterValue': props.get('RayMetricLevel')
                },
                {
                'ParameterKey': 'Logging',
                'ParameterValue': props.get('Logging')
                },
                {
                'ParameterKey': 'VpcId',
                'ParameterValue': props.get('VpcId')
                },
                {
                'ParameterKey': 'SecurityGroupId',
                'ParameterValue': props.get('SecurityGroupId')
                },                
                {
                'ParameterKey': 'PrivateSubnetIds',
                'ParameterValue': subnet_string
                },           
                {
                'ParameterKey': 'GrafanaWorkspaceName',
                'ParameterValue': props.get('GrafanaWorkspaceName')
                }, 
                {
                'ParameterKey': 'GrafanaWorkspaceId',
                'ParameterValue': props.get('GrafanaWorkspaceId')
                },
                {
                'ParameterKey': 'GrafanaWorkspaceArn',
                'ParameterValue': props.get('GrafanaWorkspaceArn')
                },                
                {
                'ParameterKey': 'PrometheusWorkspaceId',
                'ParameterValue': props.get('PrometheusWorkspaceId')
                },           
                {
                'ParameterKey': 'PrometheusWorkspaceArn',
                'ParameterValue': props.get('PrometheusWorkspaceArn')
                },
                {
                'ParameterKey': 'PrometheusWorkspaceEndpoint',
                'ParameterValue': props.get('PrometheusWorkspaceEndpoint')
                },           
                {
                'ParameterKey': 'HyperPodObservabilityRole',
                'ParameterValue': props.get('HyperPodObservabilityRole')
                },
                {
                'ParameterKey': 'GrafanaRole',
                'ParameterValue': props.get('GrafanaRole')
                }, 
                {
                'ParameterKey': 'CreateHyperPodObservabilityRole',
                'ParameterValue': props.get('CreateHyperPodObservabilityRole')
                },
                {
                'ParameterKey': 'CreateGrafanaRole',
                'ParameterValue': props.get('CreateGrafanaRole')
                },                
                {
                'ParameterKey': 'CreatePrometheusWorkspace',
                'ParameterValue': props.get('CreatePrometheusWorkspace')
                },                
                {
                'ParameterKey': 'CreateGrafanaWorkspace',
                'ParameterValue': props.get('CreateGrafanaWorkspace')
                },
                {
                'ParameterKey': 'IsRIG',
                'ParameterValue': props.get('IsRIG', 'false')
                },
                {
                'ParameterKey': 'ExecutionRoleName',
                'ParameterValue': props.get('ExecutionRoleName', '')
                },
            ] + optional_parameters(cfn, template_url, props),
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND'],
        )
        print(f"Stack creation initiated: {response['StackId']}")
        response_data['StackId'] = response['StackId']
        return response_data
    except ClientError as e:
        # A property change (e.g. HyperPodClusterName) that leaves the nested stack's template and parameters as they
        # are is a no-op for CloudFormation, not a failure.
        if 'No updates are to be performed' in str(e):
            print(f"No updates to the observability stack: {str(e)}")
            # The main stack reads the StackId attribute.
            response_data['StackId'] = cfn.describe_stacks(StackName=stack_name)['Stacks'][0]['StackId']
            return response_data
        print(f"Failed to create Cloudformation Workspace: {str(e)}")
        raise
    except Exception as e:
        print(f"Failed to create Cloudformation Workspace: {str(e)}")
        raise

def on_delete(event):
    """
    Handle Delete request to delete a Grafana Workspace
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Observability Stack deleted successfully"
        }
        print(f"Request received for Deletion of CFN")
        props = event.get('ResourceProperties', {})
        stack_name = props.get('ResourceNamePrefix') + "-ObservabilityStack"
        cfn = boto3.client('cloudformation')
        cfn.delete_stack(
            StackName=stack_name
        )
        # Wait for stack to be deleted
        while True:
            response = cfn.describe_stacks(StackName=stack_name)
            status = response['Stacks'][0]['StackStatus']
            print(f"Waiting for stack {stack_name} to be deleted, current status: {status}")
            if status in ['DELETE_COMPLETE']:
                break
            elif status in ['DELETE_FAILED', 'ROLLBACK_FAILED']:
                raise Exception(f"Stack deletion failed: {status}")
            time.sleep(10)

    except Exception as e:
        print(f"Failed to delete Observability stack: {str(e)}")
        if "does not exist" in str(e):
            return response_data
        else:
            raise e
