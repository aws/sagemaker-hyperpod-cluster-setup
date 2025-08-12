import boto3
import os
import json
import urllib3
import logging
import cfnresponse
import yaml

GRAFANA_WORKSPACE_ID = 'GRAFANA_WORKSPACE_ID'
PROMETHEUS_WORKSPACE_ID = 'PROMETHEUS_WORKSPACE_ID'
GRAFANA_WORKSPACE_TOKEN_KEY = 'GRAFANA_WORKSPACE_TOKEN_KEY'
REGION = 'REGION'
DASHBOARD_TEMPLATES_DIR = 'dashboards/templates'
RULES_TEMPLATE_PATH = 'rules/templates/alert-rules.yaml'

DASHBOARD_UIDS = {
    'cluster': 'aws-sm-hp-observability-cluster-v1_0',
    'efa': 'aws-sm-hp-observability-efa-v1_0',
    'training': 'aws-sm-hp-observability-training-v1_0',
    'inference': 'aws-sm-hp-observability-inference-v1_0',
    'tasks': 'aws-sm-hp-observability-task-v1_0'
}

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def validate_env_vars():
    """Validate required environment variables"""
    required_env_vars = [
        GRAFANA_WORKSPACE_ID,
        PROMETHEUS_WORKSPACE_ID,
        GRAFANA_WORKSPACE_TOKEN_KEY,
        REGION
    ]
    
    for var in required_env_vars:
        if var not in os.environ:
            raise ValueError(f"Missing required environment variable: {var}")

def get_workspace_endpoint():
    """Get Grafana workspace endpoint"""
    workspace_id = os.environ[GRAFANA_WORKSPACE_ID]
    region = os.environ[REGION]
    return f"{workspace_id}.grafana-workspace.{region}.amazonaws.com"

def convert_rules_to_json():
    try:
        with open(RULES_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        rules = []
        for rule in data['groups'][0]['rules']:
            rule_json = {
                "title": rule['alert'],
                "folderUID": "aws-sm-hp-observability-rules",
                "provenance": "",
                "noDataState": "OK",
                "execErrState": "Error",
                "for": rule.get('for', '5m'),
                "orgId": 1,
                "uid": "",
                "condition": "A",
                "data": [
                    {
                        "refId": "A",
                        "queryType": "",
                        "relativeTimeRange": {
                            "from": 600,
                            "to": 0
                        },
                        "datasourceUid": "prometheus",
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
        logger.error(f"Error converting rules: {str(e)}")
        return {
            'message': 'Failed to convert rules',
            'error': str(e)
        }

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

        http = urllib3.PoolManager()

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

def create_prometheus_datasource():
    try:
        validate_env_vars()
        region = os.environ[REGION]
        prometheus_url = f"https://aps-workspaces.{region}.amazonaws.com/workspaces/{os.environ[PROMETHEUS_WORKSPACE_ID]}/api"

        datasource_payload = {
            "name": "prometheus",
            "type": "prometheus",
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
        template_path = f"{DASHBOARD_TEMPLATES_DIR}/{template_name}.json"
        logger.info(f"Loading dashboard template from: {template_path}")

        with open(template_path, 'r', encoding='utf-8') as f:
            dashboard_content = json.load(f)

        dashboard_uid = DASHBOARD_UIDS.get(template_name)
        if not dashboard_uid:
            raise ValueError(f"No UID defined for dashboard: {template_name}")

        payload = {
            "dashboard": {
                **dashboard_content,
                "version": 1,
                "uid": dashboard_uid,
                "id": None
            },
            "overwrite": True
        }

        response = make_grafana_request('dashboards/db', 'POST', payload)

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
            raise Exception(f"Failed to create dashboard. Status: {response.status}")

    except Exception as e:
        return handle_resource_creation('Dashboard', lambda: raise_or_return(e))

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
        elif response.status == 409:
            return {
                'message': 'Alert folder already exists',
                'status': 'existing'
            }
        else:
            raise Exception(f"Failed to create folder. Status: {response.status}")

    except Exception as e:
        return handle_resource_creation('Folder', lambda: raise_or_return(e))

def create_alert_rules():
    try:
        rules = convert_rules_to_json()
        results = []

        for rule in rules:
            try:
                response = make_grafana_request(
                    'v1/provisioning/alert-rules',
                    'POST',
                    rule,
                    {'X-Disable-Provenance': 'true'}
                )

                if response.status in [200, 201]:
                    results.append({
                        'message': f'Alert rule {rule["title"]} created successfully',
                        'ruleId': json.loads(response.data.decode('utf-8')).get('id')
                    })
                elif response.status == 409:
                    results.append({
                        'message': f'Alert rule {rule["title"]} already exists',
                        'status': 'existing'
                    })
                else:
                    raise Exception(f"Failed to create alert rule. Status: {response.status}")

            except Exception as e:
                logger.error(f"Error creating alert rule {rule['title']}: {str(e)}")
                results.append({
                    'title': rule['title'],
                    'error': str(e)
                })

        return results

    except Exception as e:
        return handle_resource_creation('Alert Rules', lambda: raise_or_return(e))

def create_all_resources():
    try:
        validate_env_vars()

        results = {
            'grafanaDatasource': create_grafana_datasource(),
            'prometheusDatasource': create_prometheus_datasource(),
            'folder': create_folder(),
            'dashboards': [],
            'alertRules': []
        }

        dashboard_errors = []
        
        for template in DASHBOARD_UIDS.keys():
            try:
                result = create_dashboard(template)
                results['dashboards'].append({
                    'template': template,
                    'result': result
                })
            except Exception as e:
                error_msg = f"Error creating dashboard {template}: {str(e)}"
                logger.error(error_msg)
                dashboard_errors.append(error_msg)
                results['dashboards'].append({
                    'template': template,
                    'error': str(e)
                })

        # Create alert rules after folder is created
        results['alertRules'] = create_alert_rules()

        if dashboard_errors:
            raise Exception(f"Failed to create one or more dashboards:\n" + "\n".join(dashboard_errors))

        return results

    except Exception as e:
        logger.error(f"Error creating resources: {str(e)}")
        raise

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
            cfnresponse.SUCCESS,
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
    result = create_all_resources()
    return {
        "Status": "SUCCESS",
        "Reason": "Grafana resources created successfully",
        **result
    }

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
