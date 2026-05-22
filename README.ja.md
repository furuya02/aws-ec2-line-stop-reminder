# aws-ec2-line-stop-reminder

起動中の EC2 インスタンスに対して **60 分ごとに LINE で「継続 / 停止」を確認**し、無応答が続いたら**自動停止**する仕組み（停止忘れ防止）。確認間隔はデプロイ時に変更できます（既定 60 分）。

- 受動的に「使ってなさそうなら止める」のではなく、**LINE で能動的に「まだ使ってる？」と問いかける**方式
- 対象は **タグ `AutoStopNotify=true`** が付いた EC2 インスタンス
- **固定費ゼロ方針**: DynamoDB はオンデマンド、LINE トークンは SSM Parameter Store SecureString（無料）に保管（Secrets Manager は固定費があるため不使用）

> ⚠️ この仕組みは停止忘れを「減らす」ものです。EC2（特に GPU）の課金停止の最終責任は利用者にあります。

## アーキテクチャ

```
EventBridge Scheduler (60分ごと・既定)
  └─> Step Functions ステートマシン
        CheckRunning (タグ付き running を列挙)
          └─> Map (インスタンスごと)
                SendNotification (waitForTaskToken: LINE Push + Quick Reply,
                                  タスクトークンをDynamoDBに保存して待機)
                  ├─ 応答あり(SendTaskSuccess) → continue:終了 / stop:終了
                  └─ 無応答(5分でタイムアウト) → 既定1回再送 → なお無応答なら自動停止

LINE Webhook:
  API Gateway → responder Lambda
    署名検証(HMAC-SHA256)
      → DynamoDBのタスクトークンで SendTaskSuccess(ステートマシン即再開)
      → 「停止」なら即 ec2:StopInstances
```

## 構成リソース

| リソース | 用途 | 固定費 |
|---|---|---|
| Step Functions | 通知〜判定のフロー | なし(状態遷移の従量) |
| EventBridge Scheduler | 60分ごとの起動（既定・変更可） | なし |
| Lambda × 4 | check_running / notifier / stopper / responder | なし |
| DynamoDB (オンデマンド) | タスクトークンの受け渡し | **なし** |
| API Gateway (REST) | LINE Webhook 受け口 | なし |
| SSM Parameter Store (SecureString) | LINE トークン保管 | **なし(無料)** |

## 前提

- AWS アカウント / 認証情報（`ap-northeast-1` を想定）
- [pnpm](https://pnpm.io/)（`npm` / `npx` は使いません）
- AWS CDK の bootstrap 済み（未実施なら `pnpm exec cdk bootstrap`）
- LINE 公式アカウント + Messaging API チャネル

## セットアップ手順

### 1. LINE 側の準備（手作業）

1. [LINE Developers コンソール](https://developers.line.biz/) でプロバイダー作成
2. **Messaging API チャネル**を作成
3. **Channel access token (long-lived)** を発行・控える
4. **Channel secret** を控える
5. （userId は後で取得します）

### 2. リポジトリ取得 & 依存インストール

```bash
git clone https://github.com/<your-account>/aws-ec2-line-stop-reminder.git
cd aws-ec2-line-stop-reminder/cdk
pnpm install
```

### 3. デプロイ

```bash
# 初回のみ
pnpm exec cdk bootstrap

pnpm exec cdk deploy
```

デプロイ後、Outputs の `WebhookUrl` を控えます。

> デモ用に間隔を短縮したい場合:
> `pnpm exec cdk deploy -c interval_minutes=5 -c wait_minutes=1 -c max_retry=2`
> アカウントID部分の上書き: `-c suffix=20260521`

### 4. LINE トークンを SSM に登録

```bash
cd ../scripts
./put-line-params.sh "<CHANNEL_ACCESS_TOKEN>" "<CHANNEL_SECRET>"
# userId は後で登録するので、この時点では省略でOK
```

### 5. Webhook URL を LINE に登録

1. LINE Developers コンソール → Messaging API 設定 → **Webhook URL** に Outputs の `WebhookUrl` を設定
2. **Webhook の利用** を ON
3. 「検証(Verify)」で 200 が返ることを確認

### 6. userId を取得して登録

1. 作成した公式アカウントを**友だち追加**し、何かメッセージを送る
2. `responder` Lambda の CloudWatch Logs に `userId=U....` が出る
3. その userId を登録:

```bash
aws ssm put-parameter --region ap-northeast-1 --type SecureString --overwrite \
  --name /ec2-line-stop-reminder/user-id --value "<YOUR_USER_ID>"
```

### 7. 対象 EC2 にタグ付け

監視したいインスタンスに **`AutoStopNotify=true`** タグを付ける。

```bash
aws ec2 create-tags --region ap-northeast-1 \
  --resources <INSTANCE_ID> --tags Key=AutoStopNotify,Value=true
```

## 動作確認

- 待たずに試す場合は、Step Functions ステートマシンを**手動で Start execution**（入力は `{}`）
- LINE に「継続 / 停止」の Quick Reply 付きメッセージが届く（本文にインスタンス ID と Name タグを表示）
  - **継続** → そのセッションは終了（停止しない）
  - **停止** → 即 `ec2:StopInstances` が実行される
  - **無応答** → `wait_minutes`（既定 5 分）後に再送 → `max_retry`（既定 1）回で自動停止

## 削除（必須）

```bash
cd scripts
./teardown.sh
```

`cdk destroy` + SSM Parameter 削除 + 残留確認まで行います。

## コストについて

- 本仕組み自体は**固定費ゼロ**を狙う設計です。
- 従量増要素:
  - Step Functions の状態遷移（月数十円程度）
  - LINE Messaging API のフリープラン超過（日本は**月 200 通**まで無料）。
    カウントされるのは Push のみ（宛先 1 人あたり 1 通）。タップへの確認返信(reply)は即時応答のためカウントされません。
    60 分ごと + 無応答時の再送が積み重なると無料枠を超える可能性があります。
- 通知には「**今月の無料枠 残り 約 N 通**」を併記します（`GET /v2/bot/message/quota/consumption` で取得、無料枠は `FREE_QUOTA` 環境変数で既定 200）。
- **監視対象 EC2 自体の課金は別**です。本仕組みは停止忘れを減らしますが、課金停止の最終責任は利用者にあります。

## ライセンス

MIT
