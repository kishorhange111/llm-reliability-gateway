output "endpoint" {
  description = "Base URL of the gateway."
  value       = "${local.https ? "https" : "http"}://${aws_lb.gateway.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.gateway.repository_url
}

output "api_key_secret_arn" {
  description = "Put the provider key here: aws secretsmanager put-secret-value --secret-id <arn> --secret-string ..."
  value       = aws_secretsmanager_secret.anthropic_key.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.gateway.name
}
