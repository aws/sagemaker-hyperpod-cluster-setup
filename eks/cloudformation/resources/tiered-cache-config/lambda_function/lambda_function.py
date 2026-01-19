import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import json
import re
 
# General environment variables
HYPERPOD_CLUSTER_NAME = 'HYPERPOD_CLUSTER_NAME'
CLUSTER_NAME = 'CLUSTER_NAME'
REGION = 'REGION'
TESTING = 'TESTING'
IS_INF_OPERATOR_ADDON_ENABLED = 'IS_INF_OPERATOR_ADDON_ENABLED'
 
# New JSON configuration environment variables
TIERED_KV_CACHE_CONFIG = 'TIERED_KV_CACHE_CONFIG'
TIERED_STORAGE_CONFIG = 'TIERED_STORAGE_CONFIG'
 
# Legacy environment variables (kept for backward compatibility if needed)
KV_CACHE_MEMORY_BUFFER_GB = 'KV_CACHE_MEMORY_BUFFER_GB'
NVME_CAPACITY = 'NVME_CAPACITY'
NVME_PATH = 'NVME_PATH'
 
# Kubernetes resource names
AI_TOOLKIT_NAMESPACE = "aws-hyperpod"
AI_TOOLKIT_CONFIGMAP = "ai-toolkit-config"
AI_TOOLKIT_DAEMONSET = "ai-toolkit"
 
# Instance type memory mappings (in GiB)
INSTANCE_TYPE_MEMORY = {
    # P6 instances
    'ml.p6-b300.48xlarge': 4096,
    'ml.p6-b200.48xlarge': 2048,
    'ml.p6e-gb200.36xlarge': 960,
 
    # P5 instances
    'ml.p5.4xlarge': 256,
    'ml.p5.48xlarge': 2048,
    'ml.p5e.48xlarge': 2048,
    'ml.p5en.48xlarge': 2048,
 
    # P4 instances
    'ml.p4d.24xlarge': 1152,
    'ml.p4de.24xlarge': 1152,
 
    # G5 instances (only for testing)
    'ml.g5.8xlarge': 128,
}
 
 
# ============================================================================
# Cluster Information Functions
# ============================================================================
 
# Update get_cluster_info function:
 
def get_cluster_info(hyperpod_cluster_name, eks_cluster_name, region):
    """
    Get comprehensive cluster information from both EKS and SageMaker
    Returns a dict with EKS cluster details and SageMaker instance groups
    """
    print(f"\n=== Fetching Cluster Information ===")
    print(f"HyperPod Cluster: {hyperpod_cluster_name}")
    print(f"EKS Cluster: {eks_cluster_name}")
    print(f"Region: {region}")
 
    cluster_info = {}
 
    # Get EKS cluster details
    try:
        eks = boto3.client('eks', region_name=region)
        eks_response = eks.describe_cluster(name=eks_cluster_name)
        cluster_info['eks'] = eks_response['cluster']
        print(f"  ✓ Retrieved EKS cluster details for '{eks_cluster_name}'")
    except ClientError as e:
        raise Exception(f"Failed to get EKS cluster info for '{eks_cluster_name}': {str(e)}")
 
    # Get SageMaker HyperPod cluster details (for instance groups)
    try:
        sagemaker = boto3.client('sagemaker', region_name=region)
        sm_response = sagemaker.describe_cluster(ClusterName=hyperpod_cluster_name)
        cluster_info['instance_groups'] = sm_response.get('InstanceGroups', [])
        print(f"  ✓ Retrieved SageMaker instance groups for '{hyperpod_cluster_name}'")
        print(f"    Available instance groups: {[ig['InstanceGroupName'] for ig in cluster_info['instance_groups']]}")
    except ClientError as e:
        raise Exception(f"Failed to get SageMaker HyperPod cluster info for '{hyperpod_cluster_name}': {str(e)}")
 
    return cluster_info
 
 
# ============================================================================
# Configuration Parsing Functions
# ============================================================================
 
def parse_config_from_env(cluster_info):
    """
    Parse configuration from JSON environment variables
    Returns a dict with all configuration values
    """
    print("\n=== Parsing Configuration ===")
 
    config = {
        'kv_cache_enabled': False,
        'nvme_enabled': False,
        'tiered_storage_enabled': False,
        'memory_percentage': 20,
        'instance_type': None,
        'instance_groups': []
    }
 
    # Parse TIERED_KV_CACHE_CONFIG
    kv_cache_config_str = os.environ.get(TIERED_KV_CACHE_CONFIG)
    if kv_cache_config_str:
        print(f"Found TIERED_KV_CACHE_CONFIG: {kv_cache_config_str}")
        try:
            kv_cache_config = json.loads(kv_cache_config_str)
 
            # KVCacheMode → KV_CACHE_ENABLED
            kv_cache_mode = kv_cache_config.get('KVCacheMode', 'Disable')
            config['kv_cache_enabled'] = kv_cache_mode.lower() == 'enable'
            print(f"  KVCacheMode: {kv_cache_mode} → kv_cache_enabled={config['kv_cache_enabled']}")
 
            # NVMeMode → NVME_ENABLED
            nvme_mode = kv_cache_config.get('NVMeMode', 'Disable')
            config['nvme_enabled'] = nvme_mode.lower() == 'enable'
            print(f"  NVMeMode: {nvme_mode} → nvme_enabled={config['nvme_enabled']}")
 
            # InstanceGroup → TARGET_INSTANCE_GROUP_INSTANCE_TYPE
            instance_groups = kv_cache_config.get('InstanceGroup', [])
            if instance_groups:
                config['instance_groups'] = instance_groups
                # Get instance type from the first instance group
                instance_group_name = instance_groups[0]
                print(f"  InstanceGroup: {instance_groups}")
                print(f"  Fetching instance type for instance group: {instance_group_name}")
                config['instance_type'] = get_instance_type_from_instance_group(
                    cluster_info['instance_groups'],
                    instance_group_name
                )
 
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse TIERED_KV_CACHE_CONFIG: {str(e)}")
        except Exception as e:
            raise Exception(f"Error processing TIERED_KV_CACHE_CONFIG: {str(e)}")
    else:
        print("TIERED_KV_CACHE_CONFIG not found")
 
    # Parse TIERED_STORAGE_CONFIG
    storage_config_str = os.environ.get(TIERED_STORAGE_CONFIG)
    if storage_config_str:
        print(f"Found TIERED_STORAGE_CONFIG: {storage_config_str}")
        try:
            storage_config = json.loads(storage_config_str)
 
            # Mode → TIERED_STORAGE_ENABLED
            mode = storage_config.get('Mode', 'Disable')
            config['tiered_storage_enabled'] = mode.lower() == 'enable'
            print(f"  Mode: {mode} → tiered_storage_enabled={config['tiered_storage_enabled']}")
 
            # InstanceMemoryAllocationPercentage → KV_CACHE_MEMORY_PERCENTAGE
            memory_percentage = storage_config.get('InstanceMemoryAllocationPercentage', 20)
            config['memory_percentage'] = memory_percentage
            print(f"  InstanceMemoryAllocationPercentage: {memory_percentage}%")
 
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse TIERED_STORAGE_CONFIG: {str(e)}")
        except Exception as e:
            raise Exception(f"Error processing TIERED_STORAGE_CONFIG: {str(e)}")
    else:
        print("TIERED_STORAGE_CONFIG not found")
 
    print("\n=== Configuration Summary ===")
    print(f"  Tiered Storage Enabled: {config['tiered_storage_enabled']}")
    print(f"  KV Cache Enabled: {config['kv_cache_enabled']}")
    print(f"  NVMe Enabled: {config['nvme_enabled']}")
    print(f"  Memory Percentage: {config['memory_percentage']}%")
    print(f"  Instance Type: {config['instance_type']}")
    print(f"  Instance Groups: {config['instance_groups']}")
 
    return config
 
 
def get_instance_type_from_instance_group(instance_groups, instance_group_name):
    """
    Get instance type from instance group name using pre-fetched instance groups
    """
    # Find the instance group
    for ig in instance_groups:
        if ig['InstanceGroupName'] == instance_group_name:
            instance_type = ig['InstanceType']
            print(f"    Found instance type: {instance_type}")
            return instance_type
 
    raise Exception(
        f"Instance group '{instance_group_name}' not found in cluster. "
        f"Available instance groups: {[ig['InstanceGroupName'] for ig in instance_groups]}"
    )
 
 
# ============================================================================
# Memory Calculation Functions
# ============================================================================
 
def get_instance_type_memory(instance_type):
    """
    Get total memory for an instance type in GiB from hardcoded mapping
    In production mode (TESTING=false), only P-series instances are allowed
    In testing mode (TESTING=true), any instance type in the mapping is allowed
    """
    testing_mode = os.environ.get(TESTING, 'false').lower() == 'true'
 
    # Check if instance type exists in our mapping
    if instance_type not in INSTANCE_TYPE_MEMORY:
        raise Exception(
            f"Instance type '{instance_type}' not found in supported instance types. "
            f"Supported types: {', '.join(sorted(INSTANCE_TYPE_MEMORY.keys()))}"
        )
 
    # In testing mode, allow any instance type that's in the mapping
    if testing_mode:
        memory_gib = INSTANCE_TYPE_MEMORY[instance_type]
        print(f"TESTING mode: Using instance type '{instance_type}' with {memory_gib} GiB memory")
        return memory_gib
 
    # In production mode, only allow P-series instances
    instance_type_lower = instance_type.lower()
    is_p_series = instance_type_lower.startswith('ml.p')
 
    if not is_p_series:
        raise Exception(
            f"Instance type '{instance_type}' is not supported. "
            f"KV caching is only supported on P-series instances (ml.p*). "
        )
 
    memory_gib = INSTANCE_TYPE_MEMORY[instance_type]
    print(f"Validated P-series instance '{instance_type}' with {memory_gib} GiB memory")
    return memory_gib
 
 
def format_memory_value_for_config(memory_gib):
    """
    Format memory value for ConfigMap (TOML format)
    Rounds down to whole GiB to ensure it's a multiple of block_size
    """
    # Round down to whole number
    memory_gib = int(memory_gib)
 
    if memory_gib >= 1024:
        memory_tib = memory_gib / 1024
        if memory_tib == int(memory_tib):
            return f"{int(memory_tib)}TiB"
        return f"{memory_gib}GiB"
    elif memory_gib >= 1:
        return f"{memory_gib}GiB"
    else:
        return f"{int(memory_gib * 1024)}MiB"
 
 
def format_memory_value_for_k8s(memory_gib):
    """
    Format memory value for Kubernetes resources (DaemonSet)
    Kubernetes requires integer values, so we convert to Mi for precision
    """
    # Convert to MiB for precision (avoids decimal issues)
    memory_mib = int(memory_gib * 1024)
 
    # For very large values, use Gi if it's a whole number
    if memory_mib >= 1024 and memory_mib % 1024 == 0:
        memory_gi = memory_mib // 1024
        if memory_gi >= 1024:
            # Use Ti for very large values (if whole number)
            if memory_gi % 1024 == 0:
                return f"{memory_gi // 1024}Ti"
            return f"{memory_gi}Gi"
        return f"{memory_gi}Gi"
 
    # Default to Mi for precision
    return f"{memory_mib}Mi"
 
 
def calculate_memory_allocation_and_cache_capacity(instance_type, memory_percentage):
    """
    Calculate memory allocation and cache capacity based on instance type and percentage
    Returns separate formats for ConfigMap (TOML) and DaemonSet (K8s resources)
    """
    try:
        percentage = float(memory_percentage)
        if percentage <= 0 or percentage > 100:
            raise ValueError(f"Invalid percentage: {percentage}")
    except ValueError as e:
        raise Exception(f"Invalid memory percentage value '{memory_percentage}': {str(e)}")
 
    print(f"\n=== Memory Calculation ===")
    print(f"Instance type: {instance_type}")
    print(f"Memory percentage: {percentage}%")
 
    total_memory_gib = get_instance_type_memory(instance_type)
 
    memory_allocation_gib = total_memory_gib * (percentage / 100)
    memory_allocation_k8s_str = format_memory_value_for_k8s(memory_allocation_gib)
 
    print(f"\nStep 1: Memory Allocation")
    print(f"  Total instance memory: {total_memory_gib:.2f} GiB")
    print(f"  Allocation ({percentage}%): {memory_allocation_gib:.2f} GiB")
    print(f"  For DaemonSet: {memory_allocation_k8s_str}")
 
    cache_capacity_gib = memory_allocation_gib
    buffer_gb_str = os.environ.get(KV_CACHE_MEMORY_BUFFER_GB, '1')
 
    try:
        buffer_gib = float(buffer_gb_str)
        if buffer_gib < 0:
            raise ValueError(f"Buffer cannot be negative: {buffer_gib}")
    except ValueError as e:
        raise Exception(f"Invalid KV_CACHE_MEMORY_BUFFER_GB value '{buffer_gb_str}': {str(e)}")
 
    if buffer_gib > 0:
        cache_capacity_gib = memory_allocation_gib - buffer_gib
        print(f"\nStep 2: Cache Capacity (for ConfigMap)")
        print(f"  Memory allocation: {memory_allocation_gib:.2f} GiB")
        print(f"  Buffer: {buffer_gib} GiB")
        print(f"  Cache capacity (before rounding): {cache_capacity_gib:.2f} GiB")
    else:
        print(f"\nStep 2: Cache Capacity (for ConfigMap)")
        print(f"  No buffer specified, using full allocation")
        print(f"  Cache capacity (before rounding): {cache_capacity_gib:.2f} GiB")
 
    if cache_capacity_gib <= 0:
        raise Exception(
            f"Calculated cache capacity is invalid: {cache_capacity_gib:.2f} GiB "
            f"(allocation: {memory_allocation_gib:.2f} GiB, buffer: {buffer_gib} GiB)"
        )
 
    cache_capacity_config_str = format_memory_value_for_config(cache_capacity_gib)
    cache_capacity_gib_rounded = int(cache_capacity_gib)  # Store rounded value
    print(f"  Cache capacity (rounded down): {cache_capacity_config_str}")
 
    print(f"\n=== Summary ===")
    print(f"  DaemonSet memory: {memory_allocation_k8s_str}")
    print(f"  ConfigMap capacity: {cache_capacity_config_str}")
 
    return {
        'memory_allocation_k8s_str': memory_allocation_k8s_str,
        'memory_allocation_gib': memory_allocation_gib,
        'cache_capacity_config_str': cache_capacity_config_str,
        'cache_capacity_gib': cache_capacity_gib_rounded  # Return rounded value
    }
 
 
# ============================================================================
# KV Cache Configuration Functions
# ============================================================================
 
def prepare_configmap_updates(cache_capacity_str, add_nvme):
    """
    Prepare all ConfigMap updates
    """
    def apply_updates(current_config):
        updated_config = current_config
        changes = []
 
        pattern = r'(capacity\s*=\s*)"[^"]*"(\s*#\s*Total in-memory cache size)'
        replacement = f'\\1"{cache_capacity_str}"\\2'
        new_config = re.sub(pattern, replacement, updated_config)
 
        if new_config != updated_config:
            updated_config = new_config
            changes.append(f"cache_capacity={cache_capacity_str}")
            print(f"  Prepared cache capacity update: {cache_capacity_str}")
        else:
            print(f"  Warning: Cache capacity pattern not found in config")
 
        if add_nvme:
            if "cache.ssd.directory" not in updated_config:
                nvme_capacity = os.environ.get(NVME_CAPACITY, '100GiB')
                nvme_path = os.environ.get(NVME_PATH, '/tmp/ai-toolkit-kvcache')
 
                nvme_config = f"""
[[cache.ssd.directory]]
path = "{nvme_path}"
# Size of each shard file
shard_size = "64MiB"
# Total size of the on-disk cache
capacity = "{nvme_capacity}"
"""
                pattern = r"(\n# Logging configuration\n\[log\])"
                new_config = re.sub(pattern, nvme_config + r"\1", updated_config)
 
                if new_config != updated_config:
                    updated_config = new_config
                    changes.append("nvme_config")
                    print(f"  Prepared NVMe SSD configuration: {nvme_capacity}")
                else:
                    print(f"  Warning: Could not find insertion point for NVMe config")
            else:
                print(f"  NVMe SSD configuration already exists, skipping")
 
        return updated_config, changes
 
    return apply_updates
 
 
def prepare_daemonset_updates(namespace, daemonset_name, instance_groups, instance_type, memory_str):
    """
    Prepare all DaemonSet updates
    """
    changes = []
 
    try:
        result = subprocess.run(
            ["kubectl", "get", "daemonset", daemonset_name, "-n", namespace, "-o", "json"],
            check=True,
            capture_output=True,
            text=True
        )
 
        ds_data = json.loads(result.stdout)
 
        patch = {
            "spec": {
                "template": {
                    "spec": {}
                }
            }
        }
 
        # Update node selector based on instance groups
        current_node_selector = ds_data.get('spec', {}).get('template', {}).get('spec', {}).get('nodeSelector', {})
        new_node_selector = current_node_selector.copy()
 
        # If we have instance groups, use instance group selector
        # If multiple instance groups, we'll need to use node affinity instead
        if instance_groups and len(instance_groups) == 1:
            # Single instance group - use simple node selector
            instance_group = instance_groups[0]
            new_node_selector["sagemaker.amazonaws.com/instance-group-name"] = instance_group
            # Keep instance type as secondary selector for validation
            new_node_selector["node.kubernetes.io/instance-type"] = instance_type
            patch["spec"]["template"]["spec"]["nodeSelector"] = new_node_selector
            changes.append(f"node_selector=instance_group:{instance_group},instance_type:{instance_type}")
            print(f"  Prepared node selector: instance-group={instance_group}, instance-type={instance_type}")
 
        elif instance_groups and len(instance_groups) > 1:
            # Multiple instance groups - use node affinity with In operator
            print(f"  Preparing node affinity for multiple instance groups: {instance_groups}")
 
            # Remove instance-group-name from nodeSelector if it exists
            new_node_selector.pop("sagemaker.amazonaws.com/instance-group-name", None)
            # Keep instance type in nodeSelector
            new_node_selector["node.kubernetes.io/instance-type"] = instance_type
 
            patch["spec"]["template"]["spec"]["nodeSelector"] = new_node_selector
 
            # Add node affinity for instance groups
            patch["spec"]["template"]["spec"]["affinity"] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{
                            "matchExpressions": [{
                                "key": "sagemaker.amazonaws.com/instance-group-name",
                                "operator": "In",
                                "values": instance_groups
                            }]
                        }]
                    }
                }
            }
            changes.append(f"node_affinity=instance_groups:{','.join(instance_groups)},instance_type:{instance_type}")
            print(f"  Prepared node affinity: instance-groups={instance_groups}, instance-type={instance_type}")
        else:
            # Fallback to instance type only (shouldn't happen with valid config)
            new_node_selector["node.kubernetes.io/instance-type"] = instance_type
            patch["spec"]["template"]["spec"]["nodeSelector"] = new_node_selector
            changes.append(f"node_selector=instance_type:{instance_type}")
            print(f"  Prepared node selector: instance-type={instance_type} (no instance group specified)")
 
        # Update container resources (existing code)
        current_containers = ds_data.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
 
        ai_toolkit_container = None
        for container in current_containers:
            if container.get('name') == 'ai-toolkit':
                ai_toolkit_container = container.copy()
                break
 
        if ai_toolkit_container:
            if 'resources' not in ai_toolkit_container:
                ai_toolkit_container['resources'] = {}
            if 'requests' not in ai_toolkit_container['resources']:
                ai_toolkit_container['resources']['requests'] = {}
            if 'limits' not in ai_toolkit_container['resources']:
                ai_toolkit_container['resources']['limits'] = {}
 
            ai_toolkit_container['resources']['requests']['memory'] = memory_str
            ai_toolkit_container['resources']['limits']['memory'] = memory_str
 
            patch["spec"]["template"]["spec"]["containers"] = [ai_toolkit_container]
            changes.append(f"memory_request={memory_str}")
            print(f"  Prepared memory update: {memory_str}")
            print(f"    Preserving existing CPU requests/limits and other resources")
        else:
            print(f"  Warning: ai-toolkit container not found in DaemonSet")
 
        return patch, changes
 
    except subprocess.CalledProcessError as e:
        if "not found" in e.stderr:
            print(f"  ⚠ DaemonSet '{daemonset_name}' not found in namespace '{namespace}'")
            print(f"  This is expected if the AI toolkit hasn't been deployed yet")
            return {}, []
        else:
            raise Exception(f"Failed to get current DaemonSet: {e.stderr}")
    except (KeyError, json.JSONDecodeError) as e:
        raise Exception(f"Failed to parse DaemonSet configuration: {str(e)}")
 
 
def apply_configmap(namespace, configmap_name, update_function):
    """
    Get ConfigMap, apply updates, and apply once
    """
    print(f"\n=== Updating ConfigMap: {configmap_name} ===")
 
    try:
        result = subprocess.run(
            ["kubectl", "get", "configmap", configmap_name, "-n", namespace, "-o", "json"],
            check=True,
            capture_output=True,
            text=True
        )
 
        cm_data = json.loads(result.stdout)
        current_config = cm_data["data"]["config.toml"]
 
        updated_config, changes = update_function(current_config)
 
        if not changes:
            print("  No ConfigMap changes needed")
            return False, []
 
        if updated_config == current_config:
            print("  No ConfigMap changes detected")
            return False, []
 
        cm_data["data"]["config.toml"] = updated_config
 
        apply_process = subprocess.Popen(
            ["kubectl", "apply", "-f", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = apply_process.communicate(json.dumps(cm_data))
 
        if apply_process.returncode != 0:
            raise Exception(f"Failed to apply ConfigMap: {stderr}")
 
        print(f"  Successfully updated ConfigMap with changes: {', '.join(changes)}")
        return True, changes
 
    except subprocess.CalledProcessError as e:
        if "not found" in e.stderr:
            print(f"  ⚠ ConfigMap '{configmap_name}' not found in namespace '{namespace}'")
            print(f"  This is expected if the AI toolkit hasn't been deployed yet")
            return False, []
        else:
            raise Exception(f"Failed to update ConfigMap: {e.stderr}")
 
 
def apply_daemonset(namespace, daemonset_name, patch):
    """
    Apply all DaemonSet patches at once
    """
    print(f"\n=== Updating DaemonSet: {daemonset_name} ===")
 
    # If patch is empty, it means the DaemonSet doesn't exist
    if not patch:
        print(f"  ⚠ DaemonSet '{daemonset_name}' not found - skipping update")
        print(f"  This is expected if the AI toolkit hasn't been deployed yet")
        return False
 
    try:
        result = subprocess.run(
            ["kubectl", "patch", "daemonset", daemonset_name, "-n", namespace,
             "--type", "strategic", "-p", json.dumps(patch)],
            check=True,
            capture_output=True,
            text=True
        )
 
        print(f"  Successfully updated DaemonSet")
        return True
 
    except subprocess.CalledProcessError as e:
        if "not found" in e.stderr:
            print(f"  ⚠ DaemonSet '{daemonset_name}' not found in namespace '{namespace}'")
            print(f"  This is expected if the AI toolkit hasn't been deployed yet")
            return False
        else:
            raise Exception(f"Failed to update DaemonSet: {e.stderr}")
 
 
def wait_for_daemonset_ready(namespace, daemonset_name, timeout_minutes=10):
    """
    Wait for DaemonSet to have all pods ready
    """
    import time
 
    timeout_seconds = timeout_minutes * 60
    start_time = time.time()
 
    print(f"  Waiting for DaemonSet to be ready (timeout: {timeout_minutes} minutes)...")
 
    while time.time() - start_time < timeout_seconds:
        try:
            result = subprocess.run(
                ["kubectl", "get", "daemonset", daemonset_name, "-n", namespace, "-o", "json"],
                check=True,
                capture_output=True,
                text=True
            )
 
            ds_data = json.loads(result.stdout)
            status = ds_data.get('status', {})
 
            desired = status.get('desiredNumberScheduled', 0)
            ready = status.get('numberReady', 0)
 
            print(f"    Status: {ready}/{desired} pods ready", end='\r')
 
            if desired > 0 and ready == desired:
                print(f"\n  ✓ All {ready} pods are ready!")
                return True
 
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
            print(f"\n  Warning: Error checking status: {e}")
 
        time.sleep(5)
 
    raise Exception(f"Timeout waiting for DaemonSet to be ready after {timeout_minutes} minutes")
 
 
def restart_daemonset_fast(namespace, daemonset_name):
    """
    Fast restart: Delete all DaemonSet pods at once
    Kubernetes will automatically recreate them with new configuration
    """
    print(f"\n=== Fast Restarting DaemonSet: {daemonset_name} ===")
 
    try:
        # Delete all pods managed by this DaemonSet at once
        print("  Deleting all pods simultaneously...")
        subprocess.run(
            ["kubectl", "delete", "pods", "-n", namespace,
             "-l", f"name={daemonset_name}",  # Common label for DaemonSets
             "--grace-period=30"],
            check=True,
            capture_output=True,
            text=True
        )
 
        print("  All pods deleted. Waiting for new pods to be ready...")
 
        # Wait for DaemonSet to be ready
        wait_for_daemonset_ready(namespace, daemonset_name)
 
        print("  ✓ All new pods are ready!")
        return True
 
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to fast restart DaemonSet: {e.stderr}")
 
 
# Update setup_kubeconfig function:
 
def setup_kubeconfig(cluster_info, eks_cluster_name):
    """
    Generate kubeconfig using pre-fetched cluster info
    """
    try:
        eks_cluster = cluster_info['eks']
        cluster_arn = eks_cluster['arn']
 
        kubeconfig = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'clusters': [{
                'cluster': {
                    'server': eks_cluster['endpoint'],
                    'certificate-authority-data': eks_cluster['certificateAuthority']['data']
                },
                'name': eks_cluster_name
            }],
            'contexts': [{
                'context': {
                    'cluster': eks_cluster_name,
                    'user': eks_cluster_name
                },
                'name': cluster_arn
            }],
            'current-context': cluster_arn,
            'preferences': {},
            'users': [{
                'name': eks_cluster_name,
                'user': {
                    'exec': {
                        'apiVersion': 'client.authentication.k8s.io/v1beta1',
                        'command': 'aws-iam-authenticator',
                        'args': ['token', '-i', eks_cluster_name]
                    }
                }
            }]
        }
 
        kubeconfig_dir = '/tmp/.kube'
        os.makedirs(kubeconfig_dir, exist_ok=True)
        kubeconfig_path = os.path.join(kubeconfig_dir, 'config')
 
        with open(kubeconfig_path, 'w') as f:
            yaml.dump(kubeconfig, f, default_flow_style=False)
 
        os.chmod(kubeconfig_path, 0o600)
        os.environ['KUBECONFIG'] = kubeconfig_path
 
        return True
 
    except (KeyError, Exception) as e:
        print(f"Error setting up kubeconfig: {str(e)}")
        raise
 
 
def configure_kv_cache(cluster_info):
    """
    Main function to configure KV cache
    """
    print("\n" + "="*60)
    print("=== Starting KV Cache Configuration ===")
    print("="*60)
 
    all_changes = []
 
    try:
        # Parse configuration from JSON environment variables
        config = parse_config_from_env(cluster_info)
 
        # Check if KV cache is enabled
        if not (config['tiered_storage_enabled'] and config['kv_cache_enabled']):
            print("\nKV cache configuration is disabled")
            return {
                "KVCacheConfigured": False,
                "Reason": "KV cache not enabled (check TIERED_STORAGE_CONFIG.Mode and TIERED_KV_CACHE_CONFIG.KVCacheMode)"
            }
 
        # Validate instance type is set
        if not config['instance_type']:
            raise Exception("Instance type could not be determined from InstanceGroup")
 
        namespace = AI_TOOLKIT_NAMESPACE
        configmap_name = AI_TOOLKIT_CONFIGMAP
        daemonset_name = AI_TOOLKIT_DAEMONSET
 
        instance_type = config['instance_type']
 
        memory_calc = calculate_memory_allocation_and_cache_capacity(
            instance_type,
            config['memory_percentage']
        )
 
        print(f"\n{'='*60}")
        print("=== Preparing ConfigMap Updates ===")
        print(f"{'='*60}")
 
        configmap_update_func = prepare_configmap_updates(
            memory_calc['cache_capacity_config_str'],
            config['nvme_enabled']
        )
 
        configmap_changed, configmap_changes = apply_configmap(
            namespace,
            configmap_name,
            configmap_update_func
        )
 
        if configmap_changed:
            all_changes.extend(configmap_changes)
 
        print(f"\n{'='*60}")
        print("=== Preparing DaemonSet Updates ===")
        print(f"{'='*60}")
 
        daemonset_patch, daemonset_changes = prepare_daemonset_updates(
            namespace,
            daemonset_name,
            config['instance_groups'],  # Pass instance groups
            instance_type,
            memory_calc['memory_allocation_k8s_str']  # Use K8s format here
        )
 
        daemonset_changed = apply_daemonset(
            namespace,
            daemonset_name,
            daemonset_patch
        )
 
        if daemonset_changed:
            all_changes.extend(daemonset_changes)
 
        if configmap_changed or daemonset_changed:
            print(f"\n{'='*60}")
            print(f"=== Summary: {len(all_changes)} changes made ===")
            for i, change in enumerate(all_changes, 1):
                print(f"  {i}. {change}")
            print(f"{'='*60}")
 
            # Fast restart for both ConfigMap and DaemonSet changes
            print("\n=== Fast Restart: Updating All Pods Simultaneously ===")
            restart_daemonset_fast(namespace, daemonset_name)
        else:
            print("\n" + "="*60)
            print("=== No configuration changes needed ===")
            print("="*60)
 
        print("\n" + "="*60)
        print("=== KV Cache Configuration Complete ===")
        print("="*60 + "\n")
 
        return {
            "KVCacheConfigured": True,
            "ConfigChanged": bool(all_changes),
            "ChangesMade": all_changes,
            "InstanceType": instance_type,
            "InstanceGroups": config['instance_groups'],
            "MemoryAllocation": memory_calc['memory_allocation_k8s_str'],
            "MemoryAllocationGiB": memory_calc['memory_allocation_gib'],
            "MemoryPercentage": config['memory_percentage'],
            "CacheCapacity": memory_calc['cache_capacity_config_str'],
            "CacheCapacityGiB": memory_calc['cache_capacity_gib'],
            "NVMeEnabled": config['nvme_enabled'],
            "Reason": "KV cache configuration completed successfully"
        }
 
    except Exception as e:
        print(f"\nError configuring KV cache: {str(e)}")
        return {
            "KVCacheConfigured": False,
            "ConfigChanged": bool(all_changes),
            "ChangesMade": all_changes,
            "Reason": f"KV cache configuration failed: {str(e)}"
        }
 
 
# ============================================================================
# Cluster Update Functions
# ============================================================================
 
def wait_for_ai_toolkit_deployment(namespace="aws-hyperpod", configmap_name="ai-toolkit-config", daemonset_name="ai-toolkit", timeout_minutes=5):
    """
    Wait for AI toolkit to be deployed after cluster update
    Checks for ConfigMap existence, DaemonSet readiness, and pod Running status
    """
    import time
    
    timeout_seconds = timeout_minutes * 60
    start_time = time.time()
    
    print(f"\n=== Waiting for AI Toolkit Deployment ===")
    print(f"Checking for ConfigMap '{configmap_name}' in namespace '{namespace}'")
    print(f"Timeout: {timeout_minutes} minutes")
    
    # Step 1: Wait for ConfigMap to exist
    configmap_found = False
    while time.time() - start_time < timeout_seconds:
        try:
            result = subprocess.run(
                ["kubectl", "get", "configmap", configmap_name, "-n", namespace],
                check=True,
                capture_output=True,
                text=True
            )
            
            print(f"  ✓ AI toolkit ConfigMap found!")
            configmap_found = True
            break
            
        except subprocess.CalledProcessError:
            elapsed = int(time.time() - start_time)
            print(f"  Waiting for ConfigMap... ({elapsed}s elapsed)", end='\r')
            time.sleep(10)  # Check every 10 seconds
    
    if not configmap_found:
        print(f"\n  ⚠ Timeout after {timeout_minutes} minutes - ConfigMap not found")
        return False
    
    # Step 2: Wait for DaemonSet to be ready and pods to be Running
    print(f"  Checking for DaemonSet '{daemonset_name}' to be ready...")
    
    while time.time() - start_time < timeout_seconds:
        try:
            # Check DaemonSet status
            result = subprocess.run(
                ["kubectl", "get", "daemonset", daemonset_name, "-n", namespace, "-o", "json"],
                check=True,
                capture_output=True,
                text=True
            )
            
            ds_data = json.loads(result.stdout)
            status = ds_data.get('status', {})
            
            desired = status.get('desiredNumberScheduled', 0)
            ready = status.get('numberReady', 0)
            
            elapsed = int(time.time() - start_time)
            print(f"  DaemonSet status: {ready}/{desired} pods ready ({elapsed}s elapsed)", end='\r')
            
            # If DaemonSet shows all pods ready, verify pod STATUS is "Running"
            if desired > 0 and ready == desired:
                # Get actual pod status
                pod_result = subprocess.run(
                    ["kubectl", "get", "pods", "-n", namespace, "-l", f"name={daemonset_name}", "-o", "json"],
                    check=True,
                    capture_output=True,
                    text=True
                )
                
                pods_data = json.loads(pod_result.stdout)
                pods = pods_data.get('items', [])
                
                # Check if all pods are in Running status
                all_running = True
                pod_statuses = []
                for pod in pods:
                    pod_name = pod.get('metadata', {}).get('name', 'unknown')
                    pod_status = pod.get('status', {}).get('phase', 'Unknown')
                    pod_statuses.append(f"{pod_name}:{pod_status}")
                    
                    if pod_status != 'Running':
                        all_running = False
                
                if all_running and len(pods) == desired:
                    print(f"\n  ✓ AI toolkit DaemonSet ready! All {ready} pods are Running.")
                    print(f"    Pod statuses: {', '.join(pod_statuses)}")
                    return True
                else:
                    print(f"\n  Waiting for all pods to reach Running status...")
                    print(f"    Current pod statuses: {', '.join(pod_statuses)}")
            
        except subprocess.CalledProcessError:
            elapsed = int(time.time() - start_time)
            print(f"  Waiting for DaemonSet... ({elapsed}s elapsed)", end='\r')
        except (json.JSONDecodeError, KeyError) as e:
            print(f"\n  Warning: Error parsing DaemonSet status: {e}")
        
        time.sleep(10)  # Check every 10 seconds
    
    print(f"\n  ⚠ Timeout after {timeout_minutes} minutes - DaemonSet not ready")
    return False
 
 
def update_cluster(hyperpod_cluster_name, region, config):
    """
    Update the SageMaker HyperPod cluster to apply tiered cache configuration using boto3
    Only performs update if tiered storage is enabled
    """
    print(f"\n=== Updating HyperPod Cluster ===")
    print(f"Cluster Name: {hyperpod_cluster_name}")
    print(f"Region: {region}")
    
    # Check if tiered storage is enabled
    if not config['tiered_storage_enabled']:
        print(f"  ℹ Tiered storage is disabled, skipping cluster update")
        return {
            'Updated': False,
            'Reason': 'Tiered storage not enabled, no cluster update needed',
            'TieredStorageEnabled': False
        }
    
    try:
        # Build tiered storage config from parsed configuration
        tiered_storage_config = {
            'Mode': 'Enable',
            'InstanceMemoryAllocationPercentage': config['memory_percentage']
        }
 
        print(f"  Tiered Storage Config: {tiered_storage_config}")
 
        # Create SageMaker client
        sagemaker = boto3.client('sagemaker', region_name=region)
 
        try:
            # Describe cluster to get current configuration
            print(f"  Describing cluster to get current configuration...")
            describe_response = sagemaker.describe_cluster(ClusterName=hyperpod_cluster_name)
            # Atleast one of InstanceGroups or RestrictedInstanceGroups or InstanceGroupsToDelete or NodeRecovery or AutoScaling must be present in the request."
            current_node_recovery = describe_response.get('NodeRecovery', 'Automatic')
            current_tiered_config = describe_response.get('TieredStorageConfig', {})
            cluster_status = describe_response.get('ClusterStatus', 'Unknown')
            
            print(f"  Current Status: {cluster_status}")
            print(f"  Current NodeRecovery: {current_node_recovery}")
            print(f"  Current TieredStorageConfig: {current_tiered_config}")
            
            # Check if update is actually needed
            if current_tiered_config == tiered_storage_config:
                print(f"  ℹ TieredStorageConfig already set to desired value, skipping update")
                return {
                    'Updated': False,
                    'Reason': 'TieredStorageConfig already configured',
                    'TieredStorageConfig': current_tiered_config
                }
            
            # Update cluster with TieredStorageConfig and current NodeRecovery
            print(f"  Updating cluster...")
            response = sagemaker.update_cluster(
                ClusterName=hyperpod_cluster_name,
                NodeRecovery=current_node_recovery,
                TieredStorageConfig=tiered_storage_config
            )
            
            print(f"  ✓ Update successful")
            print(f"  Response: {json.dumps(response, default=str)}")
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_message = e.response['Error']['Message']
            print(f"  ✗ AWS API error [{error_code}]: {error_message}")
            
            # Check if it's a validation error (cluster update not needed)
            if error_code == "ValidationException" or "not supported" in error_message.lower():
                print(f"  ℹ Cluster update not needed or not supported")
                return {
                    'Updated': False,
                    'Reason': f'Cluster update not needed or not supported: {error_message}'
                }
            else:
                raise Exception(f"AWS API failed to update cluster '{hyperpod_cluster_name}': {error_message}")
 
        # Build response data for successful update
        response_data = {
            'TieredStorageConfig': tiered_storage_config,
            'Updated': True,
            'ClusterArn': response.get('ClusterArn'),
            'ClusterStatus': response.get('ClusterStatus')
        }
        
        # Only wait for AI toolkit if KV cache will be configured
        # Skip the wait if we know it will fail (e.g., unsupported instance type)
        try:
            # Quick validation to see if KV cache config will work
            if (config['tiered_storage_enabled'] and 
                config['kv_cache_enabled'] and 
                config['instance_type']):
                # Check if instance type is supported before waiting
                get_instance_type_memory(config['instance_type'])
                # If we get here, instance type is supported, so wait for AI toolkit
                ai_toolkit_ready = wait_for_ai_toolkit_deployment()
            else:
                print(f"  ℹ Skipping AI toolkit wait - KV cache not enabled or configured")
                ai_toolkit_ready = False
        except Exception as validation_error:
            print(f"  ℹ Skipping AI toolkit wait - KV cache validation failed: {str(validation_error)}")
            ai_toolkit_ready = False
        
        response_data['AIToolkitReady'] = ai_toolkit_ready
        
        return response_data
    except Exception as e:
        raise Exception(f"Failed to update cluster '{hyperpod_cluster_name}': {str(e)}")
 
 
# ============================================================================
# Lambda Handler
# ============================================================================
 
# Update lambda_handler:
 
def lambda_handler(event, context):
    """
    Handle CloudFormation custom resource requests for KV cache configuration
    """
    try:
        # Check if this is a CloudFormation event or direct invocation
        is_cfn_event = 'ResponseURL' in event and 'RequestType' in event
        
        if is_cfn_event:
            request_type = event['RequestType']
        else:
            # For direct testing, assume 'Create' operation
            request_type = event.get('RequestType', 'Create')
            print(f"Direct invocation detected, using RequestType: {request_type}")
 
        # Get required environment variables
        hyperpod_cluster_name = os.environ.get(HYPERPOD_CLUSTER_NAME)
        eks_cluster_name = os.environ.get(CLUSTER_NAME)
        region = os.environ.get(REGION)
 
        # Validate required environment variables
        if not hyperpod_cluster_name:
            raise Exception("HYPERPOD_CLUSTER_NAME environment variable is required")
        if not eks_cluster_name:
            raise Exception("CLUSTER_NAME environment variable is required")
        if not region:
            raise Exception("REGION environment variable is required")
 
        # Get comprehensive cluster information (single API call for both EKS and SageMaker)
        cluster_info = get_cluster_info(hyperpod_cluster_name, eks_cluster_name, region)
 
        # Configure kubectl using pre-fetched cluster info
        setup_kubeconfig(cluster_info, eks_cluster_name)
        is_inference_addon_enabled = os.environ.get(IS_INF_OPERATOR_ADDON_ENABLED, 'false').lower() == 'true'
 
        if request_type == 'Create':
            # Parse configuration to get tiered storage settings for cluster update
            config = parse_config_from_env(cluster_info)
            
            # Check if inference add-on integration is enabled
            print(f"\nInference Add-On Enabled: {is_inference_addon_enabled}")
            
            # Only call update_cluster on Create if inference add-on is enabled
            if is_inference_addon_enabled:
                print("  Calling update_cluster for inference add-on integration")
                # Update the cluster first to install AI toolkit with tiered cache configuration
                cluster_update_result = update_cluster(hyperpod_cluster_name, region, config)
            else:
                print("  Skipping update_cluster - inference add-on not enabled")
                cluster_update_result = {
                    'Updated': False,
                    'Reason': 'Inference add-on not enabled, skipping cluster update on Create'
                }
            
            # Then configure the KV cache on the installed AI toolkit
            response_data = configure_kv_cache(cluster_info)
            response_data.update({
                'ClusterUpdateResult': cluster_update_result
            })
        elif request_type == 'Update':
            response_data = configure_kv_cache(cluster_info)
        elif request_type == 'Delete':
            # Parse configuration to get current settings
            config = parse_config_from_env(cluster_info)
            
            # Override to disable tiered storage for cleanup
            config['tiered_storage_enabled'] = False
            
            # Update the cluster to disable tiered cache configuration
            cluster_update_result = update_cluster(hyperpod_cluster_name, region, config)
            
            response_data = {
                "Status": "SUCCESS",
                "Reason": "KV cache configuration disabled successfully",
                "ClusterUpdateResult": cluster_update_result
            }
        else:
            raise ValueError(f"Invalid request type: {request_type}")
 
        # Only send CloudFormation response if this is a CloudFormation event
        if is_cfn_event:
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                response_data
            )
        else:
            # For direct invocation, just return the response data
            print(f"Direct invocation completed successfully")
            print(f"Response data: {json.dumps(response_data, indent=2, default=str)}")
            return response_data
 
    except Exception as e:
        print(f"Error in lambda_handler: {str(e)}")
        
        # Only send CloudFormation response if this is a CloudFormation event
        if 'ResponseURL' in event:
            cfnresponse.send(
                event,
                context,
                cfnresponse.FAILED,
                {
                    "Status": "FAILED",
                    "Reason": f"Lambda completed with errors: {str(e)}",
                    "KVCacheConfigured": False
                }
            )
        else:
            # For direct invocation, re-raise the exception
            print(f"Direct invocation failed")
            raise
