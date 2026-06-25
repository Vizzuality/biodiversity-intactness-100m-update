# These map straight onto the .env vars the pipeline reads (BII_BATCH_* / BII_STAGE_*).
output "batch_job_queue" {
  description = "BII_BATCH_QUEUE"
  value       = aws_batch_job_queue.this.name
}

output "batch_job_def" {
  description = "BII_BATCH_JOB_DEF (processing + raster staging)"
  value       = aws_batch_job_definition.raster.name
}

output "batch_roads_job_def" {
  description = "BII_BATCH_ROADS_JOB_DEF"
  value       = aws_batch_job_definition.roads.name
}

output "ecr_raster_repo" {
  description = "Push target + BII_STAGE_IMAGE base (raster/processing)"
  value       = aws_ecr_repository.raster.repository_url
}

output "ecr_roads_repo" {
  description = "Push target + BII_STAGE_ROADS_IMAGE base (roads)"
  value       = aws_ecr_repository.roads.repository_url
}

output "bucket" {
  description = "Final outputs (BUCKET)"
  value       = aws_s3_bucket.outputs.bucket
}

output "processing_bucket" {
  description = "Staged inputs/index (PROCESSING_BUCKET)"
  value       = aws_s3_bucket.processing.bucket
}
