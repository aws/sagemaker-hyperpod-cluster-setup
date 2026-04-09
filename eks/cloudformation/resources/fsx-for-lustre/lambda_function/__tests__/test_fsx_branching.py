import unittest
import os
import sys
from unittest.mock import patch, Mock

sys.modules['cfnresponse'] = Mock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_BASE_ENV = {
    'CLUSTER_NAME': 'test-cluster',
    'AWS_REGION': 'us-west-2',
    'PER_UNIT_STORAGE_THROUGHPUT': '200',
    'DATA_COMPRESSION_TYPE': 'LZ4',
    'FILE_SYSTEM_TYPE_VERSION': '2.15',
    'PATH': '/usr/bin',
    'GIT_EXEC_PATH': '/usr/lib/git-core',
    'KUBECONFIG': '/tmp/kubeconfig',
    'LD_LIBRARY_PATH': '/usr/lib',
}


def _create_env(fsx_id='', extra=None):
    """Build env dict with FSX_FILE_SYSTEM_ID set to *fsx_id*."""
    env = {**_BASE_ENV, 'FSX_FILE_SYSTEM_ID': fsx_id}
    if extra:
        env.update(extra)
    return env


@patch('lambda_function.subprocess.run')
@patch('lambda_function.write_kubeconfig')
class TestOnCreateBranching(unittest.TestCase):
    _STEP2_EVENT = {'LogicalResourceId': 'FsxCustomResourceStep2'}

    @patch('lambda_function.create_existing_fsx_resources')
    @patch('lambda_function.create_dynamic_fsx_resources')
    def test_empty_fsx_id_calls_dynamic(self, mock_dynamic, mock_existing, _wk, _run):
        """FSX_FILE_SYSTEM_ID=='' → dynamic provisioning path."""
        from lambda_function import on_create
        with patch.dict(os.environ, _create_env(fsx_id='', extra={'STORAGE_CAPACITY': '1200'}), clear=True):
            on_create(self._STEP2_EVENT)
        mock_dynamic.assert_called_once()
        mock_existing.assert_not_called()

    @patch('lambda_function.create_existing_fsx_resources')
    @patch('lambda_function.create_dynamic_fsx_resources')
    def test_nonempty_fsx_id_calls_existing(self, mock_dynamic, mock_existing, _wk, _run):
        """FSX_FILE_SYSTEM_ID=='fs-123' → existing provisioning path."""
        from lambda_function import on_create
        with patch.dict(os.environ, _create_env(fsx_id='fs-123'), clear=True):
            on_create(self._STEP2_EVENT)
        mock_existing.assert_called_once()
        mock_dynamic.assert_not_called()

    def test_empty_fsx_id_without_storage_capacity_raises(self, _wk, _run):
        """FSX_FILE_SYSTEM_ID=='' and no STORAGE_CAPACITY → ValueError."""
        from lambda_function import on_create
        env = _create_env(fsx_id='')
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Exception) as ctx:
                on_create(self._STEP2_EVENT)
            self.assertIn('STORAGE_CAPACITY', str(ctx.exception))

    def test_nonempty_fsx_id_without_storage_capacity_ok(self, _wk, _run):
        """FSX_FILE_SYSTEM_ID=='fs-123' without STORAGE_CAPACITY should NOT raise."""
        from lambda_function import on_create
        with patch.dict(os.environ, _create_env(fsx_id='fs-123'), clear=True):
            with patch('lambda_function.create_existing_fsx_resources'):
                on_create(self._STEP2_EVENT)


@patch('lambda_function.subprocess.run')
@patch('lambda_function.write_kubeconfig')
class TestOnUpdateBranching(unittest.TestCase):
    _EVENT = {'LogicalResourceId': 'FsxCustomResourceStep2'}

    @patch('lambda_function.create_existing_fsx_resources')
    @patch('lambda_function.create_dynamic_fsx_resources')
    def test_empty_fsx_id_calls_dynamic(self, mock_dynamic, mock_existing, _wk, _run):
        from lambda_function import on_update
        with patch.dict(os.environ, _create_env(fsx_id='', extra={'STORAGE_CAPACITY': '1200'}), clear=True):
            on_update(self._EVENT)
        mock_dynamic.assert_called_once()
        mock_existing.assert_not_called()

    @patch('lambda_function.create_existing_fsx_resources')
    @patch('lambda_function.create_dynamic_fsx_resources')
    def test_nonempty_fsx_id_calls_existing(self, mock_dynamic, mock_existing, _wk, _run):
        from lambda_function import on_update
        with patch.dict(os.environ, _create_env(fsx_id='fs-456'), clear=True):
            on_update(self._EVENT)
        mock_existing.assert_called_once()
        mock_dynamic.assert_not_called()

    def test_empty_fsx_id_without_storage_capacity_raises(self, _wk, _run):
        from lambda_function import on_update
        env = _create_env(fsx_id='')
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Exception) as ctx:
                on_update(self._EVENT)
            self.assertIn('STORAGE_CAPACITY', str(ctx.exception))

    def test_nonempty_fsx_id_without_storage_capacity_ok(self, _wk, _run):
        from lambda_function import on_update
        with patch.dict(os.environ, _create_env(fsx_id='fs-456'), clear=True):
            with patch('lambda_function.create_existing_fsx_resources'):
                on_update(self._EVENT)


def _has_pv_delete_call(mock_run):
    """Check if subprocess.run was called with 'kubectl delete pv'."""
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        if cmd[:3] == ['kubectl', 'delete', 'pv']:
            return True
    return False


@patch('lambda_function.subprocess.run')
@patch('lambda_function.write_kubeconfig')
class TestOnDeleteBranching(unittest.TestCase):
    _EVENT = {'LogicalResourceId': 'FsxCustomResource'}

    def test_nonempty_fsx_id_deletes_pv(self, _wk, mock_run):
        from lambda_function import on_delete
        env = {'CLUSTER_NAME': 'c', 'AWS_REGION': 'us-west-2', 'FSX_FILE_SYSTEM_ID': 'fs-789'}
        with patch.dict(os.environ, env, clear=True):
            on_delete(self._EVENT)
        self.assertTrue(_has_pv_delete_call(mock_run))

    def test_empty_fsx_id_skips_pv_deletion(self, _wk, mock_run):
        from lambda_function import on_delete
        env = {'CLUSTER_NAME': 'c', 'AWS_REGION': 'us-west-2', 'FSX_FILE_SYSTEM_ID': ''}
        with patch.dict(os.environ, env, clear=True):
            on_delete(self._EVENT)
        self.assertFalse(_has_pv_delete_call(mock_run))


if __name__ == '__main__':
    unittest.main()
