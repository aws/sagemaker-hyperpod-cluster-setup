# Troubleshooting Guide

This guide helps you diagnose and resolve common issues with your HyperPod deployment.

## Quick Reference Table

| Issue Category | Subject (Symptom) | Reason | Resolution | Link to Details |
|----------------|-------------------|--------|------------|-----------------|
| **Deployment** | Cluster creation fails with lifecycle script error | Script syntax errors, missing dependencies, S3 access issues | Review CloudWatch logs, verify S3 access, check script syntax | [Details](#cluster-creation-failed-with-lifecycle-script-execution-error) |
| **Deployment** | EFA health checks did not run successfully | Missing security group self-referencing rule | Add outbound rule allowing all traffic to the security group itself | [Details](#efa-health-checks-did-not-run-successfully) |
| **Networking** | FSx filesystem cannot be mounted | FSx not available, security group rules, DNS issues | Verify FSx state, check security groups (port 988), enable VPC DNS | [Details](#fsx-for-lustre-connection-problems) |
| **Networking** | Resources cannot communicate | Security group rules, route tables, NAT/IGW issues | Verify security groups, check route tables, validate DNS settings | [Details](#vpc-and-networking) |
| **Node Management** | Node not responding | Network issues, slurmd daemon stopped, resource exhaustion | Check connectivity, verify slurmd status, check memory/disk | [Details](#node-not-responding) |
| **Node Management** | Slurm says node is "down" | slurmd not running, network issues, manual drain | Check slurmd status, verify connectivity, resume node state | [Details](#slurm-says-node-is-down) |
| **Node Management** | Node replacement not happening | Auto-recovery disabled, capacity unavailable, quota limits | Check auto-recovery settings, verify capacity, review quotas | [Details](#node-replacement-not-happening) |
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

### FSx for Lustre Connection Problems

**Issue**: Pods cannot mount FSx filesystem

**Resolution Steps**:
1. Verify FSx filesystem is in "Available" state
2. Check security group rules allow NFS traffic (port 988)
3. Confirm VPC DNS resolution is enabled
4. Validate FSx mount targets are in correct subnets

## Slurm-based Deployments

### HyperPod Cluster Creation Failures

**Issue**: SageMaker HyperPod cluster fails to create

**Resolution Steps**:
1. Check SageMaker console for cluster status and error messages
2. Verify lifecycle scripts are accessible in S3
3. Confirm instance types are available in selected region/AZ
4. Review VPC and subnet configuration

### Node Registration Issues

**Issue**: Compute nodes fail to join Slurm cluster

**Resolution Steps**:
1. SSH to head node and check Slurm controller logs: `sudo journalctl -u slurmctld`
2. Verify network connectivity between head and compute nodes
3. Check `/var/log/slurm/` for detailed error logs
4. Confirm security groups allow required Slurm ports (6817-6819)

### Node Not Responding

**Issue**: Slurm node becomes unresponsive or shows as "down"

**Resolution Steps**:
1. Check node status: `sinfo -N -l` or `scontrol show node <node-name>`
2. Attempt to ping the node from head node
3. SSH to the node and check system logs: `sudo journalctl -xe`
4. Verify slurmd daemon is running: `sudo systemctl status slurmd`
5. Check for out-of-memory or disk space issues: `free -h` and `df -h`
6. Restart slurmd if needed: `sudo systemctl restart slurmd`
7. If node remains down, set it back to idle: `scontrol update nodename=<node-name> state=resume`

### Slurm Says Node is "Down"

**Issue**: Node shows as "down" in Slurm even though it's running

**Resolution Steps**:
1. Check why node is down: `scontrol show node <node-name> | grep Reason`
2. Verify slurmd is running on the node: `sudo systemctl status slurmd`
3. Check network connectivity from head node to compute node
4. Review slurmd logs on the compute node: `sudo journalctl -u slurmd -n 100`
5. Clear the down state: `scontrol update nodename=<node-name> state=resume reason="manual recovery"`
6. If issue persists, restart slurmd: `sudo systemctl restart slurmd`

### Cluster Creation Failed with Lifecycle Script Execution Error

**Issue**: HyperPod cluster creation fails during lifecycle script execution

**Common Causes**:
- Syntax errors in lifecycle scripts
- Missing dependencies or packages
- S3 access issues for script retrieval
- Insufficient permissions for script operations
- Network connectivity problems

**Resolution Steps**:
1. Check CloudWatch logs for the cluster creation process
2. Verify lifecycle script is accessible in S3 bucket
3. Review script syntax and test locally if possible
4. Confirm IAM role has permissions to access S3 and execute required operations
5. Check `/var/log/provision/` on cluster nodes for detailed error logs
6. Validate script dependencies are available in the base AMI
7. Ensure script has proper shebang and execute permissions

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

### Node Replacement Not Happening

**Issue**: Failed nodes are not being automatically replaced

**Resolution Steps**:
1. Check HyperPod cluster auto-recovery settings in SageMaker console
2. Verify cluster is not in a failed state that prevents recovery
3. Review CloudWatch logs for auto-recovery attempts
4. Confirm capacity is available for replacement instances
5. Check if maximum node count has been reached
6. Verify IAM permissions allow node replacement operations
7. Look for service quotas that might block new instance launches

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

### GPUs Are Not Getting Released

**Issue**: GPUs remain allocated after job completion

**Resolution Steps**:
1. Check for zombie processes: `nvidia-smi` and look for lingering processes
2. Kill stuck processes: `sudo kill -9 <PID>`
3. Check Slurm job status: `squeue -u <username>` and `sacct -j <job-id>`
4. If job shows as completed but GPU is held, restart slurmd: `sudo systemctl restart slurmd`
5. Clear GPU memory: `sudo fuser -v /dev/nvidia*` then kill processes
6. As last resort, reboot the node

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

## Common Issues

### VPC and Networking

**Issue**: Resources cannot communicate

**Resolution Steps**:
- Verify security group rules allow required traffic
- Check route tables for proper routing
- Confirm NAT Gateway or Internet Gateway configuration
- Validate DNS resolution settings

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

### Lambda Function Timeouts

**Issue**: Custom resource Lambda functions timeout

**Resolution Steps**:
1. Increase Lambda timeout in CloudFormation template
2. Check Lambda function logs in CloudWatch
3. Verify Lambda has network access (VPC configuration if needed)
4. Review function code for performance bottlenecks

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
