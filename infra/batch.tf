resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.name}"
  retention_in_days = 30
}

resource "aws_batch_compute_environment" "this" {
  compute_environment_name = var.name
  type                     = "MANAGED"

  compute_resources {
    type                = "SPOT"
    allocation_strategy = "SPOT_PRICE_CAPACITY_OPTIMIZED"
    max_vcpus           = var.max_vcpus
    min_vcpus           = 0
    instance_type       = var.instance_types
    instance_role       = aws_iam_instance_profile.instance.arn
    subnets             = data.aws_subnets.default.ids
    security_group_ids  = [aws_security_group.batch.id]

    launch_template {
      launch_template_id = aws_launch_template.batch.id
      version            = aws_launch_template.batch.latest_version
    }
  }
}

resource "aws_batch_job_queue" "this" {
  name     = var.name
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.this.arn
  }
}

resource "aws_batch_job_definition" "process" {
  name                  = "${var.name}-process"
  type                  = "container"
  platform_capabilities = ["EC2"]
  container_properties = jsonencode({
    image      = local.image
    command    = ["bii-process"]
    jobRoleArn = aws_iam_role.job.arn
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.job_vcpu) },
      { type = "MEMORY", value = tostring(var.job_memory) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.batch.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "${var.name}-process"
      }
    }
  })
}

resource "aws_batch_job_definition" "stage" {
  name                  = "${var.name}-stage"
  type                  = "container"
  platform_capabilities = ["EC2"]
  container_properties = jsonencode({
    image      = local.image
    command    = ["bii-stage"]
    jobRoleArn = aws_iam_role.job.arn
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.job_vcpu) },
      { type = "MEMORY", value = tostring(var.job_memory) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.batch.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "${var.name}-stage"
      }
    }
  })
}
