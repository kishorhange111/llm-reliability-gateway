variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "llm-gateway"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

# Network is an input, not created here: in a customer account the VPC usually
# already exists and is owned by another team.
variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  description = "Subnets for the load balancer."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Subnets for the tasks (need NAT or VPC endpoints to reach ECR and the model API)."
  type        = list(string)
}

variable "allowed_cidrs" {
  description = "Who may call the gateway. Keep this to the customer's internal ranges."
  type        = list(string)
  default     = ["10.0.0.0/8"]
}

variable "internal_lb" {
  description = "Internal ALB (the default; the gateway is a backend service)."
  type        = bool
  default     = true
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS. Empty = plain HTTP listener (dev only)."
  type        = string
  default     = ""
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "min_tasks" {
  type    = number
  default = 2
}

variable "max_tasks" {
  type    = number
  default = 10
}

variable "providers_order" {
  description = "GATEWAY_PROVIDERS: comma list in priority order."
  type        = string
  default     = "anthropic,mock"
}

variable "timeout_s" {
  type    = number
  default = 30
}

variable "retries" {
  type    = number
  default = 1
}

variable "tenants" {
  description = "GATEWAY_TENANTS: name:rate:burst:budget_usd;..."
  type        = string
  default     = "default:10:20:50"
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alarm_topic_arn" {
  description = "SNS topic for alarms. Empty = alarms are created without actions."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
