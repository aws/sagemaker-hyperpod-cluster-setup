"""
Unit tests for LifeCycleConfig handling in enrich_instance_groups
"""
import unittest
import os
import sys
from unittest.mock import Mock, patch

# Mock cfnresponse and boto3 before importing lambda_function
sys.modules['cfnresponse'] = Mock()

sys.path.insert(0, os.path.dirname(__file__))

from lambda_function import enrich_instance_groups


class TestEnrichInstanceGroupsLCS(unittest.TestCase):

    def setUp(self):
        self.env_vars = {
            'S3_BUCKET_NAME': 'bucket',
            'ON_CREATE_PATH': '',
            'ON_INIT_COMPLETE_PATH': 'global_extension.sh',
            'SAGEMAKER_IAM_ROLE_NAME': '',
            'SECURITY_GROUP_IDS': '',
            'PRIVATE_SUBNET_IDS': '',
        }

    def _run(self, instance_groups):
        with patch.dict(os.environ, self.env_vars, clear=False):
            return enrich_instance_groups(instance_groups, isRig=False)

    def test_no_override_inherits_global_extension(self):
        """IG with no override should inherit global OnInitComplete"""
        groups = [{'InstanceGroupName': 'worker-1', 'InstanceType': 'ml.t3.medium'}]
        result = self._run(groups)

        self.assertIn('LifeCycleConfig', result[0])
        self.assertEqual(result[0]['LifeCycleConfig']['SourceS3Uri'], 's3://bucket')
        self.assertEqual(result[0]['LifeCycleConfig']['OnInitComplete'], 'global_extension.sh')

    def test_empty_lifecycle_config_skips_global_extension(self):
        """IG with empty LifeCycleConfig (default only override) should NOT get global extension"""
        groups = [{
            'InstanceGroupName': 'compute-group-1',
            'InstanceType': 'ml.t3.medium',
            'LifeCycleConfig': {'SourceS3Uri': '', 'OnInitComplete': ''},
        }]
        result = self._run(groups)

        self.assertNotIn('LifeCycleConfig', result[0])

    def test_empty_dict_lifecycle_config_skips_global_extension(self):
        """IG with empty dict LifeCycleConfig {} should NOT get global extension"""
        groups = [{
            'InstanceGroupName': 'compute-group-1',
            'InstanceType': 'ml.t3.medium',
            'LifeCycleConfig': {},
        }]
        result = self._run(groups)

        self.assertNotIn('LifeCycleConfig', result[0])

    def test_custom_override_preserved(self):
        """IG with explicit LifeCycleConfig should keep it and not inherit global"""
        groups = [{
            'InstanceGroupName': 'controller-group-1',
            'InstanceType': 'ml.t3.medium',
            'LifeCycleConfig': {
                'SourceS3Uri': 's3://bucket',
                'OnCreate': 'entrypoint.sh',
            },
        }]
        result = self._run(groups)

        self.assertEqual(result[0]['LifeCycleConfig']['SourceS3Uri'], 's3://bucket')
        self.assertEqual(result[0]['LifeCycleConfig']['OnCreate'], 'entrypoint.sh')

    def test_mixed_igs_edge_case(self):
        """Global extension + one empty LCS override + one custom override"""
        groups = [
            {
                'InstanceGroupName': 'compute-group-1',
                'InstanceType': 'ml.t3.medium',
                'LifeCycleConfig': {'SourceS3Uri': '', 'OnInitComplete': ''},
            },
            {
                'InstanceGroupName': 'controller-group-1',
                'InstanceType': 'ml.t3.medium',
                'LifeCycleConfig': {
                    'SourceS3Uri': 's3://bucket',
                    'OnCreate': 'entrypoint.sh',
                },
            },
        ]
        result = self._run(groups)

        # compute-group-1: empty LCS removed, no global inherited
        self.assertNotIn('LifeCycleConfig', result[0])

        # controller-group-1: custom override preserved
        self.assertEqual(result[1]['LifeCycleConfig']['OnCreate'], 'entrypoint.sh')

    def test_no_s3_bucket_skips_all_lcs(self):
        """No S3 bucket means no LCS config added"""
        self.env_vars['S3_BUCKET_NAME'] = ''
        groups = [{'InstanceGroupName': 'worker-1', 'InstanceType': 'ml.t3.medium'}]
        result = self._run(groups)

        self.assertNotIn('LifeCycleConfig', result[0])

    # ── EnableSourceS3UriOnly tests ──────────────────────────────────────

    def test_source_s3_uri_only_slurm_enabled(self):
        """SLURM + EnableSourceS3UriOnly=true + no paths → LifeCycleConfig with only SourceS3Uri"""
        self.env_vars['ON_INIT_COMPLETE_PATH'] = ''
        self.env_vars['ON_CREATE_PATH'] = ''
        self.env_vars['ENABLE_SOURCE_S3_URI_ONLY'] = 'true'
        self.env_vars['ORCHESTRATOR_TYPE'] = 'SLURM'
        groups = [{'InstanceGroupName': 'worker-1', 'InstanceType': 'ml.t3.medium'}]
        result = self._run(groups)

        self.assertIn('LifeCycleConfig', result[0])
        self.assertEqual(result[0]['LifeCycleConfig']['SourceS3Uri'], 's3://bucket')
        self.assertNotIn('OnCreate', result[0]['LifeCycleConfig'])
        self.assertNotIn('OnInitComplete', result[0]['LifeCycleConfig'])

    def test_source_s3_uri_only_eks_not_applied(self):
        """EKS + EnableSourceS3UriOnly=true + no paths → No LifeCycleConfig (only for SLURM)"""
        self.env_vars['ON_INIT_COMPLETE_PATH'] = ''
        self.env_vars['ON_CREATE_PATH'] = ''
        self.env_vars['ENABLE_SOURCE_S3_URI_ONLY'] = 'true'
        self.env_vars['ORCHESTRATOR_TYPE'] = 'EKS'
        groups = [{'InstanceGroupName': 'worker-1', 'InstanceType': 'ml.t3.medium'}]
        result = self._run(groups)

        self.assertNotIn('LifeCycleConfig', result[0])

    def test_source_s3_uri_only_disabled_by_default(self):
        """SLURM + EnableSourceS3UriOnly=false (default) + no paths → No LifeCycleConfig"""
        self.env_vars['ON_INIT_COMPLETE_PATH'] = ''
        self.env_vars['ON_CREATE_PATH'] = ''
        self.env_vars['ENABLE_SOURCE_S3_URI_ONLY'] = 'false'
        self.env_vars['ORCHESTRATOR_TYPE'] = 'SLURM'
        groups = [{'InstanceGroupName': 'worker-1', 'InstanceType': 'ml.t3.medium'}]
        result = self._run(groups)

        self.assertNotIn('LifeCycleConfig', result[0])

    def test_source_s3_uri_only_does_not_override_paths(self):
        """SLURM + EnableSourceS3UriOnly=true + paths provided → Normal path-based logic wins"""
        self.env_vars['ON_INIT_COMPLETE_PATH'] = 'scripts/extension.sh'
        self.env_vars['ON_CREATE_PATH'] = ''
        self.env_vars['ENABLE_SOURCE_S3_URI_ONLY'] = 'true'
        self.env_vars['ORCHESTRATOR_TYPE'] = 'SLURM'
        groups = [{'InstanceGroupName': 'worker-1', 'InstanceType': 'ml.t3.medium'}]
        result = self._run(groups)

        self.assertIn('LifeCycleConfig', result[0])
        self.assertEqual(result[0]['LifeCycleConfig']['SourceS3Uri'], 's3://bucket/scripts')
        self.assertEqual(result[0]['LifeCycleConfig']['OnInitComplete'], 'extension.sh')


if __name__ == '__main__':
    unittest.main()
