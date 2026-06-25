# Use the account's default VPC: its public subnets give Batch instances a public IP + IGW egress
# (ECR pull, S3, logs) for free, so no NAT gateway, no S3 endpoint, no custom subnets/routes.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Egress-only SG for the Batch instances (they make outbound reads/writes; nothing connects in).
resource "aws_security_group" "batch" {
  name_prefix = "${var.name}-batch-"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle { create_before_destroy = true }
  tags = { Name = "${var.name}-batch" }
}
