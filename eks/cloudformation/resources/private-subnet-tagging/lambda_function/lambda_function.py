import boto3
import cfnresponse
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client('ec2')

def lambda_handler(event, context):
    try:
        logger.info('Received event: %s', event)
        
        request_type = event['RequestType']
        properties = event['ResourceProperties']
        
        # Skip processing for DELETE requests
        if request_type == 'Delete':
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return
        
        # Get subnet IDs and split into list if it's a string
        subnet_ids = properties.get('PrivateSubnetIds', [])
        if isinstance(subnet_ids, str):
            subnet_ids = [id.strip() for id in subnet_ids.split(',')]
        
        # Tag all subnets in one API call
        ec2.create_tags(
            Resources=subnet_ids,
            Tags=properties.get('Tags', [])
        )
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'Message': 'Successfully tagged subnets'
        })
        
    except Exception as e:
        logger.error('Error: %s', str(e))
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Error': str(e)
        })
