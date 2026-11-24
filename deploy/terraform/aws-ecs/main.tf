locals {
  port  = 8080
  https = var.certificate_arn != ""
}

data "aws_caller_identity" "current" {}

# --- Image registry -----------------------------------------------------------

resource "aws_ecr_repository" "gateway" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "gateway" {
  repository = aws_ecr_repository.gateway.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# --- Secrets and logs ---------------------------------------------------------

# The value is set out of band (console / CLI), never in Terraform state.
resource "aws_secretsmanager_secret" "anthropic_key" {
  name        = "${var.name}/anthropic-api-key"
  description = "ANTHROPIC_API_KEY for the gateway"
}

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

# --- IAM ----------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# Execution role: what ECS needs to start the task (pull image, write logs, read the secret).
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.anthropic_key.arn]
  }
}

resource "aws_iam_role_policy" "read_secret" {
  name   = "read-anthropic-key"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secret.json
}

# Task role: what the running app may call. The gateway needs no AWS APIs, so it is empty.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# --- Networking ---------------------------------------------------------------

resource "aws_security_group" "lb" {
  name        = "${var.name}-lb"
  description = "Gateway load balancer"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "lb_in" {
  for_each          = toset(var.allowed_cidrs)
  security_group_id = aws_security_group.lb.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = local.https ? 443 : 80
  to_port           = local.https ? 443 : 80
}

resource "aws_vpc_security_group_egress_rule" "lb_to_tasks" {
  security_group_id            = aws_security_group.lb.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = local.port
  to_port                      = local.port
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "Gateway tasks: only the LB may connect"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_lb" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.lb.id
  ip_protocol                  = "tcp"
  from_port                    = local.port
  to_port                      = local.port
}

# Outbound HTTPS only: ECR, Secrets Manager, CloudWatch and the model provider.
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_lb" "gateway" {
  name                       = var.name
  internal                   = var.internal_lb
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.lb.id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true
  # LLM calls are slow; the ALB default (60 s) would cut long completions.
  idle_timeout = max(120, var.timeout_s * (var.retries + 1) * 2)
}

resource "aws_lb_target_group" "gateway" {
  name                 = var.name
  port                 = local.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.gateway.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

resource "aws_lb_listener" "http" {
  count             = local.https ? 0 : 1
  load_balancer_arn = aws_lb.gateway.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

# --- ECS ----------------------------------------------------------------------

resource "aws_ecs_cluster" "gateway" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  volume {
    name = "tmp"
  }
  container_definitions = jsonencode([{
    name                   = "gateway"
    image                  = "${aws_ecr_repository.gateway.repository_url}:${var.image_tag}"
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    portMappings           = [{ containerPort = local.port, protocol = "tcp" }]
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp" }]
    environment = [
      { name = "GATEWAY_PROVIDERS", value = var.providers_order },
      { name = "GATEWAY_TIMEOUT_S", value = tostring(var.timeout_s) },
      { name = "GATEWAY_RETRIES", value = tostring(var.retries) },
      { name = "GATEWAY_TENANTS", value = var.tenants },
    ]
    secrets = [{ name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.anthropic_key.arn }]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${local.port}/healthz', timeout=2)"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 10
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.gateway.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "gateway"
      }
    }
  }])
}

resource "aws_ecs_service" "gateway" {
  name                               = var.name
  cluster                            = aws_ecs_cluster.gateway.id
  task_definition                    = aws_ecs_task_definition.gateway.arn
  desired_count                      = var.min_tasks
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"

  # A bad image rolls back by itself instead of taking the gateway down.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = "gateway"
    container_port   = local.port
  }

  # Autoscaling owns the count after the first apply.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.https, aws_lb_listener.http]
}

# --- Autoscaling --------------------------------------------------------------

resource "aws_appautoscaling_target" "gateway" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.gateway.name}/${aws_ecs_service.gateway.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_tasks
  max_capacity       = var.max_tasks
}

# The gateway is I/O bound (waiting on the model), so CPU is a poor signal;
# scale on in-flight requests per task instead, with CPU as a backstop.
resource "aws_appautoscaling_policy" "requests" {
  name               = "${var.name}-requests"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.gateway.service_namespace
  resource_id        = aws_appautoscaling_target.gateway.resource_id
  scalable_dimension = aws_appautoscaling_target.gateway.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value       = 200
    scale_in_cooldown  = 120
    scale_out_cooldown = 30
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.gateway.arn_suffix}/${aws_lb_target_group.gateway.arn_suffix}"
    }
  }
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.gateway.service_namespace
  resource_id        = aws_appautoscaling_target.gateway.resource_id
  scalable_dimension = aws_appautoscaling_target.gateway.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value = 70
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# --- Alarms -------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name          = "${var.name}-5xx"
  alarm_description   = "Gateway is returning 5xx: every provider in the fallback chain is failing."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.gateway.arn_suffix, TargetGroup = aws_lb_target_group.gateway.arn_suffix }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "p99_latency" {
  alarm_name          = "${var.name}-p99-latency"
  alarm_description   = "p99 above the per-attempt timeout: requests are paying for timeouts (breaker not opening?)."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = { LoadBalancer = aws_lb.gateway.arn_suffix, TargetGroup = aws_lb_target_group.gateway.arn_suffix }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  threshold           = var.timeout_s
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy" {
  alarm_name          = "${var.name}-unhealthy-hosts"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.gateway.arn_suffix, TargetGroup = aws_lb_target_group.gateway.arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}
