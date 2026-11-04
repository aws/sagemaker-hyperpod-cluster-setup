import unittest
import os
import sys
from unittest.mock import patch, Mock

sys.modules['cfnresponse'] = Mock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_OPERATORS = 'trainingOperators.enabled=true,mlflow.enabled=true'

_BASE_ENV = {
    'CLUSTER_NAME': 'test-cluster',
    'AWS_REGION': 'us-east-1',
    'GITHUB_REPO_URL': 'https://github.com/aws/sagemaker-hyperpod-cli.git',
    'GITHUB_REPO_REVISION': 'abc123',
    'CHART_PATH': 'helm_chart/HyperPodHelmChart',
    'NAMESPACE': 'kube-system',
    'RELEASE_NAME': 'dependencies',
    'OPERATORS': _OPERATORS,
    'CREATE_RIG': 'false',
    'MODELEXPRESS_REPO_URL': 'https://github.com/ai-dynamo/modelexpress.git',
    'MODELEXPRESS_REPO_REVISION': 'def456',
    'MODELEXPRESS_NAMESPACE': 'modelexpress',
    'MODELEXPRESS_RELEASE': 'modelexpress',
    'MODELEXPRESS_PROFILE_NAMESPACE': 'default',
    'PATH': '/usr/bin',
}

_ENABLED_ENV = {**_BASE_ENV, 'OPERATORS': _OPERATORS + ',modelexpress.enabled=true'}


def _cmds(mock_run):
    return [c[0][0] for c in mock_run.call_args_list]


def _hyperpod_cmd(mock_run, subcommand):
    cmds = [c for c in _cmds(mock_run) if c[:2] == ['helm', subcommand] and 'dependencies' in c]
    assert len(cmds) == 1, f"expected one HyperPod 'helm {subcommand}' call, got {cmds}"
    return cmds[0]


def _modelexpress_installs(mock_run):
    return [c for c in _cmds(mock_run) if c[:3] == ['helm', 'upgrade', '--install'] and c[3] == 'modelexpress']


def _set_values(cmd):
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '--set']


_UNINSTALL = ['helm', 'uninstall', 'modelexpress', '--namespace', 'modelexpress']


@patch('lambda_function.os.path.exists', return_value=False)
@patch('lambda_function.subprocess.run')
class TestHyperPodChart(unittest.TestCase):

    def test_install_values_unchanged(self, mock_run, _exists):
        from lambda_function import install_helm_chart
        with patch.dict(os.environ, _BASE_ENV, clear=True):
            install_helm_chart()
        self.assertEqual(
            _set_values(_hyperpod_cmd(mock_run, 'install')),
            ['health-monitoring-agent.region=us-east-1', _OPERATORS],
        )


@patch('lambda_function.os.path.exists', return_value=False)
@patch('lambda_function.subprocess.run')
class TestModelExpress(unittest.TestCase):

    def test_install_disabled_skips_modelexpress(self, mock_run, _exists):
        from lambda_function import install_helm_chart
        with patch.dict(os.environ, _BASE_ENV, clear=True):
            install_helm_chart()
        self.assertEqual(_modelexpress_installs(mock_run), [])
        self.assertEqual([c for c in _cmds(mock_run) if c[0] == 'kubectl'], [])

    def test_install_enabled_installs_upstream_chart(self, mock_run, _exists):
        from lambda_function import install_helm_chart
        with patch.dict(os.environ, _ENABLED_ENV, clear=True):
            install_helm_chart()
        self.assertIn(['git', '-C', '/tmp/modelexpress', 'checkout', 'def456'], _cmds(mock_run))
        cmds = _modelexpress_installs(mock_run)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][4], '/tmp/modelexpress/helm')
        self.assertIn('--create-namespace', cmds[0])
        self.assertEqual(
            _set_values(cmds[0]),
            ['env.MX_METADATA_BACKEND=kubernetes', 'serviceAccount.rbac.enabled=true', 'persistence.enabled=false'],
        )

    def test_install_applies_client_profiles(self, mock_run, _exists):
        from lambda_function import install_helm_chart
        with patch.dict(os.environ, _ENABLED_ENV, clear=True):
            install_helm_chart()
        applies = [c for c in mock_run.call_args_list if c[0][0] == ['kubectl', 'apply', '-f', '-']]
        self.assertEqual(len(applies), 1)
        manifest = applies[0][1]['input']
        self.assertIn('name: modelexpress-gdr', manifest)
        self.assertIn('name: modelexpress-non-gdr', manifest)
        self.assertIn('namespace: default', manifest)
        self.assertIn('MX_SERVER_ADDRESS: modelexpress.modelexpress.svc.cluster.local:8001', manifest)

    def test_delete_uninstalls_existing_modelexpress(self, mock_run, _exists):
        from lambda_function import on_delete
        mock_run.return_value = Mock(returncode=0, stdout='')  # helm status: release exists
        with patch.dict(os.environ, _BASE_ENV, clear=True), patch('lambda_function.write_kubeconfig'):
            on_delete()
        self.assertIn(_UNINSTALL, _cmds(mock_run))

    def test_delete_without_release_skips_modelexpress(self, mock_run, _exists):
        from lambda_function import on_delete
        mock_run.return_value = Mock(returncode=1, stdout='')  # helm status: not found
        with patch.dict(os.environ, _BASE_ENV, clear=True), patch('lambda_function.write_kubeconfig'):
            on_delete()
        self.assertNotIn(_UNINSTALL, _cmds(mock_run))


if __name__ == '__main__':
    unittest.main()
