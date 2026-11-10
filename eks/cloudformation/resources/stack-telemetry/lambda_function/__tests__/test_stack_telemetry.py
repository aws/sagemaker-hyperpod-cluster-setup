import json
import re
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
for _k, _v in {"TELEMETRY_SOLUTION_ID": "SO0000", "TELEMETRY_SOLUTION_VERSION": "v1",
               "TELEMETRY_UUID": "11111111-2222-3333-4444-555555555555"}.items():
    os.environ.setdefault(_k, _v)

import lambda_function as lf  # noqa: E402

ROOT_ID = "arn:aws:cloudformation:us-west-2:111122223333:stack/customer-stack-name/11111111-2222-3333-4444-555555555555"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ev(logical, status, rtype="AWS::CloudFormation::Stack", stack="customer-stack-name", t=0, reason=None, phys=""):
    return {
        "LogicalResourceId": logical,
        "ResourceStatus": status,
        "ResourceType": rtype,
        "StackName": stack,
        "Timestamp": T0 + timedelta(seconds=t),
        "ResourceStatusReason": reason,
        "PhysicalResourceId": phys,
    }


STATUSES = {}


@pytest.fixture(autouse=True)
def stack_statuses(monkeypatch):
    """describe_stacks stub: current status per stack id (root defaults to CREATE_IN_PROGRESS)."""
    STATUSES.clear()
    STATUSES[ROOT_ID] = "CREATE_IN_PROGRESS"
    params = [
        {"ParameterKey": "NodeProvisioningMode", "ParameterValue": "Continuous"},
        {"ParameterKey": "CreateEKSClusterStack", "ParameterValue": "true"},
        {"ParameterKey": "EnableHPInferenceFeature", "ParameterValue": "true"},
        {"ParameterKey": "HyperPodClusterName", "ParameterValue": "secret-cluster-name"},
    ]
    monkeypatch.setattr(lf.cfn, "describe_stacks", lambda StackName: {
        "Stacks": [{"StackStatus": STATUSES.get(StackName, "CREATE_COMPLETE"), "Parameters": params}]})
    monkeypatch.setenv("TELEMETRY_STACK_PARAMETERS", "NodeProvisioningMode,CreateEKSClusterStack,EnableHPInferenceFeature")
    monkeypatch.setenv("TELEMETRY_ROOT_STACK_KEY", "MAIN_STACK")
    monkeypatch.setenv("TELEMETRY_STACK_KEYS", "EKSClusterStack=MAIN_STACK_EKS_CLUSTER,HelmChartStack=MAIN_STACK_HELM_CHART,"
                       "SageMakerIAMRoleStack=MAIN_STACK_SAGEMAKER_IAM_ROLE,VPCStack=MAIN_STACK_VPC")
    return STATUSES


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(lf, "send", lambda type_, data: calls.append((type_, data)))
    monkeypatch.setattr(lf, "respond", mock.Mock())
    return calls


def cfn_event(phase, request, enabled="true"):
    return {
        "RequestType": request,
        "StackId": ROOT_ID,
        "RequestId": "r",
        "LogicalResourceId": "StackTelemetry" if phase == "Start" else "StackTelemetryEnd",
        "ResponseURL": "https://example.invalid/response",
        "ResourceProperties": {"Phase": phase, "Enabled": enabled},
    }


class TestCategorize:
    @pytest.mark.parametrize(
        "reason,category",
        [
            ("Parameters: [CreateDefaultStorageClass] do not exist in the template", "ParameterMismatch"),
            ("Parameter values specified for a template which does not require them.", "ParameterMismatch"),
            ("ResourceLimitExceeded: account-level service limit", "Quota"),
            ("Cannot exceed quota for RolesPerAccount: 1500", "Quota"),
            ("Rate exceeded (Service: Ec2, Status Code: 400)", "Throttling"),
            ("RequestLimitExceeded: too many requests", "Throttling"),
            ("User is not authorized to perform: iam:CreateRole", "Permissions"),
            ("InsufficientInstanceCapacity in usw2-az1", "Capacity"),
            ("ml.p5.48xlarge does not currently have sufficient capacity to support the request", "Capacity"),
            ("The security group sg-1 has a dependent object (Service: Ec2, DependencyViolation)", "Dependency"),
            ("Stack named 'foo' already exists", "Conflict"),
            ("The vpc ID 'vpc-1' does not exist", "NotFound"),
            ("Service Unavailable, please try again (ServiceUnavailable)", "ServiceError"),
            ("something unexpected", "Other"),
            (None, "Other"),
        ],
    )
    def test_maps_reason_text_to_a_fixed_category(self, reason, category):
        assert lf.categorize(reason) == category


class TestStepTimeline:
    def test_lists_nested_stacks_only_and_never_the_main_stack(self):
        events = [
            ev("customer-stack-name", "CREATE_IN_PROGRESS"),
            ev("StackTelemetryRole", "CREATE_COMPLETE", rtype="AWS::IAM::Role", t=1),
            ev("VPCStack", "CREATE_IN_PROGRESS", t=2),
            ev("VPCStack", "CREATE_IN_PROGRESS", t=3),
            ev("VPCStack", "CREATE_COMPLETE", t=10),
            ev("EKSClusterStack", "CREATE_IN_PROGRESS", t=11),
        ]
        steps = lf.step_timeline(events)
        assert [s["step"] for s in steps] == ["VPCStack", "EKSClusterStack"]
        assert steps[0] == {
            "step": "VPCStack",
            "start": (T0 + timedelta(seconds=2)).isoformat(),
            "end": (T0 + timedelta(seconds=10)).isoformat(),
            "status": "CREATE_COMPLETE",
        }
        assert steps[1]["status"] == "NOT_FINISHED"
        assert "customer-stack-name" not in json.dumps(steps)

    def test_marks_cancelled_steps(self):
        steps = lf.step_timeline(
            [
                ev("HelmChartStack", "CREATE_IN_PROGRESS"),
                ev("HelmChartStack", "CREATE_FAILED", t=1, reason="Parameters: [X] do not exist in the template"),
                ev("SageMakerIAMRoleStack", "CREATE_IN_PROGRESS"),
                ev("SageMakerIAMRoleStack", "CREATE_FAILED", t=1, reason="Resource creation cancelled"),
            ]
        )
        by_step = {s["step"]: s for s in steps}
        assert by_step["HelmChartStack"]["cancelled"] is False
        assert by_step["SageMakerIAMRoleStack"]["cancelled"] is True


class TestRootCause:
    def test_skips_cancelled_steps_and_reports_the_real_failure(self):
        root = [
            ev("SageMakerIAMRoleStack", "CREATE_FAILED", t=1, reason="Resource creation cancelled"),
            ev("HelmChartStack", "CREATE_FAILED", t=2, reason="Parameters: [X] do not exist in the template"),
        ]
        assert lf.root_cause(root, {}) == {
            "failed_step": "HelmChartStack",
            "failed_resource_type": "AWS::CloudFormation::Stack",
            "failure_category": "ParameterMismatch",
        }

    def test_reports_the_failing_resource_inside_the_nested_stack(self):
        nested_id = "arn:aws:cloudformation:us-west-2:111122223333:stack/customer-stack-name-FsxFilesystemStack-ABC/1"
        root = [ev("FsxFilesystemStack", "CREATE_FAILED", reason="Embedded stack failed", phys=nested_id)]
        nested = {
            nested_id: [
                ev("FileSystemSecurityGroup", "CREATE_FAILED", rtype="AWS::EC2::SecurityGroup",
                   stack="customer-stack-name-FsxFilesystemStack-ABC", reason="The vpc ID 'vpc-1' does not exist")
            ]
        }
        assert lf.root_cause(root, nested) == {
            "failed_step": "FsxFilesystemStack",
            "failed_resource": "FileSystemSecurityGroup",
            "failed_resource_type": "AWS::EC2::SecurityGroup",
            "failure_category": "NotFound",
            "failure_reason": "The vpc ID 'vpc-1' does not exist",
        }

    def test_ignores_the_main_stack_own_failure_event(self):
        assert lf.root_cause([ev("customer-stack-name", "CREATE_FAILED", reason="x")], {}) == {}


FIELDS = ["root_stack_key", "stack_key", "account_id", "stack_name", "stack_status", "root_stack_status", "step", "type", "status", "on_success_latency", "on_failure_latency", "deployment_id"]


def nested_arn(step):
    return f"arn:aws:cloudformation:us-west-2:111122223333:stack/customer-stack-name-{step}-ABC/1"


class TestHandler:
    def test_start_create_sends_stack_started(self, sent):
        lf.lambda_handler(cfn_event("Start", "Create"), mock.Mock())
        (type_, data), = sent
        assert type_ == "StackStarted"
        assert {k: data[k] for k in FIELDS} == {
            "root_stack_key": "MAIN_STACK", "stack_key": "MAIN_STACK",
            "account_id": "111122223333", "stack_name": "customer-stack-name",
            "stack_status": "CREATE_IN_PROGRESS", "root_stack_status": "CREATE_IN_PROGRESS", "step": None,
            "type": "AWS::CloudFormation::Stack", "status": "started", "on_success_latency": None, "on_failure_latency": None,
            "deployment_id": None,
        }
        lf.respond.assert_called_once()

    def test_end_create_sends_stack_completed_with_total_latency_and_steps(self, sent, monkeypatch):
        monkeypatch.setattr(lf, "collect", lambda _id: (
            [ev("customer-stack-name", "CREATE_IN_PROGRESS"), ev("VPCStack", "CREATE_IN_PROGRESS", t=1), ev("VPCStack", "CREATE_COMPLETE", t=5)], {}))
        lf.lambda_handler(cfn_event("End", "Create"), mock.Mock())
        (type_, data), = sent
        assert type_ == "StackCompleted"
        assert data["status"] == "success" and data["stack_name"] == "customer-stack-name"
        assert data["on_success_latency"] > 0 and data["on_failure_latency"] is None
        assert [s["step"] for s in data["steps"]] == ["VPCStack"]
        assert data["stack_parameters"] == {
            "NodeProvisioningMode": "Continuous", "CreateEKSClusterStack": "true", "EnableHPInferenceFeature": "true",
        }

    def test_rollback_sends_one_message_per_failed_step_then_stack_failed(self, sent, monkeypatch, stack_statuses):
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        monkeypatch.setattr(lf, "collect", lambda _id: ([
            ev("customer-stack-name", "CREATE_IN_PROGRESS", t=0),
            ev("HelmChartStack", "CREATE_IN_PROGRESS", t=100, phys=nested_arn("HelmChartStack")),
            ev("SageMakerIAMRoleStack", "CREATE_IN_PROGRESS", t=100, phys=nested_arn("SageMakerIAMRoleStack")),
            ev("HelmChartStack", "CREATE_FAILED", t=101, reason="Parameters: [X] do not exist in the template", phys=nested_arn("HelmChartStack")),
            ev("SageMakerIAMRoleStack", "CREATE_FAILED", t=102, reason="Resource creation cancelled", phys=nested_arn("SageMakerIAMRoleStack")),
            ev("customer-stack-name", "ROLLBACK_IN_PROGRESS", t=103),
        ], {}))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        assert [t for t, _ in sent] == ["SubStackFailed", "SubStackFailed", "StackFailed"]
        helm, iam, root = (d for _, d in sent)
        assert helm["step"] == "HelmChartStack" and helm["status"] == "failure"
        assert helm["type"] == "AWS::CloudFormation::Stack"
        assert helm["on_failure_latency"] == 1.0 and helm["failure_category"] == "ParameterMismatch"
        assert helm["stack_name"] == "customer-stack-name-HelmChartStack-ABC"
        assert iam["status"] == "cancelled" and "failure_category" not in iam
        assert root["status"] == "failure" and root["on_failure_latency"] == 103.0
        assert root["failed_step"] == "HelmChartStack" and root["account_id"] == "111122223333"
        assert root["stack_status"] == "ROLLBACK_IN_PROGRESS" and root["root_stack_status"] == "ROLLBACK_IN_PROGRESS"
        assert root["stack_parameters"]["NodeProvisioningMode"] == "Continuous"
        assert "secret-cluster-name" not in json.dumps(sent)
        assert helm["root_stack_status"] == "ROLLBACK_IN_PROGRESS"

    def test_failed_step_reports_the_type_of_the_resource_that_failed_inside_it(self, sent, monkeypatch, stack_statuses):
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        nid = nested_arn("SageMakerIAMRoleStack")
        monkeypatch.setattr(lf, "collect", lambda _id: (
            [ev("customer-stack-name", "CREATE_IN_PROGRESS"),
             ev("SageMakerIAMRoleStack", "CREATE_IN_PROGRESS", t=1, phys=nid),
             ev("SageMakerIAMRoleStack", "CREATE_FAILED", t=9, reason="Embedded stack failed", phys=nid)],
            {nid: [ev("ExecutionRole", "CREATE_FAILED", rtype="AWS::IAM::Role", stack="customer-stack-name-SageMakerIAMRoleStack-ABC",
                      reason="User is not authorized to perform: iam:CreateRole")]},
        ))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        step = next(d for t, d in sent if t == "SubStackFailed")
        assert step["type"] == "AWS::IAM::Role"
        assert step["failed_resource"] == "ExecutionRole"
        assert step["failure_category"] == "Permissions"
        assert step["on_failure_latency"] == 8.0

    def test_follows_nested_failures_to_the_deepest_resource(self, sent, monkeypatch, stack_statuses):
        """The real failure seen in account 654: an IAM role quota hit two nested stacks down."""
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        main = nested_arn("InferenceOperatorMainStack")
        helm = "arn:aws:cloudformation:us-west-2:111122223333:stack/customer-stack-name-InferenceOperatorMainStac-InferenceOperatorHelmInstallStack-XYZ/2"
        monkeypatch.setattr(lf, "collect", lambda _id: (
            [ev("customer-stack-name", "CREATE_IN_PROGRESS"),
             ev("InferenceOperatorMainStack", "CREATE_IN_PROGRESS", t=10, phys=main),
             ev("InferenceOperatorMainStack", "CREATE_FAILED", t=370, reason="Embedded stack ... was not successfully created", phys=main)],
            {
                main: [ev("InferenceOperatorHelmInstallStack", "CREATE_FAILED", stack="customer-stack-name-InferenceOperatorMainStack-ABC",
                          reason="Embedded stack ... was not successfully created", phys=helm)],
                helm: [ev("InferenceHelmCustomResourceRole", "CREATE_FAILED", rtype="AWS::IAM::Role", stack="x",
                          reason="Cannot exceed quota for RolesPerAccount: 1500 (Service: Iam, Status Code: 409)")],
            },
        ))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        step = next(d for t, d in sent if t == "SubStackFailed")
        root = next(d for t, d in sent if t == "StackFailed")
        for d in (step, root):
            assert d["failed_resource"] == "InferenceHelmCustomResourceRole"
            assert d["failure_category"] == "Quota"
        assert step["type"] == "AWS::IAM::Role" and root["failed_resource_type"] == "AWS::IAM::Role"
        assert step["on_failure_latency"] == 360.0

    def test_start_delete_on_user_delete_sends_nothing(self, sent, monkeypatch, stack_statuses):
        # Telemetry is collected only for stack creation; a plain user delete is silent.
        stack_statuses[ROOT_ID] = "DELETE_IN_PROGRESS"
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        assert sent == []
        lf.respond.assert_called_once()

    @pytest.mark.parametrize("phase,request_type", [("End", "Delete"), ("Step", "Delete"), ("Start", "Update"), ("End", "Update")])
    def test_other_requests_send_nothing(self, sent, phase, request_type):
        lf.lambda_handler(cfn_event(phase, request_type), mock.Mock())
        assert sent == []
        lf.respond.assert_called_once()

    def test_opted_out_sends_nothing_but_still_responds(self, sent):
        lf.lambda_handler(cfn_event("Start", "Create", enabled="false"), mock.Mock())
        assert sent == []
        lf.respond.assert_called_once()

    def test_unreadable_status_is_null_and_does_not_block_the_message(self, sent, monkeypatch):
        def denied(StackName):
            raise RuntimeError("AccessDenied")

        monkeypatch.setattr(lf.cfn, "describe_stacks", denied)
        lf.lambda_handler(cfn_event("Start", "Create"), mock.Mock())
        (type_, data), = sent
        assert type_ == "StackStarted" and data["stack_status"] is None and data["root_stack_status"] is None

    def test_errors_never_fail_the_stack(self, sent, monkeypatch):
        def boom(**_):
            raise RuntimeError("AccessDenied")

        monkeypatch.setattr(lf.cfn, "describe_stacks", boom)
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        lf.respond.assert_called_once()
        args, kwargs = lf.respond.call_args
        assert kwargs.get("status", "SUCCESS") == "SUCCESS"

    def test_sends_the_deepest_nested_reason_but_never_physical_ids_or_arns(self, sent, monkeypatch, stack_statuses):
        # The deepest nested failure's full reason IS sent (failure_reason) so nested change-set
        # failures are diagnosable; physical resource IDs and stack ARNs are still never sent.
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        nid = nested_arn("FsxFilesystemStack")
        reason = "The change set creation failed: invalid subnet configuration"
        monkeypatch.setattr(lf, "collect", lambda _id: (
            [ev("customer-stack-name", "CREATE_IN_PROGRESS"),
             ev("FsxFilesystemStack", "CREATE_FAILED", reason="Embedded stack failed", phys=nid)],
            {nid: [ev("Fs", "CREATE_FAILED", rtype="AWS::FSx::FileSystem", reason=reason, phys="fs-0123secret")]},
        ))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        reasons = [d["failure_reason"] for _, d in sent if "failure_reason" in d]
        assert reasons and all(r == reason for r in reasons)
        body = json.dumps(sent)
        for leaked in ("fs-0123secret", "arn:aws"):
            assert leaked not in body


class TestSender:
    @pytest.fixture
    def captured(self, monkeypatch):
        bodies = []
        monkeypatch.delenv("TELEMETRY_TEST_SQS_URL", raising=False)
        monkeypatch.setattr(lf, "post", lambda payload, event_type="": bodies.append(json.loads(payload)) or True)
        return bodies

    def test_builds_the_solutions_envelope_with_snake_case_data(self, captured, monkeypatch):
        monkeypatch.setenv("TELEMETRY_ACCOUNT_ID", "111122223333")
        monkeypatch.setenv("ORCHESTRATOR", "EKS")
        assert lf.send("StackStarted", {"status": "started"}) is True
        (body,) = captured
        assert set(body) == {"Solution", "Version", "UUID", "TimeStamp", "Account", "Type", "Data"}
        assert body["Type"] == "StackStarted" and body["Account"] == "111122223333"
        # TimeStamp is a string ("YYYY-MM-DD HH:MM:SS.mmm"), not an epoch number: the AWS Solutions
        # JSON-to-Parquet job drops any record whose TimeStamp is not a string.
        assert isinstance(body["TimeStamp"], str) and " " in body["TimeStamp"]
        assert body["Data"] == {"orchestrator": "EKS", "region": "us-west-2", "status": "started"}

    def test_orchestrator_is_null_when_unset_so_a_missing_value_shows_as_a_bug(self, captured, monkeypatch):
        monkeypatch.delenv("ORCHESTRATOR", raising=False)
        assert lf.send("StackStarted", {"status": "started"}) is True
        assert captured[0]["Data"]["orchestrator"] is None

    def test_omits_account_when_not_configured(self, captured, monkeypatch):
        monkeypatch.delenv("TELEMETRY_ACCOUNT_ID", raising=False)
        lf.send("StackStarted", {})
        assert "Account" not in captured[0]

    def test_every_data_key_is_snake_case(self, sent, monkeypatch, stack_statuses):
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        nid = nested_arn("FsxFilesystemStack")
        monkeypatch.setattr(lf, "collect", lambda _id: (
            [ev("customer-stack-name", "CREATE_IN_PROGRESS"),
             ev("FsxFilesystemStack", "CREATE_IN_PROGRESS", t=1, phys=nid),
             ev("FsxFilesystemStack", "CREATE_FAILED", t=5, reason="x", phys=nid)],
            {nid: [ev("Fs", "CREATE_FAILED", rtype="AWS::FSx::FileSystem", reason="quota")]},
        ))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        snake = re.compile(r"^[a-z][a-z0-9_]*$")

        def keys(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    # stack_parameters is keyed by the template's original (PascalCase) parameter
                    # names on purpose, so its keys are not expected to be snake_case.
                    if k != "stack_parameters":
                        yield from keys(v)
            elif isinstance(o, list):
                for v in o:
                    yield from keys(v)

        assert sent and all(snake.match(k) for _, d in sent for k in keys(d))

    def test_oversized_payload_is_dropped_and_logged(self, captured, capsys):
        assert lf.send("StackCompleted", {"steps": ["x" * (lf.MAX_PAYLOAD_BYTES + 1)]}) is False
        assert captured == []
        assert "not delivered" in capsys.readouterr().out

    def test_send_never_raises(self, monkeypatch, capsys):
        def boom(_payload, _event_type=""):
            raise RuntimeError("network down")

        monkeypatch.delenv("TELEMETRY_TEST_SQS_URL", raising=False)
        monkeypatch.setattr(lf, "deliver", boom)
        assert lf.send("StackStarted", {}) is False
        assert "not delivered" in capsys.readouterr().out


class TestStackKeys:
    def test_failure_messages_carry_each_step_key(self, sent, monkeypatch, stack_statuses):
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        monkeypatch.setattr(lf, "collect", lambda _id: ([
            ev("customer-stack-name", "CREATE_IN_PROGRESS"),
            ev("HelmChartStack", "CREATE_FAILED", reason="Parameters: [X] do not exist in the template"),
            ev("SageMakerIAMRoleStack", "CREATE_FAILED", reason="Resource creation cancelled"),
        ], {}))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        keys = [(t, d["stack_key"], d["root_stack_key"]) for t, d in sent]
        assert keys == [
            ("SubStackFailed", "MAIN_STACK_HELM_CHART", "MAIN_STACK"),
            ("SubStackFailed", "MAIN_STACK_SAGEMAKER_IAM_ROLE", "MAIN_STACK"),
            ("StackFailed", "MAIN_STACK", "MAIN_STACK"),
        ]

    def test_unknown_step_has_null_key_and_unset_env_is_null(self, sent, monkeypatch):
        assert lf.stack_keys()["VPCStack"] == "MAIN_STACK_VPC"
        monkeypatch.setenv("TELEMETRY_STACK_KEYS", "")
        monkeypatch.setenv("TELEMETRY_ROOT_STACK_KEY", "")
        lf.lambda_handler(cfn_event("Start", "Create"), mock.Mock())
        assert sent[0][1]["stack_key"] is None and sent[0][1]["root_stack_key"] is None


class TestDeploymentId:
    def test_passed_through_unchanged_in_every_message(self, sent, monkeypatch, stack_statuses):
        monkeypatch.setenv("TELEMETRY_DEPLOYMENT_ID", "amandacn-test-rev3-001")
        lf.lambda_handler(cfn_event("Start", "Create"), mock.Mock())
        stack_statuses[ROOT_ID] = "ROLLBACK_IN_PROGRESS"
        monkeypatch.setattr(lf, "collect", lambda _id: ([ev("customer-stack-name", "CREATE_IN_PROGRESS"),
                                                          ev("HelmChartStack", "CREATE_FAILED", reason="x")], {}))
        lf.lambda_handler(cfn_event("Start", "Delete"), mock.Mock())
        assert len(sent) >= 3
        assert all(d["deployment_id"] == "amandacn-test-rev3-001" for _, d in sent)

    def test_null_when_not_set(self, sent, monkeypatch):
        monkeypatch.setenv("TELEMETRY_DEPLOYMENT_ID", "")
        lf.lambda_handler(cfn_event("Start", "Create"), mock.Mock())
        assert sent[0][1]["deployment_id"] is None


class TestStackParameters:
    def test_only_listed_parameters_are_sent_with_their_original_names(self):
        # Parameter names and values are sent as-is (not snake_cased); free-text params are never listed.
        assert lf.stack_parameters(ROOT_ID) == {
            "NodeProvisioningMode": "Continuous", "CreateEKSClusterStack": "true", "EnableHPInferenceFeature": "true",
        }

    def test_none_when_no_list_or_unreadable(self, monkeypatch):
        monkeypatch.setenv("TELEMETRY_STACK_PARAMETERS", "")
        assert lf.stack_parameters(ROOT_ID) is None
        monkeypatch.setenv("TELEMETRY_STACK_PARAMETERS", "Stage")

        def denied(StackName):
            raise RuntimeError("AccessDenied")

        monkeypatch.setattr(lf.cfn, "describe_stacks", denied)
        assert lf.stack_parameters(ROOT_ID) is None


class TestPost:
    @pytest.fixture
    def urlopen(self, monkeypatch):
        def make(results):
            calls = []

            def fake(req, timeout):
                calls.append((req.full_url, timeout))
                result = results.pop(0)
                if isinstance(result, Exception):
                    raise result
                resp = mock.MagicMock(status=result)
                resp.__enter__.return_value = resp
                resp.__exit__.return_value = False
                resp.read.return_value = json.dumps(
                    {"SendMessageResponse": {"SendMessageResult": {"MessageId": "30a64f18-0df9-4468-8b7c-7a2511efae6a"}}}
                ).encode()
                return resp

            monkeypatch.setattr(lf.urllib.request, "urlopen", fake)
            return calls

        return make

    def test_posts_to_the_default_endpoint_with_a_3s_timeout(self, urlopen, monkeypatch):
        monkeypatch.delenv("TELEMETRY_ENDPOINT", raising=False)
        calls = urlopen([200])
        assert lf.post(b"{}") is True
        assert calls == [("https://metrics.awssolutionsbuilder.com/generic", 3)]

    def test_retries_once_on_5xx_and_connection_errors(self, urlopen):
        err = lf.urllib.error.HTTPError("u", 503, "x", {}, None)
        calls = urlopen([err, 200])
        assert lf.post(b"{}") is True and len(calls) == 2
        calls = urlopen([OSError("reset"), OSError("reset")])
        assert lf.post(b"{}") is False and len(calls) == 2

    def test_logs_status_and_message_id_on_delivery(self, urlopen, capsys):
        urlopen([200])
        assert lf.post(b"{}", "StackStarted") is True
        assert "telemetry: delivered StackStarted HTTP 200 MessageId=30a64f18-0df9-4468-8b7c-7a2511efae6a" in capsys.readouterr().out

    def test_message_id_tolerates_unexpected_bodies(self):
        assert lf.message_id("not json") is None and lf.message_id("{}") is None

    def test_does_not_retry_4xx(self, urlopen):
        calls = urlopen([lf.urllib.error.HTTPError("u", 400, "x", {}, None)])
        assert lf.post(b"{}") is False and len(calls) == 1


class TestRespond:
    def test_retries_the_cloudformation_callback_with_backoff(self, monkeypatch):
        attempts, sleeps = [], []

        def fake(req, timeout):
            attempts.append(req.get_method())
            if len(attempts) < 3:
                raise OSError("connection reset")
            return mock.MagicMock()

        monkeypatch.setattr(lf.urllib.request, "urlopen", fake)
        monkeypatch.setattr(lf.time, "sleep", sleeps.append)
        assert lf.respond(cfn_event("Start", "Create"), mock.Mock(log_stream_name="s")) is True
        assert attempts == ["PUT", "PUT", "PUT"]
        assert sleeps == [1, 2]

    def test_gives_up_after_all_attempts_and_logs_it(self, monkeypatch, capsys):
        def fail(req, timeout):
            raise OSError("down")

        monkeypatch.setattr(lf.urllib.request, "urlopen", fail)
        monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
        assert lf.respond(cfn_event("Start", "Create"), mock.Mock(log_stream_name="s")) is False
        out = capsys.readouterr().out
        assert out.count("failed") >= lf.RESPONSE_ATTEMPTS and "FAILED after all attempts" in out


    @pytest.mark.parametrize("bad_event", [{}, {"StackId": ROOT_ID}, {"ResourceProperties": None, "StackId": "s", "RequestId": "r", "LogicalResourceId": "l"}])
    def test_never_raises_on_a_malformed_event_or_context(self, bad_event, monkeypatch, capsys):
        monkeypatch.setattr(lf.urllib.request, "urlopen", mock.Mock(side_effect=AssertionError("must not be called")))
        assert lf.respond(bad_event, object()) is False
        assert "FAILED" in capsys.readouterr().out

    def test_handler_never_raises_even_when_responding_fails(self, monkeypatch):
        monkeypatch.setattr(lf.urllib.request, "urlopen", mock.Mock(side_effect=TimeoutError("timed out")))
        monkeypatch.setattr(lf.time, "sleep", lambda _s: None)
        lf.lambda_handler(cfn_event("Start", "Create", enabled="false"), mock.Mock(log_stream_name="s"))  # no exception
        lf.lambda_handler({"RequestType": "Create"}, None)  # malformed: still no exception


class TestPackaging:
    def test_zip_ships_the_current_handler(self):
        import zipfile

        path = os.path.join(HERE, "..", "..", "..", "artifacts", "stack-telemetry-lambda-function.zip")
        with zipfile.ZipFile(path) as z, open(os.path.join(HERE, "..", "lambda_function.py"), "rb") as f:
            assert z.namelist() == ["lambda_function.py"]
            assert z.read("lambda_function.py") == f.read(), "stale zip: run resources/stack-telemetry/generate-stack-telemetry-lambda-zip.sh"
