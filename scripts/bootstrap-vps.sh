#!/usr/bin/env bash
# One-time host bootstrap for the IPOIntel production VPS (Ubuntu, amd64 or
# arm64). Run as a sudo-capable non-root user over SSH. Idempotent.
#
#   curl -fsSL https://raw.githubusercontent.com/satyamamarpandey/ipointel/master/scripts/bootstrap-vps.sh | bash
#   # or, once cloned:  ./scripts/bootstrap-vps.sh
#
# Prepares exactly what docker-compose.production.yml and scripts/deploy.sh
# expect, and nothing else:
#   - security updates, Docker Engine + Compose plugin, git, curl, ca-certificates
#   - UTC timezone with time synchronisation verified
#   - host firewall open for 22/80/443 only - never 5432
#   - /opt/ipointel owned by the deploy user, repository cloned at master
#
# Deliberately does NOT touch sshd_config. Disabling password auth from inside
# a script cannot verify that key login works from a *second* session first,
# and getting that wrong locks you out of a box you may have no console for.
# The script reports the current SSH state and prints the command instead.
set -euo pipefail

# id -un rather than $USER: piping this script into bash gives a non-login
# shell where $USER can be unset, and `set -u` would then abort mid-run.
RUN_USER="$(id -un)"
TARGET="${TARGET:-/opt/ipointel}"
REPO="${REPO:-https://github.com/satyamamarpandey/ipointel.git}"
BRANCH="${BRANCH:-master}"

log()  { echo; echo "== $*"; }
warn() { echo "!! $*" >&2; }

[ "$(id -u)" -ne 0 ] || { warn "run as a normal sudo user, not root (the deploy user owns $TARGET)"; exit 1; }

log "host"
. /etc/os-release
echo "$PRETTY_NAME  |  $(uname -m)  |  $(nproc) vCPU  |  $(free -h | awk '/^Mem:/{print $2}') RAM"

log "apt base + security updates"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q
sudo apt-get install -y -q ca-certificates curl gnupg git unattended-upgrades
# Applies outstanding security updates now; unattended-upgrades keeps it that
# way afterwards without pulling in a full unattended dist-upgrade.
sudo unattended-upgrade -d >/dev/null 2>&1 || true

log "timezone + time sync"
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true 2>/dev/null || true
timedatectl show -p Timezone -p NTPSynchronized --value | paste -sd' ' -
if [ "$(timedatectl show -p NTPSynchronized --value)" != "yes" ]; then
    warn "clock is not yet synchronised - TLS and ACME are time-sensitive; re-check with: timedatectl"
fi

log "Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
    sudo install -m 0755 -d /etc/apt/keyrings
    # --yes: a half-finished earlier run can leave the keyring behind, and
    # gpg refusing to overwrite it would abort the whole script.
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --yes --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -q
    sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# enable = the stack comes back by itself after a reboot, which the compose
# restart policies depend on.
sudo systemctl enable --now docker
sudo usermod -aG docker "$RUN_USER"
docker --version
docker compose version

log "firewall: 22/tcp, 80/tcp, 443/tcp+udp only"
# Postgres is never opened. It is reachable only on the internal compose
# network, and docker-compose.production.yml publishes no port for it.
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 22/tcp   >/dev/null
    sudo ufw allow 80/tcp   >/dev/null
    sudo ufw allow 443/tcp  >/dev/null
    sudo ufw allow 443/udp  >/dev/null
    sudo ufw --force enable >/dev/null
    sudo ufw status | head -8
fi
# Oracle Cloud's Ubuntu images ship a persisted iptables INPUT chain ending in
# a REJECT, which silently drops 80/443 even when the VCN security list allows
# them - the symptom is an ACME challenge that times out for no visible
# reason. Insert ACCEPT rules above it. Harmless on hosts without that chain.
if command -v iptables >/dev/null 2>&1 && sudo iptables -S INPUT 2>/dev/null | grep -q "REJECT"; then
    for rule in "-p tcp --dport 80 -j ACCEPT" "-p tcp --dport 443 -j ACCEPT" "-p udp --dport 443 -j ACCEPT"; do
        # shellcheck disable=SC2086
        sudo iptables -C INPUT $rule 2>/dev/null || sudo iptables -I INPUT 5 $rule
    done
    # `cmd && action` as a statement would abort the script under `set -e` on
    # a host that has no netfilter-persistent, so this stays an if.
    if command -v netfilter-persistent >/dev/null 2>&1; then
        sudo netfilter-persistent save >/dev/null
    fi
    echo "inserted ACCEPT rules above the image's REJECT rule (Oracle-style host)"
fi

log "deploy directory $TARGET"
sudo mkdir -p "$TARGET"
sudo chown "$RUN_USER:$RUN_USER" "$TARGET"
chmod 750 "$TARGET"
if [ -d "$TARGET/.git" ]; then
    git -C "$TARGET" fetch --quiet origin
    git -C "$TARGET" checkout --quiet "$BRANCH"
    git -C "$TARGET" pull --quiet --ff-only origin "$BRANCH"
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$TARGET"
fi
echo "$TARGET @ $(git -C "$TARGET" rev-parse --short HEAD) ($BRANCH)"

log "SSH state (not modified - see the note at the top of this script)"
PASSWD_AUTH=$(sudo sshd -T 2>/dev/null | awk '/^passwordauthentication /{print $2}')
ROOT_LOGIN=$(sudo sshd -T 2>/dev/null | awk '/^permitrootlogin /{print $2}')
echo "PasswordAuthentication=$PASSWD_AUTH   PermitRootLogin=$ROOT_LOGIN"
if [ "$PASSWD_AUTH" = "yes" ]; then
    cat <<'SSHNOTE'
   To disable password auth AFTER confirming key login works in a SECOND
   session you keep open (so a mistake cannot lock you out):
     sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
     sudo systemctl reload ssh
SSHNOTE
fi

cat <<DONE

== bootstrap complete

Docker group membership needs a new login to take effect:
    exit && ssh back in    (or: newgrp docker)

Next:
  1. create $TARGET/.env   (see docs/LAUNCH.md; chmod 600)
  2. confirm api.ipointel.brandsap.com resolves to this host's public IP
  3. cd $TARGET && ./scripts/deploy.sh
DONE
