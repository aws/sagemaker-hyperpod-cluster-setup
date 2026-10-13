import os
import sys
import unittest
from unittest.mock import Mock, mock_open, patch

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lambda_function import convert_rules_to_json  # noqa: E402

RULES_YAML = """
groups:
  - name: test
    rules:
      - alert: GPUHighTemperature
        expr: DCGM_FI_DEV_GPU_TEMP > 80
"""


@patch('lambda_function.download_and_extract_assets', return_value='/tmp/assets')
@patch('builtins.open', mock_open(read_data=RULES_YAML))
class TestAlertRulesDatasource(unittest.TestCase):
    def _datasource_uids(self):
        return [rule['data'][0]['datasourceUid'] for rule in convert_rules_to_json()]

    @patch.dict(os.environ, {'PROMETHEUS_WORKSPACE_ID': 'ws-123'})
    def test_rules_are_grouped(self, _):
        self.assertEqual([rule['ruleGroup'] for rule in convert_rules_to_json()], ['test'])

    @patch.dict(os.environ, {'PROMETHEUS_WORKSPACE_ID': 'ws-123'})
    def test_rules_use_prometheus_with_amp_workspace(self, _):
        self.assertEqual(self._datasource_uids(), ['prometheus'])

    @patch.dict(os.environ, {'PROMETHEUS_WORKSPACE_ID': ''})
    def test_rules_use_cloudwatch_opentelemetry_without_amp_workspace(self, _):
        self.assertEqual(self._datasource_uids(), ['cloudwatch-opentelemetry'])


if __name__ == '__main__':
    unittest.main()
