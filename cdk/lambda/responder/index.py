"""LINE Webhook 受信(API Gateway 経由)。

1. x-line-signature を Channel Secret で HMAC-SHA256 検証
2. postback(継続/停止)を受信し、DynamoDB のタスクトークンで SendTaskSuccess
   → ステートマシンを即再開させる
3. 「停止」なら即 ec2:StopInstances
4. message イベントは userId 取得用にログ出力
"""
import base64
import hashlib
import hmac
import json
import os
import urllib.request
from typing import Any

import boto3

ssm_client = boto3.client("ssm")
ec2_client = boto3.client("ec2")
sfn_client = boto3.client("stepfunctions")
dynamodb_resource = boto3.resource("dynamodb")

TABLE_NAME: str = os.environ["TABLE_NAME"]
TOKEN_PARAM: str = os.environ["TOKEN_PARAM"]
SECRET_PARAM: str = os.environ["SECRET_PARAM"]

parameter_cache: dict[str, str] = {}


def get_secure_parameter(parameter_name: str) -> str:
    if parameter_name not in parameter_cache:
        result = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
        parameter_cache[parameter_name] = result["Parameter"]["Value"]
    return parameter_cache[parameter_name]


def verify_signature(channel_secret: str, raw_body: str, signature: str) -> bool:
    digest = hmac.new(
        channel_secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def parse_postback_data(data: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for pair in data.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            parsed[key] = value
    return parsed


def resume_state_machine(instance_id: str, session_id: str, status: str) -> None:
    """現在セッションのタスクトークンで SendTaskSuccess し、待機中のステートマシンを再開する。"""
    state_table = dynamodb_resource.Table(TABLE_NAME)
    item = state_table.get_item(Key={"instanceId": instance_id}).get("Item", {})
    task_token = item.get("taskToken")
    # 古いセッションの応答は無視(現在の待機中タスクだけを再開)
    if not task_token or item.get("sessionId") != session_id:
        return
    try:
        sfn_client.send_task_success(
            taskToken=task_token,
            output=json.dumps({"instanceId": instance_id, "status": status}),
        )
    except Exception as error:  # TaskTimedOut / TaskDoesNotExist 等は無視
        print(f"send_task_success skipped: {error}")


def reply_message(access_token: str, reply_token: str, message_text: str) -> None:
    request_body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": message_text}],
    }
    http_request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(http_request) as http_response:
        http_response.read()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    raw_body: str = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    request_headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    signature = request_headers.get("x-line-signature", "")

    channel_secret = get_secure_parameter(SECRET_PARAM)
    if not verify_signature(channel_secret, raw_body, signature):
        return {"statusCode": 403, "body": "invalid signature"}

    payload = json.loads(raw_body) if raw_body else {}
    access_token = get_secure_parameter(TOKEN_PARAM)

    for line_event in payload.get("events", []):
        user_id = line_event.get("source", {}).get("userId", "")
        # userId 取得用のログ(初回の userId 登録に利用)
        print(f"LINE event type={line_event.get('type')} userId={user_id}")

        if line_event.get("type") != "postback":
            continue

        postback_data = parse_postback_data(line_event["postback"]["data"])
        action = postback_data.get("action")
        instance_id = postback_data.get("instanceId", "")
        session_id = postback_data.get("session", "")
        reply_token = line_event.get("replyToken", "")

        if action == "continue":
            resume_state_machine(instance_id, session_id, "continue")
            reply_message(access_token, reply_token, f"{instance_id} を継続します。")
        elif action == "stop":
            ec2_client.stop_instances(InstanceIds=[instance_id])
            resume_state_machine(instance_id, session_id, "stop")
            reply_message(access_token, reply_token, f"{instance_id} を停止しました。")

    return {"statusCode": 200, "body": "OK"}
