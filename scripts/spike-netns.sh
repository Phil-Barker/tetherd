#!/usr/bin/env bash
#
# Phase 0 validation spike.
#
# The Tetherd remediation design rests on a set of claims about how Docker
# handles `--network container:<ref>`. This script proves or disproves each of
# them against a real daemon. Run it before trusting the design, and re-run it
# when raising or lowering the minimum supported Docker version, because two of
# the findings below are daemon-version-sensitive.
#
# Findings are written up in docs/design-notes.md.
#
set -euo pipefail

readonly IMAGE="${SPIKE_IMAGE:-alpine:3.22}"
readonly PROVIDER="tetherd-spike-provider"
readonly DEPENDENT="tetherd-spike-dependent"
readonly API_DEPENDENT="tetherd-spike-api-dependent"
readonly PROBE_TARGET="${SPIKE_PROBE_TARGET:-1.1.1.1}"
readonly DOCKER_SOCK="${SPIKE_DOCKER_SOCK:-/var/run/docker.sock}"

pass_count=0
fail_count=0

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }

# Records the outcome of a claim without aborting, so a single failure still
# yields a full picture of daemon behaviour.
check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        printf '   \033[32mPASS\033[0m %s (%s)\n' "$label" "$actual"
        pass_count=$((pass_count + 1))
    else
        printf '   \033[31mFAIL\033[0m %s (expected %s, got %s)\n' "$label" "$expected" "$actual"
        fail_count=$((fail_count + 1))
    fi
}

cleanup() {
    docker rm -f "$DEPENDENT" "$API_DEPENDENT" "$PROVIDER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

inspect() { docker inspect "$1" --format "$2"; }

# Returns "ok" or "broken". Busybox ping exits non-zero when the namespace has
# no route, which is the observable symptom of a stale namespace.
probe() {
    if docker exec "$1" ping -c 1 -W 3 "$PROBE_TARGET" >/dev/null 2>&1; then
        echo "ok"
    else
        echo "broken"
    fi
}

start_provider() {
    docker run -d --name "$PROVIDER" "$IMAGE" \
        sh -c 'while true; do sleep 30; done' >/dev/null
}

start_dependent() {
    docker run -d --name "$DEPENDENT" --network "container:${PROVIDER}" "$IMAGE" \
        sh -c 'while true; do sleep 30; done' >/dev/null
}

log "Setup: provider + dependent sharing its network namespace"
cleanup
start_provider
start_dependent
provider_id_original="$(inspect "$PROVIDER" '{{.Id}}')"
info "provider   $provider_id_original"
info "dependent  $(inspect "$DEPENDENT" '{{.Id}}')"
check "baseline connectivity through provider" "ok" "$(probe "$DEPENDENT")"

# Claim: the daemon resolves container:<name> to container:<full-id> at create
# time. Discovery can therefore compare IDs rather than reconciling name forms.
check "NetworkMode was normalised from a name to the provider's full ID" \
    "container:${provider_id_original}" \
    "$(inspect "$DEPENDENT" '{{.HostConfig.NetworkMode}}')"

# Claim: a dependent owns no network metadata whatsoever. This is why upstream
# Rebuild-DNDC's EndpointID-based detection could never work reliably: for
# exactly the containers it manages, the field is structurally absent.
check "dependent has an empty SandboxKey" \
    "" "$(inspect "$DEPENDENT" '{{.NetworkSettings.SandboxKey}}')"
check "dependent has no Networks entries" \
    "{}" "$(inspect "$DEPENDENT" '{{json .NetworkSettings.Networks}}')"
# Docker's template engine has no hasKey, so probe for the key in raw JSON. The
# key is absent rather than empty, which makes `docker inspect
# --format '{{.NetworkSettings.EndpointID}}'` fail outright on a dependent.
if inspect "$DEPENDENT" '{{json .NetworkSettings}}' | grep -q '"EndpointID"'; then
    endpointid_key="present"
else
    endpointid_key="no-such-key"
fi
check "dependent has no EndpointID key at all" "no-such-key" "$endpointid_key"

log "Claim 1: restarting the provider silently breaks the dependent"
sandbox_before="$(inspect "$PROVIDER" '{{.NetworkSettings.SandboxKey}}')"
docker restart "$PROVIDER" >/dev/null
sleep 2
sandbox_after="$(inspect "$PROVIDER" '{{.NetworkSettings.SandboxKey}}')"
check "provider keeps the same container ID" \
    "$provider_id_original" "$(inspect "$PROVIDER" '{{.Id}}')"
check "dependent still reports itself running" \
    "true" "$(inspect "$DEPENDENT" '{{.State.Running}}')"
# The crux: every field upstream compares still looks correct here, which is
# why issue #67 (Appdata Backup restarting gluetun) goes undetected forever.
check "dependent NetworkMode still references a live, current provider ID" \
    "container:${provider_id_original}" \
    "$(inspect "$DEPENDENT" '{{.HostConfig.NetworkMode}}')"
if [[ "$sandbox_before" == "$sandbox_after" ]]; then
    sandbox_changed="no"
else
    sandbox_changed="yes"
fi
info "provider SandboxKey before: $sandbox_before"
info "provider SandboxKey after:  $sandbox_after"
check "provider's network namespace was replaced" "yes" "$sandbox_changed"
check "dependent connectivity is now broken" "broken" "$(probe "$DEPENDENT")"

log "Claim 2: StartedAt ordering identifies the stale dependent"
provider_started="$(inspect "$PROVIDER" '{{.State.StartedAt}}')"
dependent_started="$(inspect "$DEPENDENT" '{{.State.StartedAt}}')"
info "provider  StartedAt $provider_started"
info "dependent StartedAt $dependent_started"
if [[ "$provider_started" > "$dependent_started" ]]; then
    stale="yes"
else
    stale="no"
fi
check "provider started after dependent (RFC3339 sorts lexicographically)" "yes" "$stale"

log "Claim 3: a plain restart of the dependent is sufficient repair"
docker restart "$DEPENDENT" >/dev/null
sleep 2
check "connectivity restored with no teardown and no config reconstruction" \
    "ok" "$(probe "$DEPENDENT")"
check "dependent now started after the provider" "yes" \
    "$([[ "$(inspect "$DEPENDENT" '{{.State.StartedAt}}')" > "$provider_started" ]] && echo yes || echo no)"

log "Claim 4: recreating the provider cannot be repaired by a restart"
docker rm -f "$PROVIDER" >/dev/null
start_provider
provider_id_new="$(inspect "$PROVIDER" '{{.Id}}')"
check "recreated provider has a new container ID" "yes" \
    "$([[ "$provider_id_new" != "$provider_id_original" ]] && echo yes || echo no)"
check "dependent still points at the dead original provider ID" \
    "container:${provider_id_original}" \
    "$(inspect "$DEPENDENT" '{{.HostConfig.NetworkMode}}')"
docker stop "$DEPENDENT" >/dev/null 2>&1 || true
if docker start "$DEPENDENT" >/dev/null 2>&1; then
    tier1_outcome="started"
else
    tier1_outcome="failed"
fi
check "a restart cannot recover it, so Tier 2 rebuild is required" \
    "failed" "$tier1_outcome"

log "Claim 5: the raw Engine API also normalises a name-form reference"
docker rm -f "$DEPENDENT" >/dev/null 2>&1 || true
curl -s --unix-socket "$DOCKER_SOCK" -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"Image\":\"${IMAGE}\",\"Cmd\":[\"sh\",\"-c\",\"while true; do sleep 30; done\"],\"HostConfig\":{\"NetworkMode\":\"container:${PROVIDER}\"}}" \
    "http://localhost/v1.43/containers/create?name=${API_DEPENDENT}" >/dev/null
docker start "$API_DEPENDENT" >/dev/null
check "name-form NetworkMode set via the API is stored as an ID" \
    "container:${provider_id_new}" \
    "$(inspect "$API_DEPENDENT" '{{.HostConfig.NetworkMode}}')"

log "Results"
printf '   %d passed, %d failed\n\n' "$pass_count" "$fail_count"
[[ "$fail_count" -eq 0 ]]
