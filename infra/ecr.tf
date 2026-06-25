# One repo per image. push_images.sh builds + pushes here; the job definitions reference these
# repos (see locals). Untagged images expire so old layers don't accumulate.
resource "aws_ecr_repository" "raster" {
  name                 = var.name # "bii"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository" "roads" {
  name                 = "${var.name}-roads"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "raster" {
  repository = aws_ecr_repository.raster.name
  policy     = local.ecr_expire_untagged
}

resource "aws_ecr_lifecycle_policy" "roads" {
  repository = aws_ecr_repository.roads.name
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
  # Job-definition images: a pinned digest from var, else the repo at :latest.
  raster_image = coalesce(var.raster_image, "${aws_ecr_repository.raster.repository_url}:latest")
  roads_image  = coalesce(var.roads_image, "${aws_ecr_repository.roads.repository_url}:latest")
}
