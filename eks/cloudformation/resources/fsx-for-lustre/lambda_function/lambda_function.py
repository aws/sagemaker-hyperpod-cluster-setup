import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml

def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for managing FSx for Lustre file systems
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

def write_kubeconfig(cluster_name, region):
    """
    Generate kubeconfig using boto3
    """
    # Initialize EKS client
    eks = boto3.client('eks', region_name=region)
    
    try:
        # Get cluster info
        cluster = eks.describe_cluster(name=cluster_name)['cluster']
        
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
                'name': cluster_name
            }],
            'current-context': cluster_name,
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


def find_subnet_in_az(az_id, subnet_ids):
    """
    Find a subnet ID that is in the specified availability zone ID
    
    Args:
        az_id: The availability zone ID to search for
        subnet_ids: List of subnet IDs to search through
        
    Returns:
        The subnet ID that is in the specified AZ ID, or None if not found
    """
    if not az_id or not subnet_ids:
        return None
        
    try:
        ec2 = boto3.client('ec2', region_name=os.environ['AWS_REGION'])
        
        # Split comma-separated subnet IDs if provided as a string
        if isinstance(subnet_ids, str):
            subnet_list = [s.strip() for s in subnet_ids.split(',')]
        else:
            subnet_list = subnet_ids
            
        # Describe all subnets in the list
        response = ec2.describe_subnets(SubnetIds=subnet_list)
        
        # Find the subnet in the specified AZ ID
        for subnet in response['Subnets']:
            if subnet['AvailabilityZoneId'] == az_id:
                print(f"Found subnet {subnet['SubnetId']} in availability zone ID {az_id}")
                return subnet['SubnetId']
                
        print(f"No subnet found in availability zone ID {az_id}")
        return None
        
    except Exception as e:
        print(f"Error finding subnet in AZ ID {az_id}: {str(e)}")
        return None


def get_fsx_network_config(fsx_file_system_id, aws_region):
    """
    Get subnet ID and security group IDs from an existing FSx file system
    
    Args:
        fsx_file_system_id: The FSx file system ID
        aws_region: AWS region
        
    Returns:
        Tuple of (subnet_id, security_group_ids)
    """
    try:
        # Get FSx file system details using boto3
        fsx_client = boto3.client('fsx', region_name=aws_region)
        fsx_response = fsx_client.describe_file_systems(FileSystemIds=[fsx_file_system_id])
        
        if not fsx_response['FileSystems']:
            raise Exception(f"FSx file system {fsx_file_system_id} not found")
            
        fsx_details = fsx_response['FileSystems'][0]
        
        # Get network information
        subnet_id = fsx_details['SubnetIds'][0]  # Use first subnet if multiple
        security_group_ids = ','.join(fsx_details['NetworkInterfaceIds'])
        
        return subnet_id, security_group_ids
        
    except Exception as e:
        print(f"Error getting FSx network configuration: {str(e)}")
        raise


def create_dynamic_fsx_resources(response_data):
    """
    Create Kubernetes resources for dynamic FSx provisioning
    """
    try:
        print("FSX_FILE_SYSTEM_ID is empty. Proceeding with dynamic provisioning...")
        
        # Dynamic Provisioning 
        print("Creating FSx for Lustre StorageClass...")
        
        # Determine the subnet ID to use based on FSX_SUBNETID or find in FSX_AVAILABILITY_ZONE
        subnet_id = ""
        
        # First check if FSX_SUBNETID is provided and not empty
        fsx_subnet_id = os.environ.get('FSX_SUBNETID', '').strip()
        fsx_az = os.environ.get('FSX_AVAILABILITY_ZONE', '').strip()
        private_subnets = os.environ.get('PRIVATE_SUBNET_IDS', '').strip()
        
        if fsx_subnet_id:
            # Use explicitly provided subnet ID
            subnet_id = fsx_subnet_id
            print(f"Using provided FSX_SUBNETID: {subnet_id}")
        elif fsx_az and private_subnets:
            # Find a subnet in the provided availability zone ID
            subnet_id = find_subnet_in_az(fsx_az, private_subnets)
            if subnet_id:
                print(f"Found subnet {subnet_id} in FSX_AVAILABILITY_ZONE ID {fsx_az}")
            else:
                print(f"Warning: No subnet found in FSX_AVAILABILITY_ZONE ID {fsx_az}. StorageClass creation may fail.")
        else:
            print("Warning: Neither FSX_SUBNETID nor both FSX_AVAILABILITY_ZONE and PRIVATE_SUBNET_IDS provided or they are empty. StorageClass creation may fail.")
        
        # Create StorageClass YAML content
        storage_class_content = f"""apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fsx-sc
provisioner: fsx.csi.aws.com
parameters:
  subnetId: {subnet_id}
  securityGroupIds: {os.environ['SECURITY_GROUP_ID']}
  deploymentType: {os.environ['DEPLOYMENT_TYPE']}
  automaticBackupRetentionDays: "0"
  copyTagsToBackups: "true"
  perUnitStorageThroughput: "{os.environ['PER_UNIT_STORAGE_THROUGHPUT']}"
  dataCompressionType: "{os.environ['DATA_COMPRESSION_TYPE']}"
  fileSystemTypeVersion: "{os.environ['FILE_SYSTEM_TYPE_VERSION']}"
mountOptions:
  - flock
"""
        
        # Write StorageClass YAML to a temporary file
        storage_class_path = '/tmp/storageclass.yaml'
        with open(storage_class_path, 'w') as f:
            f.write(storage_class_content)
            
        # Apply the StorageClass using kubectl
        print("Applying StorageClass to the cluster...")
        subprocess.run(['kubectl', 'apply', '-f', storage_class_path], check=True)
        
        # Verify StorageClass creation
        print("Verifying StorageClass creation...")
        result = subprocess.run(['kubectl', 'get', 'storageclass', 'fsx-sc', '-o', 'yaml'], 
                              check=True, capture_output=True, text=True)
        print(f"StorageClass verification:\n{result.stdout}")
        
        # Add StorageClass name to response data
        response_data["StorageClassName"] = "fsx-sc"
        
        # Create a sample PersistentVolumeClaim using the StorageClass
        print("Creating a sample PersistentVolumeClaim...")
        
        # Get storage capacity and ensure it's properly formatted
        storage_capacity = os.environ['STORAGE_CAPACITY']
        
        pvc_content = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-claim
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: fsx-sc
  resources:
    requests:
      storage: {storage_capacity}Gi
"""
        
        # Write PVC YAML to a temporary file
        pvc_path = '/tmp/pvc.yaml'
        with open(pvc_path, 'w') as f:
            f.write(pvc_content)
            
        # Apply the PVC using kubectl
        print("Applying PersistentVolumeClaim to the cluster...")
        subprocess.run(['kubectl', 'apply', '-f', pvc_path], check=True)
        
        # Add PVC information to response data
        response_data["PersistentVolumeClaimName"] = "fsx-claim"
        response_data["PVCNamespace"] = "default"
        
        print("This PVC will kick off the dynamic provisioning of an FSx for Lustre file system based on the specifications provided in the storage class.")
        
        # View the status of the PVC
        print("\nChecking PVC status:")
        pvc_status = subprocess.run(['kubectl', 'describe', 'pvc', 'fsx-claim'], 
                                  check=True, capture_output=True, text=True)
        print(pvc_status.stdout)
        
        # Check if the PVC is in Pending or Bound state
        print("\nChecking PVC phase:")
        try:
            pvc_phase = subprocess.run(['kubectl', 'get', 'pvc', 'fsx-claim', '-n', 'default', '-ojsonpath={.status.phase}'],
                                     check=True, capture_output=True, text=True)
            print(f"PVC Status: {pvc_phase.stdout}")
            response_data["PVCStatus"] = pvc_phase.stdout
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to get PVC phase: {e}")
            response_data["PVCStatus"] = "Unknown"
        
        # Try to get volume info if PVC is bound (this might fail initially as provisioning takes time)
        if response_data.get("PVCStatus") == "Bound":
            try:
                # Get the PV name first
                pv_name = subprocess.run(['kubectl', 'get', 'pvc', 'fsx-claim', '-n', 'default', '-ojsonpath={.spec.volumeName}'],
                                       check=True, capture_output=True, text=True)
                
                # Get the FSx volume ID
                volume_id = subprocess.run(['kubectl', 'get', 'pv', pv_name.stdout, '-ojsonpath={.spec.csi.volumeHandle}'],
                                        check=True, capture_output=True, text=True)
                
                print(f"\nFSx Volume ID: {volume_id.stdout}")
                response_data["FSxVolumeId"] = volume_id.stdout
            except subprocess.CalledProcessError as e:
                print(f"Note: FSx volume ID not yet available. Provisioning may still be in progress: {e}")
                response_data["FSxVolumeId"] = "Provisioning"
        else:
            print("\nNote: FSx provisioning may take up to 10 minutes. Check status later with:")
            print("  kubectl describe pvc fsx-claim")
            print("  kubectl get pvc fsx-claim -n default -ojsonpath={.status.phase}")
            print("\nOnce bound, retrieve volume ID with:")
            print("  kubectl get pv $(kubectl get pvc fsx-claim -n default -ojsonpath={.spec.volumeName}) -ojsonpath={.spec.csi.volumeHandle}")
            
    except Exception as e:
        print(f"Error creating Kubernetes resources for dynamic FSx provisioning: {str(e)}")
        raise


def create_existing_fsx_resources(response_data):
    """
    Create Kubernetes resources for existing FSx file system
    """
    try:
        fsx_file_system_id = os.environ['FSX_FILE_SYSTEM_ID']
        aws_region = os.environ['AWS_REGION']
        
        # Get subnet ID and security group IDs from the existing FSx file system
        subnet_id, security_group_ids = get_fsx_network_config(fsx_file_system_id, aws_region)
        # Create unique resource names to avoid conflicts
        resource_suffix = fsx_file_system_id.replace('fs-', '')[:8]
        sc_name = f"fsx-sc-{resource_suffix}"
        pv_name = f"fsx-pv-{resource_suffix}"
        pvc_name = f"fsx-claim-{resource_suffix}"
        pod_name = f"fsx-app-{resource_suffix}"
        
        # Get FSx file system details using boto3
        fsx_client = boto3.client('fsx', region_name=aws_region)
        try:
            fsx_response = fsx_client.describe_file_systems(FileSystemIds=[fsx_file_system_id])
        except ClientError as e:
            raise Exception(f"Failed to describe FSx file system {fsx_file_system_id}: {str(e)}")
        
        if not fsx_response['FileSystems']:
            raise Exception(f"FSx file system {fsx_file_system_id} not found")
            
        fsx_details = fsx_response['FileSystems'][0]
        
        # Verify it's a Lustre file system
        if fsx_details['FileSystemType'] != 'LUSTRE':
            raise Exception(f"File system {fsx_file_system_id} is not a Lustre file system. Type: {fsx_details['FileSystemType']}")
            
        # Check file system state
        if fsx_details['Lifecycle'] != 'AVAILABLE':
            raise Exception(f"FSx file system {fsx_file_system_id} is not available. Current state: {fsx_details['Lifecycle']}")
            
        # Get storage capacity directly from FSx API instead of environment variable
        storage_capacity = str(fsx_details['StorageCapacity'])
            
        dns_name = fsx_details['DNSName']
        mount_name = fsx_details['LustreConfiguration']['MountName']
        
        print(f"Found FSx file system: {fsx_file_system_id}")
        print(f"DNS Name: {dns_name}")
        print(f"Mount Name: {mount_name}")
        
        # 1. Create StorageClass for existing FSx
        print("Creating StorageClass for existing FSx file system...")
        storage_class_content = f"""apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {sc_name}
provisioner: fsx.csi.aws.com
parameters:
  fileSystemId: {fsx_file_system_id}
  subnetId: {subnet_id}
  securityGroupIds: {security_group_ids}
"""
        
        storage_class_path = '/tmp/storageclass.yaml'
        with open(storage_class_path, 'w') as f:
            f.write(storage_class_content)
            
        subprocess.run(['kubectl', 'apply', '-f', storage_class_path], check=True)
        print("StorageClass created successfully")
        
        # 2. Create PersistentVolume for existing FSx
        print("Creating PersistentVolume for existing FSx file system...")
        pv_content = f"""apiVersion: v1
kind: PersistentVolume
metadata:
  name: {pv_name}
spec:
  capacity:
    storage: {storage_capacity}Gi
  volumeMode: Filesystem
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: {sc_name}
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: {fsx_file_system_id}
    volumeAttributes:
      dnsname: {dns_name}
      mountname: {mount_name}
"""
        
        pv_path = '/tmp/pv.yaml'
        with open(pv_path, 'w') as f:
            f.write(pv_content)
            
        subprocess.run(['kubectl', 'apply', '-f', pv_path], check=True)
        print("PersistentVolume created successfully")
        
        # 3. Create PersistentVolumeClaim
        print("Creating PersistentVolumeClaim for existing FSx file system...")
        pvc_content = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {pvc_name}
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: {sc_name}
  resources:
    requests:
      storage: {storage_capacity}Gi
"""
        
        pvc_path = '/tmp/pvc.yaml'
        with open(pvc_path, 'w') as f:
            f.write(pvc_content)
            
        subprocess.run(['kubectl', 'apply', '-f', pvc_path], check=True)
        print("PersistentVolumeClaim created successfully")
        
        # 4. Create Pod that uses the FSx volume
        print("Creating Pod that mounts the FSx volume...")
        pod_content = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
spec:
  containers:
  - name: app
    image: ubuntu
    command: ["/bin/sh"]
    args: ["-c", "while true; do echo $(date -u) >> /data/out.txt; sleep 5; done"]
    volumeMounts:
    - name: persistent-storage
      mountPath: /data
  volumes:
  - name: persistent-storage
    persistentVolumeClaim:
      claimName: {pvc_name}
"""
        
        pod_path = '/tmp/pod.yaml'
        with open(pod_path, 'w') as f:
            f.write(pod_content)
            
        subprocess.run(['kubectl', 'apply', '-f', pod_path], check=True)
        print("Sample Pod created successfully")
        
        # Verify resources were created
        print("\nVerifying created resources...")
        
        # Check StorageClass
        try:
            result = subprocess.run(['kubectl', 'get', 'storageclass', sc_name], 
                                  check=True, capture_output=True, text=True)
            print(f"StorageClass status:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify StorageClass: {e}")
            
        # Check PV
        try:
            result = subprocess.run(['kubectl', 'get', 'pv', pv_name], 
                                  check=True, capture_output=True, text=True)
            print(f"PersistentVolume status:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify PersistentVolume: {e}")
            
        # Check PVC
        try:
            result = subprocess.run(['kubectl', 'get', 'pvc', pvc_name], 
                                  check=True, capture_output=True, text=True)
            print(f"PersistentVolumeClaim status:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify PersistentVolumeClaim: {e}")
            
        # Check Pod
        try:
            result = subprocess.run(['kubectl', 'get', 'pod', pod_name], 
                                  check=True, capture_output=True, text=True)
            print(f"Pod status:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify Pod: {e}")
        
        # Update response data
        response_data.update({
            "StorageClassName": sc_name,
            "PersistentVolumeName": pv_name, 
            "PersistentVolumeClaimName": pvc_name,
            "PVCNamespace": "default",
            "SamplePodName": pod_name,
            "FSxDNSName": dns_name,
            "FSxMountName": mount_name
        })
        
        print("\nKubernetes resources for existing FSx file system created successfully!")
        print(f"You can now use the PVC '{pvc_name}' in your applications to mount the FSx volume.")
        print(f"The sample pod '{pod_name}' demonstrates how to use the volume.")
        
    except Exception as e:
        print(f"Error creating Kubernetes resources for existing FSx: {str(e)}")
        raise


def on_create(event):
    """
    Handle Set Up an FSx for Lustre File System
    """
    try:
        # Initialize response data
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx is set up successfully"
        }

        resourceId = event['LogicalResourceId']
        # Ensure required environment variables are set
        required_env_vars = [
            'CLUSTER_NAME',
            'PER_UNIT_STORAGE_THROUGHPUT',
            'DATA_COMPRESSION_TYPE',
            'FILE_SYSTEM_TYPE_VERSION',
            'FSX_FILE_SYSTEM_ID',
            'PATH',
            'GIT_EXEC_PATH',
            'KUBECONFIG',
            'LD_LIBRARY_PATH'
        ]
        
        # STORAGE_CAPACITY is only required for dynamic provisioning
        if os.environ['FSX_FILE_SYSTEM_ID'] == '' and 'STORAGE_CAPACITY' not in os.environ:
            raise ValueError("Missing required environment variable: STORAGE_CAPACITY for dynamic provisioning")
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        if resourceId == 'FsxCustomResourceStep1':
            # Associate IAM OIDC provider with the cluster
            subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', os.environ['CLUSTER_NAME'], '--approve'], check=True)

            # Create IAM service account for FSx CSI controller
            subprocess.run(['eksctl', 'create', 'iamserviceaccount',
                            '--name', 'fsx-csi-controller-sa',
                            '--namespace', 'kube-system',
                            '--cluster', os.environ['CLUSTER_NAME'],
                            '--attach-policy-arn', 'arn:aws:iam::aws:policy/AmazonFSxFullAccess',
                            '--approve',
                            '--role-name', f"FSXLCSI-{os.environ['CLUSTER_NAME']}",
                            '--region', os.environ['AWS_REGION']], check=True)

            # Verify proper annotation of the service account with the IAM role ARN
            try:
                result = subprocess.run(['kubectl', 'get', 'sa', 'fsx-csi-controller-sa', '-n', 'kube-system', '-oyaml'], 
                                    check=True, capture_output=True, text=True)
                print(f"Service account verification:\n{result.stdout}")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to verify service account: {e}")
                
            # Verify installation of the FSx for Lustre CSI driver
            try:
                result = subprocess.run(['kubectl', 'get', 'pods', '-n', 'kube-system', '-l', 'app.kubernetes.io/name=aws-fsx-csi-driver'], 
                                    check=True, capture_output=True, text=True)
                print(f"FSx for Lustre CSI driver verification:\n{result.stdout}")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to verify FSx for Lustre CSI driver installation: {e}")
        elif resourceId == 'FsxCustomResourceStep2':
            # Choose between dynamic provisioning or existing FSx
            if os.environ['FSX_FILE_SYSTEM_ID'] == '':
                # Create Kubernetes resources for dynamic FSx provisioning
                create_dynamic_fsx_resources(response_data)
            else:
                print(f"Using existing FSx for Lustre file system with ID: {os.environ['FSX_FILE_SYSTEM_ID']}")
                response_data["FSxVolumeId"] = os.environ['FSX_FILE_SYSTEM_ID']
                
                # Create Kubernetes resources for existing FSx file system
                create_existing_fsx_resources(response_data)
        
        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to install Helm chart: {str(e)}")


def on_update(event):
    """
    Handle Update request to upgrade the AWS FSx CSI driver and update StorageClass
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx CSI driver updated successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION',
            'PER_UNIT_STORAGE_THROUGHPUT',
            'DATA_COMPRESSION_TYPE',
            'FILE_SYSTEM_TYPE_VERSION'
        ]
        
        # STORAGE_CAPACITY is only required for dynamic provisioning
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] == '' and 'STORAGE_CAPACITY' not in os.environ:
            raise ValueError("Missing required environment variable: STORAGE_CAPACITY for dynamic provisioning")
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")
        

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Associate IAM OIDC provider with the cluster (if not already done)
        try:
            subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', os.environ['CLUSTER_NAME'], '--approve'], check=True)
        except subprocess.CalledProcessError as e:
            # This might fail if already exists, which is fine
            print(f"Note: OIDC provider association: {e}")

        # Create or update IAM service account for FSx CSI controller
        subprocess.run(['eksctl', 'create', 'iamserviceaccount',
                        '--name', 'fsx-csi-controller-sa',
                        '--namespace', 'kube-system',
                        '--cluster', os.environ['CLUSTER_NAME'],
                        '--attach-policy-arn', 'arn:aws:iam::aws:policy/AmazonFSxFullAccess',
                        '--approve',
                        '--role-name', f"FSXLCSI-{os.environ['CLUSTER_NAME']}-{os.environ['AWS_REGION']}",
                        '--region', os.environ['AWS_REGION']], check=True)

        # Verify proper annotation of the service account with the IAM role ARN
        try:
            result = subprocess.run(['kubectl', 'get', 'sa', 'fsx-csi-controller-sa', '-n', 'kube-system', '-oyaml'], 
                                   check=True, capture_output=True, text=True)
            print(f"Service account verification:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify service account: {e}")
            
        # Verify installation of the FSx for Lustre CSI driver
        try:
            result = subprocess.run(['kubectl', 'get', 'pods', '-n', 'kube-system', '-l', 'app.kubernetes.io/name=aws-fsx-csi-driver'], 
                                   check=True, capture_output=True, text=True)
            print(f"FSx for Lustre CSI driver verification:\n{result.stdout}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to verify FSx for Lustre CSI driver installation: {e}")
            
        # Choose between dynamic provisioning or existing FSx for updates
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] == '':
            # Update StorageClass for dynamic provisioning
            create_dynamic_fsx_resources(response_data)
        else:
            print(f"Using existing FSx for Lustre file system with ID: {os.environ.get('FSX_FILE_SYSTEM_ID', 'Not provided')}")
            if 'FSX_FILE_SYSTEM_ID' in os.environ:
                response_data["FSxVolumeId"] = os.environ['FSX_FILE_SYSTEM_ID']
                # Update Kubernetes resources for existing FSx file system
                create_existing_fsx_resources(response_data)
            
        return response_data

    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed: {e.cmd}. Return code: {e.returncode}")
    except Exception as e:
        raise Exception(f"Failed to update AWS FSx CSI driver: {str(e)}")


def on_delete(event):
    """
    Handle Delete request to uninstall the AWS FSx CSI driver
    """
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "FSx CSI driver uninstalled successfully"
        }

        # Verify required environment variables
        required_env_vars = [
            'CLUSTER_NAME',
            'AWS_REGION'
        ]
        
        for var in required_env_vars:
            if var not in os.environ:
                raise ValueError(f"Missing required environment variable: {var}")

        # Configure kubectl using boto3
        write_kubeconfig(os.environ['CLUSTER_NAME'], os.environ['AWS_REGION'])

        # Delete Kubernetes resources (both for dynamic and existing FSx)
        # For existing FSx, use unique names; for dynamic, use standard names
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] != '':
            # Existing FSx - use unique names
            resource_suffix = os.environ['FSX_FILE_SYSTEM_ID'].replace('fs-', '')[:8]
            pod_name = f"fsx-app-{resource_suffix}"
            pvc_name = f"fsx-claim-{resource_suffix}"
            pv_name = f"fsx-pv-{resource_suffix}"
            sc_name = f"fsx-sc-{resource_suffix}"
        else:
            # Dynamic provisioning - use standard names
            pod_name = "fsx-app"
            pvc_name = "fsx-claim"
            pv_name = "fsx-pv"
            sc_name = "fsx-sc"
            
        try:
            print(f"Deleting sample Pod {pod_name}...")
            subprocess.run(['kubectl', 'delete', 'pod', pod_name, '--ignore-not-found=true'], check=True)
            print("Successfully deleted Pod")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to delete Pod: {e}")
            
        try:
            print(f"Deleting PersistentVolumeClaim {pvc_name}...")
            subprocess.run(['kubectl', 'delete', 'pvc', pvc_name, '-n', 'default', '--ignore-not-found=true'], check=True)
            print("Successfully deleted PVC")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to delete PVC: {e}")
            
        # Only delete PV for existing FSx (dynamic provisioning handles PV automatically)
        if 'FSX_FILE_SYSTEM_ID' in os.environ and os.environ['FSX_FILE_SYSTEM_ID'] != '':
            try:
                print(f"Deleting PersistentVolume {pv_name}...")
                subprocess.run(['kubectl', 'delete', 'pv', pv_name, '--ignore-not-found=true'], check=True)
                print("Successfully deleted PV")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Failed to delete PV: {e}")
                
        try:
            print(f"Deleting StorageClass {sc_name}...")
            subprocess.run(['kubectl', 'delete', 'storageclass', sc_name, '--ignore-not-found=true'], check=True)
            print("Successfully deleted StorageClass")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to delete StorageClass: {e}")
            
        # Delete the IAM service account
        try:
            print("Deleting IAM service account...")
            subprocess.run(['eksctl', 'delete', 'iamserviceaccount',
                          '--name', 'fsx-csi-controller-sa',
                          '--namespace', 'kube-system',
                          '--cluster', os.environ['CLUSTER_NAME'],
                          '--region', os.environ['AWS_REGION']], check=True)
            print("Successfully deleted IAM service account")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to delete service account: {e}")

        return response_data

    except Exception as e:
        print(f"Error during deletion: {str(e)}")
        # Return SUCCESS anyway to allow stack deletion to proceed
        return {
            "Status": "SUCCESS",
            "Reason": f"Proceeding with deletion despite error: {str(e)}"
        }
