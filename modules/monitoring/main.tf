resource "aws_cloudwatch_metric_alarm" "idle_stop" {
  alarm_name          = "${var.server_name}-idle-stop"
  comparison_operator = "LessThanThreshold"
  # Evaluate in 5-minute buckets rather than 1-minute ones. The player-count
  # publisher occasionally skips a minute (timer jitter), and with 1-minute
  # periods that lone gap kept a strict 15/15 alarm from ever firing. A 5-minute
  # bucket still contains several datapoints, so per-minute jitter no longer
  # matters; idle_stop_minutes is converted to bucket count.
  evaluation_periods = ceil(var.idle_stop_minutes / 5)
  metric_name        = "PlayerCount"
  namespace          = "Minecraft"
  period             = 300
  statistic          = "Maximum"
  threshold          = 1
  # notBreaching is essential: a STOPPED server publishes nothing (missing),
  # while an idle RUNNING server publishes 0. Only the running-and-0 case must
  # trigger a stop. "breaching" here conflated the two and re-fired the alarm
  # ~1 minute after every /mc start (the trailing window was still full of the
  # stopped period's missing datapoints), killing the server right after boot.
  # With notBreaching, missing periods are ignored, so a freshly started server
  # gets a full idle window before it can stop.
  treat_missing_data = "notBreaching"
  alarm_description  = "Triggers when a running server reports 0 players for ~${var.idle_stop_minutes} min"

  dimensions = {
    InstanceId = var.instance_id
  }

  # EventBridge catches alarm state changes via event_pattern — no alarm_actions needed
  ok_actions    = []
  alarm_actions = []
}

resource "aws_cloudwatch_event_rule" "idle_stop" {
  name        = "${var.server_name}-idle-stop"
  description = "Fires on idle alarm and triggers stop Lambda"

  event_pattern = jsonencode({
    "source" : ["aws.cloudwatch"],
    "detail-type" : ["CloudWatch Alarm State Change"],
    "detail" : {
      "alarmName" : [aws_cloudwatch_metric_alarm.idle_stop.alarm_name],
      "state" : {
        "value" : ["ALARM"]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "stop_lambda" {
  rule      = aws_cloudwatch_event_rule.idle_stop.name
  target_id = "StopInstance"
  arn       = var.stop_lambda_function_arn
  input     = jsonencode({ "action" : "stop" })
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.stop_lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.idle_stop.arn
}
