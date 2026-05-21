#!/usr/bin/env bash
# 完全削除: CDK スタック destroy + SSM Parameter 削除 + 残留確認。
# 放置課金を避けるため、確実にリソースを消す。
set -euo pipefail

PARAM_PREFIX="/ec2-line-stop-reminder"
REGION="${AWS_REGION:-ap-northeast-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> CDK destroy"
cd "${SCRIPT_DIR}/../cdk"
pnpm exec cdk destroy --force

echo "==> Delete SSM Parameters (SecureString)"
for NAME in channel-access-token channel-secret user-id; do
  aws ssm delete-parameter --region "$REGION" --name "${PARAM_PREFIX}/${NAME}" 2>/dev/null \
    && echo "deleted ${PARAM_PREFIX}/${NAME}" \
    || echo "skip ${PARAM_PREFIX}/${NAME} (not found)"
done

echo "==> Residual check (should be empty)"
echo "-- SSM parameters --"
aws ssm get-parameters-by-path --region "$REGION" --path "$PARAM_PREFIX" --query 'Parameters[].Name' --output text || true
echo "-- EventBridge schedules (name match) --"
aws scheduler list-schedules --region "$REGION" --name-prefix "aws-ec2-line-stop-reminder" --query 'Schedules[].Name' --output text || true

echo "Teardown complete."
