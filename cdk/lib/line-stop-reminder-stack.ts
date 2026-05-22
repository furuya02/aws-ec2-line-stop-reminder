import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';

// SSM Parameter のパス(SecureString は put-line-params.sh で投入する)
// 注意: SSM パラメータ名は "aws" / "ssm" で始められない(予約プレフィックス)ため /ec2-... とする
const PARAM_PREFIX = '/ec2-line-stop-reminder';

export class LineStopReminderStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // -c suffix=... でアカウントID部分を上書き可能。未指定ならアカウントIDを使う
    const accountSuffix: string = this.node.tryGetContext('suffix') ?? this.account;
    const prefix = `aws-ec2-line-stop-reminder-${accountSuffix}`;

    // 起動スケジュール: 既定は毎時0分。-c schedule="cron(...) or rate(...)" で上書き可
    // 注意: 間隔は1通知セッション長(wait_minutes×(max_retry+1)≒既定10分)より長くすること
    //       (短いと同一インスタンスで実行が重複し taskToken を上書きして破綻する)
    const scheduleExpression: string = this.node.tryGetContext('schedule') ?? 'cron(0 * * * ? *)';
    const waitMinutes: number = Number(this.node.tryGetContext('wait_minutes') ?? 5);
    const maxRetry: number = Number(this.node.tryGetContext('max_retry') ?? 1);

    // --- DynamoDB(オンデマンド = 固定費ゼロ) ---
    const stateTable = new dynamodb.Table(this, 'StateTable', {
      tableName: prefix,
      partitionKey: { name: 'instanceId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // SSM SecureString 読み取り + KMS 復号(aws/ssm 経由のみ)を許可するステートメント
    const ssmReadStatement = new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${PARAM_PREFIX}/*`],
    });
    const kmsDecryptStatement = new iam.PolicyStatement({
      actions: ['kms:Decrypt'],
      resources: ['*'],
      conditions: { StringEquals: { 'kms:ViaService': `ssm.${this.region}.amazonaws.com` } },
    });
    // タグ AutoStopNotify=true のインスタンスだけ停止を許可
    const stopInstancesStatement = new iam.PolicyStatement({
      actions: ['ec2:StopInstances'],
      resources: ['*'],
      conditions: { StringEquals: { 'aws:ResourceTag/AutoStopNotify': 'true' } },
    });

    const pythonRuntime = lambda.Runtime.PYTHON_3_12;
    const makeFunction = (id: string, dir: string, environment: Record<string, string>): lambda.Function =>
      new lambda.Function(this, id, {
        runtime: pythonRuntime,
        handler: 'index.handler',
        code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', dir)),
        timeout: cdk.Duration.seconds(30),
        environment,
        functionName: `${prefix}-${dir.replace(/_/g, '-')}`,
      });

    const commonParamEnv = {
      TABLE_NAME: stateTable.tableName,
      TOKEN_PARAM: `${PARAM_PREFIX}/channel-access-token`,
    };

    // --- Lambda 群 ---
    const checkRunningFn = makeFunction('CheckRunning', 'check_running', {});
    checkRunningFn.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['ec2:DescribeInstances'], resources: ['*'] })
    );

    const notifierFn = makeFunction('Notifier', 'notifier', {
      ...commonParamEnv,
      USER_PARAM: `${PARAM_PREFIX}/user-id`,
      FREE_QUOTA: '200', // フリープラン無料枠(日本=月200通)。プラン変更時はここを調整
    });
    stateTable.grantWriteData(notifierFn); // タスクトークンを保存
    notifierFn.addToRolePolicy(ssmReadStatement);
    notifierFn.addToRolePolicy(kmsDecryptStatement);

    const stopperFn = makeFunction('Stopper', 'stopper', {});
    stopperFn.addToRolePolicy(stopInstancesStatement);

    const responderFn = makeFunction('Responder', 'responder', {
      ...commonParamEnv,
      SECRET_PARAM: `${PARAM_PREFIX}/channel-secret`,
    });
    stateTable.grantReadData(responderFn); // タスクトークンを読む
    responderFn.addToRolePolicy(ssmReadStatement);
    responderFn.addToRolePolicy(kmsDecryptStatement);
    responderFn.addToRolePolicy(stopInstancesStatement);
    // 待機中のステートマシンを応答で即再開する(トークンを持つ者のみ可能なため * で許容)
    responderFn.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['states:SendTaskSuccess'], resources: ['*'] })
    );

    // --- Step Functions ステートマシン(Task Token で応答を待つ) ---
    // SendNotification: notifier を waitForTaskToken で起動。応答が来れば即再開、
    // 来なければ waitMinutes でタイムアウト(States.Timeout)し再送/自動停止へ。
    const sendNotification = new tasks.LambdaInvoke(this, 'SendNotification', {
      lambdaFunction: notifierFn,
      integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(waitMinutes)),
      payload: sfn.TaskInput.fromObject({
        instanceId: sfn.JsonPath.stringAt('$.instanceId'),
        name: sfn.JsonPath.stringAt('$.name'),
        sessionId: sfn.JsonPath.stringAt('$.sessionId'),
        retryCount: sfn.JsonPath.numberAt('$.retryCount'),
        taskToken: sfn.JsonPath.taskToken,
      }),
    });

    const continued = new sfn.Pass(this, 'Continued'); // 継続(何もしない)
    const stoppedByUser = new sfn.Pass(this, 'StoppedByUser'); // responder が既に停止済み
    const autoStopped = new sfn.Pass(this, 'AutoStopped');

    const autoStop = new tasks.LambdaInvoke(this, 'AutoStop', {
      lambdaFunction: stopperFn,
      payload: sfn.TaskInput.fromObject({ instanceId: sfn.JsonPath.stringAt('$.instanceId') }),
      outputPath: '$.Payload',
    }).next(autoStopped);

    const incrementRetry = new sfn.Pass(this, 'IncrementRetry', {
      parameters: {
        instanceId: sfn.JsonPath.stringAt('$.instanceId'),
        name: sfn.JsonPath.stringAt('$.name'),
        sessionId: sfn.JsonPath.stringAt('$.sessionId'),
        retryCount: sfn.JsonPath.numberAt('States.MathAdd($.retryCount, 1)'),
      },
    });

    // 応答あり: stop なら StoppedByUser、それ以外(continue)は Continued で終了
    const evaluateResponse = new sfn.Choice(this, 'EvaluateResponse')
      .when(sfn.Condition.stringEquals('$.status', 'stop'), stoppedByUser)
      .otherwise(continued);

    // 無応答(タイムアウト): 再送回数が残っていれば再送、尽きたら自動停止
    const checkRetry = new sfn.Choice(this, 'CheckRetry')
      .when(sfn.Condition.numberLessThan('$.retryCount', maxRetry), incrementRetry)
      .otherwise(autoStop);

    sendNotification.addCatch(checkRetry, { errors: ['States.Timeout'], resultPath: '$.error' });
    sendNotification.next(evaluateResponse);
    incrementRetry.next(sendNotification);

    const checkRunning = new tasks.LambdaInvoke(this, 'CheckRunningTask', {
      lambdaFunction: checkRunningFn,
      payload: sfn.TaskInput.fromObject({}),
      outputPath: '$.Payload',
    });

    const processInstances = new sfn.Map(this, 'ProcessInstances', {
      itemsPath: sfn.JsonPath.stringAt('$.instances'),
      itemSelector: {
        instanceId: sfn.JsonPath.stringAt('$$.Map.Item.Value.instanceId'),
        name: sfn.JsonPath.stringAt('$$.Map.Item.Value.name'),
        sessionId: sfn.JsonPath.stringAt('$$.Execution.Name'),
        retryCount: 0,
      },
      maxConcurrency: 5,
    });
    processInstances.itemProcessor(sendNotification);

    const definition = checkRunning.next(
      new sfn.Choice(this, 'HasRunningInstances')
        .when(sfn.Condition.numberGreaterThan('$.count', 0), processInstances)
        .otherwise(new sfn.Pass(this, 'NoRunningInstances'))
    );

    const stateMachine = new sfn.StateMachine(this, 'StateMachine', {
      stateMachineName: prefix,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.minutes(60),
    });

    // --- EventBridge Scheduler(interval 分ごとに起動) ---
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      roleName: `${prefix}-scheduler-role`,
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });
    stateMachine.grantStartExecution(schedulerRole);

    new scheduler.CfnSchedule(this, 'Schedule', {
      name: `${prefix}-schedule`,
      flexibleTimeWindow: { mode: 'OFF' },
      scheduleExpression,
      scheduleExpressionTimezone: 'Asia/Tokyo', // 時刻指定 cron 用(毎時0分には影響なし)
      target: {
        arn: stateMachine.stateMachineArn,
        roleArn: schedulerRole.roleArn,
      },
    });

    // --- API Gateway(LINE Webhook 受け口) ---
    const webhookApi = new apigw.RestApi(this, 'WebhookApi', {
      restApiName: `${prefix}-webhook`,
      deployOptions: { stageName: 'prod' },
    });
    const webhookResource = webhookApi.root.addResource('webhook');
    webhookResource.addMethod('POST', new apigw.LambdaIntegration(responderFn));

    // --- Outputs ---
    new cdk.CfnOutput(this, 'WebhookUrl', { value: `${webhookApi.url}webhook` });
    new cdk.CfnOutput(this, 'StateMachineArn', { value: stateMachine.stateMachineArn });
    new cdk.CfnOutput(this, 'StateTableName', { value: stateTable.tableName });
    new cdk.CfnOutput(this, 'ParameterPrefix', { value: PARAM_PREFIX });
  }
}
