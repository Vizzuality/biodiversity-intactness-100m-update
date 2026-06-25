# Managed EC2 Spot compute environment + queue + two job definitions. SPOT_CAPACITY_OPTIMIZED uses
# the Spot service-linked role (no fleet role); the service-linked Batch role is implicit.
resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.name}"
  retention_in_days = 30
}

resource "aws_batch_compute_environment" "this" {
  compute_environment_name = var.name
  type                     = "MANAGED"

  compute_resources {
    type                = "SPOT"
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"
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

# Raster image: processing (default command bii-process) + raster staging (command overridden to
# bii-stage-worker at submit by bii.stage). Roads image: OSM staging only.
resource "aws_batch_job_definition" "raster" {
  name                  = var.name
  type                  = "container"
  platform_capabilities = ["EC2"]
  container_properties = jsonencode({
    image      = local.raster_image
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
        "awslogs-stream-prefix" = var.name
      }
    }
  })
}

resource "aws_batch_job_definition" "roads" {
  name                  = "${var.name}-roads"
  type                  = "container"
  platform_capabilities = ["EC2"]
  container_properties = jsonencode({
    image      = local.roads_image
    command    = ["bii-stage-worker"]
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
        "awslogs-stream-prefix" = "${var.name}-roads"
      }
    }
  })
}
