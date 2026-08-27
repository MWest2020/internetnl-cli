#!/usr/bin/env bash
# deploy/vps/create-vps.sh — provisions a Hetzner Cloud VPS for the
# Internet.nl batch instance (topology 1). See
# docs/how-to/deploy-instance-vps.md for the full runbook and
# openspec/changes/add-measurement-api/design.md, "VPS provisioning
# (Hetzner) — for topology 1", for the pinned decisions this script
# implements.
#
# This script CREATES A BILLABLE Hetzner Cloud resource. It never runs
# unattended: pass --yes, or confirm the interactive prompt, before it
# calls `hcloud server create`.
#
# It renders deploy/vps/cloud-init.yaml into a throwaway temp file with
# the Tailscale auth key, the operator's SSH public key and VPS_NAME
# substituted in (via awk, doing a literal — not regex, not
# sed-replacement-pattern — search/replace, so none of those values can
# contain characters that corrupt the substitution), uses that as the
# server's user-data, and deletes the temp file again — the rendered
# cloud-init (and therefore the auth key) is never written anywhere else,
# never printed and never committed by this script. It DOES, however,
# persist at rest on the created host itself (cloud-init caches the
# user-data it was given) — see "Tailscale auth key at-rest on the host"
# in docs/how-to/deploy-instance-vps.md, and use a single-use, short-TTL
# key as that section describes.
#
# Required environment (never echoed by this script):
#   HCLOUD_TOKEN    Hetzner Cloud API token (project-scoped).
#   TS_AUTHKEY      Tailscale pre-auth key. Use an EPHEMERAL, TAGGED,
#                   SINGLE-USE (reusable=false), SHORT-TTL key
#                   (Tailscale admin console -> Settings -> Keys) so it
#                   cannot be reused to register another device, is
#                   scoped by ACL tag rather than a long-lived user
#                   identity, and is worthless soon after this run even
#                   though a plaintext copy stays on the host at rest —
#                   see docs/how-to/deploy-instance-vps.md.
#   SSH_KEY_NAME    Name of an SSH key already registered in this
#                   Hetzner project, OR a path to a local public key
#                   file. Either way its content becomes the `deploy`
#                   user's ssh_authorized_keys; the same value is also
#                   passed to `hcloud server create --ssh-key`, which
#                   accepts an ID, a name, or a public-key file path.
#
# Optional environment:
#   VPS_TYPE        Hetzner server type (default: cx22)
#   VPS_IMAGE       Hetzner image (default: ubuntu-22.04)
#   VPS_LOCATION    Hetzner location (default: nbg1)
#   VPS_NAME        Server name / OS hostname / tailnet hostname
#                   (default: netnl-instance)
#
# Usage:
#   HCLOUD_TOKEN=... TS_AUTHKEY=... SSH_KEY_NAME=my-key \
#     deploy/vps/create-vps.sh --yes
set -eu

script_dir="$(cd "$(dirname "$0")" && pwd)"
cloud_init_template="$script_dir/cloud-init.yaml"

vps_type="${VPS_TYPE:-cx22}"
vps_image="${VPS_IMAGE:-ubuntu-22.04}"
vps_location="${VPS_LOCATION:-nbg1}"
vps_name="${VPS_NAME:-netnl-instance}"

assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --yes | -y)
            assume_yes=1
            ;;
        *)
            echo "create-vps.sh: unknown argument: $arg" >&2
            echo "usage: $0 [--yes]" >&2
            exit 2
            ;;
    esac
done

_require_var() {
    # $1 = variable name, $2 = human-readable hint. Never prints the
    # variable's value — only names it, so a token/key never appears in
    # this script's own output.
    local name="$1" hint="$2"
    if [ -z "${!name:-}" ]; then
        echo "create-vps.sh: required environment variable $name is not set: $hint" >&2
        exit 1
    fi
}

_require_var HCLOUD_TOKEN "Hetzner Cloud API token"
_require_var TS_AUTHKEY "Tailscale ephemeral, tagged pre-auth key"
_require_var SSH_KEY_NAME "name of an existing hcloud SSH key, or a path to a local public key file"

if ! command -v hcloud >/dev/null 2>&1; then
    echo "create-vps.sh: hcloud CLI not found on PATH — install it: https://github.com/hetznercloud/cli" >&2
    exit 1
fi

if [ ! -f "$cloud_init_template" ]; then
    echo "create-vps.sh: cloud-init template not found: $cloud_init_template" >&2
    exit 1
fi

# --- Resolve the operator's SSH public key content ---------------------------

if [ -f "$SSH_KEY_NAME" ]; then
    ssh_public_key="$(cat "$SSH_KEY_NAME")"
elif command -v jq >/dev/null 2>&1; then
    ssh_public_key="$(hcloud ssh-key describe "$SSH_KEY_NAME" -o json | jq -r '.public_key')"
elif command -v python3 >/dev/null 2>&1; then
    ssh_public_key="$(hcloud ssh-key describe "$SSH_KEY_NAME" -o json | python3 -c 'import json, sys; print(json.load(sys.stdin)["public_key"].strip())')"
else
    echo "create-vps.sh: SSH_KEY_NAME=$SSH_KEY_NAME is not a local file, and neither jq nor python3 is on PATH to read its content from hcloud — install one of them, or set SSH_KEY_NAME to a local public key file instead" >&2
    exit 1
fi

if [ -z "$ssh_public_key" ]; then
    echo "create-vps.sh: could not resolve a public key for SSH_KEY_NAME=$SSH_KEY_NAME" >&2
    exit 1
fi

# --- Confirm before creating a billable resource ------------------------------

if [ "$assume_yes" -ne 1 ]; then
    printf 'About to create a Hetzner Cloud server:\n'
    printf '  name:     %s\n' "$vps_name"
    printf '  type:     %s\n' "$vps_type"
    printf '  image:    %s\n' "$vps_image"
    printf '  location: %s\n' "$vps_location"
    printf 'This creates a BILLABLE resource in your Hetzner project (~EUR 3-5/month\n'
    printf 'for a cx22 — see docs/how-to/deploy-instance-vps.md).\n'
    printf 'Proceed? [y/N] '
    read -r confirm
    case "$confirm" in
        y | Y | yes | YES) ;;
        *)
            echo "create-vps.sh: aborted, nothing created." >&2
            exit 1
            ;;
    esac
fi

# --- Render the cloud-init user-data into a throwaway temp file --------------
# Never committed, never printed, removed unconditionally on exit — even
# if `hcloud server create` below fails partway through.

rendered_cloud_init="$(mktemp)"
trap 'rm -f "$rendered_cloud_init"' EXIT INT TERM

# Substitution is done with awk, not sed: the replacement values here
# (the Tailscale auth key, the operator's SSH public key, the VPS name)
# are untrusted-ish, attacker-adjacent input as far as text-substitution
# metacharacters go — an SSH key comment or a key value could contain
# `&`, `\`, or the delimiter of whatever sed would use, any of which
# corrupts a sed replacement (worst case: a mangled
# ssh_authorized_keys line -> lockout). literal_replace() below does a
# plain index()/substr() scan-and-splice on both the search marker and
# the replacement text, so neither is ever interpreted as a regex or as
# a replacement pattern (no `&` = whole match, no backslash escapes) —
# it is a byte-for-byte literal substitution regardless of what the
# values contain. Values are passed through the environment
# (ENVIRON[]), never interpolated into the awk program text itself, so
# they can't break out of the program either.
TS_AUTHKEY_VAL="$TS_AUTHKEY" \
    OPERATOR_SSH_PUBLIC_KEY_VAL="$ssh_public_key" \
    VPS_NAME_VAL="$vps_name" \
    awk '
        function literal_replace(str, search, repl,    result, idx, searchlen) {
            searchlen = length(search)
            if (searchlen == 0) {
                return str
            }
            result = ""
            while ((idx = index(str, search)) > 0) {
                result = result substr(str, 1, idx - 1) repl
                str = substr(str, idx + searchlen)
            }
            return result str
        }
        {
            line = $0
            line = literal_replace(line, "${TS_AUTHKEY}", ENVIRON["TS_AUTHKEY_VAL"])
            line = literal_replace(line, "${OPERATOR_SSH_PUBLIC_KEY}", ENVIRON["OPERATOR_SSH_PUBLIC_KEY_VAL"])
            line = literal_replace(line, "${VPS_NAME}", ENVIRON["VPS_NAME_VAL"])
            print line
        }
    ' "$cloud_init_template" >"$rendered_cloud_init"

# --- Create the server ---------------------------------------------------------
# IPv4 and IPv6 are both provisioned by default (no --without-ipv4 /
# --without-ipv6 flags below). The host firewall baked into cloud-init
# (ufw, default-deny inbound — see cloud-init.yaml) is the primary
# control here; this script does not also create a separate Hetzner
# Cloud Firewall resource. Add one yourself with `hcloud firewall` if
# your project's policy wants defence in depth at the network edge too.

echo "create-vps.sh: creating server '$vps_name' ..." >&2
hcloud server create \
    --name "$vps_name" \
    --type "$vps_type" \
    --image "$vps_image" \
    --location "$vps_location" \
    --ssh-key "$SSH_KEY_NAME" \
    --user-data-from-file "$rendered_cloud_init"

rm -f "$rendered_cloud_init"
trap - EXIT INT TERM

# --- Report --------------------------------------------------------------------

public_ipv4="$(hcloud server describe "$vps_name" -o format='{{.PublicNet.IPv4.IP}}')"
public_ipv6="$(hcloud server describe "$vps_name" -o format='{{.PublicNet.IPv6.IP}}')"

printf '\nServer created.\n'
printf '  public IPv4:      %s\n' "$public_ipv4"
printf '  public IPv6 net:  %s\n' "$public_ipv6"
printf '  tailnet hostname: %s (once tailscale up finishes on first boot; see the\n' "$vps_name"
printf '                    tailnet admin console or "tailscale status")\n'
printf '\nNext: wait for cloud-init to finish on the host, then follow\n'
printf 'docs/how-to/deploy-instance-vps.md, "Provisioning on Hetzner", for\n'
printf 'the Internet.nl-specific steps (upstream .env, DNS delegation, batch\n'
printf 'user) and wiring up the facade.\n'
