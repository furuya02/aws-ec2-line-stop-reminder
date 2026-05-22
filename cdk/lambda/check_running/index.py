"""タグ AutoStopNotify=true かつ running 状態の EC2 を列挙する(Name タグも返す)。"""
from typing import Any

import boto3

ec2_client = boto3.client("ec2")


def get_name_tag(instance: dict[str, Any]) -> str:
    for tag in instance.get("Tags", []):
        if tag["Key"] == "Name":
            return str(tag["Value"])
    return ""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:AutoStopNotify", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    running_instances: list[dict[str, str]] = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            running_instances.append(
                {"instanceId": instance["InstanceId"], "name": get_name_tag(instance)}
            )
    return {"instances": running_instances, "count": len(running_instances)}
