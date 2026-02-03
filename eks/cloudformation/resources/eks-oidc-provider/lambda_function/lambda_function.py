import os
import subprocess
import cfnresponse

def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for associating IAM OIDC provider with EKS cluster
    """
    try: 
        request_type = event['RequestType']

        if request_type == 'Create':
            response_data = on_create(event)
        elif request_type == 'Update':
            response_data = on_update(event)
        elif request_type == 'Delete':
            response_data = on_delete(event)
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


def associate_oidc_provider(cluster_name):
    """
    Associate IAM OIDC provider with the EKS cluster using eksctl
    This is idempotent - if already associated, eksctl will report that it exists
    
    Args:
        cluster_name: Name of the EKS cluster
    """
    try:
        print(f"Associating IAM OIDC provider with cluster {cluster_name}...")
        
        # Run eksctl command - this is idempotent
        result = subprocess.run(
            ['eksctl', 'utils', 'associate-iam-oidc-provider',
             '--cluster', cluster_name,
             '--region', os.environ['AWS_REGION'],
             '--approve'],
            check=True,
            capture_output=True,
            text=True
        )
        
        print(f"eksctl output: {result.stdout}")
        if result.stderr:
            print(f"eksctl stderr: {result.stderr}")
        
        print("IAM OIDC provider association successful")
        
    except subprocess.CalledProcessError as e:
        error_msg = f"Command failed with return code {e.returncode}"
        if e.stdout:
            error_msg += f"\nStdout: {e.stdout}"
        if e.stderr:
            error_msg += f"\nStderr: {e.stderr}"
        raise Exception(error_msg)
        
    except FileNotFoundError:
        raise Exception("eksctl command not found. Please ensure eksctl is installed in the Lambda environment.")
        
    except Exception as e:
        raise Exception(f"Failed to associate OIDC provider: {str(e)}")


def on_create(event):
    """
    Handle Create request to associate OIDC provider
    """
    try:
        # Ensure required environment variables are set
        cluster_name = os.environ.get('EKS_CLUSTER_NAME')
        if not cluster_name:
            raise ValueError("Missing required environment variable: EKS_CLUSTER_NAME")

        if 'AWS_REGION' not in os.environ:
            raise ValueError("Missing required environment variable: AWS_REGION")
        
        # Associate OIDC provider (idempotent operation)
        associate_oidc_provider(cluster_name)
        
        return {
            "Status": "SUCCESS",
            "Reason": "OIDC provider associated successfully"
        }

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to associate OIDC provider: {str(e)}")


def on_update(event):
    """
    Handle Update request - OIDC provider association is idempotent
    """
    try:
        cluster_name = os.environ.get('EKS_CLUSTER_NAME')
        if not cluster_name:
            raise ValueError("Missing required environment variable: EKS_CLUSTER_NAME")

        if 'AWS_REGION' not in os.environ:
            raise ValueError("Missing required environment variable: AWS_REGION")
        
        # Associate OIDC provider (idempotent - will succeed if already exists)
        associate_oidc_provider(cluster_name)
        
        return {
            "Status": "SUCCESS",
            "Reason": "OIDC provider verified"
        }

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to update OIDC provider: {str(e)}")


def on_delete(event):
    """
    Handle Delete request - no action taken, OIDC provider is retained
    """
    print("Delete request received - OIDC provider will be retained")
    return {
        "Status": "SUCCESS",
        "Reason": "OIDC provider retained (not deleted)"
    }
