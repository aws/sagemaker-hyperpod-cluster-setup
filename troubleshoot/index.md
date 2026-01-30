# Troubleshooting Guide

This guide helps you diagnose and resolve common issues with your HyperPod deployment.

## Quick Reference Table

| Issue Category | Subject (Symptom) | Reason | Resolution | Link to Details |
|----------------|-------------------|--------|------------|-----------------|
| **Deployment** | Cluster creation fails with lifecycle script error | Script syntax errors, missing dependencies, S3 access issues | Review CloudWatch logs, verify S3 access, check script syntax | [Details](#cluster-creation-failed-with-lifecycle-script-execution-error) |
| **Deployment** | EFA health checks did not run successfully | Missing security group self-referencing rule | Add outbound rule allowing all traffic to the security group itself | [Details](#efa-health-checks-did-not-run-successfully) |
| **Node Management** | Node not responding / Slurm says node is "down" | Network issues, slurmd daemon stopped, resource exhaustion | Check connectivity, verify slurmd status, check memory/disk | [Details](#node-not-responding--slurm-says-node-is-down) |
| **Node Management** | Node replacement not happening automatically | Auto-recovery disabled, capacity unavailable, quota limits | Check auto-recovery settings, verify capacity, review quotas | [Details](#node-replacement-not-happening-automatically) |
| **Node Management** | Node replacement not happening even after manual trigger | Wrong command syntax, cluster state, IAM permissions, capacity issues | Verify command syntax, check cluster state, review IAM permissions | [Details](#node-replacement-not-happening-even-after-manual-trigger) |
| **Performance** | NCCL timeouts | Network congestion, EFA issues, insufficient timeout value | Increase NCCL_TIMEOUT, verify EFA, check network connectivity | [Details](#nccl-timeouts) |
| **Performance** | Uneven NCCL performance across nodes | Network topology differences, degraded EFA, instance variations | Check EFA bandwidth, verify instance types, use placement groups | [Details](#uneven-nccl-performance-depending-on-the-set-of-nodes) |
| **Performance** | Poor filesystem performance | Insufficient throughput, wrong volume type, I/O bottleneck | Check filesystem metrics, increase throughput, optimize data loading | [Details](#poor-filesystem-performance) |
| **Memory** | DataLoader "Cannot allocate memory" error | Insufficient shared memory (/dev/shm), too many workers | Increase --shm-size, reduce num_workers, check /dev/shm usage | [Details](#multi-process-dataloader-raises-oserror-errno-12-cannot-allocate-memory-error) |
| **Memory** | FI_EFA_USE_HUGE_PAGE=0 required | Huge pages not configured, EFA memory registration fails | Set FI_EFA_USE_HUGE_PAGE=0 or configure huge pages properly | [Details](#fi_efa_use_huge_page0-has-to-be-set) |
| **GPU** | Suspecting GPU failure | Hardware failure, ECC errors, thermal throttling | Run nvidia-smi diagnostics, check ECC errors, drain node | [Details](#suspecting-gpu-failure) |
| **GPU** | GPUs not getting released | Zombie processes, stuck jobs, slurmd issues | Kill lingering processes, restart slurmd, reboot if needed | [Details](#gpus-are-not-getting-released) |
| **GPU** | EFA/NCCL/CUDA/driver version mismatch | Incompatible versions, host/container mismatch | Check version compatibility, rebuild containers with matching versions | [Details](#efanclccudanvidia-driver-version-mismatch) |
| **GPU** | Host and container environment mismatch | Different CUDA versions, missing EFA mounts | Mount EFA libraries, verify device access, match versions | [Details](#mismatch-between-host-environment-and-container-environment) |
| **IAM** | Execution role permissions invalid | Missing policies, incorrect trust relationship, SCPs | Verify IAM policies, check trust relationships, validate permissions | [Details](#iam-permission-errors) |
| **Lambda** | Custom resource Lambda timeout | Insufficient timeout, network issues, code bottlenecks | Increase timeout, check CloudWatch logs, verify VPC access | [Details](#lambda-function-timeouts) |

## Quick Links

- [EKS-based Deployments](#eks-based-deployments)
- [Slurm-based Deployments](#slurm-based-deployments)
- [Performance Issues](#performance-issues)
- [GPU and Accelerator Issues](#gpu-and-accelerator-issues)
- [Common Issues](#common-issues)

## EKS-based Deployments

## Slurm-based Deployments

### Node Not Responding / Slurm Says Node is "Down"

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

### Node Replacement Not Happening Automatically

**Issue**: Failed nodes are not being automatically replaced by HyperPod

**Resolution Steps**:
1. Check HyperPod cluster auto-recovery settings in SageMaker console or via CLI:
   ```bash
   aws sagemaker describe-cluster --cluster-name <cluster-name>
   ```
   Look for the auto-recovery configuration
2. Verify cluster is not in a failed state that prevents recovery
3. Review CloudWatch logs for auto-recovery attempts:
   - Log Group: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
4. Confirm capacity is available for replacement instances in the selected availability zones
5. Check if maximum node count has been reached in the cluster configuration
6. Verify IAM permissions allow node replacement operations:
   - ec2:RunInstances
   - ec2:TerminateInstances
   - ec2:DescribeInstances
7. Look for service quotas that might block new instance launches:
   ```bash
   aws service-quotas get-service-quota \
     --service-code ec2 \
     --quota-code <quota-code>
   ```

---

### Node Replacement Not Happening Even After Manual Trigger

**Issue**: Manual node replacement command fails or doesn't complete

**Resolution Steps**:
1. Verify the replacement command syntax:
   ```bash
   aws sagemaker batch-replace-cluster-nodes \
     --cluster-name <cluster-name> \
     --node-ids <instance-id>
   ```
2. Check the command output for error messages
3. Verify the instance ID is correct and belongs to the cluster:
   ```bash
   aws sagemaker list-cluster-nodes --cluster-name <cluster-name>
   ```
4. Ensure the cluster is in a state that allows node replacement (not in "Creating" or "Deleting" state)
5. Check IAM permissions for the user/role executing the command:
   - sagemaker:UpdateClusterSoftware (for batch operations)
   - Required EC2 permissions
6. Review CloudWatch logs for detailed error messages:
   - Log Group: `/aws/sagemaker/Clusters/<cluster-name>/<cluster-id>`
7. Verify capacity is available for the instance type in the target availability zone
8. Check for any service quotas or limits that might prevent instance launch
9. If the command appears to hang, check the cluster node status:
   ```bash
   aws sagemaker list-cluster-nodes \
     --cluster-name <cluster-name> \
     --instance-group-name-contains <node-group-name>
   ```
10. Contact AWS Support if the issue persists with command output and CloudWatch logs

---

## GPU and Accelerator Issues

### Suspecting GPU Failure

**Issue**: Training jobs fail or produce incorrect results, GPU errors in logs

**Diagnostic Steps**:
1. Run GPU diagnostics: `nvidia-smi -q` and check for errors
2. Run memory test: `nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv`
3. Check GPU temperature and throttling: `nvidia-smi dmon -s pucvmet`
4. Run CUDA samples or DCGM diagnostics if available
5. Check for ECC errors: `nvidia-smi -q | grep -A 5 "ECC Errors"`
6. Review application logs for CUDA errors

**Resolution Steps**:
- If GPU shows errors, drain the node: `scontrol update nodename=<node-name> state=drain reason="GPU failure"`
- Contact AWS support for hardware replacement
- Document GPU serial number and error details for support case

---

### GPUs Are Not Getting Released

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

**Issue**: Training fails with EFA or NCCL errors, performance degradation

**Common Symptoms**:
- NCCL initialization failures
- EFA device not found errors
- Unexpected performance drops
- Segmentation faults during distributed training

**Resolution Steps**:
1. Check installed versions:
   ```bash
   nvidia-smi  # Driver version
   nvcc --version  # CUDA version
   cat /opt/amazon/efa/lib/libfabric/version  # EFA version
   python -c "import torch; print(torch.cuda.nccl.version())"  # NCCL version
   ```
2. Verify compatibility matrix in AWS documentation
3. Check for mismatches between host and container environments:
   ```bash
   # On host
   nvidia-smi
   # In container
   docker exec <container> nvidia-smi
   ```
4. Ensure container has proper device mounts and environment variables
5. Rebuild containers with matching versions if needed
6. Update drivers/libraries to compatible versions

---

### Mismatch Between Host Environment and Container Environment

**Issue**: Training works on host but fails in container, or vice versa

**Resolution Steps**:
1. Compare CUDA versions: host vs container
2. Verify EFA libraries are mounted into container:
   ```bash
   docker run --device=/dev/infiniband/uverbs0 \
     -v /opt/amazon/efa:/opt/amazon/efa \
     -v /opt/amazon/openmpi:/opt/amazon/openmpi
   ```
3. Check LD_LIBRARY_PATH includes EFA libraries in container
4. Ensure NCCL plugin for EFA is available in container
5. Verify GPU device access: `--gpus all` or `--runtime=nvidia`
6. Match PyTorch/TensorFlow versions between environments

---

## Common Issues

### IAM Permission Errors

**Issue**: "Access Denied" or permission-related errors

**Common Error Messages**:
- "Execution role permissions are invalid. Please ensure the execution role arn:aws:iam::123456789012:role/sagemaker-hyperpodcluster-abcd1234ExecRole"

**Resolution Steps**:
- Review IAM role trust relationships - ensure SageMaker service principal is trusted
- Verify required policies are attached (SageMaker execution policy, S3 access, etc.)
- Check for service control policies (SCPs) restrictions
- Confirm resource-based policies allow access
- Validate IAM role has permissions for:
  - S3 bucket access (lifecycle scripts, data)
  - CloudWatch Logs write permissions
  - EC2 network interface creation (for VPC)
  - FSx access if using shared filesystem
- Ensure role ARN is correctly specified in cluster configuration
- Check if role was recently created (may need a few minutes to propagate)

---

### Lambda Function Timeouts

**Issue**: Custom resource Lambda functions timeout

**Resolution Steps**:
1. Increase Lambda timeout in CloudFormation template
2. Check Lambda function logs in CloudWatch
3. Verify Lambda has network access (VPC configuration if needed)
4. Review function code for performance bottlenecks

---

## Performance Issues

### NCCL Timeouts

**Issue**: Distributed training fails with NCCL timeout errors

**Common Error Messages**:
- "NCCL timeout in call to..."
- "NCCL communicator was aborted"
- "Net/IB : Got completion with error"

**Resolution Steps**:
1. Increase NCCL timeout: `export NCCL_TIMEOUT=3600` (default is 1800 seconds)
2. Enable NCCL debug logging: `export NCCL_DEBUG=INFO`
3. Verify EFA is being used: `export FI_EFA_USE_DEVICE_RDMA=1`
4. Check network connectivity between nodes: `fi_pingpong -p efa`
5. Verify all nodes have working EFA adapters: `fi_info -p efa`
6. Check for network congestion or packet loss
7. Ensure security groups allow all traffic between compute nodes
8. Try reducing batch size or number of workers if memory pressure exists

---

### Uneven NCCL Performance Depending on the Set of Nodes

**Issue**: Training performance varies significantly based on which nodes are allocated

**Resolution Steps**:
1. Check for network topology differences: `nvidia-smi topo -m`
2. Verify all nodes have same EFA configuration: `fi_info -p efa`
3. Check for nodes with degraded EFA performance:
   ```bash
   # Run EFA bandwidth test between node pairs
   # On node 1: efa_test -s
   # On node 2: efa_test -c <node1-ip>
   ```
4. Look for nodes with different instance generations or types
5. Check CPU frequency scaling: `cat /proc/cpuinfo | grep MHz`
6. Verify NCCL is using optimal algorithm: `export NCCL_ALGO=Ring` or `Tree`
7. Enable NCCL tuning: `export NCCL_TUNER_PLUGIN=libnccl-tuner.so`
8. Drain and investigate underperforming nodes
9. Consider using placement groups for consistent network performance

---

### Poor Filesystem Performance

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

### Multi-process DataLoader Raises "OSError: [Errno 12] Cannot Allocate Memory" Error

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

### FI_EFA_USE_HUGE_PAGE=0 Has to Be Set

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

If you continue to experience issues:

1. **Check CloudWatch Logs**: Most services log detailed information to CloudWatch
2. **Review CloudFormation Events**: Stack events provide deployment timeline and errors
3. **AWS Support**: Open a support case with relevant logs and error messages
4. **GitHub Issues**: Report bugs or request features in the project repository

## Additional Resources

- [AWS HyperPod Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [EKS Troubleshooting Guide](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html)
- [Slurm Documentation](https://slurm.schedmd.com/documentation.html)
