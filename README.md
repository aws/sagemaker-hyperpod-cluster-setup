## SageMaker HyperPod cluster setup assets

This repository contains the setup assets required to create Amazon SageMaker HyperPod clusters using either Slurm or Amazon EKS for orchestration. You can create all the resources needed for large-scale AI/ML workloads—including networking, storage, compute, and IAM permissions. 

SageMaker HyperPod clusters are purpose-built for scalability and resilience, designed to accelerate large-scale distributed training and deployment of complex machine learning models like LLMs and diffusion models, as well as customization of Amazon Nova foundation models. 

## Pre-requisites needed to setup a HyperPod cluster

The CloudFormation templates in this repository automate the provisioning of all necessary AWS resources along with your SageMaker HyperPod cluster. The templates are designed for flexibility, allowing you to either create a completely new stack of resources or integrate with your existing infrastructure by providing the IDs of existing components. The following resources will be managed by the templates: 

* Networking (VPC, Subnets, Security Groups) - Provides the network foundation optimized for high-performance.  
* S3 bucket for LifeCycle scripts  - Stores the lifecycle scripts needed to bootstrap the cluster nodes for both [Slurm](https://github.com/aws-samples/awsome-distributed-training/tree/main/1.architectures/5.sagemaker-hyperpod/LifecycleScripts/base-config) and [EKS](https://github.com/aws-samples/awsome-distributed-training/tree/main/1.architectures/7.sagemaker-hyperpod-eks/LifecycleScripts/base-config). 
* FSx for Lustre - A high-performance, shared file system for datasets and model checkpoints. 
* Amazon EKS Cluster - A managed Kubernetes service provided by Amazon Web Services (AWS). 
* [Helm charts](https://github.com/aws/sagemaker-hyperpod-cli/tree/main/helm_chart) - Deploys necessary Kubernetes components (e.g., health monitoring agent, training,  inference operator) onto the EKS cluster required for HyperPod. 
* IAM role - Allows the HyperPod cluster to run and communicate with the necessary AWS resources on your behalf.


## Configure resources and deploy using CloudFormation

You can configure resources and deploy using the CloudFormation templates for SageMaker HyperPod. Follow the steps mentioned in the AWS documentation to get started:

* [Slurm orchestration](https://docs.aws.amazon.com/sagemaker/latest/dg/smcluster-getting-started-slurm-console-create-cluster-cfn.html)
* [Amazon EKS orchestration](https://docs.aws.amazon.com/sagemaker/latest/dg/smcluster-getting-started-eks-console-create-cluster-cfn.html)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

