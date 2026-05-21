#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { LineStopReminderStack } from '../lib/line-stop-reminder-stack';

const app = new cdk.App();
new LineStopReminderStack(app, 'LineStopReminderStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'ap-northeast-1',
  },
});
