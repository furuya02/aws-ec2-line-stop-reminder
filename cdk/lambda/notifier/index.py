"""LINE へ「継続 / 停止」の Quick Reply 付き Push メッセージを送る。

Step Functions の waitForTaskToken で起動され、受け取ったタスクトークンを
DynamoDB に保存する。ユーザーが応答すると responder がこのトークンで
SendTaskSuccess を呼び、ステートマシンが即再開する(無応答時はタスク側が
タイムアウト → 再送)。
"""
import json
import os
import time
import urllib.request
from typing import Any

import boto3

ssm_client = boto3.client("ssm")
dynamodb_resource = boto3.resource("dynamodb")

TABLE_NAME: str = os.environ["TABLE_NAME"]
TOKEN_PARAM: str = os.environ["TOKEN_PARAM"]
USER_PARAM: str = os.environ["USER_PARAM"]
FREE_QUOTA: int = int(os.environ.get("FREE_QUOTA", "200"))  # フリープラン無料枠(日本=月200通)

parameter_cache: dict[str, str] = {}


def get_secure_parameter(parameter_name: str) -> str:
    if parameter_name not in parameter_cache:
        result = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
        parameter_cache[parameter_name] = result["Parameter"]["Value"]
    return parameter_cache[parameter_name]


def push_quick_reply(
    access_token: str,
    user_id: str,
    message_text: str,
    instance_id: str,
    session_id: str,
) -> None:
    request_body = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": message_text,
                "quickReply": {
                    "items": [
                        {
                            "type": "action",
                            "action": {
                                "type": "postback",
                                "label": "継続",
                                "data": f"action=continue&instanceId={instance_id}&session={session_id}",
                                "displayText": "継続します",
                            },
                        },
                        {
                            "type": "action",
                            "action": {
                                "type": "postback",
                                "label": "停止",
                                "data": f"action=stop&instanceId={instance_id}&session={session_id}",
                                "displayText": "停止します",
                            },
                        },
                    ]
                },
            }
        ],
    }
    http_request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(http_request) as http_response:
        http_response.read()


def get_remaining_free_quota(access_token: str) -> int | None:
    """今月の送信済み通数から無料枠の残数を概算で返す(取得失敗時は None)。

    この統計 API の呼び出し自体は送信ではないため通数にカウントされない。
    """
    try:
        http_request = urllib.request.Request(
            "https://api.line.me/v2/bot/message/quota/consumption",
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        with urllib.request.urlopen(http_request) as http_response:
            total_usage = int(json.loads(http_response.read())["totalUsage"])
        return max(0, FREE_QUOTA - total_usage)
    except Exception as error:  # 取得失敗時は残数表示を省略
        print(f"get_remaining_free_quota skipped: {error}")
        return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    instance_id: str = event["instanceId"]
    session_id: str = event["sessionId"]
    task_token: str = event["taskToken"]

    # 応答受信時に responder が使うトークンを保存(セッションごとに上書き)
    state_table = dynamodb_resource.Table(TABLE_NAME)
    state_table.put_item(
        Item={
            "instanceId": instance_id,
            "sessionId": session_id,
            "taskToken": task_token,
            "ttl": int(time.time()) + 3600,
        }
    )

    access_token = get_secure_parameter(TOKEN_PARAM)
    user_id = get_secure_parameter(USER_PARAM)
    remaining = get_remaining_free_quota(access_token)
    quota_line = f"\n（今月の無料枠 残り 約 {remaining} 通）" if remaining is not None else ""
    message_text = (
        f"EC2 インスタンス {instance_id} が起動中です。\n"
        f"継続しますか？停止しますか？\n"
        f"（5 分以内に応答がない場合は再確認します。無応答が続くと自動停止します）"
        f"{quota_line}"
    )
    push_quick_reply(access_token, user_id, message_text, instance_id, session_id)
    return {"instanceId": instance_id}
