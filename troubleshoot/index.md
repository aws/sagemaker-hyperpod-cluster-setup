# Troubleshooting Guide

This guide helps you diagnose and resolve common issues with your HyperPod deployment.

## Quick Reference Table

| Issue Category | Orchestrator | Subject (Symptom) | Reason | Resolution | Link to Details |
|----------------|--------------|-------------------|--------|------------|-----------------|
| **Deployment** | Common | Cluster creation fails with lifecycle script error | Script syntax errors, missing dependencies, S3 access issues | Review CloudWatch logs, verify S3 access, check script syntax | [Details](#cluster-creation-failed-with-lifecycle-script-execution-error) |
| **Deployment** | Common | EFA health checks did not run successfully | Missing security group self-referencing rule | Add outbound rule allowing all traffic to the security group itself | [Details](#efa-health-checks-did-not-run-successfully) |
| **Deployment** | Common | Cluster is InService but not seeing instances | Continuous Provisioning mode behavior, instance creation failures | Check cluster events for instance creation status and errors | [Details](#cluster-is-inservice-status-but-not-seeing-instances) |
| **Deployment** | Common | SSM session not starting or getting error | SSM plugin not installed, wrong target format, incorrect region | Install SSM plugin, use HyperPod target format, verify region | [Details](#ssm-session-not-starting-or-getting-error) |
| **Node Management** | Slurm | Node not responding / Slurm says node is "down" | Network issues, slurmd daemon stopped, resource exhaustion | Check connectivity, verify slurmd status, check memory/disk | [Details](#node-not-responding--slurm-says-node-is-down) |
| **Node Management** | Common | Node replacement not happening automatically | Auto-recovery disabled, capacity unavailable, quota limits | Check auto-recovery settings, verify capacity, review quotas | [Details](#node-replacement-not-happening-automatically) |
| **Node Management** | Common | Node replacement not happening even after manual trigger | Wrong command syntax, cluster state, IAM permissions, capacity issues | Verify command syntax, check cluster state, review IAM permissions | [Details](#node-replacement-not-happening-even-after-manual-trigger) |
| **Performance** | Common | NCCL timeouts | Network congestion, EFA issues, insufficient timeout value | Increase NCCL_TIMEOUT, verify EFA, check network connectivity | [Details](#nccl-timeouts) |
| **Performance** | Common | Uneven NCCL performance across nodes | Network topology differences, degraded EFA, instance variations | Check EFA bandwidth, verify instance types, use placement groups | [Details](#uneven-nccl-performance-depending-on-the-set-of-nodes) |
| **Performance** | Common | Poor filesystem performance | Insufficient throughput, wrong volume type, I/O bottleneck | Check filesystem metrics, increase throughput, optimize data loading | [Details](#poor-filesystem-performance) |
| **Memory** | Common | DataLoader "Cannot allocate memory" error | Insufficient shared memory (/dev/shm), too many workers | Increase --shm-size, reduce num_workers, check /dev/shm usage | [Details](#multi-process-dataloader-raises-oserror-errno-12-cannot-allocate-memory-error) |
| **Memory** | Common | FI_EFA_USE_HUGE_PAGE=0 required | Huge pages not configured, EFA memory registration fails | Set FI_EFA_USE_HUGE_PAGE=0 or configure huge pages properly | [Details](#fi_efa_use_huge_page0-has-to-be-set) |
| **GPU** | Common | Suspecting GPU failure | Hardware failure, ECC errors, thermal throttling | Run nvidia-smi diagnostics, check ECC errors, drain node | [Details](#suspecting-gpu-failure) |
| **GPU** | Common | GPUs not getting released | Zombie processes, stuck jobs, slurmd issues | Kill lingering processes, restart slurmd, reboot if needed | [Details](#gpus-are-not-getting-released) |
| **GPU** | Common | EFA/NCCL/CUDA/driver version mismatch | Incompatible versions, host/container mismatch | Check version compatibility, rebuild containers with matching versions | [Details](#efanclccudanvidia-driver-version-mismatch) |

## Troubleshooting Details

### Node Not Responding / Slurm Says Node is "Down"

**Orchestrator**: Slurm

**Issue**: Slurm node becomes unresponsive or shows as "down"

**Resolution Steps**:
1. Check node status: `sinfo -N -l` or `scontrol show node <node-name>`
2. If node shows "down" status, check the reason message:
   ```bash
   sinfo -o "%N %T %30E"
   ```
   This will display the node name, state, and reason for the current state
3. Check HyperPod cluster node status:
   - **Via AWS CLI**:
     ```bash
     aws sagemaker list-cluster-nodes --cluster-name <cluster-name>
     ```
   - **Via Management Console**: Navigate to https://console.aws.amazon.com/sagemaker/home#/cluster-management → Select your cluster → View node details
   - Look for node health status, instance state, and any error messages
4. Test connectivity to the node using multiple methods to identify what's working:
   - **PING**: `ping <node-ip-or-hostname>`
   - **Cross-node SSH**: From another node, try `ssh <node-ip-or-hostname>`
   - **SSM Session**: `aws ssm start-session --target <instance-id>`
   - **Slurm srun**: `srun -w <node-name> hostname`
   
   By testing these variations, you can determine which communication paths are functional
5. If you can access the node, check system logs: `sudo journalctl -xe`
6. Verify slurmd daemon is running: `sudo systemctl status slurmd`
7. Check for out-of-memory or disk space issues: `free -h` and `df -h`
8. If disk space is full, identify what is consuming space:
   ```bash
   # Check disk usage by filesystem
   df -h
   
   # Find large directories
   sudo du -h --max-depth=1 / | sort -hr | head -20
   
   # Check common locations for large files
   sudo du -sh /var/log/* | sort -hr
   sudo du -sh /tmp/* | sort -hr
   sudo du -sh /home/*/* | sort -hr
   ```
9. Clean up disk space if needed:
   - Delete old log files: `sudo rm -f /var/log/*.log.* /var/log/*/*.gz`
   - Clear temporary files: `sudo rm -rf /tmp/*`
   - Clean package manager cache: `sudo yum clean all` or `sudo apt-get clean`
   - Remove old container images if using Docker: `docker system prune -a`
10. Restart slurmd if needed: `sudo systemctl restart slurmd`
11. If node remains down, set it back to idle: `scontrol update nodename=<node-name> state=resume`
12. If none of the above steps resolve the issue, reboot the instance:
   ```bash
   aws sagemaker batch-reboot-cluster-nodes \
     --cluster-name <cluster-name> \
     --node-ids <instance-id>
   ```
13. If rebooting doesn't help, replace the node:
   ```bash
   aws sagemaker batch-replace-cluster-nodes \
     --cluster-name <cluster-name> \
     --node-ids <instance-id>
   ```

---

### Cluster Creation Failed with Lifecycle Script Execution Error

**Orchestrator**: Common (Slurm, EKS)

**Issue**: HyperPod cluster creation fails during lifecycle script execution

**Common Causes**:
- Syntax errors in lifecycle scripts
- Missing dependencies or packages
- S3 access issues for script retrieval
- Insufficient permissions for script operations
- Network connectivity problems

**Resolution Steps**:
1. Check CloudWatch logs for the cluster creation process:
   - **Log Group**: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
     - Example: `/aws/sagemaker/Clusters/k8-3/gyazigf6kqq9`
   - **Log Stream**: `LifecycleConfig/<node-group-name>/<instance-id>`
     - Example: `LifecycleConfig/group-g5-8x/i-0df4aefe56f4ef3bc`
   - Look for error messages, stack traces, or failed commands in the logs
2. If logs are not available or empty, verify IAM permissions:
   - Check if the IAM execution role has CloudWatch Logs write permissions
   - Verify the IAM role has permissions to access the S3 bucket where lifecycle scripts are stored:
     - S3 read permissions (s3:GetObject, s3:ListBucket)
     - Confirm the S3 path is correct in cluster configuration
     - Check bucket permissions and IAM role policies
   - Ensure the S3 bucket policy allows the IAM role to read objects
3. Check for updated versions of default lifecycle scripts:
   - The lifecycle script version you're using may have known issues that have been fixed
   - Compare your scripts with the latest versions:
     - **HyperPod EKS**: https://github.com/aws-samples/awsome-distributed-training/tree/main/1.architectures/7.sagemaker-hyperpod-eks/LifecycleScripts/base-config
     - **HyperPod Slurm**: https://github.com/aws-samples/awsome-distributed-training/tree/main/1.architectures/5.sagemaker-hyperpod/LifecycleScripts/base-config
   - Review the commit history for bug fixes and improvements
   - Update to the latest version if available
4. Review script syntax and test locally if possible
5. Verify the script uses Linux line endings (LF, not CRLF):
   - Scripts created on Windows may have CRLF line endings which cause execution failures on Linux
   - Convert to LF using: `dos2unix script.sh` or your text editor's line ending conversion
   - Check line endings: `file script.sh` (should show "ASCII text" not "ASCII text, with CRLF line terminators")
6. Ensure script has proper shebang (e.g., `#!/bin/bash`) and execute permissions

---

### EFA Health Checks Did Not Run Successfully

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Cluster creation fails with error "EFA health checks did not run successfully. Ensure that your VPC and security groups are properly configured before attempting to create a new cluster."

**Common Cause**:
- Security group is missing a self-referencing outbound rule that allows nodes to communicate with each other via EFA

**Resolution Steps**:
1. Identify the security group used for the HyperPod cluster
2. Add the required outbound rules to the security group:
   - **Rule 1 - Intra-SG Communication (Required for EFA)**:
     - Type: All traffic
     - Protocol: All (-1)
     - Destination: The security group itself (self-referencing)
     - Description: Allow traffic within the security group
   
   - **Rule 2 - Internet Access**:
     - Type: All traffic
     - Protocol: All (-1)
     - Destination: 0.0.0.0/0
     - Description: Allow traffic to internet (for AWS API calls, package downloads, etc.)

3. Verify the security group has the following inbound rules:
   - **Intra-SG Communication**:
     - Type: All traffic
     - Protocol: All (-1)
     - Source: The security group itself (self-referencing)

4. Ensure all nodes in the cluster use the same security group
5. After fixing the security group, retry cluster creation

**Reference Configuration**:
See the CloudFormation template at `eks/cloudformation/security-group-template.yaml` for the complete security group setup used by HyperPod.

**Prevention**:
- Always include self-referencing rules (both inbound and outbound) when creating security groups for HyperPod clusters
- Use the provided CloudFormation templates which include proper security group configuration
- Test security group configuration before cluster creation

---

### Cluster is InService Status but Not Seeing Instances

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Cluster shows "InService" status but instances are not visible or not being created

**Common Cause**:
This is expected behavior when using Continuous Provisioning mode. In this mode:
- The cluster transitions to "InService" status before all instances are created
- Instance creation happens asynchronously after the cluster becomes InService
- Instance creation failures are not reported as cluster or instance group creation failures

**Resolution Steps**:
1. Check cluster events for instance creation status:
   - **Via Management Console**: Navigate to https://console.aws.amazon.com/sagemaker/home#/cluster-management → Select your cluster → Events tab
   - **Via AWS CLI**:
     ```bash
     aws sagemaker list-cluster-events --cluster-name <cluster-name>
     ```
   - Look for events related to instance creation, provisioning status, and any error messages
   - **Note**: Cluster events are available for HyperPod EKS. For HyperPod Slurm, this feature is not yet available as of January 2026
2. Verify the cluster provisioning mode:
   ```bash
   aws sagemaker describe-cluster --cluster-name <cluster-name>
   ```
   Look for the provisioning configuration to confirm if Continuous Provisioning is enabled
3. Check HyperPod cluster node status:
   - **Via AWS CLI**:
     ```bash
     aws sagemaker list-cluster-nodes --cluster-name <cluster-name>
     ```
   - **Via Management Console**: Navigate to https://console.aws.amazon.com/sagemaker/home#/cluster-management → Select your cluster → View node details
   - Look for node health status, instance state, and creation timestamps
4. Review CloudWatch logs for instance creation attempts:
   - Log Group: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
   - Check for recent log streams from lifecycle scripts: `LifecycleConfig/<node-group-name>/<instance-id>`
   - Look for errors during instance provisioning or lifecycle script execution
5. If instances are failing to create, check for common issues:
   - Insufficient capacity in the selected availability zones
   - Lifecycle script errors (see [Cluster Creation Failed with Lifecycle Script Execution Error](#cluster-creation-failed-with-lifecycle-script-execution-error))
   - IAM permission issues
   - Service quotas or limits

**Understanding Continuous Provisioning Mode**:
- Allows the cluster to become operational even if some instances fail to provision
- Provides faster cluster availability for partial deployments
- Requires monitoring cluster events and node status to track instance creation progress
- Failed instances can be replaced individually without affecting the overall cluster status

---

### SSM Session Not Starting or Getting Error

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Unable to start SSM session to HyperPod cluster nodes or receiving errors

**Common Causes**:
- SSM plugin not installed on development machine
- Incorrect SSM target name format
- Wrong AWS region configuration

**Resolution Steps**:
1. Install the AWS Systems Manager Session Manager plugin on your development machine:
   - Follow the official installation guide: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
   - Verify installation: `session-manager-plugin --version`

2. Use the correct HyperPod-specific SSM target name format:
   - **Standard format**: `sagemaker-cluster:<cluster-name>_<instance-group-name>-<instance-id>`
   - **Example**: `sagemaker-cluster:my-cluster_worker-group-i-0abc123def456789`
   - **Command**:
     ```bash
     aws ssm start-session --target sagemaker-cluster:<cluster-name>_<instance-group-name>-<instance-id>
     ```
   - **Note**: Do NOT use the EC2 instance ID directly (e.g., `i-0abc123def456789`) - you must use the HyperPod target format

3. Verify the AWS region is correctly configured:
   - Check your AWS CLI profile's default region:
     ```bash
     aws configure get region
     ```
   - Or set the region explicitly using environment variables:
     ```bash
     export AWS_REGION=us-west-2
     export AWS_DEFAULT_REGION=us-west-2
     ```
   - Or specify region in the command:
     ```bash
     aws ssm start-session --target <target> --region us-west-2
     ```
   - Ensure the region matches where your HyperPod cluster is deployed

4. Verify IAM permissions for SSM access:
   - Your IAM user/role needs the following permissions:
     - `ssm:StartSession`
     - `sagemaker:DescribeCluster`
     - `sagemaker:ListClusterNodes`
   - The cluster nodes must have the SSM agent running and proper IAM role attached

5. Check if the instance is running and accessible:
   ```bash
   aws sagemaker list-cluster-nodes --cluster-name <cluster-name>
   ```
   Verify the instance status is "Running" or "InService"

6. Test connectivity with verbose output:
   ```bash
   aws ssm start-session --target <target> --debug
   ```
   Review the debug output for specific error messages

**Common Error Messages**:
- "Target is not connected": Instance may be stopped, SSM agent not running, or network connectivity issues
- "Invalid target": Check the target name format is correct for HyperPod
- "Access denied": Verify IAM permissions for both your user and the instance role
- "Region not found": Ensure AWS region is correctly configured

**SSH over SSM**:
For SSH access using SSM as a transport:
```bash
ssh -i <key-file> <username>@<instance-id> \
  -o ProxyCommand="aws ssm start-session --target sagemaker-cluster:<cluster-name>_<instance-group-name>-%h --document-name AWS-StartSSHSession"
```

**Important**: Before using SSH, you must add your SSH public key to the `~/.ssh/authorized_keys` file on the target node.

You can also configure SSH to use SSM by adding entries to your SSH config file (`~/.ssh/config`):
```
Host my-cluster-controller
  HostName sagemaker-cluster:abcdfe1234_controller-i-0abc123def456789
  User ubuntu
  IdentityFile ~/keys/my-key.pem
  ProxyCommand aws --profile default --region us-west-2 ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p
```

Then connect simply with:
```bash
ssh my-cluster-controller
```

**Helpful Tool**:
For easier SSM session management with HyperPod clusters, consider using the `hyperpod_ssm` tool:
- Repository: https://github.com/shimomut/sagemaker-solutions/tree/main/hyperpod_ssm
- Simplifies SSM target name construction and session management
- Provides convenient commands for listing nodes and starting sessions
- Handles the HyperPod-specific target format automatically

---

### Node Replacement Not Happening Automatically

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Failed nodes are not being automatically replaced by HyperPod

**Resolution Steps**:
1. Check HyperPod cluster auto-recovery settings in SageMaker console or via CLI:
   ```bash
   aws sagemaker describe-cluster --cluster-name <cluster-name>
   ```
   Look for the auto-recovery configuration
2. Verify cluster is not in a failed state that prevents recovery
3. Check cluster events for auto-recovery information:
   - **Via Management Console**: Navigate to https://console.aws.amazon.com/sagemaker/home#/cluster-management → Select your cluster → Events tab
   - **Via AWS CLI**:
     ```bash
     aws sagemaker list-cluster-events --cluster-name <cluster-name>
     ```
   - Look for events related to node health, replacement attempts, and any failures
   - **Note**: Cluster events are available for HyperPod EKS. For HyperPod Slurm, this feature is not yet available as of January 2026
4. Check if HyperPod's health monitoring agent detected an issue and triggered resiliency actions:
   - **Check CloudWatch Logs for health monitoring agent**:
     - Log Group: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
     - Log Stream: `SagemakerHealthMonitoringAgent/<node-group-name>/<instance-id>`
     - Example: `SagemakerHealthMonitoringAgent/group-g5-8x/i-0aa017cbf6c240f3f`
     - Look for detected issues and triggered actions
   - **For HyperPod Slurm**: Check if the node reason message indicates a resiliency action:
     ```bash
     sinfo -o "%N %T %30E"
     ```
     The reason message must be exactly "Action:Reboot" or "Action:Replace" for auto-recovery to trigger
   - **For HyperPod EKS**: Check node labels for resiliency actions:
     ```bash
     kubectl get nodes --show-labels
     kubectl describe node <node-name>
     ```
     Look for the following labels indicating resiliency actions have been triggered:
     - `sagemaker.amazonaws.com/node-health-status: UnschedulablePendingReplacement` - Node is marked for replacement
     - `sagemaker.amazonaws.com/node-health-status: UnschedulablePendingReboot` - Node is marked for reboot
     
     See: https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-resiliency-node-labels.html
5. Review CloudWatch logs for auto-recovery attempts:
   - Log Group: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
   - Check for recent log streams from lifecycle scripts: `LifecycleConfig/<node-group-name>/<instance-id>`
   - If lifecycle script fails during auto-recovery, the new instance cannot be created and auto-recovery will fail
   - Look for error messages in the lifecycle script logs that might prevent successful node replacement
6. Confirm capacity is available for replacement instances in the selected availability zones
7. If you need to immediately recover from the failed instance, trigger manual reboot or replacement:
   - **Manual reboot**:
     ```bash
     aws sagemaker batch-reboot-cluster-nodes \
       --cluster-name <cluster-name> \
       --node-ids <instance-id>
     ```
   - **Manual replacement**:
     ```bash
     aws sagemaker batch-replace-cluster-nodes \
       --cluster-name <cluster-name> \
       --node-ids <instance-id>
     ```

---

### Node Replacement Not Happening Even After Manual Trigger

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Manual node replacement command fails or doesn't complete

**Resolution Steps**:
1. Use the recommended batch commands instead of legacy methods:
   - **Recommended**: Use `batch-replace-cluster-nodes` or `batch-reboot-cluster-nodes` commands
   - **Legacy methods** (not recommended): Setting node status in Slurm or node labels in Kubernetes
   - The new batch commands provide clear success/failure messages indicating whether the service accepted the request
2. Check HyperPod cluster node status:
   - **Via AWS CLI**:
     ```bash
     aws sagemaker list-cluster-nodes --cluster-name <cluster-name>
     ```
   - **Via Management Console**: Navigate to https://console.aws.amazon.com/sagemaker/home#/cluster-management → Select your cluster → View node details
   - Look for node health status, instance state, and any error messages
3. Check cluster events for replacement information:
   - **Via Management Console**: Navigate to https://console.aws.amazon.com/sagemaker/home#/cluster-management → Select your cluster → Events tab
   - **Via AWS CLI**:
     ```bash
     aws sagemaker list-cluster-events --cluster-name <cluster-name>
     ```
   - Look for events related to the replacement request, node status changes, and any error messages
   - **Note**: Cluster events are available for HyperPod EKS. For HyperPod Slurm, this feature is not yet available as of January 2026
4. Verify the replacement command syntax:
   ```bash
   aws sagemaker batch-replace-cluster-nodes \
     --cluster-name <cluster-name> \
     --node-ids <instance-id>
   ```
5. Check the command output for error messages
6. Verify the instance ID is correct and belongs to the cluster:
   ```bash
   aws sagemaker list-cluster-nodes --cluster-name <cluster-name>
   ```
7. Ensure the cluster is in a state that allows node replacement (not in "Creating" or "Deleting" state)
8. Review CloudWatch logs for replacement attempts:
7. Review CloudWatch logs for replacement attempts:
   - Log Group: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
   - Check for recent log streams from lifecycle scripts: `LifecycleConfig/<node-group-name>/<instance-id>`
   - If lifecycle script fails during replacement, the new instance cannot be created and replacement will fail
   - Look for error messages in the lifecycle script logs that might prevent successful node replacement
9. Verify capacity is available for the instance type in the target availability zone

---

## GPU and Accelerator Issues

### Suspecting GPU Failure

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Training jobs fail or produce incorrect results, GPU errors in logs

**Common Symptoms**:
- CUDA errors in application logs
- Training produces NaN or incorrect results
- GPU memory errors or allocation failures
- System crashes during GPU-intensive operations
- High temperatures or thermal throttling

**Diagnostic Steps**:
1. Check GPU status: `nvidia-smi -q` and look for errors
2. Check for ECC errors: `nvidia-smi -q | grep -A 5 "ECC Errors"`
3. Monitor temperature and power: `nvidia-smi dmon -s pucvmet`
4. Run DCGM diagnostic tests for comprehensive validation
5. Run GPU burn tests to stress test under sustained load
6. Monitor for thermal throttling and memory errors during stress tests

**Resolution Steps**:
1. Document baseline thermal and performance characteristics
2. If GPU shows errors or high temperatures, drain the node from scheduler
3. Analyze temperature, power draw, and performance consistency
4. Document GPU serial number, error details, and test results
5. Contact AWS Support for hardware replacement
6. Replace the node once new hardware is available

**Detailed Guides**:
- GPU Stress Testing: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/performance-testing/gpu-stress-testing

---

### (WIP) GPUs Are Not Getting Released

**Orchestrator**: Common (Slurm, EKS)

**Issue**: GPUs remain allocated after job completion

**Resolution Steps**:
1. Check for zombie processes: `nvidia-smi` and look for lingering processes
2. Kill stuck processes: `sudo kill -9 <PID>`
3. Check Slurm job status: `squeue -u <username>` and `sacct -j <job-id>`
4. If job shows as completed but GPU is held, restart slurmd: `sudo systemctl restart slurmd`
5. Clear GPU memory: `sudo fuser -v /dev/nvidia*` then kill processes
6. As last resort, reboot the node

---

### EFA/NCCL/CUDA/Nvidia Driver Version Mismatch

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Training fails with EFA or NCCL errors, performance degradation

**Common Symptoms**:
- NCCL initialization failures
- EFA device not found errors
- CUDA device not initialized
- Unexpected performance drops
- Segmentation faults during distributed training
- Training works on host but fails in container, or vice versa

**Common Causes**:
- Incompatible versions between CUDA, NCCL, EFA, and drivers
- CUDA driver and nvcc compiler version mismatch
- Mismatch between host and container environments
- Missing or incorrectly mounted EFA libraries in containers
- Different PyTorch/TensorFlow versions between host and container

**Diagnostic Steps**:
1. Run PyTorch environment validation to check CUDA, NCCL, MPI availability
2. Run EFA validation script to check EFA installer, libfabric, AWS OFI NCCL versions
3. Check CUDA driver vs compiler version: `nvidia-smi` vs `nvcc --version`
4. Verify NVLink status and topology: `nvidia-smi nvlink --status`
5. Compare versions between host and container environments
6. Check if EFA interfaces are found and properly configured

**Resolution Steps**:
1. Ensure CUDA driver and nvcc compiler versions match
2. Verify version compatibility using the EFA compatibility matrix
3. For containers: mount EFA libraries and devices properly
4. Verify LD_LIBRARY_PATH includes EFA and CUDA libraries
5. Initialize CUDA devices if needed (may require reboot)
6. Match PyTorch/TensorFlow versions between host and container
7. Rebuild containers with compatible versions from the compatibility matrix

**Detailed Guides**:
- PyTorch Environment Validation: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/environment-validation/pytorch-environment-validation
- EFA and Network Stack Validation: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/environment-validation/efa-validation
- Troubleshoot NCCL and CUDA: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/nccl-cuda-validation/Troubleshoot%20NCCL%20and%20CUDA

---

## Performance Issues

### NCCL Timeouts

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Distributed training fails with NCCL timeout errors

**Common Error Messages**:
- "NCCL timeout in call to..."
- "NCCL communicator was aborted"
- "Net/IB : Got completion with error"

**Diagnostic Steps**:
1. Enable NCCL debug logging: `export NCCL_DEBUG=INFO`
2. Verify EFA adapters are working: `fi_info -p efa`
3. Run pairwise NCCL tests between nodes to identify problematic connections
4. Check for security group restrictions blocking inter-node traffic
5. Monitor for test failures or hangs that indicate network issues

**Resolution Steps**:
1. Increase NCCL timeout if needed: `export NCCL_TIMEOUT=3600`
2. Verify EFA is being used: `export FI_EFA_USE_DEVICE_RDMA=1`
3. Optimize NCCL settings: `export NCCL_PROTO=simple` and tune buffer sizes
4. Check and fix security group rules to allow all traffic between nodes
5. Isolate and drain problematic nodes showing low bandwidth
6. Reduce batch size or adjust parallelism if memory pressure exists

**Detailed Guides**:
- NCCL Performance Tests: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/performance-testing/nccl-tests
- Troubleshoot NCCL and CUDA: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/nccl-cuda-validation/Troubleshoot%20NCCL%20and%20CUDA

---

### Uneven NCCL Performance Depending on the Set of Nodes

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Training performance varies significantly based on which nodes are allocated

**Common Causes**:
- Network topology differences between nodes
- Degraded EFA performance on some nodes
- Mixed instance types or generations
- CPU frequency scaling differences

**Diagnostic Steps**:
1. Check network topology: `nvidia-smi topo -m`
2. Verify EFA configuration on all nodes: `fi_info -p efa`
3. Run pairwise NCCL bandwidth tests to identify slow node pairs
4. Check for mixed instance types or generations
5. Monitor for inconsistent results across multiple test runs

**Resolution Steps**:
1. Run comprehensive NCCL all-reduce tests across all nodes
2. Use topology-aware testing scripts to systematically identify bad nodes
3. Check failed jobs and isolate problematic nodes
4. Optimize NCCL environment variables (NCCL_PROTO, NCCL_ALGO)
5. Configure EFA optimization settings and GPU affinity
6. Drain underperforming nodes and use placement groups for consistency

**Detailed Guides**:
- NCCL Performance Tests: https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/performance-testing/nccl-tests

---

### (WIP) Poor Filesystem Performance

**Orchestrator**: Common (Slurm, EKS)

**Issue**: Slow I/O operations, training bottlenecked by data loading

**Resolution Steps**:
1. Check filesystem type and mount options: `mount | grep <mount-point>`
2. For FSx for Lustre:
   - Verify filesystem is in "Available" state
   - Check throughput capacity matches workload needs
   - Monitor CloudWatch metrics for IOPS and throughput
   - Consider increasing filesystem size for more throughput
3. For EBS volumes:
   - Check volume type (gp3, io2 recommended for performance)
   - Monitor EBS burst balance if using gp2
   - Increase IOPS/throughput if needed
4. Test filesystem performance: `dd if=/dev/zero of=testfile bs=1M count=1024`
5. Check for I/O wait: `iostat -x 1`
6. Optimize data loading:
   - Increase DataLoader num_workers
   - Use faster data formats (TFRecord, WebDataset)
   - Enable data caching or prefetching
7. Consider using instance store for temporary data if available

---

### (WIP) Multi-process DataLoader Raises "OSError: [Errno 12] Cannot Allocate Memory" Error

**Orchestrator**: Common (Slurm, EKS)

**Issue**: PyTorch DataLoader with multiple workers fails with memory allocation error

**Common Causes**:
- Insufficient shared memory (/dev/shm) for multi-process communication
- Too many DataLoader workers for available memory
- Large batch sizes combined with many workers

**Resolution Steps**:
1. Increase shared memory size:
   ```bash
   # For Docker containers
   docker run --shm-size=8g ...
   
   # For Kubernetes pods, add to pod spec:
   volumes:
   - name: dshm
     emptyDir:
       medium: Memory
       sizeLimit: 8Gi
   ```
2. Reduce number of DataLoader workers: `num_workers=4` instead of higher values
3. Reduce batch size to lower memory pressure
4. Use `persistent_workers=True` to avoid recreating workers
5. Set `pin_memory=False` if not needed
6. Check available memory: `free -h` and `/dev/shm` usage: `df -h /dev/shm`

---

### (WIP) FI_EFA_USE_HUGE_PAGE=0 Has to Be Set

**Orchestrator**: Common (Slurm, EKS)

**Issue**: EFA initialization fails or training crashes without this setting

**Common Symptoms**:
- "Failed to register memory" errors
- EFA device initialization failures
- Segmentation faults during NCCL operations

**Resolution Steps**:
1. Set environment variable: `export FI_EFA_USE_HUGE_PAGE=0`
2. Add to job script or container environment
3. For persistent setting, add to `/etc/environment` or user profile
4. Verify huge pages configuration: `cat /proc/meminfo | grep Huge`
5. If huge pages are needed for other workloads, configure them properly:
   ```bash
   # Check current huge pages
   cat /proc/sys/vm/nr_hugepages
   # Set huge pages (requires root)
   echo 1024 | sudo tee /proc/sys/vm/nr_hugepages
   ```
6. Consider using `FI_EFA_USE_HUGE_PAGE=1` only if huge pages are properly configured

---

## Getting Help

### Collecting Diagnostic Data for Issue Reporting

**Orchestrator**: Common (Slurm, EKS)

When reporting issues to AWS Support, providing comprehensive diagnostic data helps expedite troubleshooting and resolution. 

**Recommended Tool**:
Use the `hyperpod_issue_report` tool to automatically collect relevant diagnostic information from your HyperPod cluster:
- Repository: https://github.com/shimomut/sagemaker-solutions/tree/main/hyperpod_issue_report
- Follow the instructions in the README for installation and usage

---

If you continue to experience issues:

1. **Check CloudWatch Logs**: Most services log detailed information to CloudWatch
2. **Review CloudFormation Events**: Stack events provide deployment timeline and errors
3. **AWS Support**: Open a support case with relevant logs and error messages
4. **GitHub Issues**: Report bugs or request features in the project repository

## Additional Resources

- [AWS HyperPod Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [EKS Troubleshooting Guide](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html)
- [Slurm Documentation](https://slurm.schedmd.com/documentation.html)
- [NCCOM Tests for Trainium Instances](https://awslabs.github.io/ai-on-sagemaker-hyperpod/docs/validation-and-testing/performance-testing/nccom-tests)
