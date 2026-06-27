resource "aws_ecr_repository" "this" {
  name                 = var.name # "bii"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy     = local.ecr_expire_untagged
}

locals {
  ecr_expire_untagged = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "expire untagged after 14 days"
      selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 }
      action       = { type = "expire" }
    }]
  })
  image = coalesce(var.image, "${aws_ecr_repository.this.repository_url}:latest")
}
