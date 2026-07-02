# Puts container scratch / docker root on instance-store NVMe. Runs as a cloud-boothook
# (cloud-init's early stage, before docker.service) so Docker starts once already on the
# NVMe root -- no stop/start, which would cancel ecs.service's pending boot start job and
# leave the instance unregistered. MIME multipart so Batch can append its own ECS bootstrap.
locals {
  scratch_user_data = <<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==BII=="

    --==BII==
    Content-Type: text/cloud-boothook; charset="us-ascii"

    #!/bin/bash
    set -euo pipefail
    [ -e /etc/docker/daemon.json ] && grep -q /scratch/docker /etc/docker/daemon.json && exit 0
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
    mkdir -p /scratch/docker /etc/docker
    echo '{"data-root":"/scratch/docker"}' > /etc/docker/daemon.json
    --==BII==--
  EOT
}

resource "aws_launch_template" "batch" {
  name_prefix = "${var.name}-"
  user_data   = base64encode(local.scratch_user_data)
  tags        = { Name = var.name }
}
