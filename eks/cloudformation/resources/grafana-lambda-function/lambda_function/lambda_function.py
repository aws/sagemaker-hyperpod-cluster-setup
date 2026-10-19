import boto3
import os
import json
import urllib3
import logging
import cfnresponse
import yaml
import re
import zipfile
import tempfile
from botocore.exceptions import ClientError

GRAFANA_WORKSPACE_ID = 'GRAFANA_WORKSPACE_ID'
PROMETHEUS_WORKSPACE_ID = 'PROMETHEUS_WORKSPACE_ID'
GRAFANA_WORKSPACE_TOKEN_KEY = 'GRAFANA_WORKSPACE_TOKEN_KEY'
REGION = 'REGION'
S3_BUCKET = 'S3_BUCKET'
DASHBOARD_PREFIX_S3_KEY = 'DASHBOARD_PREFIX_S3_KEY'
HYPERPOD_CLUSTER_NAME = 'HYPERPOD_CLUSTER_NAME'

# cloudwatchRulesClusterNamePlaceholder in HyperPodObservabilityAssets.
CLOUDWATCH_RULES_CLUSTER_PLACEHOLDER = '__HYPERPOD_CLUSTER_NAME__'
CLOUDWATCH_OPENTELEMETRY_DATASOURCE_UID = 'cloudwatch-opentelemetry'

# Bounded so a hung Grafana call can't run to the Lambda timeout and leave CloudFormation without a response.
GRAFANA_HTTP_TIMEOUT = urllib3.Timeout(connect=5, read=30)
# urllib3 only status-retries idempotent methods, so POSTs are never replayed.
GRAFANA_HTTP_RETRIES = urllib3.Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), raise_on_status=False)


logger = logging.getLogger()
logger.setLevel(logging.INFO)

def validate_env_vars():
    """Validate required environment variables"""
    required_env_vars = [
        GRAFANA_WORKSPACE_ID,
        PROMETHEUS_WORKSPACE_ID,
        GRAFANA_WORKSPACE_TOKEN_KEY,
        REGION,
        S3_BUCKET,
        DASHBOARD_PREFIX_S3_KEY
    ]
    
    for var in required_env_vars:
        if var not in os.environ:
            raise ValueError(f"Missing required environment variable: {var}")

def get_workspace_endpoint():
    """Get Grafana workspace endpoint"""
    workspace_id = os.environ[GRAFANA_WORKSPACE_ID]
    region = os.environ[REGION]
    return f"{workspace_id}.grafana-workspace.{region}.amazonaws.com"

def download_and_extract_assets():
    """Download and extract observability assets zip from S3"""
    try:
        s3 = boto3.client('s3')
        bucket = os.environ[S3_BUCKET]
        key = os.environ[DASHBOARD_PREFIX_S3_KEY]
        
        # Create temporary directory
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, 'assets.zip')
        
        # Download zip file
        logger.info(f"Downloading assets from s3://{bucket}/{key}")
        s3.download_file(bucket, key, zip_path)
        
        # Extract zip file
        extract_dir = os.path.join(temp_dir, 'extracted')
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        
        return extract_dir
    except Exception as e:
        logger.error(f"Error downloading/extracting assets: {str(e)}")
        raise

def get_rules_datasource_uid():
    """Alert rules query AMP when there is a Prometheus workspace; otherwise metrics go to CloudWatch"""
    return 'prometheus' if os.environ.get(PROMETHEUS_WORKSPACE_ID) else CLOUDWATCH_OPENTELEMETRY_DATASOURCE_UID

def cluster_hash(cluster_name):
    """8-hex FNV-1a of the cluster name. Keep in sync with clusterHash in IronManConsole-Assets."""
    h = 0x811c9dc5
    for b in cluster_name.encode('utf-8'):
        h = ((h ^ b) * 0x01000193) & 0xffffffff
    return f"{h:08x}"

def alert_rule_uid(alert, cluster_name=None):
    """
    Fixed per alert (e.g. GPUHighTemperature -> hp-gpu-high-temperature) so stack updates and reruns hit the same
    rule instead of creating a duplicate. CloudWatch rules are per cluster (several clusters can share a workspace),
    so their uid also carries a hash of the cluster name. Keep in sync with alertRuleUid in IronManConsole-Assets.
    """
    uid = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', alert)
    uid = re.sub(r'([A-Z])([A-Z][a-z])', r'\1-\2', uid)
    uid = f"hp-{uid.lower()}"
    return f"{uid}-{cluster_hash(cluster_name)}" if cluster_name else uid

def alert_rule_title(alert, cluster_name=None):
    """CloudWatch rules are per cluster, so their title names it. Keep in sync with IronManConsole-Assets."""
    return f"{alert} ({cluster_name})" if cluster_name else alert

def cloudwatch_cluster_name():
    """The HyperPod cluster CloudWatch rules are scoped to, or None for AMP rules."""
    return None if os.environ.get(PROMETHEUS_WORKSPACE_ID) else os.environ.get(HYPERPOD_CLUSTER_NAME)

def load_rules(extract_dir):
    """
    The AMP rules, or with no Prometheus workspace the CloudWatch rules scoped to the HyperPod cluster.
    CloudWatch metrics are account-wide, so without the cluster name no rules are created rather than unscoped ones.
    """
    if os.environ.get(PROMETHEUS_WORKSPACE_ID):
        with open(os.path.join(extract_dir, 'alerts', 'alert-rules.yaml'), 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    cluster_name = os.environ.get(HYPERPOD_CLUSTER_NAME)
    rules_path = os.path.join(extract_dir, 'alerts', 'alert-rules-cloudwatch.yaml')
    if not cluster_name or not os.path.exists(rules_path):
        logger.warning("Skipping alert rules: no HyperPod cluster name or CloudWatch rules for a CloudWatch destination")
        return None
    with open(rules_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f.read().replace(CLOUDWATCH_RULES_CLUSTER_PLACEHOLDER, cluster_name))

def convert_rules_to_json():
    try:
        datasource_uid = get_rules_datasource_uid()
        extract_dir = download_and_extract_assets()
        data = load_rules(extract_dir)
        if data is None:
            return []

        cluster_name = cloudwatch_cluster_name()
        rules = []
        for rule in data['groups'][0]['rules']:
            rule_json = {
                "title": alert_rule_title(rule['alert'], cluster_name),
                "folderUID": "aws-sm-hp-observability-rules",
                # Grafana 12+ rejects rules without a group ("interval (0s) should be non-zero").
                "ruleGroup": data['groups'][0]['name'],
                "provenance": "",
                "noDataState": "OK",
                "execErrState": "Error",
                "for": rule.get('for', '5m'),
                "labels": rule.get('labels', {}),
                "annotations": rule.get('annotations', {}),
                "orgId": 1,
                "uid": alert_rule_uid(rule['alert'], cluster_name),
                "condition": "A",
                "data": [
                    {
                        "refId": "A",
                        "queryType": "",
                        "relativeTimeRange": {
                            "from": 600,
                            "to": 0
                        },
                        "datasourceUid": datasource_uid,
                        "model": {
                            "refId": "A",
                            "expr": rule['expr'],
                            "range": False,
                            "instant": True,
                            "editorMode": "code",
                            "legendFormat": "__auto"
                        }
                    }
                ]
            }
            rules.append(rule_json)

        return rules
    except Exception as e:
        # Raised so create_alert_rules records the error in the response data.
        logger.error(f"Error converting rules: {str(e)}")
        raise

def make_grafana_request(endpoint, method, payload=None, additional_headers=None):
    try:
        workspace_endpoint = get_workspace_endpoint()
        token_key = os.environ[GRAFANA_WORKSPACE_TOKEN_KEY]

        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token_key}'
        }

        if additional_headers:
            headers.update(additional_headers)

        http = urllib3.PoolManager(timeout=GRAFANA_HTTP_TIMEOUT, retries=GRAFANA_HTTP_RETRIES)

        response = http.request(
            method,
            f'https://{workspace_endpoint}/api/{endpoint}',
            headers=headers,
            body=json.dumps(payload).encode('utf-8') if payload else None
        )

        logger.info(f"Response status: {response.status}")
        logger.info(f"Response body: {response.data.decode('utf-8')}")

        return response

    except Exception as e:
        logger.error(f"API request failed: {str(e)}")
        raise

def handle_resource_creation(resource_type, create_func):
    try:
        return create_func()
    except Exception as e:
        if "409" in str(e) or "already exists" in str(e).lower():
            logger.info(f"{resource_type} already exists")
            return {
                'message': f'{resource_type} already exists',
                'status': 'existing'
            }
        logger.error(f"Error creating {resource_type}: {str(e)}")
        return {
            'message': f'Failed to create {resource_type}',
            'error': str(e)
        }

def create_grafana_datasource():
    try:
        validate_env_vars()
        region = os.environ[REGION]

        datasource_payload = {
            "name": "cloudwatch",
            "type": "cloudwatch",
            "uid": "cloudwatch",
            "access": "proxy",
            "isDefault": True,
            "jsonData": {
                "authType": "sigv4",
                "sigV4Auth": True,
                "sigV4Region": region,
                "defaultRegion": region,
                "httpMethod": "POST",
                "sigV4AuthType": "ec2_iam_role"
            }
        }

        response = make_grafana_request('datasources', 'POST', datasource_payload)

        if response.status in [200, 201]:
            response_data = json.loads(response.data.decode('utf-8'))
            return {
                'message': 'Cloudwatch datasource created successfully',
                'datasourceId': response_data.get('id')
            }
        elif response.status == 409:
            return {
                'message': 'Cloudwatch datasource already exists',
                'status': 'existing'
            }
        else:
            raise Exception(f"Failed to create Grafana datasource. Status: {response.status}")

    except Exception as e:
        return handle_resource_creation('Datasource', lambda: raise_or_return(e))

AMAZON_PROMETHEUS_PLUGIN_ID = 'grafana-amazonprometheus-datasource'

def create_cloudwatch_opentelemetry_datasource():
    """CloudWatch OpenTelemetry (PromQL over CloudWatch OTLP metrics), used when there is no Prometheus workspace."""
    try:
        validate_env_vars()
        region = os.environ[REGION]

        datasource_payload = {
            "name": "CloudWatch OpenTelemetry",
            "type": AMAZON_PROMETHEUS_PLUGIN_ID,
            "uid": CLOUDWATCH_OPENTELEMETRY_DATASOURCE_UID,
            "url": f"https://monitoring.{region}.amazonaws.com",
            "access": "proxy",
            "isDefault": False,
            "jsonData": {
                "authType": "sigv4",
                "sigV4Auth": True,
                "sigV4Region": region,
                "defaultRegion": region,
                "httpMethod": "POST",
                "sigV4AuthType": "ec2_iam_role",
                # The plugin signs as aps unless told otherwise; CloudWatch requires monitoring.
                "sigv4Service": "monitoring"
            }
        }

        response = make_grafana_request('datasources', 'POST', datasource_payload)

        if response.status in [200, 201]:
            response_data = json.loads(response.data.decode('utf-8'))
            return {
                'message': 'CloudWatch OpenTelemetry datasource created successfully',
                'datasourceId': response_data.get('id')
            }
        elif response.status == 409:
            return {
                'message': 'CloudWatch OpenTelemetry datasource already exists',
                'status': 'existing'
            }
        else:
            raise Exception(f"Failed to create CloudWatch OpenTelemetry datasource. Status: {response.status}")

    except Exception as e:
        return handle_resource_creation('Datasource', lambda: raise_or_return(e))

def get_prometheus_datasource_type():
    """
    Grafana 13's core Prometheus plugin no longer SigV4-signs requests (AMP returns 403), so use the Amazon
    Managed Prometheus plugin wherever it is installed (AMG 12+). Older workspaces keep the core plugin.
    """
    response = make_grafana_request(f'plugins/{AMAZON_PROMETHEUS_PLUGIN_ID}/settings', 'GET')
    if response.status == 200:
        return AMAZON_PROMETHEUS_PLUGIN_ID
    if response.status == 404:
        return 'prometheus'
    # Anything else is transient; guessing would provision a data source that 403s on Grafana 13.
    raise Exception(f"Failed to check {AMAZON_PROMETHEUS_PLUGIN_ID} plugin. Status: {response.status}")

def create_prometheus_datasource():
    # Outside the try: the handler below folds errors into response_data, which would report SUCCESS or break cfnresponse.
    datasource_type = get_prometheus_datasource_type()
    try:
        validate_env_vars()
        region = os.environ[REGION]
        prometheus_url = f"https://aps-workspaces.{region}.amazonaws.com/workspaces/{os.environ[PROMETHEUS_WORKSPACE_ID]}"

        datasource_payload = {
            "name": "prometheus",
            "type": datasource_type,
            "uid": "prometheus",
            "url": prometheus_url,
            "access": "proxy",
            "isDefault": True,
            "jsonData": {
                "authType": "sigv4",
                "sigV4Auth": True,
                "sigV4Region": region,
                "defaultRegion": region,
                "httpMethod": "POST",
                "sigV4AuthType": "ec2_iam_role"
            }
        }

        response = make_grafana_request('datasources', 'POST', datasource_payload)

        if response.status in [200, 201]:
            response_data = json.loads(response.data.decode('utf-8'))
            return {
                'message': 'Prometheus datasource created successfully',
                'datasourceId': response_data.get('id')
            }
        elif response.status == 409:
            return {
                'message': 'Prometheus datasource already exists',
                'status': 'existing'
            }
        else:
            raise Exception(f"Failed to create Prometheus datasource. Status: {response.status}")

    except Exception as e:
        return handle_resource_creation('Datasource', lambda: raise_or_return(e))

def create_dashboard(template_name):
    try:
        extract_dir = download_and_extract_assets()
        orchestrator = os.environ.get('ORCHESTRATOR_TYPE', 'eks')
        if orchestrator == 'slurm':
            dashboard_path = os.path.join(extract_dir, 'dashboards', 'slurmTemplates', f'{template_name}.json')
        else:
            dashboard_path = os.path.join(extract_dir, 'dashboards', 'templates', f'{template_name}.json')
        logger.info(f"Loading dashboard template from: {dashboard_path}")

        with open(dashboard_path, 'r', encoding='utf-8') as f:
            dashboard_data = json.load(f)

        # Extract dashboard content from wrapper if it exists
        if 'dashboard' in dashboard_data:
            dashboard_content = dashboard_data['dashboard']
        else:
            dashboard_content = dashboard_data

        payload = {
            "dashboard": {
                **dashboard_content
            },
            "overwrite": True
        }
        
        logger.info(f"Making POST request to dashboards/db")
        response = make_grafana_request('dashboards/db', 'POST', payload)
        logger.info(f"Response status: {response.status}")

        if response.status in [200, 201]:
            response_data = json.loads(response.data.decode('utf-8'))
            return {
                'message': f'Dashboard {template_name} created successfully',
                'dashboardUrl': response_data.get('url'),
                'uid': response_data.get('uid')
            }
        elif response.status == 409:
            return {
                'message': f'Dashboard {template_name} already exists',
                'status': 'existing'
            }
        else:
            error_msg = response.data.decode('utf-8')
            logger.error(f"Dashboard creation failed. Status: {response.status}, Error: {error_msg}")
            raise Exception(f"Failed to create dashboard. Status: {response.status}, Error: {error_msg}")

    except Exception as e:
        logger.error(f"Error creating dashboard {template_name}: {str(e)}")
        return {
            'message': f'Failed to create dashboard {template_name}',
            'error': str(e)
        }

def create_folder():
    try:
        folder_payload = {
            "uid": "aws-sm-hp-observability-rules",
            "title": "Sagemaker Hyperpod Alerts"
        }

        response = make_grafana_request('folders', 'POST', folder_payload)

        if response.status in [200, 201]:
            response_data = json.loads(response.data.decode('utf-8'))
            return {
                'message': 'Alert folder created successfully',
                'folderId': response_data.get('id'),
                'folderUid': response_data.get('uid')
            }
        elif response.status == 409 or response.status == 412:  # Add 412 status
            return {
                'message': 'Alert folder already exists',
                'status': 'existing'
            }
        else:
            # Return error dict instead of raising exception
            return {
                'message': f'Failed to create folder',
                'status': 'error',
                'error': f'Status: {response.status}'
            }

    except Exception as e:
        return {
            'message': 'Failed to create folder',
            'status': 'error',
            'error': str(e)
        }

def get_existing_rules():
    """
    The rules already in the alerts folder. Rules created before uids were fixed (9.4/10.4) have random uids, and
    earlier runs could leave several copies, so copies are found by title. If the rules can't be listed, rules are
    created by uid only.
    """
    try:
        response = make_grafana_request('v1/provisioning/alert-rules', 'GET')
        if response.status != 200:
            logger.warning(f"Could not list alert rules (status {response.status}); creating rules by uid only")
            return []
        return [r for r in json.loads(response.data.decode('utf-8')) if r.get('folderUID') == 'aws-sm-hp-observability-rules']
    except Exception as e:
        logger.warning(f"Could not list alert rules ({str(e)}); creating rules by uid only")
        return []

def rule_datasource_uids(rule):
    return {query.get('datasourceUid') for query in rule.get('data', [])}

def alert_rule_base_title(rule):
    """The plain alert title a CloudWatch rule's per-cluster title is built from, or None for an AMP rule."""
    cluster_name = cloudwatch_cluster_name()
    suffix = f" ({cluster_name})"
    return rule['title'][:-len(suffix)] if cluster_name and rule['title'].endswith(suffix) else None

def delete_alert_rule(uid):
    response = make_grafana_request(f'v1/provisioning/alert-rules/{uid}', 'DELETE', None, {'X-Disable-Provenance': 'true'})
    if response.status not in (200, 204, 404):
        logger.warning(f"Failed to delete alert rule {uid}. Status: {response.status}")

def create_alert_rules():
    try:
        rules = convert_rules_to_json()
        results = []
        existing_rules = get_existing_rules() if rules else []

        for rule in rules:
            try:
                # Only rules on the same data source: another cluster sharing the workspace (e.g. an AMP cluster when
                # this one is on CloudWatch) can have rules with the same titles.
                same_datasource = [
                    r for r in existing_rules if rule['data'][0]['datasourceUid'] in rule_datasource_uids(r)
                ]
                copies = [r['uid'] for r in same_datasource if r['title'] == rule['title']]
                # Keep one copy (the fixed uid if present) and update it in place; otherwise create the rule.
                keep = (rule['uid'] if rule['uid'] in copies else copies[0]) if copies else None
                if keep:
                    response = make_grafana_request(
                        f'v1/provisioning/alert-rules/{keep}',
                        'PUT',
                        {**rule, 'uid': keep},
                        {'X-Disable-Provenance': 'true'}
                    )
                else:
                    response = make_grafana_request(
                        'v1/provisioning/alert-rules',
                        'POST',
                        rule,
                        {'X-Disable-Provenance': 'true'}
                    )

                # Rules have fixed uids, so a rerun gets 409; older Grafana reports a conflict as 400.
                if not keep and (response.status == 409 or (response.status == 400 and
                        'conflict' in response.data.decode('utf-8').lower())):
                    logger.info(f"Alert rule {rule['title']} already exists - skipping")
                    result = {'message': f'Alert rule {rule["title"]} already exists', 'status': 'existing'}
                elif response.status in [200, 201]:
                    result = {
                        'message': f'Alert rule {rule["title"]} {"updated" if keep else "created successfully"}',
                        'status': 'updated' if keep else 'created',
                    }
                else:
                    result = {
                        'message': f'Failed to {"update" if keep else "create"} alert rule {rule["title"]}',
                        'status': response.status
                    }

                # Only once the rule is written: delete the other copies, and the plain-titled CloudWatch rule a
                # per-cluster CloudWatch rule replaces (an unscoped one from before rules were per cluster). A failed
                # write never leaves the alert without a rule.
                if result['status'] in ('updated', 'created', 'existing'):
                    replaced = [r['uid'] for r in same_datasource if r['title'] == alert_rule_base_title(rule)]
                    for uid in [u for u in copies if u != keep] + replaced:
                        delete_alert_rule(uid)

                results.append(result)

            except Exception as e:
                logger.error(f"Error creating alert rule {rule.get('title')}: {str(e)}")
                results.append({
                    'message': f'Error creating alert rule {rule["title"]}',
                    'error': str(e)
                })

        return results

    except Exception as e:
        logger.error(f"Error in create_alert_rules: {str(e)}")
        return [{'message': f'Alert rules processing failed: {str(e)}'}]

def cleanup_service_account():
    """Cleanup Grafana service account (tokens are deleted automatically)"""
    try:
        workspace_id = os.environ.get('GRAFANA_WORKSPACE_ID')
        service_account_id = os.environ.get('SERVICE_ACCOUNT_ID')
        
        if not all([workspace_id, service_account_id]):
            missing = []
            if not workspace_id:
                missing.append('GRAFANA_WORKSPACE_ID')
            if not service_account_id:
                missing.append('SERVICE_ACCOUNT_ID')
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        
        grafana = boto3.client('grafana')
        
        # Delete service account (this will automatically delete associated tokens)
        try:
            grafana.delete_workspace_service_account(
                workspaceId=workspace_id,
                serviceAccountId=service_account_id
            )
            logger.info(f"Successfully deleted service account {service_account_id}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.info(f"Service account {service_account_id} not found, may already be deleted")
            else:
                logger.error(f"Failed to delete service account: {str(e)}")

    except Exception as e:
        logger.error(f"Error during service account cleanup: {str(e)}")

def lambda_handler(event, context):
    """Main Lambda handler"""
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        request_type = event['RequestType']

        if request_type == 'Create':
            response_data = on_create()
        elif request_type == 'Update':
            response_data = on_update()
        elif request_type == 'Delete':
            response_data = on_delete()
        else:
            raise ValueError(f"Invalid request type: {request_type}")
        
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS if response_data.get("Status") == "SUCCESS" else cfnresponse.FAILED,
            response_data
        )

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {
                "Status": "FAILED",
                "Reason": str(e)
            }
        )

def on_create():
    """Handle Create request"""
    try:
        response_data = {
            "Status": "SUCCESS",
            "Reason": "Grafana resources created successfully",
            "resources": {
                'grafanaDatasource': None,
                'prometheusDatasource': None,
                'folder': None,
                'dashboards': [],
                'alertRules': []
            }
        }

        # Validate environment variables
        validate_env_vars()

        # Create data sources with logging
        datasource_result = create_grafana_datasource()
        response_data["resources"]["grafanaDatasource"] = datasource_result

        # Without a Prometheus workspace, metrics go to CloudWatch and are queried through CloudWatch OpenTelemetry.
        if os.environ.get(PROMETHEUS_WORKSPACE_ID):
            prometheus_result = create_prometheus_datasource()
            response_data["resources"]["prometheusDatasource"] = prometheus_result
        else:
            cloudwatch_opentelemetry_result = create_cloudwatch_opentelemetry_datasource()
            response_data["resources"]["cloudwatchOpenTelemetryDatasource"] = cloudwatch_opentelemetry_result
        
        # Create folder
        folder_result = create_folder()
        response_data["resources"]["folder"] = folder_result

        # Create dashboards
        orchestrator = os.environ.get('ORCHESTRATOR_TYPE', 'eks')
        if orchestrator == 'slurm':
            dashboard_templates = ['clusters', 'jobs']
        else:
            dashboard_templates = ['cluster', 'efa', 'training', 'inference', 'tasks']
        for template in dashboard_templates:
            try:
                result = create_dashboard(template)
                
                dashboard_entry = {
                    'template': template,
                    'status': 'success' if result.get('status') != 'existing' else 'existing',
                    'result': result
                }
                response_data["resources"]["dashboards"].append(dashboard_entry)
            except Exception as e:
                logger.error(f"Error creating dashboard {template}: {str(e)}")
                response_data["resources"]["dashboards"].append({
                    'template': template,
                    'status': 'error',
                    'error': str(e)
                })

        # Create alert rules
        alert_rules_result = create_alert_rules()
        response_data["resources"]["alertRules"] = alert_rules_result

        return response_data

    except Exception as e:
        logger.error(f"Error in on_create: {str(e)}")
        return {
            "Status": "FAILED",
            "Reason": str(e)
        }
    finally:
        # Cleanup service account
        cleanup_service_account()

def on_update():
    """Handle Update request"""
    # Todo: figure out what we want to do here
    return on_create()

def on_delete():
    """Handle Delete request"""
    return {
        "Status": "SUCCESS",
        "Reason": "No cleanup required"
    }

def raise_or_return(error):
    """Helper to either raise or return based on error type"""
    if isinstance(error, ValueError):
        raise error
    return error
    