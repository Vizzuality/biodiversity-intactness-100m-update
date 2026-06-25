# These map straight onto the .env vars the pipeline reads (BII_BATCH_* / BII_STAGE_*).
output "batch_job_queue" {
  description = "BII_BATCH_QUEUE"
  value       = aws_batch_job_queue.this.name
}

output "batch_job_def" {
  description = "BII_BATCH_JOB_DEF (processing)"
  value       = aws_batch_job_definition.process.name
}

output "batch_stage_job_def" {
  description = "BII_BATCH_STAGE_JOB_DEF (staging)"
  value       = aws_batch_job_definition.stage.name
}

output "ecr_repo" {
  description = "Push target + BII_STAGE_IMAGE base"
  value       = aws_ecr_repository.this.repository_url
}

output "bucket" {
  description = "Final outputs (BUCKET)"
  value       = aws_s3_bucket.outputs.bucket
}

output "processing_bucket" {
  description = "Staged inputs/index (PROCESSING_BUCKET)"
  value       = aws_s3_bucket.processing.bucket
}
