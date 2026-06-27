# 1. EC2 Instance role
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
# 2. Container job 
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

# 3. Local role for delegated user access
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
