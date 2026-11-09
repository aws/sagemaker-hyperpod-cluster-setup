"""
Unit tests for creating the cluster with the observability setup stack tag (SETUP_STACK_TAG)
"""
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

# Mock cfnresponse before importing lambda_function
sys.modules['cfnresponse'] = Mock()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lambda_function import get_tags_from_env  # noqa: E402

KEY = 'hyperpod-cluster-extensions-stack'


def tags(**env):
    with patch.dict(os.environ, env, clear=True):
        return get_tags_from_env()


class TestSetupStackTag(unittest.TestCase):
    def test_adds_the_tag_to_json_customer_tags(self):
        customer = json.dumps([{'Key': 'Team', 'Value': 'ml'}])
        self.assertEqual(
            tags(CLUSTER_TAGS=customer, SETUP_STACK_TAG='pending'),
            [{'Key': 'Team', 'Value': 'ml'}, {'Key': KEY, 'Value': 'pending'}],
        )

    def test_adds_the_tag_to_key_value_customer_tags(self):
        self.assertEqual(
            tags(CLUSTER_TAGS='Team=ml,Env=prod', SETUP_STACK_TAG='pending'),
            [{'Key': 'Team', 'Value': 'ml'}, {'Key': 'Env', 'Value': 'prod'}, {'Key': KEY, 'Value': 'pending'}],
        )

    def test_adds_the_tag_without_customer_tags(self):
        self.assertEqual(tags(SETUP_STACK_TAG='pending'), [{'Key': KEY, 'Value': 'pending'}])

    def test_replaces_a_customer_tag_with_the_same_key(self):
        self.assertEqual(
            tags(CLUSTER_TAGS=f'{KEY}=mine,Team=ml', SETUP_STACK_TAG='pending'),
            [{'Key': 'Team', 'Value': 'ml'}, {'Key': KEY, 'Value': 'pending'}],
        )

    def test_keeps_the_hpto_tag(self):
        self.assertEqual(
            tags(ENABLE_HP_TRAINING_OPERATOR_FEATURE='true', SETUP_STACK_TAG='pending'),
            [{'Key': 'SageMaker', 'Value': 'true'}, {'Key': KEY, 'Value': 'pending'}],
        )

    def test_changes_nothing_without_it(self):
        for env in ({}, {'SETUP_STACK_TAG': ''}):
            with self.subTest(env=env):
                self.assertEqual(tags(CLUSTER_TAGS=f'Team=ml,{KEY}=mine', **env),
                                 [{'Key': 'Team', 'Value': 'ml'}, {'Key': KEY, 'Value': 'mine'}])
                self.assertEqual(tags(**env), [])


if __name__ == '__main__':
    unittest.main()
