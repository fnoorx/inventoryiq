"""Run one database job with the deployed task definition and report its exit code.

The AWS region comes from the usual AWS configuration (``AWS_REGION`` or your profile).
"""

import argparse
import json
import time

import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", required=True, help="Path to terraform output -json")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    with open(args.outputs, encoding="utf-8-sig") as source:
        outputs = {key: item["value"] for key, item in json.load(source).items()}
    if not args.command:
        parser.error("Supply the container command after --outputs FILE")
    ecs = boto3.client("ecs")
    logs = boto3.client("logs")
    cluster = outputs["ecs_cluster_name"]
    response = ecs.run_task(
        cluster=cluster, taskDefinition=outputs["ecs_task_definition_arn"],
        launchType="FARGATE", count=1,
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": outputs["public_subnet_ids"],
            "securityGroups": [outputs["ecs_security_group_id"]],
            "assignPublicIp": "ENABLED",
        }},
        overrides={"containerOverrides": [{"name": "bot", "command": args.command}]},
    )
    if response.get("failures"):
        raise RuntimeError("ECS rejected the task request.")
    arn = response["tasks"][0]["taskArn"]
    print(f"Started job: {arn}", flush=True)
    previous = None
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        task = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]
        status = task["lastStatus"]
        if status != previous:
            print(f"Job status: {status}", flush=True)
            previous = status
        if status == "STOPPED":
            break
        time.sleep(10)
    else:
        ecs.stop_task(cluster=cluster, task=arn, reason="Database job timeout")
        raise RuntimeError("Job timed out and was stopped.")

    try:
        stream = "ecs/bot/" + arn.rsplit("/", 1)[-1]
        events = logs.get_log_events(
            logGroupName=f"/ecs/{cluster}", logStreamName=stream, startFromHead=True,
        )["events"]
        for event in events:
            print(event["message"])
    except logs.exceptions.ResourceNotFoundException:
        print("No log stream was created.")
    code = task["containers"][0].get("exitCode", 1)
    print(f"Exit code: {code}; reason: {task.get('stoppedReason', 'unknown')}")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
