"""無応答が続いたインスタンスを自動停止する(Step Functions の最終分岐)。"""
from typing import Any

import boto3

ec2_client = boto3.client("ec2")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    instance_id: str = event["instanceId"]
    ec2_client.stop_instances(InstanceIds=[instance_id])
    return {"instanceId": instance_id, "stopped": True}
