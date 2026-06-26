# Three roles. The compute environment uses Batch's service-linked role automatically (not set
# here), and SPOT_CAPACITY_OPTIMIZED uses the Spot service-linked role, so no Spot fleet role.

# 1. EC2 instance role: the ECS agent on each Spot instance — pulls images from ECR, ships logs.
resource "aws_iam_role" "instance" {
  name = "${var.name}-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "instance_ecs" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name}-instance"
  role = aws_iam_role.instance.name
}

# 2. Job role: the container's own credentials — read/write the pipeline bucket.
resource "aws_iam_role" "job" {
  name = "${var.name}-job"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "job_s3" {
  name = "s3"
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${aws_s3_bucket.outputs.arn}/*", "${aws_s3_bucket.processing.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.outputs.arn, aws_s3_bucket.processing.arn]
      },
    ]
  })
}

# Local role: assume from your own AWS identity to run the pipeline locally — same S3 access as the
# job role, plus submitting + monitoring Batch jobs. Trusted by the account root by default (so any
# IAM principal you grant sts:AssumeRole can use it), or pin a single principal via local_principal_arn.
resource "aws_iam_role" "local" {
  name = "${var.name}-local"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = coalesce(var.local_principal_arn, "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root") }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "local_s3" {
  name   = "s3"
  role   = aws_iam_role.local.id
  policy = aws_iam_role_policy.job_s3.policy
}

resource "aws_iam_role_policy" "local_batch" {
  name = "batch"
  role = aws_iam_role.local.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "batch:SubmitJob"
        Resource = [
          aws_batch_job_queue.this.arn,
          "arn:aws:batch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:job-definition/${var.name}-*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["batch:DescribeJobs", "batch:ListJobs"]
        Resource = "*"
      },
    ]
  })
}
