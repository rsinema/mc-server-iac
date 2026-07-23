resource "aws_cloudwatch_metric_alarm" "idle_stop" {
  alarm_name          = "${var.server_name}-idle-stop"
  comparison_operator = "LessThanThreshold"
  # BACKSTOP ONLY. The primary idle-stop decision now lives on the instance
  # (modules/compute check_players.sh): the box watches the live player count
  # and stops itself after ${var.idle_stop_minutes} min of confirmed emptiness.
  # This alarm only catches the case where the agent is alive and publishing
  # empty readings but fails to actually stop the box (e.g. its self-stop path
  # is broken) -- a stuck box that would otherwise bill forever. The window is
  # deliberately long (backstop_minutes) so it sits far outside any normal
  # play/idle cycle; the fast, precise decision is the on-box agent's job.
  evaluation_periods = ceil(var.backstop_minutes / 5)
  metric_name        = "PlayerCount"
  namespace          = "Minecraft"
  period             = 300
  statistic          = "Maximum"
  threshold          = 1
  # "notBreaching" is critical here: MISSING data must NOT push the alarm toward
  # ALARM. The metric is absent whenever the box is booting (RCON not up yet),
  # stopped, or the agent is dead -- and treating any of those as breaching
  # re-creates the original "stops seconds after boot" bug, because CloudWatch
  # back-fills the whole evaluation range as breaching the instant the alarm is
  # created/modified with no data. With notBreaching the alarm can only reach
  # ALARM after ${var.backstop_minutes} min of *real* zero-player datapoints
  # (a running, publishing, but non-self-stopping agent), and a freshly started
  # box always gets a clean window. At a 60-min / 5-min-bucket / Maximum scale
  # the per-minute publisher jitter that once made notBreaching flap is
  # irrelevant, and the stop Lambda is idempotent (no-ops when not running).
  treat_missing_data = "notBreaching"
  alarm_description  = "BACKSTOP: fires only after ~${var.backstop_minutes} min of real zero-player datapoints (on-box agent alive but not self-stopping). Primary idle-stop is on-instance; missing data never trips this."

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
