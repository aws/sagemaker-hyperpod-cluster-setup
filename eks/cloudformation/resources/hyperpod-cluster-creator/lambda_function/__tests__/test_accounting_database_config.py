"""
Unit tests for the external accounting database config parsing in the HyperPod cluster Lambda.
"""
import unittest
import json
import os
from unittest.mock import Mock
import sys

# Mock cfnresponse before importing lambda_function
sys.modules['cfnresponse'] = Mock()

# Add the parent lambda_function directory (containing lambda_function.py) to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lambda_function import get_accounting_database_config_from_env


class TestAccountingDatabaseConfig(unittest.TestCase):
    """Test cases for ACCOUNTING_DATABASE_CONFIG parsing."""

    ENV = 'ACCOUNTING_DATABASE_CONFIG'

    def setUp(self):
        os.environ.pop(self.ENV, None)

    def tearDown(self):
        os.environ.pop(self.ENV, None)

    def test_returns_none_when_unset_or_empty(self):
        self.assertIsNone(get_accounting_database_config_from_env())
        os.environ[self.ENV] = ''
        self.assertIsNone(get_accounting_database_config_from_env())

    def test_returns_none_on_invalid_json(self):
        os.environ[self.ENV] = 'not-json'
        self.assertIsNone(get_accounting_database_config_from_env())

    def test_full_config(self):
        os.environ[self.ENV] = json.dumps({
            'Endpoint': 'db.example.com',
            'Port': 5306,
            'Name': 'my_acct',
            'SecretArn': 'arn:aws:secretsmanager:us-west-2:111122223333:secret:x',
        })
        result = get_accounting_database_config_from_env()
        self.assertEqual(result, {
            'Endpoint': 'db.example.com',
            'SecretArn': 'arn:aws:secretsmanager:us-west-2:111122223333:secret:x',
            'Port': 5306,
            'Name': 'my_acct',
        })

    def test_minimal_config_omits_optional_fields(self):
        os.environ[self.ENV] = json.dumps({
            'Endpoint': 'db.example.com',
            'SecretArn': 'arn:aws:secretsmanager:us-west-2:111122223333:secret:x',
        })
        result = get_accounting_database_config_from_env()
        self.assertEqual(set(result.keys()), {'Endpoint', 'SecretArn'})

    def test_port_coerced_to_int(self):
        os.environ[self.ENV] = json.dumps({
            'Endpoint': 'db.example.com',
            'Port': '3306',
            'SecretArn': 'arn:aws:secretsmanager:us-west-2:111122223333:secret:x',
        })
        result = get_accounting_database_config_from_env()
        self.assertEqual(result['Port'], 3306)
        self.assertIsInstance(result['Port'], int)

    def test_returns_none_on_invalid_port(self):
        os.environ[self.ENV] = json.dumps({
            'Endpoint': 'db.example.com',
            'Port': 'not-a-number',
            'SecretArn': 'arn:aws:secretsmanager:us-west-2:111122223333:secret:x',
        })
        self.assertIsNone(get_accounting_database_config_from_env())

    def test_returns_none_when_required_fields_missing(self):
        os.environ[self.ENV] = json.dumps({'Endpoint': 'db.example.com'})
        self.assertIsNone(get_accounting_database_config_from_env())
        os.environ[self.ENV] = json.dumps({
            'SecretArn': 'arn:aws:secretsmanager:us-west-2:111122223333:secret:x',
        })
        self.assertIsNone(get_accounting_database_config_from_env())


if __name__ == '__main__':
    unittest.main()
