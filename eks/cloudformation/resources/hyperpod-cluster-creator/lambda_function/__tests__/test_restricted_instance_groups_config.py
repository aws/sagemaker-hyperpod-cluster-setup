"""
Unit tests for RestrictedInstanceGroupsConfig functionality in the HyperPod cluster Lambda function
"""
import unittest
import json
import os
from unittest.mock import patch, Mock
import sys

# Mock cfnresponse before importing lambda_function
sys.modules['cfnresponse'] = Mock()

sys.path.insert(0, os.path.dirname(__file__))

from lambda_function import create_hyperpod_cluster


class TestRestrictedInstanceGroupsConfig(unittest.TestCase):
    """Test cases for RestrictedInstanceGroupsConfig env var parsing"""

    def setUp(self):
        self.base_env = {
            'HYPER_POD_CLUSTER_NAME': 'test-cluster',
            'ORCHESTRATOR_TYPE': 'EKS',
            'NODE_RECOVERY': 'Automatic',
            'NODE_PROVISIONING_MODE': '',
            'EKS_CLUSTER_ARN': 'arn:aws:eks:us-west-2:123456789012:cluster/test',
            'SECURITY_GROUP_IDS': 'sg-123',
            'PRIVATE_SUBNET_IDS': 'subnet-123',
            'SAGEMAKER_IAM_ROLE_NAME': 'arn:aws:iam::123456789012:role/test',
            'S3_BUCKET_NAME': 'test-bucket',
            'ON_CREATE_PATH': 'scripts/on_create.sh',
            'NUMBER_OF_INSTANCE_GROUPS': '1',
            'INSTANCE_GROUP_SETTINGS1': json.dumps({
                'InstanceGroupName': 'worker',
                'InstanceType': 'ml.c5.xlarge',
                'InstanceCount': 1,
                'LifeCycleConfig': {'SourceS3Uri': 's3://bucket/scripts', 'OnCreate': 'on_create.sh'},
            }),
            'RIG_SETTINGS1': '[]',
            'CLUSTER_TAGS': '',
            'TIERED_STORAGE_CONFIG': '',
            'RESTRICTED_INSTANCE_GROUPS_CONFIG': '',
            'ENABLE_HP_TRAINING_OPERATOR_FEATURE': 'false',
            'AUTOSCALER_TYPE': 'None',
            'CLUSTER_ROLE': '',
        }

    @patch('lambda_function.upload_cluster_template_to_s3')
    @patch('lambda_function.generate_cluster_template_yaml')
    def test_valid_config_with_shared_env(self, mock_generate, mock_upload):
        """Test valid RestrictedInstanceGroupsConfig passes through to create_params"""
        config = {
            "SharedEnvironmentConfig": {
                "FSxLustreDeletionPolicy": "Keep",
                "FSxLustreConfig": {
                    "SizeInGiB": 1200,
                    "PerUnitStorageThroughput": 125
                }
            }
        }
        self.base_env['RESTRICTED_INSTANCE_GROUPS_CONFIG'] = json.dumps(config)
        mock_upload.return_value = 'https://s3.amazonaws.com/template.yaml'

        with patch.dict(os.environ, self.base_env, clear=False):
            create_hyperpod_cluster()

        create_params = mock_generate.call_args[0][0]
        self.assertEqual(create_params['RestrictedInstanceGroupsConfig'], config)

    @patch('lambda_function.upload_cluster_template_to_s3')
    @patch('lambda_function.generate_cluster_template_yaml')
    def test_config_without_fsx_lustre_config(self, mock_generate, mock_upload):
        """Test config with only FSxLustreDeletionPolicy (no FSxLustreConfig)"""
        config = {
            "SharedEnvironmentConfig": {
                "FSxLustreDeletionPolicy": "DeleteIfNotUsed"
            }
        }
        self.base_env['RESTRICTED_INSTANCE_GROUPS_CONFIG'] = json.dumps(config)
        mock_upload.return_value = 'https://s3.amazonaws.com/template.yaml'

        with patch.dict(os.environ, self.base_env, clear=False):
            create_hyperpod_cluster()

        create_params = mock_generate.call_args[0][0]
        self.assertEqual(create_params['RestrictedInstanceGroupsConfig'], config)

    @patch('lambda_function.upload_cluster_template_to_s3')
    @patch('lambda_function.generate_cluster_template_yaml')
    def test_empty_config_not_added(self, mock_generate, mock_upload):
        """Test empty string does not add RestrictedInstanceGroupsConfig"""
        self.base_env['RESTRICTED_INSTANCE_GROUPS_CONFIG'] = ''
        mock_upload.return_value = 'https://s3.amazonaws.com/template.yaml'

        with patch.dict(os.environ, self.base_env, clear=False):
            create_hyperpod_cluster()

        create_params = mock_generate.call_args[0][0]
        self.assertNotIn('RestrictedInstanceGroupsConfig', create_params)

    @patch('lambda_function.upload_cluster_template_to_s3')
    @patch('lambda_function.generate_cluster_template_yaml')
    def test_invalid_json_not_added(self, mock_generate, mock_upload):
        """Test invalid JSON does not add RestrictedInstanceGroupsConfig"""
        self.base_env['RESTRICTED_INSTANCE_GROUPS_CONFIG'] = '{invalid json}'
        mock_upload.return_value = 'https://s3.amazonaws.com/template.yaml'

        with patch.dict(os.environ, self.base_env, clear=False):
            create_hyperpod_cluster()

        create_params = mock_generate.call_args[0][0]
        self.assertNotIn('RestrictedInstanceGroupsConfig', create_params)


if __name__ == '__main__':
    unittest.main()
