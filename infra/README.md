# infra — AWS Batch stack (OpenTofu)

Greenfield stack for running BII staging + processing on AWS Batch (EC2 Spot)

State is local (`terraform.tfstate` in this dir, gitignored).

## Setup

```sh
cd infra
tofu init
tofu apply                      # creates VPC, bucket, ECR repo, Batch (job defs -> repo:latest)

cd ..
./scripts/deploy.sh             # build + push the bii image, then pin its digest into the job defs
```

Then wire the outputs into `.env`:

```sh
tofu output    # batch_job_queue -> BII_BATCH_QUEUE, batch_job_def -> BII_BATCH_JOB_DEF,
               # batch_stage_job_def -> BII_BATCH_STAGE_JOB_DEF
```

## Redeploy

`./scripts/deploy.sh` — builds + pushes the current HEAD image and pins its digest into the job defs.

## Running

`tofu output local_role_arn` is a least-privilege role (read/write the two buckets + submit/monitor
Batch jobs) that you assume from your own AWS identity.

Add a profile to `~/.aws/config` that assumes it from your normal credentials:

```ini
[profile bii-local]
role_arn       = <local_role_arn>
source_profile = default        # your existing creds/SSO profile
region         = us-west-2
```

Then run the pipeline with `AWS_PROFILE=bii-local` — boto3 picks up the assumed role automatically.
