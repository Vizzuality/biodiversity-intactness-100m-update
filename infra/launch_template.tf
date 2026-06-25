# Launch template that puts container scratch on the instance-store NVMe instead of the default
# 30 GB gp3 root. The staging/processing temp churn (source download + COG-driver intermediate +
# the staged temp before upload — up to ~3x output size, per job, and jobs bin-pack onto one
# instance) is the IO bottleneck at the larger raster size; NVMe local disk removes EBS throughput
# and capacity as a variable. Pointing Docker's data-root at it moves every container's /tmp there
# with no job-def or code change. MIME multipart so Batch can append its own ECS bootstrap.
locals {
  scratch_user_data = <<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==BII=="

    --==BII==
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    set -euo pipefail
    mapfile -t D < <(lsblk -dno NAME,MODEL | awk '/Instance Storage/{print "/dev/"$1}')
    [ $${#D[@]} -eq 0 ] && exit 0
    if [ $${#D[@]} -gt 1 ]; then
      mdadm --create /dev/md0 --level=0 --raid-devices=$${#D[@]} "$${D[@]}"
      DISK=/dev/md0
    else
      DISK=$${D[0]}
    fi
    mkfs.xfs -f "$DISK"
    mkdir -p /scratch
    mount "$DISK" /scratch
    mkdir -p /scratch/docker
    systemctl stop docker
    echo '{"data-root":"/scratch/docker"}' > /etc/docker/daemon.json
    systemctl start docker
    --==BII==--
  EOT
}

resource "aws_launch_template" "batch" {
  name_prefix = "${var.name}-"
  user_data   = base64encode(local.scratch_user_data)
  tags        = { Name = var.name }
}
