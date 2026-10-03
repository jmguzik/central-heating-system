# Central Heating System

Central controls and a responsive Home Assistant dashboard for local Tuya fan-coils supplied by two independently monitored heating-water zones. This installation deliberately uses `fan_only` to control fans directly; thermostat setpoints do not determine operation.

| Mode | Operation |
| --- | --- |
| Off | All managed fan-coils off. |
| Manual | Fan-only at low speed, ignoring water temperature. |
| Adaptive | Each zone enables after smoothed and raw water readings stay at or above 33°C for 30 seconds and disables immediately on a valid raw reading at or below 30°C. Between thresholds it remembers its previous permission. Eligible rooms switch to medium at target minus 1.5°C and to low at target minus 0.5°C; between these values they retain their previous speed. |

The central target defaults to **22°C**. With 0.5°C room readings, medium starts at **20.5°C**, low resumes at **21.5°C**, and **21.0°C** retains the previous speed. Target selection changes fan speed. Reaching the target keeps the fan at low; the separate room maximum stops it.

Expand a room's **Override target & details**, choose a target and **30 minutes / 1 hour / 2 hours / Until cancelled**, then press **Apply override**. Target and duration are applied together. Temporary overrides show a countdown and **Cancel**; expiry restores the current central target, preserving the saved custom value. **Apply / restart timer** explicitly starts a fresh duration. Deadlines survive HA restarts and overdue overrides expire during startup. Existing permanent overrides from v0.1.0 remain permanent. Native room target, override switch, and duration selector are also available for automations.

In Manual and Adaptive, a room at **24°C** is switched off. It becomes eligible again at **23.5°C**. Water thresholds, central target, room maximum, and restart gap are editable on the dashboard. Mode, overrides, water permission, and room blocks survive restarts. Each room is evaluated independently.

| Zone label | Water sensor | Initial rooms |
| --- | --- | --- |
| `heating_poddasze` | Składzik / Poddasze Shelly | Sypialnia, Biuro |
| `heating_piwnica` | Kotłownia / Piwnica Shelly | Salon |

The entity mapping is in [config/central_heating.yaml](config/central_heating.yaml). After the initial membership is seeded, add or remove rooms by editing **labels on the climate entity** in Settings → Devices & services → Entities. Assign exactly one of the zone labels. Its dashboard row, override switch, target number, status, and temperature sensor appear automatically within 30 seconds. No additional Shellys or controller edits are needed for four rooms upstairs and two downstairs.

The dashboard is available at **`/central-heating/heating`**, with an **Ogrzewanie** sidebar entry. Mode and central target stay at the top, with large 0.5°C step buttons. Thresholds and room limits are under **Advanced settings**. Room cards show temperature, effective target, actual fan state, decision reason, command confirmation, and timed overrides. **Add room** opens setup instructions. Small **Thermostat settings** links sit inside expanded room details; **Shelly settings** links sit inside expanded sensor details and open the corresponding HA device configuration. The eight-hour chart uses accepted water temperatures.

**v0.3.0 uses Polish by default.** Change **Ustawienia zaawansowane → Język** to **English** to translate the controls, status messages, overrides, countdowns, setup instructions, and diagnostics. The choice is saved per browser/app; a fresh client starts in Polish. Card YAML accepts `language: pl` or `language: en`. [config/dashboard.en.yaml](config/dashboard.en.yaml) includes English dashboard and chart titles for English installations. Native integration setup and entity labels follow HA's configured language using Polish and English translations. Existing entity IDs, mode values, and duration values remain compatible with automations.

The custom card registers as a versioned **Lovelace module resource**, rather than relying on the separately loaded frontend extras. This addresses mobile cold loads where the chart can appear while the custom card shows a configuration error. Upgrades replace old resource versions and remove duplicates while preserving other cards. Restart/reopen the Android app once after updating so an already-open WebView fetches the new resources. When using YAML resource management, also declare `/central_heating/central-heating-card.js?v=0.5.0` with `type: module` in `lovelace.resources` so manual resource reloads retain it.

**v0.5.0 smooths both Shelly water readings at every temperature**, including the fluctuations around 22°C. Each reader independently uses the median of up to three real reports followed by exponential smoothing with a **60-second time constant**. The lower median is used during the two-sample startup period. Smoothing follows elapsed time, so irregular report intervals and extra controller reconciliations do not change its strength. Dashboard temperatures and new chart history use the smoothed value, with one decimal displayed; control retains the underlying precision. Existing history is unchanged. No additional helpers or switches are needed.

Shellys can stop reporting when a temperature is steady. Between reports the filter continues toward trusted data without appending samples or refreshing report timestamps. The median can hold an older value for at most **60 seconds after the last accepted report**, then smoothing follows the last validated raw reading. This removes short dips while allowing a sustained change to settle even if no further reports arrive. Near the final value the filter settles within 0.01°C, allowing an exact 33°C reading to finish warm-water confirmation. A genuine warmup can take longer to enable fans because the smoothed reading must catch up.

An abrupt exact **85°C** reading is rejected as a suspected DS18B20 reset when there is no trusted raw baseline or it differs from that baseline by more than 5°C; a gradual approach to genuinely hot water is accepted even while smoothing lags behind. Isolated deviations over 10°C from the median are rejected, except that a valid raw cold-water reading must always stop its zone immediately. **A valid raw reading at or below the OFF threshold stops fans without waiting for smoothing.** New permission requires 30 seconds with smoothed and raw temperatures at or above the ON threshold. Rejected readings freeze smoothing and reset confirmation without extending the existing fault grace. Recovery after unavailability or an expired fault starts with a fresh filter baseline so old readings cannot bias the recovered sensor.

**v0.4.0 adds automatic Shelly backup in Adaptive mode.** If a primary water sensor is missing, unknown, unavailable, or outside the valid range, its zone immediately uses the other healthy Shelly. Brief rejected spikes still retain the primary's last accepted reading for up to **60 seconds**; if the fault persists, the healthy reader takes over. A backup must currently report valid data; a reader holding an invalid reading during its grace period cannot serve as backup. There is no switch to enable this behavior.

The affected zone follows the backup reader's own filtered temperature, 33°C/30°C hysteresis, and warm-water confirmation. It cannot inherit the failed primary's ON permission. Valid cold backup readings stop the fan immediately. Both readers unavailable stop Adaptive fans immediately; when both have rejected spikes, the existing 60-second grace can retain their previously enabled permissions before stopping. Readers never serve as backups for each other recursively. A valid primary report restores normal zone control automatically, with its own water permission and confirmation. Room limits, target overrides, fan-speed hysteresis, and Off mode retain priority. Manual continues to bypass water permission.

A prominent Polish/English dashboard warning names the affected zone and backup source, and the zone header shows the temperature actually used for control. One HA notification uses the installation's HA language, updates only when the backup mapping changes, and clears on recovery or when leaving Adaptive mode. `sensor.central_heating_status` exposes `backup_active`; each zone also exposes `water_source_id`, `backup_source_name`, `backup_source_sensor`, and `operating_water_temperature`. These are available for automations. Primary sensor diagnostics and history remain tied to their own reader, so a backup is not graphed as a recovered primary.

Sensor details show raw and accepted temperatures, report ages, rejection count, and faults. A stable, available HA reading is not considered invalid merely because its numeric value has not changed; the filter does not infer a hardware heartbeat from temperature changes.

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

Invalid water thresholds are rejected. Missing room temperature blocks that room. Adaptive needs a usable primary or healthy backup water sensor; without either, its fans are requested Off. Manual bypasses the water rule. Conflicting zone labels stop the affected room. Unreachable devices are shown as unconfirmed and retried; the dashboard never claims a physical fan is off solely because an off command was requested. Removing a room's zone label requests Off before releasing control. Disabling the integration requests Off for its managed fan-coils.

The policy tests cover threshold boundaries, noisy water sequences, independent zones, room guard priority, fan-speed memory, target inheritance, timed overrides, restarts, and invalid readings. Smoothing tests replay the observed five-second 22°C/21.06°C dip and cover irregular ticks, steady temperatures without further reports, an exact ON threshold, genuine 85°C warming, immediate raw cold-water shutdown, frozen fault grace, and recovery baselines. Backup tests cover both directions, startup confirmation, independent hysteresis, cold-water shutdown, both-reader failure, recovery, notifications, restarts, and loss of the backup during a device command. HA adapter tests also cover command ordering, real report handling, legacy migration, and expiry during device commands. Browser tests exercise Polish/English backup warnings, the controls, hidden settings links, error feedback, and draft inputs at desktop and phone widths when Chrome/Chromium is installed. Releases also run configuration and runtime checks on the target HA installation.
