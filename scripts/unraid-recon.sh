#!/usr/bin/env bash
#
# Read-only reconnaissance for an Unraid host.
#
# Tetherd's Unraid support is written against assumptions about label names,
# template XML structure and daemon behaviour. This script gathers the evidence
# needed to confirm or correct them on a real server.
#
# It changes NOTHING: only `docker inspect`, `docker ps` and file reads. No
# container is created, started, stopped or removed, and no file is written.
#
# Values that could be sensitive are redacted. Environment variable values in
# templates routinely contain API keys and passwords, so their contents are
# never printed - only their names and types. Check the output before sharing it
# anyway.
#
# Usage:  bash unraid-recon.sh
#
set -uo pipefail

TEMPLATE_DIR="${TEMPLATE_DIR:-/boot/config/plugins/dockerMan/templates-user}"
MAX_ITEMS="${MAX_ITEMS:-6}"

section() { printf '\n========== %s ==========\n' "$*"; }
item()    { printf '  %s\n' "$*"; }

# Truncate anything that might be long or identifying.
redact_url() {
    local value="$1"
    [[ -z "$value" ]] && { echo "(empty)"; return; }
    echo "${value:0:60}$([[ ${#value} -gt 60 ]] && echo '...')"
}

section "Host and daemon"
item "unraid version: $(grep -h '^version' /etc/unraid-version 2>/dev/null || echo 'not found')"
item "kernel:         $(uname -r)"
item "docker client:  $(docker --version 2>/dev/null || echo 'not found')"
item "docker server:  $(docker version --format '{{.Server.Version}} (API {{.Server.APIVersion}}, min {{.Server.MinAPIVersion}})' 2>/dev/null || echo 'not found')"

section "Expected paths"
for path in \
    /usr/local/emhttp \
    /usr/local/emhttp/webGui/scripts/notify \
    /tmp/notifications \
    /tmp/notifications/unread \
    "$TEMPLATE_DIR" \
    /var/lib/docker/unraid-update-status.json \
    /boot/config/plugins/dynamix.docker.manager/update-status.json
do
    if [[ -e "$path" ]]; then
        item "PRESENT  $path"
    else
        item "absent   $path"
    fi
done

section "Containers borrowing another container's network"
# The key question: does this daemon store the reference as a 64-character ID,
# or as the name the user typed? Tetherd handles both, but which one is normal
# here decides how much the name path matters.
found_dependent=""
while read -r name mode; do
    [[ -z "$name" ]] && continue
    ref="${mode#container:}"
    if [[ "$ref" =~ ^[0-9a-f]{64}$ ]]; then
        form="full 64-char ID (daemon normalised it)"
    elif [[ "$ref" =~ ^[0-9a-f]{12,}$ ]]; then
        form="abbreviated ID (${#ref} chars)"
    else
        form="NAME, not an ID -> '$ref'"
    fi
    item "$name -> $form"
    [[ -z "$found_dependent" ]] && found_dependent="$name"
done < <(docker ps -a --format '{{.Names}}' 2>/dev/null | while read -r c; do
    m=$(docker inspect "$c" --format '{{.HostConfig.NetworkMode}}' 2>/dev/null)
    [[ "$m" == container:* ]] && echo "$c $m"
done)
[[ -z "$found_dependent" ]] && item "none found - is anything routed through a VPN container right now?"

section "Network metadata on a dependent"
# Verified on Docker 29 as: SandboxKey empty, Networks {}, EndpointID key absent.
# If that differs on this daemon, Tetherd's detection needs to know.
if [[ -n "$found_dependent" ]]; then
    item "container:    $found_dependent"
    item "SandboxKey:   '$(docker inspect "$found_dependent" --format '{{.NetworkSettings.SandboxKey}}' 2>/dev/null)'"
    item "Networks:     $(docker inspect "$found_dependent" --format '{{json .NetworkSettings.Networks}}' 2>/dev/null)"
    if docker inspect "$found_dependent" --format '{{json .NetworkSettings}}' 2>/dev/null | grep -q '"EndpointID"'; then
        item "EndpointID:   key IS present (differs from Docker 29)"
    else
        item "EndpointID:   key absent, as expected"
    fi
    item "StartedAt:    $(docker inspect "$found_dependent" --format '{{.State.StartedAt}}' 2>/dev/null)"
else
    item "skipped - no dependent container to inspect"
fi

section "Unraid integration labels"
# Tetherd preserves these across a rebuild. Confirming which ones real
# containers actually carry tells us what 'preserved' needs to mean, and whether
# net.unraid.docker.template exists on this version.
count=0
while read -r c; do
    [[ -z "$c" ]] && continue
    managed=$(docker inspect "$c" --format '{{index .Config.Labels "net.unraid.docker.managed"}}' 2>/dev/null)
    [[ -z "$managed" || "$managed" == "<no value>" ]] && continue
    count=$((count + 1))
    [[ "$count" -gt "$MAX_ITEMS" ]] && break
    item "$c"
    item "    managed:  $managed"
    for label in icon webui shell template; do
        value=$(docker inspect "$c" --format "{{index .Config.Labels \"net.unraid.docker.${label}\"}}" 2>/dev/null)
        if [[ -z "$value" || "$value" == "<no value>" ]]; then
            item "    ${label}:$(printf '%*s' $((9 - ${#label})) '')(not set)"
        else
            item "    ${label}:$(printf '%*s' $((9 - ${#label})) '')$(redact_url "$value")"
        fi
    done
done < <(docker ps -a --format '{{.Names}}' 2>/dev/null)
[[ "$count" -eq 0 ]] && item "no containers carry net.unraid.docker.managed"
item ""
item "all distinct net.unraid.* label keys in use on this host:"
docker ps -aq 2>/dev/null | while read -r id; do
    docker inspect "$id" --format '{{json .Config.Labels}}' 2>/dev/null
done | grep -o '"net\.unraid\.[a-z.]*"' | sort -u | sed 's/^/    /'

section "Template XML structure"
# Tetherd only ever AUDITS these files, never rebuilds from them. But the audit
# parses them, and upstream issue #59 was caused by Unraid changing this format,
# so the element and attribute names need confirming. Values are redacted.
if [[ -d "$TEMPLATE_DIR" ]]; then
    total=$(find "$TEMPLATE_DIR" -maxdepth 1 -name '*.xml' 2>/dev/null | wc -l)
    item "template files: $total in $TEMPLATE_DIR"
    item ""
    item "declared <Name> values (this is how Tetherd matches, not by filename):"
    find "$TEMPLATE_DIR" -maxdepth 1 -name '*.xml' 2>/dev/null | sort | head -n "$MAX_ITEMS" | while read -r f; do
        declared=$(grep -o '<Name>[^<]*</Name>' "$f" 2>/dev/null | head -1 | sed -e 's/<Name>//' -e 's|</Name>||')
        item "    $(basename "$f")  ->  <Name>${declared}</Name>"
    done

    item ""
    item "root element and top-level element names across all templates:"
    cat "$TEMPLATE_DIR"/*.xml 2>/dev/null \
        | grep -o '<[A-Za-z][A-Za-z0-9_]*' | sed 's/<//' | sort | uniq -c | sort -rn | head -30 | sed 's/^/    /'

    item ""
    item "Config element attribute patterns (types and modes only, no values):"
    cat "$TEMPLATE_DIR"/*.xml 2>/dev/null \
        | grep -o '<Config[^>]*' \
        | grep -o 'Type="[^"]*"' | sort | uniq -c | sort -rn | sed 's/^/    /'

    item ""
    item "how networking is expressed (the #57 misconfiguration lives here):"
    find "$TEMPLATE_DIR" -maxdepth 1 -name '*.xml' 2>/dev/null | sort | while read -r f; do
        net=$(grep -o '<Network>[^<]*</Network>' "$f" 2>/dev/null | head -1 | sed -e 's/<Network>//' -e 's|</Network>||')
        extra=$(grep -o '<ExtraParams>[^<]*</ExtraParams>' "$f" 2>/dev/null | head -1 | sed -e 's/<ExtraParams>//' -e 's|</ExtraParams>||')
        # Only report templates that route through another container.
        if [[ "$extra" == *"container:"* || "$net" == *"container:"* ]]; then
            ports=$(grep -c 'Type="Port"' "$f" 2>/dev/null)
            item "    $(basename "$f"): Network='${net}' ExtraParams='${extra}' portMappings=${ports}"
        fi
    done
    item "    (any line above with portMappings greater than 0 is a template Unraid"
    item "     itself cannot recreate - see issues #80, #69, #65)"
else
    item "template directory not found at $TEMPLATE_DIR"
fi

section "Done"
item "This output contains no environment variable values, but please skim it"
item "before sharing."
