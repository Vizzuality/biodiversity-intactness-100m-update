# Two buckets: `outputs` (vizz-bii) holds final COGs + manifests, versioned and kept forever;
# `processing` (vizz-bii-processing) holds regenerable staged inputs/index, objects auto-deleted.
resource "aws_s3_bucket" "outputs" {
  bucket = var.bucket
}

resource "aws_s3_bucket" "processing" {
  bucket = var.processing_bucket
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = { outputs = aws_s3_bucket.outputs.id, processing = aws_s3_bucket.processing.id }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "outputs" {
  bucket = aws_s3_bucket.outputs.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Outputs: keep current versions forever; expire old versions and stalled uploads so versioning
# doesn't accrue cost.
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

# Processing: regenerable, so delete objects outright after 30 days.
resource "aws_s3_bucket_lifecycle_configuration" "processing" {
  bucket = aws_s3_bucket.processing.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration { days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}
