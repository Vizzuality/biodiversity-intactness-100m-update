# infra — AWS Batch stack (OpenTofu)

Greenfield stack for running BII staging + processing on AWS Batch (EC2 Spot): the account's
default VPC, two S3 buckets (`vizz-bii` outputs + `vizz-bii-processing` staged inputs), one ECR
repo, IAM, one compute environment + queue, and two job definitions (`bii-process`, `bii-stage`,
both on the one merged image). Region comes from `AWS_REGION` (the same `.env` the pipeline uses).

State is **local** (`terraform.tfstate` in this dir, gitignored). For a shared/remote setup, add an
S3 backend block and `tofu init -migrate-state` — deferred until more than one operator needs it.

## Bring-up (chicken-and-egg: repos exist before images)

```sh
cd infra
tofu init
tofu apply                      # creates VPC, bucket, ECR repo, Batch (job defs -> repo:latest)

cd ..
./scripts/push_images.sh        # build + push the bii image; prints the digest

cd infra
tofu apply \                    # re-point job defs at the exact pushed digest
  -var "image=<bii digest>"
```

Then wire the outputs into `.env`:

```sh
tofu output    # batch_job_queue -> BII_BATCH_QUEUE, batch_job_def -> BII_BATCH_JOB_DEF,
               # batch_stage_job_def -> BII_BATCH_STAGE_JOB_DEF
```

`tofu apply` with no `-var` leaves the job defs on `:latest` — fine for a first run, but pinning the
digest is what guarantees local `docker` and Batch run the same image.

## Notes

- **Service-linked roles.** Batch uses `AWSServiceRoleForBatch` and Spot uses
  `AWSServiceRoleForEC2Spot` — account-global, not created here. They exist in any account that has
  used Batch/Spot before; on a brand-new account create them once:
  `aws iam create-service-linked-role --aws-service-name batch.amazonaws.com` (and
  `spot.amazonaws.com`). Tofu can't own them cleanly (it errors if they already exist).
- **Networking.** Batch runs in the default VPC's public subnets, so instances get a public IP and
  egress (ECR/S3/logs) via the IGW — no NAT gateway, no S3 endpoint, nothing that costs money idle.
  The egress-only SG means nothing can connect in.
- **Cost.** Nothing accrues cost while idle: the Spot compute environment scales to 0 vCPUs, and the
  `vizz-bii-processing` bucket auto-deletes staged objects (30d) while `vizz-bii` keeps current
  output versions forever (old versions expire at 30d).
- **Scratch.** `launch_template.tf` formats the instance-store NVMe and points Docker's `data-root`
  at it, so container `/tmp` (source downloads + COG-driver intermediates + the staged temp before
  upload) lands on local NVMe, not the 30 GB gp3 root. Instance types are therefore NVMe-backed
  R-family (x86); a job is 2 vCPU / 16 GiB (1:8), which packs onto them with no idle vCPU or memory.
- Not yet run through `tofu validate` (no binary in the authoring env) — run `tofu fmt` + `tofu
  validate` before the first apply.
