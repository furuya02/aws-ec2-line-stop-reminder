#!/usr/bin/env bash
# LINE のトークン類を SSM Parameter Store (SecureString) に登録する。
# SecureString は固定費ゼロ(Secrets Manager は $0.40/月/secret の固定費があるため不使用)。
#
# 使い方:
#   ./put-line-params.sh <CHANNEL_ACCESS_TOKEN> <CHANNEL_SECRET> [USER_ID]
#
# USER_ID は後から取得できるため省略可。省略時は user-id は登録しない。
# (デプロイ → Webhook 登録 → Bot にメッセージ送信 → responder のログに出た userId を後で登録)
set -euo pipefail

PARAM_PREFIX="/ec2-line-stop-reminder"
REGION="${AWS_REGION:-ap-northeast-1}"

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <CHANNEL_ACCESS_TOKEN> <CHANNEL_SECRET> [USER_ID]" >&2
  exit 1
fi

CHANNEL_ACCESS_TOKEN="$1"
CHANNEL_SECRET="$2"
USER_ID="${3:-}"

aws ssm put-parameter --region "$REGION" --type SecureString --overwrite \
  --name "${PARAM_PREFIX}/channel-access-token" --value "$CHANNEL_ACCESS_TOKEN"
aws ssm put-parameter --region "$REGION" --type SecureString --overwrite \
  --name "${PARAM_PREFIX}/channel-secret" --value "$CHANNEL_SECRET"

if [ -n "$USER_ID" ]; then
  aws ssm put-parameter --region "$REGION" --type SecureString --overwrite \
    --name "${PARAM_PREFIX}/user-id" --value "$USER_ID"
  echo "Registered: channel-access-token, channel-secret, user-id"
else
  echo "Registered: channel-access-token, channel-secret"
  echo "NOTE: user-id is not set yet. Send a message to your bot, find userId in the"
  echo "      responder Lambda's CloudWatch Logs, then run:"
  echo "      aws ssm put-parameter --region ${REGION} --type SecureString --overwrite \\"
  echo "        --name ${PARAM_PREFIX}/user-id --value <YOUR_USER_ID>"
fi
