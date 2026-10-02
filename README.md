# Central Heating System

Central controls and a responsive Home Assistant dashboard for local Tuya fan-coils supplied by two independently monitored heating-water zones. This installation deliberately uses `fan_only` to control fans directly; thermostat setpoints do not determine operation.

| Mode | Operation |
| --- | --- |
| Off | All managed fan-coils off. |
| Manual | Fan-only at low speed, ignoring water temperature. |
| Adaptive | Each zone enables after accepted water stays at or above 33°C for 30 seconds and disables on a valid reading at or below 30°C. Between thresholds it remembers its previous permission. Eligible rooms switch to medium at target minus 1.5°C and to low at target minus 0.5°C; between these values they retain their previous speed. |

The central target defaults to **22°C**. With 0.5°C room readings, medium starts at **20.5°C**, low resumes at **21.5°C**, and **21.0°C** retains the previous speed. Target selection changes fan speed. Reaching the target keeps the fan at low; the separate room maximum stops it.

Expand a room's **Override target & details**, choose a target and **30 minutes / 1 hour / 2 hours / Until cancelled**, then press **Apply override**. Target and duration are applied together. Temporary overrides show a countdown and **Cancel**; expiry restores the current central target, preserving the saved custom value. **Apply / restart timer** explicitly starts a fresh duration. Deadlines survive HA restarts and overdue overrides expire during startup. Existing permanent overrides from v0.1.0 remain permanent. Native room target, override switch, and duration selector are also available for automations.

In Manual and Adaptive, a room at **24°C** is switched off. It becomes eligible again at **23.5°C**. Water thresholds, central target, room maximum, and restart gap are editable on the dashboard. Mode, overrides, water permission, and room blocks survive restarts. Each room is evaluated independently.

| Zone label | Water sensor | Initial rooms |
| --- | --- | --- |
| `heating_poddasze` | Składzik / Poddasze Shelly | Sypialnia, Biuro |
| `heating_piwnica` | Kotłownia / Piwnica Shelly | Salon |

The entity mapping is in [config/central_heating.yaml](config/central_heating.yaml). After the initial membership is seeded, add or remove rooms by editing **labels on the climate entity** in Settings → Devices & services → Entities. Assign exactly one of the zone labels. Its dashboard row, override switch, target number, status, and temperature sensor appear automatically within 30 seconds. No additional Shellys or controller edits are needed for four rooms upstairs and two downstairs.

The dashboard is available at **`/central-heating/heating`**, with an **Ogrzewanie** sidebar entry. Mode and central target stay at the top, with large 0.5°C step buttons. Thresholds and room limits are under **Advanced settings**. Room cards show temperature, effective target, actual fan state, decision reason, command confirmation, and timed overrides. **Add room** opens setup instructions. Small **Thermostat settings** links sit inside expanded room details; **Shelly settings** links sit inside expanded sensor details and open the corresponding HA device configuration. The eight-hour chart uses accepted water temperatures.

Each Shelly has its own filter. It uses the median of up to three fresh sensor reports, using the lower value during the two-sample startup period. Controller timer ticks never count as fresh reports. An abrupt exact **85°C** reading is rejected as a suspected DS18B20 reset when there is no trusted baseline or it differs from that baseline by more than 5°C; a gradual approach to genuinely hot water is accepted. Isolated deviations over 10°C from the median are rejected. A valid cold-water reading bypasses median delay and stops its zone immediately. New permission requires 30 seconds of accepted and raw readings at or above the ON threshold. Rejected readings reset this confirmation, so a spike cannot enable a cold zone.

On rejected or missing readings, an already-enabled zone can temporarily retain its last accepted temperature for **60 seconds**. Persistent invalid readings then request that Adaptive zone's fans Off. Recovery requires the warm-water confirmation again. Sensor details show raw and accepted temperatures, report ages, rejection count, and faults. A stable, available HA reading is not considered invalid merely because its numeric value has not changed; the filter does not infer a hardware heartbeat from temperature changes. Manual still bypasses water permission, while room limits remain active.

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

Invalid water thresholds are rejected. Missing room temperature blocks that room; missing water temperature blocks Adaptive startup immediately or stops an already-enabled zone after the 60-second grace period. Manual bypasses the water rule. Conflicting zone labels stop the affected room. Unreachable devices are shown as unconfirmed and retried; the dashboard never claims a physical fan is off solely because an off command was requested. Removing a room's zone label requests Off before releasing control. Disabling the integration requests Off for its managed fan-coils.

The policy tests cover threshold boundaries, noisy water sequences, independent zones, room guard priority, fan-speed memory, target inheritance, timed overrides, restarts, and invalid readings. HA adapter tests cover command ordering, real report handling, legacy migration, and expiry during device commands. Browser tests exercise the controls, hidden settings links, error feedback, and draft inputs at desktop and phone widths when Chrome/Chromium is installed. Releases also run configuration and runtime checks on the target HA installation.
