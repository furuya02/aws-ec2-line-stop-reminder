"""LINE へ「継続 / 停止」ボタン付きの Flex Message(カード)を Push 送信する。

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


def build_flex_bubble(
    instance_id: str,
    instance_name: str,
    remaining: int | None,
    session_id: str,
) -> dict[str, Any]:
    """継続/停止ボタン付きの Flex バブル(カード)を組み立てる。"""
    body_contents: list[dict[str, Any]] = [
        {"type": "text", "text": "インスタンス", "size": "xs", "color": "#888888"},
        {"type": "text", "text": instance_id, "weight": "bold", "size": "sm", "wrap": True},
    ]
    if instance_name:
        body_contents.append(
            {"type": "text", "text": f"Name : {instance_name}", "size": "sm", "color": "#555555", "wrap": True}
        )
    body_contents.append({"type": "separator", "margin": "md"})
    body_contents.append(
        {
            "type": "text",
            "text": "起動したままです。継続しますか？停止しますか？",
            "wrap": True,
            "margin": "md",
            "size": "sm",
        }
    )
    if remaining is not None:
        body_contents.append(
            {
                "type": "text",
                "text": f"今月の無料枠 残り 約 {remaining} 通",
                "size": "xs",
                "color": "#888888",
                "margin": "md",
            }
        )

    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#ED7100",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "EC2 起動中の確認", "color": "#FFFFFF", "weight": "bold", "size": "md"}
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#06C755",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "継続",
                        "data": f"action=continue&instanceId={instance_id}&session={session_id}",
                        "displayText": "継続します",
                    },
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#E0334B",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "停止",
                        "data": f"action=stop&instanceId={instance_id}&session={session_id}",
                        "displayText": "停止します",
                    },
                },
            ],
        },
    }


def push_flex_message(access_token: str, user_id: str, alt_text: str, bubble: dict[str, Any]) -> None:
    request_body = {
        "to": user_id,
        "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}],
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


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    instance_id: str = event["instanceId"]
    instance_name: str = event.get("name", "")
    session_id: str = event["sessionId"]
    task_token: str = event["taskToken"]
    instance_label: str = f"{instance_id}（{instance_name}）" if instance_name else instance_id

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
    bubble = build_flex_bubble(instance_id, instance_name, remaining, session_id)
    alt_text = f"EC2 インスタンス {instance_label} が起動中です。継続しますか？停止しますか？"
    push_flex_message(access_token, user_id, alt_text, bubble)
    return {"instanceId": instance_id}
