import boto3
import os
import subprocess
import cfnresponse
from botocore.exceptions import ClientError
import yaml
import json
import time

# Environment variables
EKS_CLUSTER_NAME = 'EKS_CLUSTER_NAME'
BACKUP_S3_BUCKET = 'BACKUP_S3_BUCKET'
TASK_GOVERNANCE_ADDON_NAME = 'TASK_GOVERNANCE_ADDON_NAME'
TASK_GOVERNANCE_ADDON_VERSION = 'TASK_GOVERNANCE_ADDON_VERSION'
CURRENT_TASK_GOVERNANCE_ADDON_VERSION = 'CURRENT_TASK_GOVERNANCE_ADDON_VERSION'
MIGRATION_MODE = 'MIGRATION_MODE'
BACKUP_S3_KEY = 'BACKUP_S3_KEY'

# All Kueue CRDs from HyperPodTaskGovernanceEKSAddonConfig.
# Source of truth: configuration/data/versions/*/kueue/templates/crd/
ALL_KUEUE_CRDS = [
    'admissionchecks.kueue.x-k8s.io',
    'clusterqueues.kueue.x-k8s.io',
    'cohorts.kueue.x-k8s.io',
    'localqueues.kueue.x-k8s.io',
    'multikueueclusters.kueue.x-k8s.io',
    'multikueueconfigs.kueue.x-k8s.io',
    'provisioningrequestconfigs.kueue.x-k8s.io',
    'resourceflavors.kueue.x-k8s.io',
    'topologies.kueue.x-k8s.io',
    'workloadpriorityclasses.kueue.x-k8s.io',
    'workloads.kueue.x-k8s.io',
]

# Single-hop migration definitions. Only adjacent version hops are defined here;
# multi-hop paths (e.g. v1.1→v1.5) are composed at runtime by chaining hops.
# This keeps the map O(n) instead of O(n²) as new versions are added.
#
# Versions that share the same Kueue version (v1.1–v1.3 all use Kueue v0.12)
# are grouped via VERSION_GROUPS below.
SINGLE_HOP_MIGRATIONS = {
    # v1alpha1 → v1beta1 (Kueue v0.12 → v0.14)
    ('v1.3', 'v1.4'): {
        'source_apis': ['v1alpha1'],
        'target_api': 'v1beta1',
        'crds': ALL_KUEUE_CRDS,
    },
    # v1beta1 → v1beta2 (Kueue v0.14 → v0.16)
    ('v1.4', 'v1.5'): {
        'source_apis': ['v1beta1'],
        'target_api': 'v1beta2',
        'crds': ALL_KUEUE_CRDS,
    },
}

# Addon versions that share the same Kueue version (same CRD schema).
# The canonical version is the last in each group.
# v1.0 ships Kueue v0.8.1 (no topologies/cohorts CRDs yet), but the migration
# Lambda handles missing CRDs gracefully (crd_exists → skip), so v1.0 can safely
# canonicalize to v1.3 for migration path resolution.
# v1.1, v1.2, v1.3 all ship Kueue v0.12 → canonical 'v1.3'
VERSION_GROUPS = {
    'v1.0': 'v1.3',
    'v1.1': 'v1.3',
    'v1.2': 'v1.3',
}

# Timeout guard: abort if less than this many ms remain
TIMEOUT_BUFFER_MS = 120_000


def extract_major_minor(addon_version):
    """Extract 'vMAJOR.MINOR' from a version string like 'v1.3.1-eksbuild.1' or '1.3.1'."""
    if not addon_version:
        return ''
    v = addon_version.lstrip('v').split('-', 1)[0]
    parts = v.split('.')
    if len(parts) < 2:
        return ''
    return f'v{parts[0]}.{parts[1]}'


def _canonicalize(version_key):
    """Map aliased addon versions to their canonical form."""
    return VERSION_GROUPS.get(version_key, version_key)


def get_migration_spec(current_version, target_version):
    """Return the migration spec for (current, target) versions, or None if no migration is needed.

    Composes multi-hop paths at runtime from SINGLE_HOP_MIGRATIONS.
    Raises ValueError if the (current, target) pair has no defined migration path.
    """
    current_key = _canonicalize(extract_major_minor(current_version))
    target_key = _canonicalize(extract_major_minor(target_version))
    if not current_key or not target_key:
        raise ValueError(
            f"Unable to parse addon versions: current={current_version!r}, target={target_version!r}"
        )
    if current_key == target_key:
        return None  # Same canonical version → no-op

    # Build ordered hop chain from current_key to target_key.
    # ASSUMPTION: SINGLE_HOP_MIGRATIONS forms a linear chain (no branching).
    # The first-match break works because each version has at most one outgoing hop.
    # If branching paths are ever needed, replace with a proper graph traversal.
    hops = []
    cursor = current_key
    visited = {cursor}
    while cursor != target_key:
        found = False
        for (src, dst), spec in SINGLE_HOP_MIGRATIONS.items():
            if src == cursor and dst not in visited:
                hops.append(spec)
                visited.add(dst)
                cursor = dst
                found = True
                break
        if not found:
            raise ValueError(
                f"No migration path defined for {current_key} -> {target_key}. "
                f"Update SINGLE_HOP_MIGRATIONS in lambda_function.py."
            )

    if len(hops) == 1:
        return hops[0]

    # Compose multi-hop: collect all source_apis, use final target_api, union CRDs
    all_source_apis = []
    seen_apis = set()
    all_crds = set()
    for hop in hops:
        for api in hop['source_apis']:
            if api not in seen_apis:
                all_source_apis.append(api)
                seen_apis.add(api)
        all_crds.update(hop['crds'])
    return {
        'source_apis': all_source_apis,
        'target_api': hops[-1]['target_api'],
        'crds': sorted(all_crds),
    }


def load_migration_spec_from_env():
    """Read current/target addon versions from env and return the migration spec (or None)."""
    current = os.environ.get(CURRENT_TASK_GOVERNANCE_ADDON_VERSION, '')
    target = os.environ.get(TASK_GOVERNANCE_ADDON_VERSION, '')
    return get_migration_spec(current, target)


def lambda_handler(event, context):
    """Handle CloudFormation custom resource requests for TG addon CRD migration."""
    try:
        request_type = event['RequestType']

        if request_type == 'Create':
            mode = os.environ.get(MIGRATION_MODE)
            if not mode:
                raise ValueError(f"Missing required environment variable: {MIGRATION_MODE}")
            if mode == 'backup':
                response_data = on_backup(context)
            elif mode == 'update':
                response_data = on_update(context)
            elif mode == 'restore':
                response_data = on_restore(context)
            elif mode == 'cleanup':
                response_data = on_cleanup(context)
            else:
                raise ValueError(f"Invalid MIGRATION_MODE: {mode}")
        elif request_type == 'Update':
            response_data = {
                'Status': 'SUCCESS',
                'Reason': 'Update is a no-op for TG addon migration',
            }
        elif request_type == 'Delete':
            response_data = on_delete()
        else:
            raise ValueError(f"Invalid request type: {request_type}")

        cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data)

    except Exception as e:
        print(f"Error: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Status': 'FAILED',
            'Reason': str(e),
        })


def check_timeout(context):
    """Raise if remaining Lambda execution time is below safety buffer."""
    remaining = context.get_remaining_time_in_millis()
    if remaining < TIMEOUT_BUFFER_MS:
        raise TimeoutError(
            f"Aborting: only {remaining}ms remaining (buffer={TIMEOUT_BUFFER_MS}ms). "
            "Sending cfnresponse before Lambda hard-timeout."
        )


def write_kubeconfig(cluster_name, region):
    """Generate kubeconfig using boto3 and aws-iam-authenticator."""
    eks = boto3.client('eks', region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)['cluster']

    kubeconfig = {
        'apiVersion': 'v1',
        'kind': 'Config',
        'clusters': [{
            'cluster': {
                'server': cluster['endpoint'],
                'certificate-authority-data': cluster['certificateAuthority']['data'],
            },
            'name': cluster_name,
        }],
        'contexts': [{
            'context': {
                'cluster': cluster_name,
                'user': cluster_name,
            },
            'name': cluster['arn'],
        }],
        'current-context': cluster['arn'],
        'preferences': {},
        'users': [{
            'name': cluster_name,
            'user': {
                'exec': {
                    'apiVersion': 'client.authentication.k8s.io/v1beta1',
                    'command': 'aws-iam-authenticator',
                    'args': ['token', '-i', cluster_name],
                },
            },
        }],
    }

    kubeconfig_dir = '/tmp/.kube'
    os.makedirs(kubeconfig_dir, exist_ok=True)
    kubeconfig_path = os.path.join(kubeconfig_dir, 'config')

    with open(kubeconfig_path, 'w') as f:
        yaml.dump(kubeconfig, f, default_flow_style=False)

    os.chmod(kubeconfig_path, 0o600)
    os.environ['KUBECONFIG'] = kubeconfig_path


def run_kubectl(args, check=True, capture=True, timeout=300):
    """Run a kubectl command and return the result."""
    cmd = ['kubectl'] + args
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=True, check=check, timeout=timeout)
    if capture and result.stdout:
        print(f"stdout: {result.stdout[:500]}")
    if capture and result.stderr:
        print(f"stderr: {result.stderr[:500]}")
    return result


def crd_exists(crd_name):
    """Check if a CRD exists in the cluster."""
    result = run_kubectl(['get', 'crd', crd_name], check=False)
    return result.returncode == 0


def get_custom_resources(crd_name):
    """Get all custom resources for a given CRD across all namespaces."""
    resource_plural = crd_name.split('.')[0]
    result = run_kubectl([
        'get', f'{resource_plural}.kueue.x-k8s.io',
        '--all-namespaces', '-o', 'json',
    ], check=False)

    if result.returncode != 0:
        stderr = result.stderr or ''
        if 'NotFound' in stderr or "the server doesn't have a resource type" in stderr:
            return []
        raise RuntimeError(f"Failed to list {resource_plural} resources: {stderr}")

    data = json.loads(result.stdout)
    return data.get('items', [])


def strip_metadata(resource):
    """Strip server-managed metadata fields and status for re-apply."""
    cleaned = json.loads(json.dumps(resource))
    metadata = cleaned.get('metadata', {})
    for field in ['resourceVersion', 'uid', 'creationTimestamp', 'generation',
                  'managedFields', 'selfLink']:
        metadata.pop(field, None)
    annotations = metadata.get('annotations', {})
    annotations.pop('kubectl.kubernetes.io/last-applied-configuration', None)
    if not annotations:
        metadata.pop('annotations', None)
    cleaned.pop('status', None)
    return cleaned


def transform_resource(resource, spec):
    """Transform a resource from any spec['source_apis'] to spec['target_api'].

    spec: {'source_apis': ['v1alpha1', 'v1beta1'], 'target_api': 'v1beta2', ...}
    """
    transformed = strip_metadata(resource)

    api_version = transformed.get('apiVersion', '')
    for source_api in spec['source_apis']:
        if api_version == f"kueue.x-k8s.io/{source_api}":
            transformed['apiVersion'] = f"kueue.x-k8s.io/{spec['target_api']}"
            break

    # --- Field transforms per target API version ---
    # The Kueue conversion webhook may not be running during migration, so the
    # Lambda handles field renames/removals explicitly based on CRD schema diffs.
    #
    # v1alpha1 → v1beta1 (Kueue v0.12 → v0.14):
    #   Only Cohort and Topology ever had v1alpha1.
    #   - Cohort: spec.parent → spec.parentName
    #   - Topology: no field changes
    #
    # v1beta1 → v1beta2 (Kueue v0.14 → v0.16):
    #   - ClusterQueue: spec.cohort → spec.cohortName, spec.admissionChecks removed
    #   - AdmissionCheck: spec.retryDelayMinutes removed
    #   - Cohort: no field changes (parentName in both v1beta1 and v1beta2)
    #   - All others: no spec field changes
    #   Note: status fields (LocalQueue.status.flavors, ClusterQueue.status.pendingWorkloadsStatus)
    #   are already stripped by strip_metadata().

    kind = transformed.get('kind', '')
    spec_body = transformed.get('spec', {})

    # v1alpha1 → v1beta1 transforms (also applied for v1alpha1 → v1beta2 multi-hop)
    if spec_body and kind == 'Cohort' and 'parent' in spec_body:
        if 'parentName' not in spec_body:
            spec_body['parentName'] = spec_body['parent']
        del spec_body['parent']

    # v1beta1 → v1beta2 transforms
    if spec['target_api'] == 'v1beta2' and spec_body:
        if kind == 'ClusterQueue':
            if 'cohort' in spec_body:
                if 'cohortName' not in spec_body:
                    spec_body['cohortName'] = spec_body['cohort']
                del spec_body['cohort']
            spec_body.pop('admissionChecks', None)

        if kind == 'AdmissionCheck':
            spec_body.pop('retryDelayMinutes', None)

    return transformed


def backup_to_s3(resources, crd_name, bucket, cluster_name):
    """Backup resources to S3 as JSON. Returns the S3 key."""
    if not resources:
        print(f"No resources to backup for {crd_name}")
        return None

    s3 = boto3.client('s3')
    timestamp = time.strftime('%Y%m%dT%H%M%S')
    key = f"task-governance-addon-migration/{cluster_name}/{crd_name}/{timestamp}.json"

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(resources, indent=2),
        ContentType='application/json',
    )
    print(f"Backed up {len(resources)} {crd_name} resources to s3://{bucket}/{key}")
    return key


def verify_s3_backup(bucket, key, expected_count):
    """Verify S3 backup object exists and contains expected number of resources."""
    s3 = boto3.client('s3')
    resp = s3.get_object(Bucket=bucket, Key=key)
    resources = json.loads(resp['Body'].read().decode('utf-8'))
    actual_count = len(resources)
    if actual_count != expected_count:
        raise RuntimeError(
            f"S3 verification failed for {key}: expected {expected_count} resources, got {actual_count}"
        )
    print(f"S3 verification passed: {key} contains {actual_count} resources")


def delete_crd(crd_name):
    """Delete a CRD from the cluster."""
    if not crd_exists(crd_name):
        print(f"CRD {crd_name} does not exist, skipping delete")
        return
    print(f"Deleting CRD {crd_name}...")
    run_kubectl(['delete', 'crd', crd_name, '--timeout=120s'], check=True)
    print(f"CRD {crd_name} deleted")


def apply_resource(resource):
    """Apply a single Kubernetes resource via kubectl apply, with replace --force fallback."""
    resource_json = json.dumps(resource)
    kind = resource.get('kind', 'unknown')
    name = resource.get('metadata', {}).get('name', 'unknown')
    result = subprocess.run(
        ['kubectl', 'apply', '-f', '-'],
        input=resource_json,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode == 0:
        print(f"Applied {kind}/{name}")
        return True
    # Fallback: replace --force (delete + create) for stuck/unconvertible CRs
    print(f"apply failed for {kind}/{name}, trying replace --force: {result.stderr[:200]}")
    result = subprocess.run(
        ['kubectl', 'replace', '--force', '-f', '-'],
        input=resource_json,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode == 0:
        print(f"replace --force succeeded for {kind}/{name}")
        return True
    print(f"Warning: apply and replace --force both failed for {kind}/{name}: {result.stderr[:200]}")
    return False


def patch_crd_storage_version(crd_name, target_api):
    """Set target_api as storage=true on the CRD; all other versions storage=false.

    Only called when target_api is already in spec.versions (normal double-version CRDs).
    For single-version CRDs where target_api is missing, on_backup uses the
    delete-and-restore path instead.
    """
    result = run_kubectl(['get', 'crd', crd_name, '-o', 'json'])
    crd = json.loads(result.stdout)
    versions = crd.get('spec', {}).get('versions', [])
    version_names = [v['name'] for v in versions]
    if target_api not in version_names:
        raise RuntimeError(
            f"CRD {crd_name} does not have {target_api} in spec.versions "
            f"(has {version_names}). Use delete-and-restore path instead."
        )
    changed = False
    for v in versions:
        should_be_storage = (v['name'] == target_api)
        if v.get('storage', False) != should_be_storage:
            v['storage'] = should_be_storage
            changed = True
    if not changed:
        print(f"CRD {crd_name} already has {target_api} as storage version, skipping patch")
        return
    print(f"Patching CRD {crd_name} to set {target_api} as storage version")
    replace_result = subprocess.run(
        ['kubectl', 'replace', '-f', '-'],
        input=json.dumps(crd), text=True,
        capture_output=True, check=False, timeout=60,
    )
    if replace_result.returncode != 0:
        raise RuntimeError(
            f"Failed to patch CRD {crd_name} storage version to {target_api}: "
            f"{replace_result.stderr[:500]}"
        )


def migrate_storage_version(crd_name, target_api, context):
    """GET+REPLACE each CR to trigger etcd rewrite in target_api."""
    resource_plural = crd_name.split('.')[0]
    result = run_kubectl([
        'get', f'{resource_plural}.kueue.x-k8s.io',
        '--all-namespaces', '-o', 'json',
    ], check=False)
    if result.returncode != 0:
        stderr = result.stderr or ''
        if 'NotFound' in stderr or "the server doesn't have a resource type" in stderr:
            print(f"No CRs to migrate for {crd_name} (resource not found)")
            return
        raise RuntimeError(
            f"Failed to list CRs for {crd_name} during storage migration: {stderr[:200]}"
        )
    items = json.loads(result.stdout).get('items', [])
    if not items:
        print(f"No CRs found for {crd_name}, skipping storage migration")
        return
    failed = 0
    for item in items:
        check_timeout(context)
        kind = item.get('kind', 'unknown')
        name = item.get('metadata', {}).get('name', 'unknown')
        # Transform resource (field renames) + preserve resourceVersion for optimistic concurrency
        resource_version = item.get('metadata', {}).get('resourceVersion')
        spec_inline = {
            'source_apis': [item['apiVersion'].split('/')[1]],
            'target_api': target_api,
        }
        rewritten = transform_resource(item, spec_inline)
        if resource_version:
            rewritten.setdefault('metadata', {})['resourceVersion'] = resource_version
        replace_result = subprocess.run(
            ['kubectl', 'replace', '-f', '-'],
            input=json.dumps(rewritten), text=True,
            capture_output=True, check=False, timeout=60,
        )
        if replace_result.returncode == 0:
            print(f"Storage-migrated {kind}/{name}")
        else:
            failed += 1
            print(f"WARNING: Failed to storage-migrate {kind}/{name}: {replace_result.stderr[:200]}")
    if failed:
        raise RuntimeError(
            f"Storage migration failed for {failed}/{len(items)} CRs of {crd_name}. "
            "Aborting before patch_stored_versions to prevent data loss."
        )


def patch_stored_versions(crd_name, target_api):
    """Remove all non-target versions from status.storedVersions."""
    patch = {'status': {'storedVersions': [target_api]}}
    result = run_kubectl([
        'patch', 'crd', crd_name,
        '--subresource=status', '--type=merge',
        '-p', json.dumps(patch),
    ], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to patch storedVersions for {crd_name}: {result.stderr[:200]}"
        )
    print(f"Patched storedVersions for {crd_name} to [{target_api}]")


def delete_crs_for_crd(crd_name, context):
    """Delete all CRs for a CRD. Used in the cross-version path where target_api
    is not in the current CRD's spec.versions. CRs are backed up to S3 first and
    will be re-created from backup in the Restore phase after UpdateAddon."""
    check_timeout(context)
    resource_plural = crd_name.split('.')[0]
    result = run_kubectl([
        'delete', f'{resource_plural}.kueue.x-k8s.io',
        '--all', '--all-namespaces', '--ignore-not-found=true',
        '--wait=false',
    ], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to delete CRs for {crd_name}: {result.stderr[:200]}")
    print(f"Deleted all CRs for {crd_name} (will be restored post-UpdateAddon)")


def force_stored_versions(crd_name, versions):
    """Overwrite status.storedVersions. Used in the cross-version path to clear
    old apiVersions from storedVersions so UpdateAddon can replace the CRD."""
    patch = {'status': {'storedVersions': versions}}
    result = run_kubectl([
        'patch', 'crd', crd_name,
        '--subresource=status', '--type=merge',
        '-p', json.dumps(patch),
    ], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to set storedVersions for {crd_name}: {result.stderr[:200]}"
        )
    print(f"Set storedVersions for {crd_name} to {versions}")


def on_backup(context):
    """Backup CRs to S3, verify backup integrity, migrate storage versions."""
    for var in [EKS_CLUSTER_NAME, BACKUP_S3_BUCKET]:
        if var not in os.environ:
            raise ValueError(f"Missing required environment variable: {var}")

    spec = load_migration_spec_from_env()
    if spec is None:
        print("No CRD schema migration required for this version upgrade. Skipping backup.")
        return {
            'Status': 'SUCCESS',
            'Reason': 'No CRD schema migration required.',
            'BackupBucket': '',
            'BackupManifestKey': '',
            'BackupKeys': '[]',
        }

    cluster_name = os.environ[EKS_CLUSTER_NAME]
    region = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
    bucket = os.environ[BACKUP_S3_BUCKET]

    write_kubeconfig(cluster_name, region)

    backup_keys = []
    for crd_name in spec['crds']:
        check_timeout(context)

        if not crd_exists(crd_name):
            print(f"CRD {crd_name} does not exist, nothing to migrate")
            continue

        resources = get_custom_resources(crd_name)
        if resources:
            key = backup_to_s3(resources, crd_name, bucket, cluster_name)
            if key:
                # Verify S3 backup before proceeding
                verify_s3_backup(bucket, key, len(resources))
                backup_keys.append(key)

        # Determine migration path based on whether CRD has target_api
        check_timeout(context)
        crd_json = json.loads(run_kubectl(['get', 'crd', crd_name, '-o', 'json']).stdout)
        version_names = [v['name'] for v in crd_json.get('spec', {}).get('versions', [])]

        if spec['target_api'] in version_names:
            # Normal storage cutover: CRD already has target_api (double-version).
            # Flip storage flag, rewrite CRs in etcd, patch storedVersions.
            patch_crd_storage_version(crd_name, spec['target_api'])
            migrate_storage_version(crd_name, spec['target_api'], context)
            patch_stored_versions(crd_name, spec['target_api'])
        else:
            # Cross-version: target_api not in current CRD (e.g. v1.3 cohorts
            # only has v1alpha1, target is v1beta1). Delete CRs (backed up to S3),
            # patch storedVersions to [target_api] so UpdateAddon can replace CRD.
            # Restore phase re-creates CRs from S3 backup.
            print(f"CRD {crd_name} lacks {spec['target_api']} in spec.versions "
                  f"(has {version_names}). Using delete-and-restore path.")
            delete_crs_for_crd(crd_name, context)
            force_stored_versions(crd_name, [spec['target_api']])

    # Write manifest for restore step so restore can locate backup keys
    # even if Lambda crashes later in the migration flow.
    s3 = boto3.client('s3')
    manifest_key = f"task-governance-addon-migration/{cluster_name}/backup-manifest.json"
    manifest = {
        'cluster_name': cluster_name,
        'backup_keys': backup_keys,
        'timestamp': time.strftime('%Y%m%dT%H%M%S'),
    }
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType='application/json',
    )

    # NOTE: We intentionally do NOT delete CRDs here. The Kueue controller
    # holds `kueue.x-k8s.io/resource-in-use` finalizers on live CRs, so
    # `kubectl delete crd` would block indefinitely. The subsequent
    # UpdateAddon phase uses `resolveConflicts: OVERWRITE` to replace CRDs
    # in-place, which handles the controller restart + schema upgrade
    # atomically. If schema transformation is needed (v1beta1 → v1beta2
    # field renames), the Restore phase re-applies transformed CRs.

    return {
        'Status': 'SUCCESS',
        'Reason': f"Backup complete. {len(backup_keys)} CRD types backed up and verified.",
        'BackupBucket': bucket,
        'BackupManifestKey': manifest_key,
        'BackupKeys': json.dumps(backup_keys),
    }


def update_addon_and_wait(eks_client, cluster_name, addon_name, addon_version, context):
    """Call EKS UpdateAddon and poll until ACTIVE."""
    print(f"Updating addon {addon_name} to version {addon_version}...")

    update_params = {
        'clusterName': cluster_name,
        'addonName': addon_name,
        'resolveConflicts': 'OVERWRITE',
    }
    if addon_version:
        update_params['addonVersion'] = addon_version

    try:
        eks_client.update_addon(**update_params)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ('ResourceInUseException', 'ConflictException'):
            print(f"Addon already updating (caught {error_code}), will poll for status.")
        else:
            raise

    start = time.time()
    timeout = 780
    poll_interval = 30
    while time.time() - start < timeout:
        check_timeout(context)
        addon_info = eks_client.describe_addon(clusterName=cluster_name, addonName=addon_name)
        status = addon_info['addon']['status']
        print(f"Addon status: {status}")

        if status == 'ACTIVE':
            return status
        if status in ('DEGRADED', 'CREATE_FAILED', 'UPDATE_FAILED', 'DELETE_FAILED'):
            health = addon_info.get('addon', {}).get('health', {}).get('issues', [])
            issues = '; '.join(f"{i.get('code')}: {i.get('message')}" for i in health)
            raise RuntimeError(f"Addon reached {status}. Issues: {issues}")

        time.sleep(poll_interval)

    raise TimeoutError(f"Addon did not reach ACTIVE within {timeout}s")


def on_update(context):
    """Call EKS UpdateAddon API and wait for ACTIVE."""
    for var in [EKS_CLUSTER_NAME, TASK_GOVERNANCE_ADDON_NAME]:
        if var not in os.environ:
            raise ValueError(f"Missing required environment variable: {var}")

    cluster_name = os.environ[EKS_CLUSTER_NAME]
    region = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
    addon_name = os.environ[TASK_GOVERNANCE_ADDON_NAME]
    addon_version = os.environ.get(TASK_GOVERNANCE_ADDON_VERSION, '')

    eks = boto3.client('eks', region_name=region)
    addon_status = update_addon_and_wait(eks, cluster_name, addon_name, addon_version, context)

    return {
        'Status': 'SUCCESS',
        'Reason': f'Addon update complete. Status: {addon_status}',
        'AddonStatus': addon_status,
    }


def _do_restore(context):
    """Core restore logic: read backup from S3, transform, and apply CRs."""
    spec = load_migration_spec_from_env()
    if spec is None:
        print("No CRD schema migration required for this version upgrade. Skipping restore.")
        return {
            'Status': 'SUCCESS',
            'Reason': 'No CRD schema migration required.',
            'RestoredCRs': '0',
        }

    cluster_name = os.environ[EKS_CLUSTER_NAME]
    region = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
    bucket = os.environ[BACKUP_S3_BUCKET]
    backup_key = os.environ.get(BACKUP_S3_KEY, '')

    write_kubeconfig(cluster_name, region)

    s3 = boto3.client('s3')

    if backup_key:
        backup_keys = [backup_key]
    else:
        manifest_key = f"task-governance-addon-migration/{cluster_name}/backup-manifest.json"
        try:
            resp = s3.get_object(Bucket=bucket, Key=manifest_key)
            manifest = json.loads(resp['Body'].read().decode('utf-8'))
            backup_keys = manifest.get('backup_keys', [])
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                return {
                    'Status': 'SUCCESS',
                    'Reason': 'No backup manifest found. Nothing to restore.',
                    'RestoredCRs': '0',
                }
            raise

    if not backup_keys:
        return {
            'Status': 'SUCCESS',
            'Reason': 'No backup keys in manifest. Nothing to restore.',
            'RestoredCRs': '0',
        }

    restored = 0
    failed = 0
    fallback_resources = []
    for key in backup_keys:
        check_timeout(context)
        resp = s3.get_object(Bucket=bucket, Key=key)
        resources = json.loads(resp['Body'].read().decode('utf-8'))
        for resource in resources:
            check_timeout(context)
            transformed = transform_resource(resource, spec)
            if apply_resource(transformed):
                restored += 1
            else:
                original = strip_metadata(resource)
                if original != transformed:
                    kind = resource.get('kind', 'unknown')
                    name = resource.get('metadata', {}).get('name', 'unknown')
                    print(f"Retrying with original schema (apiVersion={resource.get('apiVersion')})")
                    if apply_resource(original):
                        print(f"RESTORE_FALLBACK_USED: {kind}/{name} restored with original apiVersion "
                              f"{resource.get('apiVersion')} instead of transformed. "
                              f"Manual verification recommended post-migration.")
                        fallback_resources.append(f"{kind}/{name}")
                        restored += 1
                        continue
                failed += 1

    if failed > 0:
        raise RuntimeError(
            f"Restore partially failed. Restored: {restored}, Failed: {failed}"
        )

    if fallback_resources:
        print(f"WARNING: {len(fallback_resources)} resource(s) restored via fallback: "
              f"{', '.join(fallback_resources)}")

    return {
        'Status': 'SUCCESS',
        'Reason': f"Restore complete. Restored: {restored}, Failed: {failed}",
        'RestoredCRs': str(restored),
        'FailedCRs': str(failed),
        'FallbackRestoredCRs': json.dumps(fallback_resources),
    }


def on_restore(context):
    """Restore with one automatic retry on failure."""
    max_attempts = 2
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _do_restore(context)
        except TimeoutError:
            raise  # Don't retry on timeout — no time left
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                # Check if we have enough time for a retry
                remaining = context.get_remaining_time_in_millis()
                if remaining < TIMEOUT_BUFFER_MS * 2:
                    print(f"Restore attempt {attempt} failed and insufficient time for retry. Failing.")
                    raise
                print(f"Restore attempt {attempt} failed: {e}. Retrying in 30s...")
                time.sleep(30)
            else:
                print(f"Restore failed after {max_attempts} attempts: {e}")
                raise last_error


def on_cleanup(context):
    """Delete S3 backup objects. Always returns SUCCESS (best-effort)."""
    cluster_name = os.environ.get(EKS_CLUSTER_NAME, '')
    bucket = os.environ.get(BACKUP_S3_BUCKET, '')
    if not cluster_name or not bucket:
        print("WARNING: Missing env vars for cleanup, skipping S3 delete")
        return {
            'Status': 'SUCCESS',
            'Reason': 'Cleanup skipped — missing environment variables.',
            'DeletedObjects': '0',
        }

    prefix = f"task-governance-addon-migration/{cluster_name}/"

    s3 = boto3.client('s3')
    deleted = 0
    failed_count = 0
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            check_timeout(context)
            objects = page.get('Contents', [])
            if not objects:
                continue
            keys = [{'Key': obj['Key']} for obj in objects]
            try:
                resp = s3.delete_objects(Bucket=bucket, Delete={'Objects': keys})
                errors = resp.get('Errors', [])
                deleted += len(keys) - len(errors)
                failed_count += len(errors)
                if errors:
                    print(f"WARNING: {len(errors)} objects failed to delete in batch")
            except Exception as e:
                print(f"WARNING: Failed to delete batch: {e}")
                failed_count += len(keys)
    except TimeoutError:
        print(f"WARNING: Cleanup timed out. Deleted {deleted} objects so far.")
    except Exception as e:
        print(f"ERROR: Cleanup failed: {e}")

    if failed_count > 0:
        print(f"CLEANUP_PARTIAL_FAILURE: {failed_count} objects failed to delete")

    print(f"Cleanup complete. Deleted {deleted} objects from s3://{bucket}/{prefix}")
    # Always return SUCCESS — cleanup failure must not block migration
    return {
        'Status': 'SUCCESS',
        'Reason': f'Cleanup complete. Deleted {deleted} backup objects.',
        'DeletedObjects': str(deleted),
    }


def on_delete():
    """Handle Delete: no-op to avoid blocking stack deletion/rollback."""
    print("Delete request received - no-op for migration resource")
    return {
        'Status': 'SUCCESS',
        'Reason': 'Delete is a no-op for TG addon migration',
    }
