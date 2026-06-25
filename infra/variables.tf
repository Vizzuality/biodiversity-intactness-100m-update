variable "name" {
  description = "Prefix for all named resources (VPC, Batch queue/defs, log group)."
  type        = string
  default     = "bii"
}

variable "bucket" {
  description = "S3 bucket for final outputs + manifests, kept forever (matches bii.config.BUCKET)."
  type        = string
  default     = "vizz-bii"
}

variable "processing_bucket" {
  description = "S3 bucket for regenerable staged inputs/index; objects auto-deleted (bii.config.PROCESSING_BUCKET)."
  type        = string
  default     = "vizz-bii-processing"
}

variable "max_vcpus" {
  description = "Ceiling on concurrent vCPUs in the Spot compute environment."
  type        = number
  default     = 256
}

# Memory-optimized (1:8 vCPU:mem, matches job_vcpu:job_memory) with local NVMe instance store for
# scratch (see launch_template.tf). r6id is current gen; r5d/r5dn widen the Spot pool for
# CAPACITY_OPTIMIZED. x86 only — the images are x86, so don't add Graviton (r7gd/r6gd) here.
variable "instance_types" {
  description = "Instance families Batch may launch (R-family current gen, NVMe-backed)."
  type        = list(string)
  default     = ["r6id", "r5d", "r5dn"]
}

variable "job_vcpu" {
  description = "vCPUs per job (one chunk / staging unit)."
  type        = number
  default     = 2
}

# ~4x the arrays at the larger raster size. Starting point — tune against a real chunk. Kept at a
# 1:8 ratio with job_vcpu so R-family instances pack with no idle vCPU or memory.
variable "job_memory" {
  description = "MiB of memory per job."
  type        = number
  default     = 16384
}

# Image defaults to the Tofu-created ECR repo at :latest. Pin to a digest
# (repo@sha256:...) here once scripts/push_images.sh has pushed, so Batch runs the exact image
# you tested locally. null -> repo:latest (see locals in ecr.tf).
variable "image" {
  description = "Merged image for staging + processing (null = <bii repo>:latest)."
  type        = string
  default     = null
}
