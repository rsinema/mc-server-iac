resource "aws_cloudwatch_metric_alarm" "idle_stop" {
  alarm_name          = "${var.server_name}-idle-stop"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = var.idle_stop_minutes
  metric_name         = "PlayerCount"
  namespace           = "Minecraft"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  # For an idle-stop, a MISSING datapoint should count toward stopping, not
  # reset the streak. With "notBreaching", a single missing minute per 15-min
  # window (normal publish jitter) kept the alarm from ever reaching 15/15, so
  # an idle server ran indefinitely. "breaching" makes idle/flaky-metric periods
  # accumulate to ALARM; a server with real players still publishes >=1 every
  # minute, which breaks the streak, so active sessions are not stopped. On
  # /mc start the controller resets this alarm to OK, so a fresh instance gets a
  # full idle window before it can stop.
  treat_missing_data = "breaching"
  alarm_description  = "Triggers when PlayerCount is <1 (or unreported) for ${var.idle_stop_minutes} consecutive minutes"

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
