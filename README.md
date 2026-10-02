# Central Heating System

Central controls and a responsive Home Assistant dashboard for local Tuya fan-coils supplied by two independently monitored heating-water zones. This installation deliberately uses `fan_only` to control fans directly; thermostat setpoints do not determine operation.

| Mode | Operation |
| --- | --- |
| Off | All managed fan-coils off. |
| Manual | Fan-only at low speed, ignoring water temperature. |
| Adaptive | Each zone enables at 33°C water and disables at 30°C. Between thresholds it remembers its previous permission. Eligible rooms use medium below their effective target minus 1°C, otherwise low. |

The central target defaults to **22°C**. Each room can enable **Use custom target** and choose its own value; disabling the override restores central inheritance without erasing the saved custom value. Target selection changes fan speed. Reaching the target keeps the fan at low; the separate room maximum stops it.

In Manual and Adaptive, a room at **24°C** is switched off. It becomes eligible again at **23.5°C**. Water thresholds, central target, room maximum, and restart gap are editable on the dashboard. Mode, overrides, water permission, and room blocks survive restarts. Each room is evaluated independently.

| Zone label | Water sensor | Initial rooms |
| --- | --- | --- |
| `heating_poddasze` | Składzik / Poddasze Shelly | Sypialnia, Biuro |
| `heating_piwnica` | Kotłownia / Piwnica Shelly | Salon |

The entity mapping is in [config/central_heating.yaml](config/central_heating.yaml). After the initial membership is seeded, add or remove rooms by editing **labels on the climate entity** in Settings → Devices & services → Entities. Assign exactly one of the zone labels. Its dashboard row, override switch, target number, status, and temperature sensor appear automatically within 30 seconds. No additional Shellys or controller edits are needed for four rooms upstairs and two downstairs.

The dashboard is available at **`/central-heating/heating`**, with an **Ogrzewanie** sidebar entry. It includes central controls, water availability, per-room targets and override controls, actual fan state, the reason for each decision, command-confirmation status, and an eight-hour water-temperature chart.

Install and update from a computer on the HA network:

```sh
make check
make deploy
```

Requires Python 3.12+, Node.js for JavaScript syntax checking, OpenSSH, and the Home Assistant SSH add-on. `make deploy` defaults to `root@192.168.68.59:22` and prompts privately for the SSH password. It builds a versioned archive, uploads it, backs up the affected files on HA, runs `ha core check`, restarts HA, and verifies that the controller and room controls load. First installation starts in **Off**; updates preserve the selected mode. No passwords or API tokens are included in the repository or release archive.

For another SSH destination or key-based login:

```sh
python3 scripts/deploy.py --host 192.168.68.59 --user root --ssh-key
```

The installer keeps backups under `/config/central-heating-releases/`. It restores the previous configuration/component files if validation, restart, or the runtime check fails. For the runtime check, an existing active owner login session is required; the installer obtains a temporary API token locally on HA and does not export or save it. Existing unmanaged `central_heating:` or `lovelace:` YAML blocks require merging the supplied includes before installation. The installer only replaces its own component, site files, and marked configuration block.

To install a downloaded release manually, extract the archive in the HA SSH add-on and run `bash scripts/install.sh "$PWD"`. The release archive contains the integration, dashboard, site configuration, and installer. `make release` creates `dist/central-heating-system-vVERSION.tar.gz` and its SHA-256 checksum. The integration can also be installed as a HACS custom integration from this repository; the supplied dashboard/site configuration is still needed for this installation.

Invalid water thresholds are rejected. Missing room temperature blocks that room; missing water temperature disables only that zone in Adaptive. Manual bypasses the water rule. Conflicting zone labels stop the affected room. Unreachable devices are shown as unconfirmed and retried; the dashboard never claims a physical fan is off solely because an off command was requested. Removing a room's zone label requests Off before releasing control. Disabling the integration requests Off for its managed fan-coils.

The policy tests cover threshold boundaries, heating/cooling sequences, independent zones, room guard priority, target inheritance, overrides, restarts, and invalid readings. HA adapter tests cover command ordering and state changes during device commands. Configuration and runtime checks should also be performed on the target HA version when releasing.
