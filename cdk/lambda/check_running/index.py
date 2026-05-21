"""タグ AutoStopNotify=true かつ running 状態の EC2 を列挙する。"""
from typing import Any

import boto3

ec2_client = boto3.client("ec2")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:AutoStopNotify", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    running_instance_ids: list[str] = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            running_instance_ids.append(instance["InstanceId"])
    return {"instanceIds": running_instance_ids, "count": len(running_instance_ids)}
