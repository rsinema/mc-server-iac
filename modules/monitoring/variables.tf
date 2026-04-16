variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "instance_id" {
  description = "EC2 instance ID to monitor"
  type        = string
}

variable "idle_stop_minutes" {
  description = "Minutes of idle time (no players) before auto-stop"
  type        = number
  default     = 15
}

variable "stop_lambda_function_arn" {
  description = "ARN of the Lambda function to invoke for stopping"
  type        = string
}

variable "stop_lambda_function_name" {
  description = "Name of the Lambda function to invoke for stopping"
  type        = string
}
