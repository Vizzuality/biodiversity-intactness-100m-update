# outputs holds final COGs/manifests (versioned, kept); processing holds regenerable staged inputs.
resource "aws_s3_bucket" "outputs" {
  bucket = var.bucket
}

resource "aws_s3_bucket" "processing" {
  bucket = var.processing_bucket
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = {
    outputs    = { id = aws_s3_bucket.outputs.id, block_policy = false }
    processing = { id = aws_s3_bucket.processing.id, block_policy = true }
  }
  bucket                  = each.value.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = each.value.block_policy
  restrict_public_buckets = each.value.block_policy
}

# Public read limited to published prefixes only.
resource "aws_s3_bucket_policy" "outputs_public" {
  bucket     = aws_s3_bucket.outputs.id
  depends_on = [aws_s3_bucket_public_access_block.this]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadPublishedPrefixes"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = ["${aws_s3_bucket.outputs.arn}/out/*", "${aws_s3_bucket.outputs.arn}/source/*"]
    }]
  })
}

# Allow browsers (e.g. the COG map viewer) to range-read published objects.
resource "aws_s3_bucket_cors_configuration" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["Content-Range", "Content-Length", "Accept-Ranges"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_bucket_versioning" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  rule {
    id     = "cleanup"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}


resource "aws_s3_bucket_lifecycle_configuration" "processing" {
  bucket = aws_s3_bucket.processing.id
  rule {
    id     = "expire"
    status = "Disabled"
    filter {}
    expiration { days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}
