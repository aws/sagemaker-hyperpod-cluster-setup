"""
Unit tests for FsxOpenZfsConfig handling in __override_openzfs_config_in_instance_groups
"""
import unittest
import os
import sys
from unittest.mock import Mock, patch

# Mock cfnresponse and boto3 before importing lambda_function
sys.modules['cfnresponse'] = Mock()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Aliased at module level so the double-underscore name isn't mangled inside the test class
from lambda_function import __override_openzfs_config_in_instance_groups as override_openzfs_config

DNS_NAME = 'fs-0123456789abcdef0.fsx.us-west-2.amazonaws.com'


class TestOverrideOpenZfsConfig(unittest.TestCase):

    def _run(self, instance_groups, env_vars):
        with patch.dict(os.environ, env_vars, clear=False):
            override_openzfs_config(instance_groups)
        return instance_groups

    def _enabled_env(self, mount_path='/home'):
        return {'ENABLED_OPENZFS': 'true', 'OPENZFS_DNS_NAME': DNS_NAME, 'OPENZFS_MOUNT_PATH': mount_path}

    def test_adds_config_to_every_instance_group(self):
        groups = self._run(
            [{'InstanceGroupName': 'controller'}, {'InstanceGroupName': 'compute'}],
            self._enabled_env(),
        )
        for group in groups:
            self.assertEqual(
                group['InstanceStorageConfigs'],
                [{'FsxOpenZfsConfig': {'DnsName': DNS_NAME, 'MountPath': '/home'}}],
            )

    def test_appends_alongside_existing_lustre_config(self):
        lustre = {'FsxLustreConfig': {'DnsName': 'lustre-dns', 'MountName': 'abcdefgh'}}
        groups = self._run(
            [{'InstanceGroupName': 'compute', 'InstanceStorageConfigs': [lustre]}],
            self._enabled_env('/shared'),
        )
        self.assertEqual(
            groups[0]['InstanceStorageConfigs'],
            [lustre, {'FsxOpenZfsConfig': {'DnsName': DNS_NAME, 'MountPath': '/shared'}}],
        )

    def test_overrides_existing_openzfs_config(self):
        groups = self._run(
            [
                {
                    'InstanceGroupName': 'compute',
                    'InstanceStorageConfigs': [{'FsxOpenZfsConfig': {'DnsName': 'stale', 'MountPath': '/old'}}],
                }
            ],
            self._enabled_env(),
        )
        self.assertEqual(
            groups[0]['InstanceStorageConfigs'],
            [{'FsxOpenZfsConfig': {'DnsName': DNS_NAME, 'MountPath': '/home'}}],
        )

    def test_defaults_mount_path_when_empty(self):
        groups = self._run([{'InstanceGroupName': 'compute'}], self._enabled_env(''))
        self.assertEqual(groups[0]['InstanceStorageConfigs'][0]['FsxOpenZfsConfig']['MountPath'], '/home')

    def test_no_op_when_disabled(self):
        env = {**self._enabled_env(), 'ENABLED_OPENZFS': 'false'}
        groups = self._run([{'InstanceGroupName': 'compute'}], env)
        self.assertNotIn('InstanceStorageConfigs', groups[0])

    def test_no_op_when_dns_name_missing(self):
        env = {**self._enabled_env(), 'OPENZFS_DNS_NAME': ''}
        groups = self._run([{'InstanceGroupName': 'compute'}], env)
        self.assertNotIn('InstanceStorageConfigs', groups[0])

    def test_no_op_for_empty_instance_groups(self):
        self.assertEqual(self._run([], self._enabled_env()), [])


if __name__ == '__main__':
    unittest.main()
