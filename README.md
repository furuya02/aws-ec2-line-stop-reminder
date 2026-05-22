# aws-ec2-line-stop-reminder

A mechanism that **asks "continue / stop?" via LINE every 60 minutes** for running EC2 instances and **auto-stops** them when there is no response (prevents leaving instances running). The interval is configurable at deploy time (default 60 minutes).

- Instead of passively stopping "idle-looking" instances, it **actively asks "are you still using this?" over LINE**
- Targets EC2 instances tagged **`AutoStopNotify=true`**
- **Zero fixed-cost design**: DynamoDB on-demand, LINE tokens stored in SSM Parameter Store SecureString (free). Secrets Manager is not used because it has a fixed monthly cost.

> ⚠️ This only *reduces* the chance of forgetting to stop instances. The ultimate responsibility for stopping EC2 (especially GPU) billing remains with the user.

## Architecture

```
EventBridge Scheduler (every 60 min)
  └─> Step Functions state machine
        CheckRunning (list tagged running instances)
          └─> Map (per instance)
                SendNotification (waitForTaskToken: LINE Push + Quick Reply,
                                  store task token in DynamoDB and wait)
                  ├─ responded (SendTaskSuccess) → continue:end / stop:end
                  └─ no response (5-min timeout) → resend (default 1x) → still no response: auto stop

LINE Webhook:
  API Gateway → responder Lambda
    verify signature (HMAC-SHA256)
      → resume the state machine via SendTaskSuccess (using the token in DynamoDB)
      → if "stop", call ec2:StopInstances
```

## Resources

| Resource | Purpose | Fixed cost |
|---|---|---|
| Step Functions | Notify → evaluate flow | none (per-transition) |
| EventBridge Scheduler | Trigger every 60 min (default, configurable) | none |
| Lambda × 4 | check_running / notifier / stopper / responder | none |
| DynamoDB (on-demand) | Pass the task token | **none** |
| API Gateway (REST) | LINE Webhook endpoint | none |
| SSM Parameter Store (SecureString) | LINE token storage | **none (free)** |

## Prerequisites

- AWS account / credentials (assumes `ap-northeast-1`)
- [pnpm](https://pnpm.io/) (`npm` / `npx` are not used)
- AWS CDK bootstrapped (`pnpm exec cdk bootstrap` if not yet)
- A LINE Official Account + Messaging API channel

## Setup

### 1. Prepare LINE (manual)

1. Create a provider in the [LINE Developers Console](https://developers.line.biz/)
2. Create a **Messaging API channel**
3. Issue and note the **Channel access token (long-lived)**
4. Note the **Channel secret**
5. (userId is obtained later)

### 2. Clone & install

```bash
git clone https://github.com/<your-account>/aws-ec2-line-stop-reminder.git
cd aws-ec2-line-stop-reminder/cdk
pnpm install
```

### 3. Deploy

```bash
# first time only
pnpm exec cdk bootstrap

pnpm exec cdk deploy
```

Note the `WebhookUrl` from the Outputs.

> To shorten intervals for a demo:
> `pnpm exec cdk deploy -c interval_minutes=5 -c wait_minutes=1 -c max_retry=2`
> Override the account-id part: `-c suffix=20260521`

### 4. Store LINE tokens in SSM

```bash
cd ../scripts
./put-line-params.sh "<CHANNEL_ACCESS_TOKEN>" "<CHANNEL_SECRET>"
# userId can be registered later, so it is optional here
```

### 5. Register the Webhook URL in LINE

1. LINE Developers Console → Messaging API settings → set **Webhook URL** to the `WebhookUrl` output
2. Turn **Use webhook** ON
3. Click "Verify" and confirm a 200 response

### 6. Get and register userId

1. Add your Official Account as a friend and send any message
2. The `responder` Lambda's CloudWatch Logs will show `userId=U....`
3. Register it:

```bash
aws ssm put-parameter --region ap-northeast-1 --type SecureString --overwrite \
  --name /ec2-line-stop-reminder/user-id --value "<YOUR_USER_ID>"
```

### 7. Tag target EC2 instances

Tag the instances you want to monitor with **`AutoStopNotify=true`**.

```bash
aws ec2 create-tags --region ap-northeast-1 \
  --resources <INSTANCE_ID> --tags Key=AutoStopNotify,Value=true
```

## Try it

- To test without waiting, **Start execution** on the Step Functions state machine manually (input `{}`)
- A LINE message with "continue / stop" Quick Reply buttons arrives (the body shows the instance ID and its Name tag)
  - **continue** → that session ends (no stop)
  - **stop** → `ec2:StopInstances` runs immediately
  - **no response** → resend after `wait_minutes` (default 5 min) → auto-stop after `max_retry` (default 1) resends

## Teardown (required)

```bash
cd scripts
./teardown.sh
```

Runs `cdk destroy` + deletes SSM parameters + residual check.

## Cost

- The mechanism itself is designed to be **zero fixed-cost**.
- Variable cost factors:
  - Step Functions state transitions (a few tens of yen / month)
  - LINE Messaging API beyond the free tier (in Japan, **200 messages/month** free).
    Only push messages count (1 per recipient); reply messages to a tap are not counted (immediate response).
    Frequent 60-min notifications plus resends may exceed the free tier.
- Each notification appends the **remaining free quota** ("残り 約 N 通") via `GET /v2/bot/message/quota/consumption` (free tier set by the `FREE_QUOTA` env var, default 200).
- **Billing for the monitored EC2 itself is separate.** This reduces forgotten instances but the responsibility to stop billing remains with the user.

## License

MIT
