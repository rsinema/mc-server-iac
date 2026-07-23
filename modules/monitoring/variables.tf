variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "instance_id" {
  description = "EC2 instance ID to monitor"
  type        = string
}

variable "idle_stop_minutes" {
  description = "Minutes of idle time (no players) before auto-stop. The on-box agent (modules/compute) enforces this; kept here for the alarm description."
  type        = number
  default     = 15
}

variable "backstop_minutes" {
  description = "Minutes a running instance may publish no players before the CloudWatch backstop alarm stops it. Deliberately long — this only catches a dead/wedged on-box agent, not normal idle (which the on-box agent handles at idle_stop_minutes)."
  type        = number
  default     = 60
}

variable "stop_lambda_function_arn" {
  description = "ARN of the Lambda function to invoke for stopping"
  type        = string
}

variable "stop_lambda_function_name" {
  description = "Name of the Lambda function to invoke for stopping"
  type        = string
}
