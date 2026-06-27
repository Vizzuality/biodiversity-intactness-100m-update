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

# Memory-optimized (1:8 vCPU:mem) with NVMe scratch (launch_template.tf). 
# image built for arm64
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

# Hold ~1 GiB under 8 GiB/vCPU so a job fits with overhead
variable "job_memory" {
  description = "MiB of memory per job."
  type        = number
  default     = 15360
}

# Pin var.image to a digest (repo@sha256:...) so Batch runs the exact image tested locally.
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
