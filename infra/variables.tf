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
# scratch (see launch_template.tf). r8gd is current gen; r7gd/r6gd widen the Spot pool for
# CAPACITY_OPTIMIZED. arm64 only — the image is arm64, so don't add x86 (r6id/r5d) here.
variable "instance_types" {
  description = "Instance families Batch may launch (Graviton R-family, NVMe-backed)."
  type        = list(string)
  default     = ["r8gd", "r7gd", "r6gd"]
}

variable "job_vcpu" {
  description = "vCPUs per job (one chunk / staging unit)."
  type        = number
  default     = 2
}

# ~4x the arrays at the larger raster size. Starting point — tune against a real chunk. Held ~1 GiB
# under 8 GiB/vCPU so a job fits the smallest instance (.large) after kernel + ECS-agent reserve, and
# 2/4/8 jobs pack the .xlarge/.2xlarge/.4xlarge at full vCPU.
variable "job_memory" {
  description = "MiB of memory per job."
  type        = number
  default     = 15360
}

# Image defaults to the Tofu-created ECR repo at :latest. Pin to a digest
# (repo@sha256:...) here once scripts/push_images.sh has pushed, so Batch runs the exact image
# you tested locally. null -> repo:latest (see locals in ecr.tf).
variable "local_principal_arn" {
  description = "IAM principal allowed to assume the local role (null = account root)."
  type        = string
  default     = null
}

variable "image" {
  description = "Merged image for staging + processing (null = <bii repo>:latest)."
  type        = string
  default     = null
}
