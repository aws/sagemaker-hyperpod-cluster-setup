import json
import tempfile
import unittest
import os
import sys
from unittest.mock import patch, Mock

import yaml

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
    'MODELEXPRESS_CHART_SHA256': 'f00d',
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

    def setUp(self):
        patcher = patch('lambda_function.verify_modelexpress_chart')
        self.mock_verify = patcher.start()
        self.addCleanup(patcher.stop)

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
        self.mock_verify.assert_called_once_with('/tmp/modelexpress', '/tmp/modelexpress/helm')
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
        self.assertNotIn('FI_EFA_USE_DEVICE_RDMA', manifest)

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


def _sc(name, provisioner, default=None):
    annotations = {default: 'true'} if default else {}
    return {'metadata': {'name': name, 'annotations': annotations}, 'provisioner': provisioner}


_DEFAULT = 'storageclass.kubernetes.io/is-default-class'
_BETA_DEFAULT = 'storageclass.beta.kubernetes.io/is-default-class'
_STORAGECLASS_READ = ['kubectl', 'get', 'storageclass', '-o', 'json']
_APPLY = ['kubectl', 'apply', '-f', '-']
_ANNOTATE_GP3 = ['kubectl', 'annotate', 'storageclass', 'gp3', _DEFAULT + '=true', '--overwrite']
_DELETE_MANAGED = ['kubectl', 'delete', 'storageclass', '--selector',
                   'app.kubernetes.io/managed-by=sagemaker-hyperpod-cluster-setup', '--ignore-not-found']
_STORAGE_CLASS_ENV = {**_BASE_ENV, 'CREATE_DEFAULT_STORAGE_CLASS': 'true'}


def _kubectl_reads(*storage_classes):
    """subprocess.run side effect answering the StorageClass listing the step makes."""
    def run(cmd, **kwargs):
        if cmd == _STORAGECLASS_READ:
            return Mock(returncode=0, stdout=json.dumps({'items': list(storage_classes)}))
        return Mock(returncode=0, stdout='')
    return run


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)


class TestModelExpressChartChecksum(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.chart = os.path.join(self.repo, 'helm')
        _write(os.path.join(self.chart, 'Chart.yaml'), b'name: modelexpress\n')
        _write(os.path.join(self.chart, 'templates', 'server.yaml'), b'kind: Deployment\n')

    def _verify(self, head='def456', sha256=None):
        from lambda_function import modelexpress_chart_sha256, verify_modelexpress_chart
        env = {**_BASE_ENV, 'MODELEXPRESS_CHART_SHA256': sha256 or modelexpress_chart_sha256(self.chart)}
        with patch.dict(os.environ, env, clear=True), \
                patch('lambda_function.subprocess.run', return_value=Mock(stdout=head + '\n')) as mock_run:
            verify_modelexpress_chart(self.repo, self.chart)
        return mock_run

    def test_matches_the_documented_shell_command(self):
        from lambda_function import modelexpress_chart_sha256
        import hashlib
        # find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum
        listing = ''.join(
            f'{hashlib.sha256(content).hexdigest()}  {path}\n'
            for path, content in sorted([
                ('./Chart.yaml', b'name: modelexpress\n'),
                ('./templates/server.yaml', b'kind: Deployment\n'),
            ])
        )
        self.assertEqual(modelexpress_chart_sha256(self.chart), hashlib.sha256(listing.encode()).hexdigest())

    def test_passes_for_the_pinned_commit_and_checksum(self):
        mock_run = self._verify()
        mock_run.assert_called_once_with(
            ['git', '-C', self.repo, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True,
        )

    def test_rejects_a_modified_file(self):
        from lambda_function import modelexpress_chart_sha256
        expected = modelexpress_chart_sha256(self.chart)
        _write(os.path.join(self.chart, 'templates', 'server.yaml'), b'kind: DaemonSet\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self._verify(sha256=expected)

    def test_rejects_an_added_file(self):
        from lambda_function import modelexpress_chart_sha256
        expected = modelexpress_chart_sha256(self.chart)
        _write(os.path.join(self.chart, 'templates', 'extra.yaml'), b'kind: ClusterRoleBinding\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self._verify(sha256=expected)

    def test_rejects_a_renamed_file(self):
        from lambda_function import modelexpress_chart_sha256
        expected = modelexpress_chart_sha256(self.chart)
        os.rename(os.path.join(self.chart, 'templates', 'server.yaml'), os.path.join(self.chart, 'templates', 'srv.yaml'))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self._verify(sha256=expected)

    def test_rejects_a_symlink(self):
        os.symlink('/etc/passwd', os.path.join(self.chart, 'templates', 'link.yaml'))
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self._verify(sha256='0' * 64)

    def test_rejects_a_different_commit(self):
        with self.assertRaisesRegex(ValueError, 'expected def456'):
            self._verify(head='0123abc')


@patch('lambda_function.os.path.exists', return_value=False)
@patch('lambda_function.subprocess.run')
class TestModelExpressInstallFailsClosed(unittest.TestCase):

    def test_checksum_mismatch_stops_before_helm_install(self, mock_run, _exists):
        from lambda_function import install_helm_chart
        with patch.dict(os.environ, _ENABLED_ENV, clear=True), \
                patch('lambda_function.verify_modelexpress_chart', side_effect=ValueError('checksum mismatch')):
            with self.assertRaisesRegex(Exception, 'checksum mismatch'):
                install_helm_chart()
        self.assertEqual(_modelexpress_installs(mock_run), [])
        self.assertEqual([c for c in _cmds(mock_run) if c[0] == 'kubectl'], [])


@patch('lambda_function.subprocess.run')
class TestDefaultStorageClass(unittest.TestCase):

    def _apply(self, mock_run, env, *storage_classes):
        from lambda_function import apply_default_storage_class
        mock_run.side_effect = _kubectl_reads(*storage_classes)
        with patch.dict(os.environ, env, clear=True):
            created = apply_default_storage_class()
        manifests = [c[1]['input'] for c in mock_run.call_args_list if c[0][0] == _APPLY]
        return created, manifests, _cmds(mock_run)

    def test_creates_default_gp3_when_nothing_is_default(self, mock_run):
        created, manifests, cmds = self._apply(mock_run, _STORAGE_CLASS_ENV, _sc('gp2', 'kubernetes.io/aws-ebs'))
        self.assertTrue(created)
        self.assertEqual(len(manifests), 1)
        manifest = yaml.safe_load(manifests[0])
        self.assertEqual(manifest['kind'], 'StorageClass')
        self.assertEqual(manifest['metadata']['name'], 'gp3')
        self.assertEqual(manifest['metadata']['annotations'], {_DEFAULT: 'true'})
        self.assertEqual(manifest['metadata']['labels'], {'app.kubernetes.io/managed-by': 'sagemaker-hyperpod-cluster-setup'})
        self.assertEqual(manifest['provisioner'], 'ebs.csi.aws.com')
        self.assertEqual(manifest['parameters'], {'type': 'gp3', 'encrypted': 'true'})
        self.assertEqual(manifest['volumeBindingMode'], 'WaitForFirstConsumer')
        self.assertTrue(manifest['allowVolumeExpansion'])

    def test_skips_a_cluster_this_stack_did_not_create(self, mock_run):
        created, manifests, cmds = self._apply(mock_run, _BASE_ENV)
        self.assertFalse(created)
        self.assertEqual(manifests, [])
        self.assertEqual([c for c in cmds if c[0] == 'kubectl'], [])

    def test_fails_loudly_with_stderr_when_kubectl_fails(self, mock_run):
        import subprocess
        from lambda_function import apply_default_storage_class
        mock_run.side_effect = subprocess.CalledProcessError(1, _STORAGECLASS_READ, stderr='Unauthorized')
        with patch.dict(os.environ, _STORAGE_CLASS_ENV, clear=True), \
                self.assertRaisesRegex(Exception, 'Failed to create the default StorageClass.*Unauthorized'):
            apply_default_storage_class()

    def test_fails_without_a_stray_none_when_stderr_was_not_captured(self, mock_run):
        import subprocess
        from lambda_function import apply_default_storage_class

        def run(cmd, **kwargs):
            if cmd == _ANNOTATE_GP3:
                raise subprocess.CalledProcessError(1, cmd)  # stderr not captured -> None
            return _kubectl_reads(_sc('gp3', 'ebs.csi.aws.com'))(cmd, **kwargs)

        mock_run.side_effect = run
        with patch.dict(os.environ, _STORAGE_CLASS_ENV, clear=True), self.assertRaises(Exception) as raised:
            apply_default_storage_class()
        self.assertIn('Failed to create the default StorageClass', str(raised.exception))
        self.assertNotIn('None', str(raised.exception))

    def test_marks_an_existing_gp3_class_default_instead_of_recreating_it(self, mock_run):
        created, manifests, cmds = self._apply(mock_run, _STORAGE_CLASS_ENV, _sc('gp3', 'ebs.csi.aws.com'))
        self.assertTrue(created)
        self.assertEqual(manifests, [])
        self.assertIn(_ANNOTATE_GP3, cmds)

    def test_refuses_a_gp3_class_on_another_provisioner(self, mock_run):
        from lambda_function import apply_default_storage_class
        mock_run.side_effect = _kubectl_reads(_sc('gp3', 'kubernetes.io/aws-ebs'))
        with patch.dict(os.environ, _STORAGE_CLASS_ENV, clear=True), \
                self.assertRaisesRegex(Exception, 'provisioner kubernetes.io/aws-ebs, expected ebs.csi.aws.com'):
            apply_default_storage_class()
        self.assertNotIn(_ANNOTATE_GP3, _cmds(mock_run))

    def test_is_a_no_op_once_gp3_is_the_default(self, mock_run):
        created, manifests, cmds = self._apply(mock_run, _STORAGE_CLASS_ENV, _sc('gp3', 'ebs.csi.aws.com', default=_DEFAULT))
        self.assertFalse(created)
        self.assertEqual(manifests, [])
        self.assertEqual([c for c in cmds if c[0] == 'kubectl'], [_STORAGECLASS_READ])

    def test_an_existing_default_wins_over_an_existing_gp3(self, mock_run):
        created, manifests, cmds = self._apply(
            mock_run, _STORAGE_CLASS_ENV, _sc('gp3', 'ebs.csi.aws.com'), _sc('gp2', 'kubernetes.io/aws-ebs', default=_DEFAULT)
        )
        self.assertFalse(created)
        self.assertEqual(manifests, [])
        self.assertEqual([c for c in cmds if c[0] == 'kubectl'], [_STORAGECLASS_READ])

    def test_keeps_an_existing_default(self, mock_run):
        for annotation in (_DEFAULT, _BETA_DEFAULT):
            mock_run.reset_mock()
            created, manifests, cmds = self._apply(
                mock_run, _STORAGE_CLASS_ENV, _sc('gp2', 'kubernetes.io/aws-ebs', default=annotation)
            )
            self.assertFalse(created, annotation)
            self.assertEqual(manifests, [])
            self.assertNotIn(_ANNOTATE_GP3, cmds)

    def test_delete_removes_only_the_managed_class(self, mock_run):
        from lambda_function import on_delete
        mock_run.return_value = Mock(returncode=1, stdout='')  # helm status: ModelExpress not found
        with patch.dict(os.environ, _BASE_ENV, clear=True), patch('lambda_function.write_kubeconfig'):
            on_delete()
        self.assertIn(_DELETE_MANAGED, _cmds(mock_run))

    def test_create_reports_the_storage_class(self, mock_run):
        from lambda_function import on_create
        mock_run.side_effect = _kubectl_reads()
        with patch.dict(os.environ, _STORAGE_CLASS_ENV, clear=True), \
                patch('lambda_function.write_kubeconfig'), \
                patch('lambda_function.os.path.exists', return_value=False):
            response = on_create()
        self.assertTrue(response['CustomChartInstalled'])
        self.assertTrue(response['DefaultStorageClassSet'])


if __name__ == '__main__':
    unittest.main()
