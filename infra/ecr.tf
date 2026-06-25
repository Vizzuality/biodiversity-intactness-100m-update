# Single repo for the merged image. push_images.sh builds + pushes here; both job definitions
# reference it (see locals). Untagged images expire so old layers don't accumulate.
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
  # Job-definition image: a pinned digest from var, else the repo at :latest.
  image = coalesce(var.image, "${aws_ecr_repository.this.repository_url}:latest")
}
