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

# Service account for running test_chunk --remote locally (e.g. in Docker).
resource "aws_iam_user" "local" {
  name = "${var.bucket}-local-s3"
}

resource "aws_iam_user_policy" "local_s3" {
  name = "s3"
  user = aws_iam_user.local.name
  policy = aws_iam_role_policy.job_s3.policy
}
