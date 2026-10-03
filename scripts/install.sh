#!/usr/bin/env bash
# Runs on the Home Assistant SSH add-on after the release archive is extracted.
set -euo pipefail
umask 077

stage=${1:?Usage: install.sh EXTRACTED_RELEASE_DIRECTORY}
version=$(cat "$stage/VERSION")
backup="/config/central-heating-releases/$(date -u +%Y%m%dT%H%M%SZ)-v$version"
mkdir -p "$backup"
cp -p /config/configuration.yaml "$backup/configuration.yaml"
for registry in core.config_entries core.entity_registry core.label_registry central_heating lovelace_resources; do
    if [[ -f "/config/.storage/$registry" ]]; then
        mkdir -p "$backup/storage"
        cp -p "/config/.storage/$registry" "$backup/storage/$registry"
    fi
done
if [[ -d /config/custom_components/central_heating ]]; then
    cp -a /config/custom_components/central_heating "$backup/component"
fi
if [[ -d /config/central-heating ]]; then
    cp -a /config/central-heating "$backup/site"
fi
printf 'Configuration backup: %s\n' "$backup"

restore_files() {
    cp -p "$backup/configuration.yaml" /config/configuration.yaml
    rm -rf -- /config/custom_components/central_heating /config/central-heating
    if [[ -d "$backup/component" ]]; then
        cp -a "$backup/component" /config/custom_components/central_heating
    fi
    if [[ -d "$backup/site" ]]; then
        cp -a "$backup/site" /config/central-heating
    fi
}

awk '/^# BEGIN CENTRAL-HEATING-SYSTEM$/ { managed=1; next } /^# END CENTRAL-HEATING-SYSTEM$/ { managed=0; next } !managed { print }' /config/configuration.yaml > "$backup/base.yaml"
if grep -Eq '^(central_heating|lovelace):' "$backup/base.yaml"; then
    printf '%s\n' 'Existing unmanaged central_heating/lovelace configuration found. Merge the dashboard and integration includes before installing.' >&2
    exit 1
fi

mkdir -p /config/custom_components /config/central-heating
rm -rf -- /config/custom_components/central_heating
cp -a "$stage/custom_components/central_heating" /config/custom_components/
cp "$stage/config/central_heating.yaml" /config/central-heating/config.yaml
cp "$stage/config/dashboard.yaml" /config/central-heating/dashboard.yaml
cp "$backup/base.yaml" /config/configuration.yaml
cat >> /config/configuration.yaml <<'CONFIG'

# BEGIN CENTRAL-HEATING-SYSTEM
central_heating: !include central-heating/config.yaml
lovelace:
  dashboards:
    central-heating:
      mode: yaml
      title: Ogrzewanie
      icon: mdi:radiator
      show_in_sidebar: true
      filename: central-heating/dashboard.yaml
# END CENTRAL-HEATING-SYSTEM
CONFIG

if ! ha core check; then
    printf '%s\n' 'Home Assistant configuration check failed; restoring previous files.' >&2
    restore_files
    exit 1
fi
if ! ha core restart; then
    printf '%s\n' 'Home Assistant restart failed; restoring previous files.' >&2
    restore_files
    ha core restart || true
    exit 1
fi

# Use an existing owner session only on HA, without exporting or persisting its
# refresh/access credentials. SSH access is already required by this installer.
core_port=$(ha core info --raw-json | jq -r '.data.port // .port // 8123')
api_url="http://homeassistant:$core_port"
session_filter='.data as $auth | ($auth.users[] | select(.is_owner and .is_active) | .id) as $owner | $auth.refresh_tokens | map(select(.user_id == $owner and .token_type == "normal" and .client_id != null)) | last'
refresh_token=$(jq -r "$session_filter | .token // empty" /config/.storage/auth)
client_id=$(jq -r "$session_filter | .client_id // empty" /config/.storage/auth)
access_token=''
ready=false
for attempt in {1..60}; do
    if [[ -z "$access_token" && -n "$refresh_token" ]]; then
        access_token=$(curl -fsS --max-time 5 -X POST "$api_url/auth/token" \
            --data-urlencode grant_type=refresh_token \
            --data-urlencode "refresh_token=$refresh_token" \
            --data-urlencode "client_id=$client_id" 2>/dev/null | jq -r '.access_token // empty') || access_token=''
    fi
    if [[ -n "$access_token" ]] && curl -fsS --max-time 5 -H "Authorization: Bearer $access_token" \
        "$api_url/api/states/sensor.central_heating_status" -o "$backup/runtime-check.json" 2>/dev/null && \
        jq -e --arg version "$version" '.attributes.version == $version and (.attributes.controls.mode != null) and (.attributes.controls.target != null) and (.attributes.rooms | all(.controls.override != null and .controls.target != null and .controls.override_duration != null and .configuration_url != null and .low_at_or_above != null)) and (.attributes.zones | all(.sensor_status != null and .configuration_url != null))' "$backup/runtime-check.json" >/dev/null && \
        curl -fsS --max-time 5 -H "Authorization: Bearer $access_token" "$api_url/api/services" 2>/dev/null | \
        jq -e 'any(.[]; .domain == "central_heating" and .services.set_room_override != null)' >/dev/null; then
        ready=true
        break
    fi
    sleep 2
done
unset refresh_token access_token
if [[ "$ready" != true ]]; then
    printf '%s\n' 'Controller runtime check failed; restoring the previous component and configuration.' >&2
    restore_files
    ha core restart || true
    exit 1
fi
jq -r '"Runtime verified: \(.attributes.rooms | length) rooms; mode \(.state)."' "$backup/runtime-check.json"
printf 'Installed Central Heating System v%s. Dashboard: /central-heating/heating\n' "$version"
printf '%s\n' 'The first installation starts in Off; subsequent releases preserve your selected mode and targets.'
