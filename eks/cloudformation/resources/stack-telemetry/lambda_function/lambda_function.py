"""
Cluster-setup outcome telemetry for the HyperPod cluster setup templates, as a Lambda-backed custom
resource (see lib/constructs/stack-telemetry-custom-resource.ts).

Custom resources in the main stack that use this function:
  Phase=Start  - created FIRST (every other resource DependsOn it), so it is deleted LAST.
                 Create -> StackStarted.
                 Delete while rolling back -> one SubStackFailed per failed/cancelled step, then StackFailed.
                 Delete on a plain user delete -> nothing (telemetry is collected only for stack creation).
  Phase=End    - created LAST (references every resource).  Create -> StackCompleted.

Messages use the AWS Solutions operational-metrics envelope (Solution, Version, UUID, TimeStamp,
Account, Type, Data; same contract as SpecEngSolutionTelemetry). Every Data key is snake_case:
orchestrator, region, root_stack_key, stack_key, account_id, stack_name, stack_status, root_stack_status, step,
type (CloudFormation resource type), status, on_success_latency, on_failure_latency (seconds; null
when not applicable), deployment_id (TelemetryDeploymentId, passed through; null when empty); failure messages add failed_step, failed_resource, failed_resource_type and
failure_category; StackCompleted / StackFailed add stack_parameters (fixed-choice parameters only, keyed by
their original parameter names). Physical resource IDs other than stack names are never sent.

Telemetry must never fail the stack: every path responds SUCCESS to CloudFormation.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

cfn = boto3.client("cloudformation")

NESTED = "AWS::CloudFormation::Stack"
CANCELLED = "Resource creation cancelled"
DEFAULT_ENDPOINT = "https://metrics.awssolutionsbuilder.com/generic"
SEND_TIMEOUT_S = 3
MAX_PAYLOAD_BYTES = 256 * 1024
RESPONSE_ATTEMPTS = 5  # CloudFormation callback: 1 s, 2 s, 4 s, 8 s between attempts
CATEGORIES = [  # first match wins, IGNORECASE
    ("ParameterMismatch", r"do not exist in the template|does not require them"),

    # Above Quota. Throttles are spelled "...LimitExceeded" but mean back off and retry, not
    # raise a limit: RequestLimitExceeded, EC2RequestLimitExceeded, UpdateLimitExceeded.
    ("Throttling", r"Throttling|TooManyRequests|RequestLimitExceeded|TooManyUpdates"
                   r"|SlowDown|Rate exceeded"),

    # Above Quota. CFN providers have no capacity value in their error enum, so they report
    # capacity shortfalls as ServiceLimitExceeded. Testing Quota first turns a p5 shortage into
    # "file a limit increase". EKS words it "does not currently have sufficient capacity", which
    # the single-token `insufficient capacity` misses.
    ("Capacity", r"Insufficient[A-Za-z]*Capacity|InsufficientFreeAddresses"
                 r"|sufficient capacity to support|enough available hardware capacity"
                 r"|Not enough available addresses|enough free private IP addresses"
                 r"|not (?:currently )?(?:available|supported) in (?:the specified|your requested) Availability Zone"),

    ("Quota", r"ServiceQuotaExceeded|ServiceLimitExceeded|\bquota\b|QuotaExceeded"
              r"|\blimit exceeded\b|has been reached|maximum number of"
              r"|(?<!Backoff)LimitExceeded"),

    ("Permissions", r"AccessDenied|Access Denied|not authorized|NotAuthorized|AuthFailure"
                    r"|UnauthorizedOperation|UnauthorizedTagging|is forbidden"
                    r"|does not have permission|no resource-based policy allows"),

    # Above Validation. DependencyViolation is the top stack-delete blocker and reaches Other
    # today; the .InUse codes reach Validation.
    ("Dependency", r"DependencyViolation|BucketNotEmpty|DeleteConflict|AssociatedInstances"
                   r"|\.InUse\b|has dependencies and cannot be|has a dependent object"
                   r"|must detach all policies first|is not empty"),

    # Above Validation. Every name collision and every redeploy after a failed rollback is
    # invisible today, and the Invalid*State family reads as a template typo.
    ("Conflict", r"AlreadyExists|ResourceInUse|ResourceConflict|ConflictException"
                 r"|\.Duplicate\b|\.Conflict\b|OperationAborted|ConcurrentModification"
                 r"|Invalid[A-Za-z]*State\b|already exists|already in use|already associated"
                 r"|another operation is in progress|marked for deletion"),

    ("NotFound", r"\bNotFound|does not exist|doesn't exist|cannot be found|could not be found"
                 r"|\bnot found\b"),

    ("Validation", r"ValidationError|ValidationException|validation|Malformed"
                   r"|is not valid|isn't valid|is invalid|Invalid"),

    # Last, so it never shadows a specific cause. Retriable AWS-side faults.
    ("ServiceError", r"InternalFailure|InternalError|InternalServerError|ServiceUnavailable"
                     r"|ServerException|ServiceInternalError"),
]


# ---------------------------------------------------------------- sending


def envelope(event_type, data):
    body = {
        "Solution": os.environ["TELEMETRY_SOLUTION_ID"],
        "Version": os.environ["TELEMETRY_SOLUTION_VERSION"],
        "UUID": os.environ["TELEMETRY_UUID"],
        # String, not an epoch number: the AWS Solutions JSON-to-Parquet daily job drops any record
        # whose TimeStamp is not a string, so a numeric value silently never reaches Athena.
        "TimeStamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "Type": event_type,
        "Data": data,
    }
    if os.environ.get("TELEMETRY_ACCOUNT_ID"):
        body["Account"] = os.environ["TELEMETRY_ACCOUNT_ID"]
    return body


def message_id(response_body):
    """The AWS Solutions endpoint answers with its queue's SendMessage result; return its MessageId if present."""
    try:
        return json.loads(response_body)["SendMessageResponse"]["SendMessageResult"]["MessageId"]
    except Exception:
        return None


def post(payload, event_type=""):
    """
    POST once with a 3 s timeout; retry once on a connection error or a 5xx. Returns True if delivered.
    Logs every outcome: "telemetry: delivered <Type> HTTP <status> MessageId=<id>" or "telemetry: not delivered: ...".
    """
    endpoint = os.environ.get("TELEMETRY_ENDPOINT") or DEFAULT_ENDPOINT
    for attempt in (1, 2):
        req = urllib.request.Request(endpoint, data=payload, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:
                ok = 200 <= resp.status < 300
                body = resp.read(4096).decode("utf-8", "replace")
                if ok:
                    print(f"telemetry: delivered {event_type} HTTP {resp.status} MessageId={message_id(body)}")
                else:
                    print(f"telemetry: not delivered: {event_type} HTTP {resp.status}")
                return ok
        except urllib.error.HTTPError as err:
            if err.code < 500 or attempt == 2:
                print(f"telemetry: not delivered: HTTP {err.code}")
                return False
        except Exception as err:  # connection error or timeout
            if attempt == 2:
                print(f"telemetry: not delivered: {type(err).__name__}")
                return False
    return False


def deliver(payload, event_type=""):
    """Test hook: TELEMETRY_TEST_SQS_URL (the template never sets it) sends to SQS instead of HTTP."""
    queue = os.environ.get("TELEMETRY_TEST_SQS_URL")
    if queue:
        result = boto3.client("sqs").send_message(QueueUrl=queue, MessageBody=payload.decode())
        print(f"telemetry: delivered {event_type} to test SQS MessageId={result.get('MessageId')}")
        return True
    return post(payload, event_type)


def send(event_type, data):
    """Never raises: a telemetry problem must not fail the stack. Every send that doesn't go out is logged."""
    try:
        # Do not default the orchestrator: if ORCHESTRATOR is unset the value goes out empty so the
        # gap is visible as a bug, rather than being silently reported as "EKS".
        data = {"orchestrator": os.environ.get("ORCHESTRATOR") or None, "region": os.environ["AWS_REGION"], **data}
        payload = json.dumps(envelope(event_type, data), separators=(",", ":")).encode()
        if len(payload) > MAX_PAYLOAD_BYTES:
            print(f"telemetry: not delivered: {event_type} is {len(payload)} bytes, over {MAX_PAYLOAD_BYTES}")
            return False
        print(f"telemetry: sending {payload.decode()}")
        return deliver(payload, event_type)
    except Exception as err:
        print(f"telemetry: not delivered: {event_type}: {type(err).__name__}: {err}")
        return False


# ---------------------------------------------------------------- reading the stack


def categorize(reason):
    for name, pattern in CATEGORIES:
        if re.search(pattern, reason or "", re.IGNORECASE):
            return name
    return "Other"


def all_events(stack_id):
    """Events oldest-first for one stack."""
    events = []
    for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack_id):
        events.extend(page["StackEvents"])
    return sorted(events, key=lambda e: e["Timestamp"])


def collect(root_id):
    """Root events, plus events of every nested stack (recursively), keyed by nested stack id."""
    root = all_events(root_id)
    nested, todo = {}, [e["PhysicalResourceId"] for e in root if e["ResourceType"] == NESTED]
    while todo:
        sid = todo.pop()
        if not sid or not sid.startswith("arn:") or sid in nested or sid == root_id:
            continue
        nested[sid] = all_events(sid)
        todo += [e["PhysicalResourceId"] for e in nested[sid] if e["ResourceType"] == NESTED]
    return root, nested


def step_timeline(root_events):
    """Per root-level step (nested stacks only): start, end, final status."""
    steps = {}
    for e in root_events:
        # Skip the main stack's own events: they are not a step.
        if e["ResourceType"] != NESTED or e["LogicalResourceId"] == e["StackName"]:
            continue
        s = steps.setdefault(e["LogicalResourceId"], {"step": e["LogicalResourceId"]})
        status = e["ResourceStatus"]
        if status == "CREATE_IN_PROGRESS" and "start" not in s:
            s["start"] = e["Timestamp"].isoformat()
        if status in ("CREATE_COMPLETE", "CREATE_FAILED"):
            s["end"] = e["Timestamp"].isoformat()
            s["status"] = status
            if status == "CREATE_FAILED":
                s["cancelled"] = CANCELLED in (e.get("ResourceStatusReason") or "")
    for s in steps.values():
        s.setdefault("status", "NOT_FINISHED")
    return sorted(steps.values(), key=lambda s: s.get("start", "~"))


def inner_failure(nested_events):
    """The first real (not cancelled) CREATE_FAILED resource inside a nested stack, if any."""
    return next(
        (e for e in nested_events
         if e["ResourceStatus"] == "CREATE_FAILED" and e["LogicalResourceId"] != e["StackName"]
         and CANCELLED not in (e.get("ResourceStatusReason") or "")),
        None,
    )


def deepest_failure(nested, stack_id):
    """
    The resource that actually failed under a failed nested stack, following nested stacks down
    (e.g. InferenceOperatorMainStack -> InferenceOperatorHelmInstallStack -> an AWS::IAM::Role).
    """
    found = inner_failure(nested.get(stack_id, []))
    seen = {stack_id}
    while found and found["ResourceType"] == NESTED and found.get("PhysicalResourceId") in nested:
        child = found["PhysicalResourceId"]
        if child in seen:
            break
        seen.add(child)
        deeper = inner_failure(nested[child])
        if not deeper:
            break
        found = deeper
    return found


def root_cause(root_events, nested):
    """Earliest real (not cancelled) CREATE_FAILED: the root step, and the deepest failing resource in it."""
    failed = [
        e for e in root_events
        if e["ResourceStatus"] == "CREATE_FAILED"
        and e["LogicalResourceId"] != e["StackName"]
        and CANCELLED not in (e.get("ResourceStatusReason") or "")
    ]
    if not failed:
        return {}
    first_failed = failed[0]
    cause = {
        "failed_step": first_failed["LogicalResourceId"],
        "failed_resource_type": first_failed["ResourceType"],
        "failure_category": categorize(first_failed.get("ResourceStatusReason")),
    }
    inner = deepest_failure(nested, first_failed.get("PhysicalResourceId"))
    if inner:
        cause["failed_resource"] = inner["LogicalResourceId"]
        cause["failed_resource_type"] = inner["ResourceType"]
        cause["failure_category"] = categorize(inner.get("ResourceStatusReason"))
        # For a nested failure, send the deepest resource's full reason as well as the category: it is
        # the only signal that distinguishes nested change-set failures that otherwise read as "Other".
        cause["failure_reason"] = inner.get("ResourceStatusReason")
    return cause


def account_of(stack_id):
    return stack_id.split(":")[4]


def name_of(stack_id):
    return stack_id.split("/")[1]


def current_status(stack_id):
    """Current CloudFormation status (works for deleted stacks by ARN); None if it can't be read."""
    if not stack_id:
        return None
    try:
        return cfn.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
    except Exception as err:
        print("could not read stack status:", type(err).__name__)
        return None


def stack_keys():
    """Nested stack logical ID -> searchable key, from TELEMETRY_STACK_KEYS ("VPCStack=MAIN_STACK_VPC,...")."""
    pairs = (item.split("=", 1) for item in os.environ.get("TELEMETRY_STACK_KEYS", "").split(",") if "=" in item)
    return {logical: key for logical, key in pairs}


def fields(root_id, stack_id, status, resource_type=NESTED, step=None, success=None, failure=None):
    """
    The per-message fields: same keys on every message, null when not applicable.
    type   = CloudFormation resource type of what the message is about (for a failure, the resource
             that actually failed inside the step when there is one, e.g. AWS::IAM::Role).
    status = started | success | failure | cancelled | deleted.
    stack_status / root_stack_status = current CloudFormation status, read when the message is sent.
    """
    root_key = os.environ.get("TELEMETRY_ROOT_STACK_KEY") or None
    return {
        # Searchable keys (lib/constructs/stack-telemetry-keys.ts): root_stack_key is the main stack
        # (e.g. MAIN_STACK); stack_key is the stack this message is about (e.g. MAIN_STACK_EKS_CLUSTER).
        "root_stack_key": root_key,
        "stack_key": stack_keys().get(step) if step else root_key,
        "account_id": account_of(root_id),
        "stack_name": name_of(stack_id) if stack_id else None,
        "stack_status": current_status(stack_id),
        "root_stack_status": current_status(root_id),
        "step": step,
        "type": resource_type,
        "status": status,
        "on_success_latency": success,
        "on_failure_latency": failure,
        # Passed through unchanged from the TelemetryDeploymentId parameter, to tell deployments apart.
        "deployment_id": os.environ.get("TELEMETRY_DEPLOYMENT_ID") or None,
    }


def stack_parameters(root_id):
    """
    Values of the fixed-choice parameters the template lists in TELEMETRY_STACK_PARAMETERS (those with
    AllowedValues), keyed by their original parameter names. Free-text parameters are never listed.
    None if unreadable.
    """
    allowed = {n for n in os.environ.get("TELEMETRY_STACK_PARAMETERS", "").split(",") if n}
    if not allowed:
        return None
    try:
        params = cfn.describe_stacks(StackName=root_id)["Stacks"][0].get("Parameters", [])
    except Exception as err:
        print("could not read stack parameters:", type(err).__name__)
        return None
    return {p["ParameterKey"]: p.get("ParameterValue") for p in params if p["ParameterKey"] in allowed}


def first(events, logical, statuses):
    return next((e for e in events if e["LogicalResourceId"] == logical and e["ResourceStatus"] in statuses), None)


def seconds(start, end):
    return round((end["Timestamp"] - start["Timestamp"]).total_seconds(), 1) if start and end else None


def nested_id(events, logical):
    ids = [e["PhysicalResourceId"] for e in events
           if e["LogicalResourceId"] == logical and (e.get("PhysicalResourceId") or "").startswith("arn:")]
    return ids[0] if ids else None


# ---------------------------------------------------------------- messages


def stack_failed(root_id):
    root, nested = collect(root_id)
    root_name = name_of(root_id)
    for e in root:
        if e["ResourceType"] == NESTED and e["ResourceStatus"] == "CREATE_FAILED" and e["LogicalResourceId"] != e["StackName"]:
            step = e["LogicalResourceId"]
            cancelled = CANCELLED in (e.get("ResourceStatusReason") or "")
            inner = None if cancelled else deepest_failure(nested, e.get("PhysicalResourceId"))
            data = fields(root_id, nested_id(root, step), "cancelled" if cancelled else "failure",
                          resource_type=inner["ResourceType"] if inner else NESTED, step=step,
                          failure=seconds(first(root, step, ("CREATE_IN_PROGRESS",)), e))
            if not cancelled:
                data["failure_category"] = categorize((inner or e).get("ResourceStatusReason"))
                if inner:
                    data["failed_resource"] = inner["LogicalResourceId"]
                    # Full reason for the deepest nested failure (see root_cause): catches nested
                    # change-set failures the parent only reports as a generic "failed to create".
                    data["failure_reason"] = inner.get("ResourceStatusReason")
            send("SubStackFailed", data)
    started = first(root, root_name, ("CREATE_IN_PROGRESS",))
    rolled_back = first(root, root_name, ("ROLLBACK_IN_PROGRESS",))
    send("StackFailed", {**fields(root_id, root_id, "failure", failure=seconds(started, rolled_back)),
                         **root_cause(root, nested), "stack_parameters": stack_parameters(root_id), "steps": step_timeline(root)})


def stack_completed(root_id):
    root, _ = collect(root_id)
    started = first(root, name_of(root_id), ("CREATE_IN_PROGRESS",))
    now = {"Timestamp": datetime.now(timezone.utc)}
    send("StackCompleted", {**fields(root_id, root_id, "success", success=seconds(started, now)),
                            "stack_parameters": stack_parameters(root_id), "steps": step_timeline(root)})


# ---------------------------------------------------------------- CloudFormation


def respond(event, context, status="SUCCESS"):
    """
    Tell CloudFormation the resource is done. Every resource depends on StackTelemetry, so a lost
    callback would hang the launch for an hour: retry with exponential backoff, and log each failure.
    Never raises, including on a malformed event or context: nothing here can escape lambda_handler.
    """
    try:
        body = json.dumps({
            "Status": status,
            "Reason": "See CloudWatch log stream: " + str(getattr(context, "log_stream_name", "")),
            "PhysicalResourceId": event.get("PhysicalResourceId")
            or "telemetry-" + str((event.get("ResourceProperties") or {}).get("Phase", "")),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
        }).encode()
        url = event["ResponseURL"]
    except Exception as err:
        print(f"cloudformation response FAILED: could not build it: {type(err).__name__}: {err}")
        return False
    for attempt in range(1, RESPONSE_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, data=body, method="PUT",
                                         headers={"Content-Type": "", "Content-Length": str(len(body))})
            urllib.request.urlopen(req, timeout=10).close()
            return True
        except Exception as err:
            print(f"cloudformation response attempt {attempt}/{RESPONSE_ATTEMPTS} failed: {type(err).__name__}: {err}")
            if attempt < RESPONSE_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
    print("cloudformation response FAILED after all attempts; the stack will wait for CloudFormation's timeout")
    return False


def lambda_handler(event, context):
    try:
        print("request:", json.dumps({k: event.get(k) for k in ("RequestType", "LogicalResourceId")}))
        if event["ResourceProperties"].get("Enabled", "true") != "true":
            return
        root_id, phase, request = event["StackId"], event["ResourceProperties"]["Phase"], event["RequestType"]
        if phase == "Start" and request == "Create":
            send("StackStarted", fields(root_id, root_id, "started"))
        elif phase == "End" and request == "Create":
            stack_completed(root_id)
        elif phase == "Start" and request == "Delete":
            # Telemetry is only collected for stack creation. ROLLBACK_IN_PROGRESS only ever follows a
            # failed create, so report that; a plain user delete (DELETE_IN_PROGRESS) is left silent.
            status = cfn.describe_stacks(StackName=root_id)["Stacks"][0]["StackStatus"]
            if status == "ROLLBACK_IN_PROGRESS":
                stack_failed(root_id)
    except Exception as err:
        print("telemetry error (ignored):", repr(err))
    finally:
        respond(event, context)
