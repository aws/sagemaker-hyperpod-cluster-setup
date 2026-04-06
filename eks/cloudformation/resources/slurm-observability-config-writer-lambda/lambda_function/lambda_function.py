import boto3
import cfnresponse
import os
import yaml
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def build_config():
    return {
        'apiVersion': 'slurm.hyperpod.sagemaker.amazonaws.com/v1alpha1',
        'kind': 'ObservabilityConfig',
        'spec': {
            'enabled': True,
            'ampWorkspace': {
                'prometheusEndpoint': os.environ['AMP_REMOTE_WRITE_URL'],
                'arn': os.environ['AMP_WORKSPACE_ARN'],
            },
            'amgWorkspace': {
                'workspaceName': os.environ['AMG_WORKSPACE_NAME'],
                'arn': os.environ['AMG_WORKSPACE_ARN'],
            },
            'poller': {
                'enabled': True,
                'pollingIntervalSeconds': 300,
            },
            'metricsProvider': {
                'nodeMetrics': {
                    'level': os.environ['NODE_METRIC_LEVEL'],
                    'scrapeIntervalSeconds': 30,
                },
                'acceleratedComputeMetrics': {
                    'level': os.environ['ACCELERATED_COMPUTE_METRIC_LEVEL'],
                    'scrapeIntervalSeconds': 30,
                },
                'clusterMetrics': {
                    'level': os.environ['CLUSTER_METRIC_LEVEL'],
                    'scrapeIntervalSeconds': 30,
                },
                'networkMetrics': {
                    'level': os.environ['NETWORK_METRIC_LEVEL'],
                    'scrapeIntervalSeconds': 30,
                },
                'ncclMetrics': {
                    'level': os.environ['NCCL_METRIC_LEVEL'],
                    'scrapeIntervalSeconds': 30,
                    'promTextFilepath': '',
                },
                'jobMetrics': {
                    'level': os.environ['JOB_METRIC_LEVEL'],
                    'scrapeIntervalSeconds': 30,
                },
            },
        },
    }


def lambda_handler(event, context):
    """CFN Custom Resource handler for writing observability-config.yaml to S3."""
    logger.info(f"Received event: {event}")

    try:
        request_type = event['RequestType']

        if request_type == 'Delete':
            s3 = boto3.client('s3')
            try:
                s3.delete_object(
                    Bucket=os.environ['LCS_BUCKET_NAME'],
                    Key=os.environ['LCS_CONFIG_S3_KEY']
                )
                logger.info("Config file deleted from S3")
            except Exception:
                pass
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return

        config = build_config()
        yaml_content = yaml.dump(config, default_flow_style=False, sort_keys=False)
        logger.info(f"Generated config:\n{yaml_content}")

        s3 = boto3.client('s3')
        bucket_name = os.environ['LCS_BUCKET_NAME']
        config_key = os.environ['LCS_CONFIG_S3_KEY']

        try:
            s3.put_object(
                Bucket=bucket_name,
                Key=config_key,
                Body=yaml_content,
                ContentType='application/x-yaml',
            )
            config_uri = f"s3://{bucket_name}/{config_key}"
            logger.info(f"Config written to {config_uri}")
        except s3.exceptions.NoSuchBucket:
            logger.warning(f"LCS bucket '{bucket_name}' does not exist. Skipping config write.")
            config_uri = ''

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'ConfigS3Uri': config_uri,
        })

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Reason': str(e)})
