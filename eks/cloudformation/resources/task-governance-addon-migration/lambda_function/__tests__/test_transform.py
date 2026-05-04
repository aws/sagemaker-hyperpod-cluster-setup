import os
import sys
import json
import unittest
from unittest.mock import Mock, patch, MagicMock
from io import BytesIO

sys.modules['cfnresponse'] = Mock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lambda_function import (  # noqa: E402
    ALL_KUEUE_CRDS,
    TIMEOUT_BUFFER_MS,
    _do_restore,
    apply_resource,
    delete_crs_for_crd,
    extract_major_minor,
    force_stored_versions,
    get_migration_spec,
    migrate_storage_version,
    on_backup,
    patch_crd_storage_version,
    patch_stored_versions,
    strip_metadata,
    transform_resource,
)

# Reusable spec for the currently-supported multi-hop upgrade path.
V13_TO_V15 = {
    'source_apis': ['v1alpha1', 'v1beta1'],
    'target_api': 'v1beta2',
    'crds': sorted(ALL_KUEUE_CRDS),
}


class TestStripMetadata(unittest.TestCase):
    def test_strips_server_fields_and_status(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta2',
            'kind': 'Topology',
            'metadata': {
                'name': 'keep-me',
                'namespace': 'keep-me',
                'resourceVersion': '123',
                'uid': 'abc',
                'creationTimestamp': '2020-01-01T00:00:00Z',
                'generation': 5,
                'managedFields': [{'manager': 'kubectl'}],
                'selfLink': '/should/go',
            },
            'spec': {'levels': []},
            'status': {'conditions': []},
        }
        cleaned = strip_metadata(resource)
        self.assertEqual(cleaned['metadata'], {'name': 'keep-me', 'namespace': 'keep-me'})
        self.assertNotIn('status', cleaned)
        # Original input must not be mutated
        self.assertIn('status', resource)
        self.assertIn('uid', resource['metadata'])

    def test_strips_last_applied_configuration_annotation(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'ClusterQueue',
            'metadata': {
                'name': 'cq1',
                'annotations': {
                    'kubectl.kubernetes.io/last-applied-configuration': '{"big":"json"}',
                    'custom-annotation': 'keep-me',
                },
            },
        }
        cleaned = strip_metadata(resource)
        self.assertNotIn('kubectl.kubernetes.io/last-applied-configuration',
                         cleaned['metadata'].get('annotations', {}))
        self.assertEqual(cleaned['metadata']['annotations']['custom-annotation'], 'keep-me')

    def test_removes_empty_annotations_after_stripping(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Topology',
            'metadata': {
                'name': 't1',
                'annotations': {
                    'kubectl.kubernetes.io/last-applied-configuration': '{}',
                },
            },
        }
        cleaned = strip_metadata(resource)
        self.assertNotIn('annotations', cleaned['metadata'])


class TestExtractMajorMinor(unittest.TestCase):
    def test_eksbuild_suffix(self):
        self.assertEqual(extract_major_minor('v1.3.1-eksbuild.1'), 'v1.3')

    def test_no_v_prefix(self):
        self.assertEqual(extract_major_minor('1.5.0-eksbuild.2'), 'v1.5')

    def test_empty(self):
        self.assertEqual(extract_major_minor(''), '')

    def test_single_number(self):
        self.assertEqual(extract_major_minor('v1'), '')


class TestGetMigrationSpec(unittest.TestCase):
    # --- Same-version no-ops ---
    def test_same_version_returns_none(self):
        self.assertIsNone(get_migration_spec('v1.3.0-eksbuild.1', 'v1.3.1-eksbuild.1'))

    def test_v1_1_same_version_returns_none(self):
        self.assertIsNone(get_migration_spec('v1.1.0-eksbuild.1', 'v1.1.3-eksbuild.3'))

    def test_v1_4_same_version_returns_none(self):
        self.assertIsNone(get_migration_spec('v1.4.0-eksbuild.1', 'v1.4.0-eksbuild.2'))

    def test_v1_5_same_version_returns_none(self):
        self.assertIsNone(get_migration_spec('v1.5.0-eksbuild.1', 'v1.5.0-eksbuild.2'))

    def test_v1_1_to_v1_2_same_canonical_returns_none(self):
        """v1.1 and v1.2 share canonical v1.3 → no migration needed."""
        self.assertIsNone(get_migration_spec('v1.1.0-eksbuild.1', 'v1.2.0-eksbuild.1'))

    def test_v1_2_to_v1_3_same_canonical_returns_none(self):
        self.assertIsNone(get_migration_spec('v1.2.0-eksbuild.1', 'v1.3.0-eksbuild.1'))

    def test_v1_0_same_canonical_returns_none(self):
        """v1.0 canonicalizes to v1.3 → v1.0→v1.1 is a no-op."""
        self.assertIsNone(get_migration_spec('v1.0.0-eksbuild.3', 'v1.1.0-eksbuild.1'))

    # --- Single-hop migrations ---
    def test_v1_3_to_v1_4_returns_spec(self):
        spec = get_migration_spec('v1.3.1-eksbuild.1', 'v1.4.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1'])
        self.assertEqual(spec['target_api'], 'v1beta1')
        self.assertEqual(spec['crds'], ALL_KUEUE_CRDS)

    def test_v1_1_to_v1_4_returns_spec(self):
        """v1.1 canonicalizes to v1.3, so this is a single hop v1.3→v1.4."""
        spec = get_migration_spec('v1.1.3-eksbuild.3', 'v1.4.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1'])
        self.assertEqual(spec['target_api'], 'v1beta1')

    def test_v1_0_to_v1_4_returns_spec(self):
        """v1.0 canonicalizes to v1.3, so this is a single hop v1.3→v1.4."""
        spec = get_migration_spec('v1.0.0-eksbuild.3', 'v1.4.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1'])
        self.assertEqual(spec['target_api'], 'v1beta1')

    def test_v1_4_to_v1_5_returns_spec(self):
        spec = get_migration_spec('v1.4.0-eksbuild.1', 'v1.5.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1beta1'])
        self.assertEqual(spec['target_api'], 'v1beta2')

    # --- Multi-hop composed migrations ---
    def test_v1_3_to_v1_5_composes_multi_hop(self):
        spec = get_migration_spec('v1.3.1-eksbuild.1', 'v1.5.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1', 'v1beta1'])
        self.assertEqual(spec['target_api'], 'v1beta2')
        self.assertEqual(len(spec['crds']), len(ALL_KUEUE_CRDS))

    def test_v1_2_to_v1_5_composes_multi_hop(self):
        spec = get_migration_spec('v1.2.2-eksbuild.1', 'v1.5.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1', 'v1beta1'])
        self.assertEqual(spec['target_api'], 'v1beta2')

    def test_v1_1_to_v1_5_composes_multi_hop(self):
        spec = get_migration_spec('v1.1.0-eksbuild.1', 'v1.5.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1', 'v1beta1'])
        self.assertEqual(spec['target_api'], 'v1beta2')

    def test_v1_0_to_v1_5_composes_multi_hop(self):
        """v1.0 (Kueue v0.8.1) → v1.5 (Kueue v0.16) via v1.3→v1.4→v1.5."""
        spec = get_migration_spec('v1.0.0-eksbuild.6', 'v1.5.0-eksbuild.1')
        self.assertEqual(spec['source_apis'], ['v1alpha1', 'v1beta1'])
        self.assertEqual(spec['target_api'], 'v1beta2')

    # --- Error cases ---
    def test_unknown_path_raises(self):
        with self.assertRaises(ValueError):
            get_migration_spec('v1.3.1-eksbuild.1', 'v2.0.0-eksbuild.1')

    def test_empty_version_raises(self):
        with self.assertRaises(ValueError):
            get_migration_spec('', 'v1.5.0-eksbuild.1')

    # --- CRD coverage ---
    def test_all_kueue_crds_has_11_entries(self):
        self.assertEqual(len(ALL_KUEUE_CRDS), 11)

    def test_all_kueue_crds_includes_key_resources(self):
        for crd in ['topologies.kueue.x-k8s.io', 'cohorts.kueue.x-k8s.io',
                     'clusterqueues.kueue.x-k8s.io', 'workloads.kueue.x-k8s.io',
                     'resourceflavors.kueue.x-k8s.io']:
            self.assertIn(crd, ALL_KUEUE_CRDS)


class TestTransformResource(unittest.TestCase):
    def test_v1beta1_cohort_renames_parent_to_parentName(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Cohort',
            'metadata': {'name': 'child', 'uid': 'x'},
            'spec': {'parent': 'root-cohort'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertEqual(out['spec'], {'parentName': 'root-cohort'})
        self.assertNotIn('parent', out['spec'])
        self.assertNotIn('uid', out['metadata'])

    def test_v1alpha1_cohort_renames_parent_to_parentName(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1alpha1',
            'kind': 'Cohort',
            'metadata': {'name': 'child'},
            'spec': {'parent': 'root-cohort'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertEqual(out['spec'], {'parentName': 'root-cohort'})

    def test_target_passthrough_no_changes(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta2',
            'kind': 'Cohort',
            'metadata': {'name': 'already-migrated'},
            'spec': {'parentName': 'root-cohort'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertEqual(out['spec'], {'parentName': 'root-cohort'})

    def test_v1beta1_topology_apiversion_only(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Topology',
            'metadata': {'name': 't1'},
            'spec': {'levels': [{'nodeLabel': 'topology.kubernetes.io/zone'}]},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertEqual(out['spec'], {'levels': [{'nodeLabel': 'topology.kubernetes.io/zone'}]})

    def test_missing_spec_does_not_crash(self):
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Topology',
            'metadata': {'name': 'no-spec'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertNotIn('spec', out)

    def test_does_not_overwrite_existing_parentName(self):
        """If both 'parent' and 'parentName' present, parentName wins and parent is removed."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Cohort',
            'metadata': {'name': 'c'},
            'spec': {'parent': 'old', 'parentName': 'new'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['spec']['parentName'], 'new')
        self.assertNotIn('parent', out['spec'])

    def test_v1_4_to_v1_5_topology_transform(self):
        """v1.4 → v1.5 path only contains v1beta1 in source_apis and targets v1beta2."""
        v14_to_v15 = get_migration_spec('v1.4.0-eksbuild.1', 'v1.5.0-eksbuild.1')
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Topology',
            'metadata': {'name': 't1'},
            'spec': {'levels': [{'nodeLabel': 'topology.kubernetes.io/zone'}]},
        }
        out = transform_resource(resource, v14_to_v15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')

    def test_clusterqueue_cohort_renamed_to_cohortName(self):
        """ClusterQueue spec.cohort → spec.cohortName on v1beta2 target."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq1'},
            'spec': {'cohort': 'shared-pool'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertEqual(out['spec']['cohortName'], 'shared-pool')
        self.assertNotIn('cohort', out['spec'])

    def test_clusterqueue_without_cohort_unchanged(self):
        """ClusterQueue without cohort field is not modified."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq-no-cohort'},
            'spec': {'resourceGroups': []},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta2')
        self.assertNotIn('cohort', out['spec'])
        self.assertNotIn('cohortName', out['spec'])

    def test_clusterqueue_with_both_cohort_and_cohortName(self):
        """If both cohort and cohortName present, cohortName wins."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq-both'},
            'spec': {'cohort': 'old-pool', 'cohortName': 'new-pool'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['spec']['cohortName'], 'new-pool')
        self.assertNotIn('cohort', out['spec'])

    def test_clusterqueue_cohort_not_renamed_on_v1beta1_target(self):
        """cohort→cohortName rename only applies when target is v1beta2."""
        v13_to_v14 = get_migration_spec('v1.3.0-eksbuild.1', 'v1.4.0-eksbuild.1')
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1alpha1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq1'},
            'spec': {'cohort': 'shared-pool'},
        }
        out = transform_resource(resource, v13_to_v14)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta1')
        self.assertEqual(out['spec']['cohort'], 'shared-pool')
        self.assertNotIn('cohortName', out['spec'])

    def test_non_cohort_parent_field_not_renamed(self):
        """parent→parentName rename only applies to Cohort kind."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'SomeOtherResource',
            'metadata': {'name': 'r1'},
            'spec': {'parent': 'should-stay'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertEqual(out['spec']['parent'], 'should-stay')
        self.assertNotIn('parentName', out['spec'])

    def test_cohort_parent_renamed_on_v1beta1_target(self):
        """Cohort parent→parentName applies for v1beta1 target too (v1alpha1→v1beta1)."""
        v13_to_v14 = get_migration_spec('v1.3.0-eksbuild.1', 'v1.4.0-eksbuild.1')
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1alpha1',
            'kind': 'Cohort',
            'metadata': {'name': 'c1'},
            'spec': {'parent': 'root'},
        }
        out = transform_resource(resource, v13_to_v14)
        self.assertEqual(out['apiVersion'], 'kueue.x-k8s.io/v1beta1')
        self.assertEqual(out['spec']['parentName'], 'root')
        self.assertNotIn('parent', out['spec'])

    def test_clusterqueue_admissionChecks_removed_on_v1beta2(self):
        """ClusterQueue spec.admissionChecks is removed for v1beta2 target."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq1'},
            'spec': {'admissionChecks': ['check1', 'check2'], 'cohort': 'pool'},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertNotIn('admissionChecks', out['spec'])
        self.assertEqual(out['spec']['cohortName'], 'pool')

    def test_clusterqueue_admissionChecks_kept_on_v1beta1(self):
        """ClusterQueue spec.admissionChecks is NOT removed for v1beta1 target."""
        v13_to_v14 = get_migration_spec('v1.3.0-eksbuild.1', 'v1.4.0-eksbuild.1')
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1alpha1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq1'},
            'spec': {'admissionChecks': ['check1']},
        }
        out = transform_resource(resource, v13_to_v14)
        self.assertEqual(out['spec']['admissionChecks'], ['check1'])

    def test_admissioncheck_retryDelayMinutes_removed_on_v1beta2(self):
        """AdmissionCheck spec.retryDelayMinutes is removed for v1beta2 target."""
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'AdmissionCheck',
            'metadata': {'name': 'ac1'},
            'spec': {'controllerName': 'test', 'retryDelayMinutes': 15},
        }
        out = transform_resource(resource, V13_TO_V15)
        self.assertNotIn('retryDelayMinutes', out['spec'])
        self.assertEqual(out['spec']['controllerName'], 'test')

    def test_admissioncheck_retryDelayMinutes_kept_on_v1beta1(self):
        """AdmissionCheck spec.retryDelayMinutes is NOT removed for v1beta1 target."""
        v13_to_v14 = get_migration_spec('v1.3.0-eksbuild.1', 'v1.4.0-eksbuild.1')
        resource = {
            'apiVersion': 'kueue.x-k8s.io/v1alpha1',
            'kind': 'AdmissionCheck',
            'metadata': {'name': 'ac1'},
            'spec': {'controllerName': 'test', 'retryDelayMinutes': 15},
        }
        out = transform_resource(resource, v13_to_v14)
        self.assertEqual(out['spec']['retryDelayMinutes'], 15)


def _make_context(remaining_ms=600_000):
    """Create a mock Lambda context with configurable remaining time."""
    ctx = Mock()
    ctx.get_remaining_time_in_millis.return_value = remaining_ms
    return ctx


def _s3_get_body(data):
    """Create a mock S3 GetObject response body."""
    body = Mock()
    body.read.return_value = json.dumps(data).encode('utf-8')
    return {'Body': body}


# Module path prefix for patching
_M = 'lambda_function'


class TestDoRestore(unittest.TestCase):
    """Tests for _do_restore fallback logic and continue vs failed branching."""

    def setUp(self):
        self.env = {
            'EKS_CLUSTER_NAME': 'test-cluster',
            'BACKUP_S3_BUCKET': 'test-bucket',
            'CURRENT_TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.0-eksbuild.1',
            'TASK_GOVERNANCE_ADDON_VERSION': 'v1.5.0-eksbuild.1',
        }
        self.ctx = _make_context()
        self.resource_v1beta1 = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'ClusterQueue',
            'metadata': {'name': 'cq1', 'resourceVersion': '100'},
            'spec': {'cohort': 'pool'},
        }

    @patch.dict(os.environ, {
        'EKS_CLUSTER_NAME': 'c', 'BACKUP_S3_BUCKET': 'b',
        'CURRENT_TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.0-eksbuild.1',
        'TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.1-eksbuild.1',
    })
    @patch(f'{_M}.write_kubeconfig')
    def test_no_migration_needed_returns_success(self, _wk):
        result = _do_restore(self.ctx)
        self.assertEqual(result['RestoredCRs'], '0')

    @patch(f'{_M}.apply_resource', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_all_resources_apply_successfully(self, mock_boto3, _wk, mock_apply):
        with patch.dict(os.environ, self.env):
            s3 = MagicMock()
            mock_boto3.client.return_value = s3
            manifest = {'backup_keys': ['key1']}
            s3.get_object.side_effect = [
                _s3_get_body(manifest),
                _s3_get_body([self.resource_v1beta1]),
            ]
            result = _do_restore(self.ctx)
        self.assertEqual(result['RestoredCRs'], '1')
        self.assertEqual(result['FailedCRs'], '0')
        # apply_resource called once with transformed resource
        self.assertEqual(mock_apply.call_count, 1)

    @patch(f'{_M}.apply_resource')
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_fallback_to_original_on_transform_failure(self, mock_boto3, _wk, mock_apply):
        """When transformed apply fails but original succeeds, resource is counted as restored."""
        mock_apply.side_effect = [False, True]  # transformed fails, original succeeds
        with patch.dict(os.environ, self.env):
            s3 = MagicMock()
            mock_boto3.client.return_value = s3
            s3.get_object.side_effect = [
                _s3_get_body({'backup_keys': ['key1']}),
                _s3_get_body([self.resource_v1beta1]),
            ]
            result = _do_restore(self.ctx)
        self.assertEqual(result['RestoredCRs'], '1')
        self.assertEqual(result['FailedCRs'], '0')
        self.assertIn('ClusterQueue/cq1', json.loads(result['FallbackRestoredCRs']))

    @patch(f'{_M}.apply_resource', return_value=False)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_both_transformed_and_fallback_fail_increments_failed(self, mock_boto3, _wk, mock_apply):
        """When both transformed and original apply fail, failed counter increments."""
        with patch.dict(os.environ, self.env):
            s3 = MagicMock()
            mock_boto3.client.return_value = s3
            s3.get_object.side_effect = [
                _s3_get_body({'backup_keys': ['key1']}),
                _s3_get_body([self.resource_v1beta1]),
            ]
            with self.assertRaises(RuntimeError) as cm:
                _do_restore(self.ctx)
        self.assertIn('Failed: 1', str(cm.exception))

    @patch(f'{_M}.apply_resource')
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_same_schema_no_fallback_attempt(self, mock_boto3, _wk, mock_apply):
        """When original == transformed (already target api), no fallback is attempted."""
        resource_already_target = {
            'apiVersion': 'kueue.x-k8s.io/v1beta2',
            'kind': 'Topology',
            'metadata': {'name': 't1'},
            'spec': {'levels': []},
        }
        mock_apply.return_value = False  # transformed fails
        with patch.dict(os.environ, self.env):
            s3 = MagicMock()
            mock_boto3.client.return_value = s3
            s3.get_object.side_effect = [
                _s3_get_body({'backup_keys': ['key1']}),
                _s3_get_body([resource_already_target]),
            ]
            with self.assertRaises(RuntimeError) as cm:
                _do_restore(self.ctx)
        # apply_resource called only once (no fallback since original == transformed)
        self.assertEqual(mock_apply.call_count, 1)
        self.assertIn('Failed: 1', str(cm.exception))

    @patch(f'{_M}.apply_resource', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_no_backup_manifest_returns_success(self, mock_boto3, _wk, mock_apply):
        """When backup manifest doesn't exist (NoSuchKey), returns success with 0 restored."""
        from botocore.exceptions import ClientError
        with patch.dict(os.environ, self.env):
            s3 = MagicMock()
            mock_boto3.client.return_value = s3
            s3.get_object.side_effect = ClientError(
                {'Error': {'Code': 'NoSuchKey', 'Message': 'not found'}}, 'GetObject'
            )
            result = _do_restore(self.ctx)
        self.assertEqual(result['RestoredCRs'], '0')

    @patch(f'{_M}.apply_resource')
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_mixed_success_and_fallback(self, mock_boto3, _wk, mock_apply):
        """Multiple resources: one succeeds directly, one via fallback."""
        resource2 = {
            'apiVersion': 'kueue.x-k8s.io/v1beta1',
            'kind': 'Cohort',
            'metadata': {'name': 'c1', 'resourceVersion': '200'},
            'spec': {'parent': 'root'},
        }
        # First resource: transformed succeeds
        # Second resource: transformed fails, original succeeds
        mock_apply.side_effect = [True, False, True]
        with patch.dict(os.environ, self.env):
            s3 = MagicMock()
            mock_boto3.client.return_value = s3
            s3.get_object.side_effect = [
                _s3_get_body({'backup_keys': ['key1']}),
                _s3_get_body([self.resource_v1beta1, resource2]),
            ]
            result = _do_restore(self.ctx)
        self.assertEqual(result['RestoredCRs'], '2')
        self.assertEqual(result['FailedCRs'], '0')
        fallbacks = json.loads(result['FallbackRestoredCRs'])
        self.assertEqual(len(fallbacks), 1)
        self.assertIn('Cohort/c1', fallbacks)


class TestOnBackup(unittest.TestCase):
    """Tests for on_backup storage version migration sequencing and partial failure."""

    def setUp(self):
        self.env = {
            'EKS_CLUSTER_NAME': 'test-cluster',
            'BACKUP_S3_BUCKET': 'test-bucket',
            'CURRENT_TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.0-eksbuild.1',
            'TASK_GOVERNANCE_ADDON_VERSION': 'v1.5.0-eksbuild.1',
        }
        self.ctx = _make_context()

    @patch.dict(os.environ, {
        'EKS_CLUSTER_NAME': 'c', 'BACKUP_S3_BUCKET': 'b',
        'CURRENT_TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.0-eksbuild.1',
        'TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.1-eksbuild.1',
    })
    def test_no_migration_needed_skips_backup(self):
        result = on_backup(self.ctx)
        self.assertEqual(result['Status'], 'SUCCESS')
        self.assertEqual(result['BackupKeys'], '[]')

    @patch(f'{_M}.force_stored_versions')
    @patch(f'{_M}.delete_crs_for_crd')
    @patch(f'{_M}.patch_stored_versions')
    @patch(f'{_M}.migrate_storage_version')
    @patch(f'{_M}.patch_crd_storage_version')
    @patch(f'{_M}.run_kubectl')
    @patch(f'{_M}.verify_s3_backup')
    @patch(f'{_M}.backup_to_s3', return_value='backup-key-1')
    @patch(f'{_M}.get_custom_resources')
    @patch(f'{_M}.crd_exists', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_storage_migration_sequence_per_crd(
        self, mock_boto3, _wk, mock_crd_exists, mock_get_crs,
        mock_backup, mock_verify, mock_run_kubectl, mock_patch_crd,
        mock_migrate, mock_patch_stored, mock_delete_crs, mock_force_stored,
    ):
        """For each CRD with target_api in versions: normal storage cutover path."""
        mock_get_crs.return_value = [{'kind': 'Topology', 'metadata': {'name': 't1'}}]
        # CRD has target_api → normal path
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [{'name': 'v1alpha1'}, {'name': 'v1beta2'}]}
        }))
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        with patch.dict(os.environ, self.env):
            result = on_backup(self.ctx)
        self.assertEqual(result['Status'], 'SUCCESS')
        crd_count = len(ALL_KUEUE_CRDS)
        self.assertEqual(mock_patch_crd.call_count, crd_count)
        self.assertEqual(mock_migrate.call_count, crd_count)
        self.assertEqual(mock_patch_stored.call_count, crd_count)
        mock_delete_crs.assert_not_called()
        mock_force_stored.assert_not_called()
        # Verify ordering: all calls use target_api='v1beta2'
        for call in mock_patch_crd.call_args_list:
            self.assertEqual(call[0][1], 'v1beta2')

    @patch(f'{_M}.patch_stored_versions')
    @patch(f'{_M}.migrate_storage_version')
    @patch(f'{_M}.patch_crd_storage_version')
    @patch(f'{_M}.get_custom_resources', return_value=[])
    @patch(f'{_M}.crd_exists', return_value=False)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_missing_crd_skipped_gracefully(
        self, mock_boto3, _wk, mock_crd_exists, mock_get_crs,
        mock_patch_crd, mock_migrate, mock_patch_stored,
    ):
        """CRDs that don't exist are skipped; storage migration still runs for existing ones."""
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        with patch.dict(os.environ, self.env):
            result = on_backup(self.ctx)
        self.assertEqual(result['Status'], 'SUCCESS')
        # No CRDs exist → no storage migration calls
        self.assertEqual(mock_patch_crd.call_count, 0)

    @patch(f'{_M}.force_stored_versions')
    @patch(f'{_M}.delete_crs_for_crd')
    @patch(f'{_M}.patch_stored_versions')
    @patch(f'{_M}.migrate_storage_version')
    @patch(f'{_M}.patch_crd_storage_version')
    @patch(f'{_M}.run_kubectl')
    @patch(f'{_M}.verify_s3_backup')
    @patch(f'{_M}.backup_to_s3', return_value='key1')
    @patch(f'{_M}.get_custom_resources')
    @patch(f'{_M}.crd_exists', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_manifest_written_to_s3(
        self, mock_boto3, _wk, mock_crd_exists, mock_get_crs,
        mock_backup, mock_verify, mock_run_kubectl, mock_patch_crd,
        mock_migrate, mock_patch_stored, mock_delete_crs, mock_force_stored,
    ):
        """Backup manifest is written to S3 with all backup keys."""
        mock_get_crs.return_value = [{'kind': 'Topology', 'metadata': {'name': 't1'}}]
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [{'name': 'v1alpha1'}, {'name': 'v1beta2'}]}
        }))
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        with patch.dict(os.environ, self.env):
            result = on_backup(self.ctx)
        # Verify manifest was written
        put_calls = [c for c in s3.put_object.call_args_list
                     if 'backup-manifest.json' in str(c)]
        self.assertEqual(len(put_calls), 1)
        manifest_body = json.loads(put_calls[0][1]['Body'])
        self.assertEqual(manifest_body['cluster_name'], 'test-cluster')
        self.assertTrue(len(manifest_body['backup_keys']) > 0)

    @patch.dict(os.environ, {
        'BACKUP_S3_BUCKET': 'b',
        'CURRENT_TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.0-eksbuild.1',
        'TASK_GOVERNANCE_ADDON_VERSION': 'v1.5.0-eksbuild.1',
    })
    def test_missing_env_var_raises(self):
        with self.assertRaises(ValueError) as cm:
            on_backup(self.ctx)
        self.assertIn('EKS_CLUSTER_NAME', str(cm.exception))

    @patch(f'{_M}.patch_stored_versions')
    @patch(f'{_M}.migrate_storage_version')
    @patch(f'{_M}.patch_crd_storage_version')
    @patch(f'{_M}.verify_s3_backup', side_effect=RuntimeError('S3 verification failed'))
    @patch(f'{_M}.backup_to_s3', return_value='key1')
    @patch(f'{_M}.get_custom_resources')
    @patch(f'{_M}.crd_exists', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_s3_verification_failure_propagates(
        self, mock_boto3, _wk, mock_crd_exists, mock_get_crs,
        mock_backup, mock_verify, mock_patch_crd, mock_migrate, mock_patch_stored,
    ):
        """If S3 backup verification fails, the error propagates (no silent data loss)."""
        mock_get_crs.return_value = [{'kind': 'Topology', 'metadata': {'name': 't1'}}]
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        with patch.dict(os.environ, self.env):
            with self.assertRaises(RuntimeError) as cm:
                on_backup(self.ctx)
        self.assertIn('S3 verification failed', str(cm.exception))

    @patch(f'{_M}.patch_stored_versions')
    @patch(f'{_M}.migrate_storage_version', side_effect=RuntimeError('Storage migration failed'))
    @patch(f'{_M}.patch_crd_storage_version')
    @patch(f'{_M}.run_kubectl')
    @patch(f'{_M}.verify_s3_backup')
    @patch(f'{_M}.backup_to_s3', return_value='key1')
    @patch(f'{_M}.get_custom_resources')
    @patch(f'{_M}.crd_exists', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_migrate_failure_aborts_before_patch_stored_versions(
        self, mock_boto3, _wk, mock_crd_exists, mock_get_crs,
        mock_backup, mock_verify, mock_run_kubectl, mock_patch_crd, mock_migrate, mock_patch_stored,
    ):
        """If migrate_storage_version raises, on_backup aborts and patch_stored_versions is never called."""
        mock_get_crs.return_value = [{'kind': 'Topology', 'metadata': {'name': 't1'}}]
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [{'name': 'v1alpha1'}, {'name': 'v1beta2'}]}
        }))
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        with patch.dict(os.environ, self.env):
            with self.assertRaises(RuntimeError) as cm:
                on_backup(self.ctx)
        self.assertIn('Storage migration failed', str(cm.exception))
        mock_patch_stored.assert_not_called()


class TestMigrateStorageVersion(unittest.TestCase):
    """Tests for migrate_storage_version failure tracking."""

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_partial_failure_raises(self, mock_run_kubectl, mock_subprocess):
        """If some CRs fail kubectl replace, raises RuntimeError with count."""
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({'items': [
            {'kind': 'Topology', 'metadata': {'name': 't1'}, 'apiVersion': 'kueue.x-k8s.io/v1beta1'},
            {'kind': 'Topology', 'metadata': {'name': 't2'}, 'apiVersion': 'kueue.x-k8s.io/v1beta1'},
        ]}))
        # First replace succeeds, second fails
        mock_subprocess.run.side_effect = [
            Mock(returncode=0),
            Mock(returncode=1, stderr='conflict'),
        ]
        ctx = _make_context()
        with self.assertRaises(RuntimeError) as cm:
            migrate_storage_version('topologies.kueue.x-k8s.io', 'v1beta2', ctx)
        self.assertIn('1/2', str(cm.exception))

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_all_succeed_no_raise(self, mock_run_kubectl, mock_subprocess):
        """If all CRs succeed, no exception is raised."""
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({'items': [
            {'kind': 'Topology', 'metadata': {'name': 't1'}, 'apiVersion': 'kueue.x-k8s.io/v1beta1'},
        ]}))
        mock_subprocess.run.return_value = Mock(returncode=0)
        ctx = _make_context()
        migrate_storage_version('topologies.kueue.x-k8s.io', 'v1beta2', ctx)  # should not raise

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_transient_get_failure_raises(self, mock_run_kubectl, mock_subprocess):
        """Transient kubectl get failure raises instead of silently returning."""
        mock_run_kubectl.return_value = Mock(returncode=1, stderr='connection refused')
        ctx = _make_context()
        with self.assertRaises(RuntimeError) as cm:
            migrate_storage_version('topologies.kueue.x-k8s.io', 'v1beta2', ctx)
        self.assertIn('Failed to list CRs', str(cm.exception))


class TestPatchStoredVersions(unittest.TestCase):
    """Tests for patch_stored_versions failure handling."""

    @patch(f'{_M}.run_kubectl')
    def test_failure_raises(self, mock_run_kubectl):
        """If kubectl patch fails, raises RuntimeError."""
        mock_run_kubectl.return_value = Mock(returncode=1, stderr='forbidden')
        with self.assertRaises(RuntimeError) as cm:
            patch_stored_versions('topologies.kueue.x-k8s.io', 'v1beta2')
        self.assertIn('Failed to patch storedVersions', str(cm.exception))

    @patch(f'{_M}.run_kubectl')
    def test_success_no_raise(self, mock_run_kubectl):
        """If kubectl patch succeeds, no exception."""
        mock_run_kubectl.return_value = Mock(returncode=0)
        patch_stored_versions('topologies.kueue.x-k8s.io', 'v1beta2')  # should not raise


class TestPatchCrdStorageVersion(unittest.TestCase):
    """Tests for patch_crd_storage_version (normal path only, no bridge)."""

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_raises_when_target_not_in_crd(self, mock_run_kubectl, mock_subprocess):
        """Raises RuntimeError when target_api not in spec.versions."""
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [{'name': 'v1alpha1', 'storage': True}]}
        }))
        with self.assertRaises(RuntimeError):
            patch_crd_storage_version('cohorts.kueue.x-k8s.io', 'v1beta1')

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_raises_on_replace_failure(self, mock_run_kubectl, mock_subprocess):
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [
                {'name': 'v1alpha1', 'storage': True},
                {'name': 'v1beta1', 'storage': False},
            ]}
        }))
        mock_subprocess.run.return_value = Mock(returncode=1, stderr='denied')
        with self.assertRaises(RuntimeError):
            patch_crd_storage_version('cohorts.kueue.x-k8s.io', 'v1beta1')

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_success_path(self, mock_run_kubectl, mock_subprocess):
        """Normal path: target in spec.versions, storage flag flipped, no raise."""
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [
                {'name': 'v1alpha1', 'storage': True},
                {'name': 'v1beta1', 'storage': False},
            ]}
        }))
        mock_subprocess.run.return_value = Mock(returncode=0)
        patch_crd_storage_version('cohorts.kueue.x-k8s.io', 'v1beta1')  # should not raise


class TestDeleteCrsForCrd(unittest.TestCase):
    @patch(f'{_M}.run_kubectl')
    def test_calls_kubectl_delete_all(self, mock_run_kubectl):
        mock_run_kubectl.return_value = Mock(returncode=0)
        delete_crs_for_crd('topologies.kueue.x-k8s.io', _make_context())
        args = mock_run_kubectl.call_args[0][0]
        self.assertIn('delete', args)
        self.assertIn('--all', args)

    @patch(f'{_M}.run_kubectl')
    def test_raises_on_failure(self, mock_run_kubectl):
        mock_run_kubectl.return_value = Mock(returncode=1, stderr='forbidden')
        with self.assertRaises(RuntimeError):
            delete_crs_for_crd('topologies.kueue.x-k8s.io', _make_context())


class TestForceStoredVersions(unittest.TestCase):
    @patch(f'{_M}.run_kubectl')
    def test_patches_stored_versions(self, mock_run_kubectl):
        mock_run_kubectl.return_value = Mock(returncode=0)
        force_stored_versions('cohorts.kueue.x-k8s.io', ['v1beta1'])
        args = mock_run_kubectl.call_args[0][0]
        self.assertIn('patch', args)
        self.assertIn('--subresource=status', args)

    @patch(f'{_M}.run_kubectl')
    def test_raises_on_failure(self, mock_run_kubectl):
        mock_run_kubectl.return_value = Mock(returncode=1, stderr='err')
        with self.assertRaises(RuntimeError):
            force_stored_versions('cohorts.kueue.x-k8s.io', ['v1beta1'])


class TestOnBackupPathSplit(unittest.TestCase):
    """Tests for on_backup two-path split (normal vs cross-version)."""

    def setUp(self):
        self.env = {
            'EKS_CLUSTER_NAME': 'test-cluster',
            'BACKUP_S3_BUCKET': 'test-bucket',
            'CURRENT_TASK_GOVERNANCE_ADDON_VERSION': 'v1.3.0-eksbuild.1',
            'TASK_GOVERNANCE_ADDON_VERSION': 'v1.5.0-eksbuild.1',
        }

    @patch(f'{_M}.force_stored_versions')
    @patch(f'{_M}.delete_crs_for_crd')
    @patch(f'{_M}.patch_stored_versions')
    @patch(f'{_M}.migrate_storage_version')
    @patch(f'{_M}.patch_crd_storage_version')
    @patch(f'{_M}.run_kubectl')
    @patch(f'{_M}.verify_s3_backup')
    @patch(f'{_M}.backup_to_s3', return_value='key1')
    @patch(f'{_M}.get_custom_resources')
    @patch(f'{_M}.crd_exists', return_value=True)
    @patch(f'{_M}.write_kubeconfig')
    @patch(f'{_M}.boto3')
    def test_cross_version_uses_delete_path(
        self, mock_boto3, _wk, mock_crd_exists, mock_get_crs,
        mock_backup, mock_verify, mock_run_kubectl, mock_patch_crd,
        mock_migrate, mock_patch_stored, mock_delete_crs, mock_force_stored,
    ):
        """CRD without target_api uses delete-and-restore path."""
        mock_get_crs.return_value = [{'kind': 'Topology', 'metadata': {'name': 't1'}}]
        # CRD only has v1alpha1 (target is v1beta2)
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({
            'spec': {'versions': [{'name': 'v1alpha1'}]}
        }))
        s3 = MagicMock()
        mock_boto3.client.return_value = s3
        with patch.dict(os.environ, self.env):
            on_backup(_make_context())
        # delete + force_stored called, NOT patch_crd/migrate/patch_stored
        self.assertTrue(mock_delete_crs.call_count > 0)
        self.assertTrue(mock_force_stored.call_count > 0)
        mock_patch_crd.assert_not_called()
        mock_migrate.assert_not_called()
        mock_patch_stored.assert_not_called()


class TestApplyResourceFallback(unittest.TestCase):
    """Tests for apply_resource replace --force fallback."""

    @patch(f'{_M}.subprocess')
    def test_fallback_to_replace_force(self, mock_subprocess):
        """apply fails + replace --force succeeds → return True."""
        mock_subprocess.run.side_effect = [
            Mock(returncode=1, stderr='conversion error'),  # apply
            Mock(returncode=0),  # replace --force
        ]
        result = apply_resource({'kind': 'Topology', 'metadata': {'name': 't1'}})
        self.assertTrue(result)
        self.assertEqual(mock_subprocess.run.call_count, 2)

    @patch(f'{_M}.subprocess')
    def test_both_fail_returns_false(self, mock_subprocess):
        """apply fails + replace --force fails → return False."""
        mock_subprocess.run.side_effect = [
            Mock(returncode=1, stderr='err1'),
            Mock(returncode=1, stderr='err2'),
        ]
        result = apply_resource({'kind': 'Topology', 'metadata': {'name': 't1'}})
        self.assertFalse(result)


class TestMigrateStorageVersionTransform(unittest.TestCase):
    """Tests for migrate_storage_version field transform."""

    @patch(f'{_M}.subprocess')
    @patch(f'{_M}.run_kubectl')
    def test_applies_field_transform(self, mock_run_kubectl, mock_subprocess):
        """migrate_storage_version transforms fields (e.g. parent→parentName) before replace."""
        mock_run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({'items': [
            {'apiVersion': 'kueue.x-k8s.io/v1alpha1', 'kind': 'Cohort',
             'metadata': {'name': 'c1', 'resourceVersion': '999'},
             'spec': {'parent': 'root'}},
        ]}))
        mock_subprocess.run.return_value = Mock(returncode=0)
        ctx = _make_context()
        migrate_storage_version('cohorts.kueue.x-k8s.io', 'v1beta1', ctx)
        # Verify the body sent to kubectl replace has parentName, not parent
        call_kwargs = mock_subprocess.run.call_args
        body = json.loads(call_kwargs.kwargs.get('input', call_kwargs[1].get('input', '')))
        self.assertEqual(body['spec']['parentName'], 'root')
        self.assertNotIn('parent', body['spec'])
        self.assertEqual(body['metadata']['resourceVersion'], '999')
        self.assertEqual(body['apiVersion'], 'kueue.x-k8s.io/v1beta1')


if __name__ == '__main__':
    unittest.main()
