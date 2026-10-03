/* Keep globals isolated if an old cached resource and a new module both load. */
(() => {
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const durations = ["30 minutes", "1 hour", "2 hours", "Until cancelled"];
const polish = {
  "Central heating": "Ogrzewanie centralne",
  "Two water zones · individual room control": "Dwie strefy wody · osobne sterowanie pokojami",
  "Fans running": "Pracujące wentylatory", "Add room": "Dodaj pokój",
  "Heating mode": "Tryb ogrzewania", "Off": "Wyłączony", "Manual": "Ręczny", "Adaptive": "Adaptacyjny",
  "low": "Niski", "medium": "Średni", "high": "Wysoki", "Fan": "Wentylator",
  "unavailable": "niedostępne", "Unavailable": "Niedostępne",
  "Central target": "Temperatura centralna", "Target": "Temperatura docelowa",
  "Central": "Centralna", "Custom": "Własna", "Temporary": "Tymczasowa",
  "All managed fans are requested off.": "Wszystkie sterowane wentylatory otrzymują polecenie wyłączenia.",
  "Fans run at low speed; the room temperature limit still applies.": "Wentylatory pracują na niskim biegu; limit temperatury pokoju nadal obowiązuje.",
  "Warm water enables fans. Cold rooms use medium speed, then low as they warm.": "Ciepła woda uruchamia wentylatory. W chłodnych pokojach pracują na średnim biegu, a po ogrzaniu na niskim.",
  "Targets choose speed; the room maximum stops the fan.": "Temperatura docelowa wybiera bieg; limit temperatury pokoju wyłącza wentylator.",
  "Advanced settings": "Ustawienia zaawansowane", "Language": "Język",
  "Water ON": "Woda: próg włączenia", "Water OFF": "Woda: próg wyłączenia",
  "Room maximum": "Limit temperatury pokoju", "Room restart gap": "Spadek do ponownego włączenia",
  "Restart explanation": "Po osiągnięciu limitu wentylatory mogą ponownie ruszyć przy {temperature}. Bieg pozostaje bez zmian między temperaturą docelową −1,5°C a −0,5°C. Woda jest filtrowana medianą trzech odczytów; włączenie wymaga 30 sekund potwierdzenia. Niedostępny czujnik jest automatycznie zastępowany drugim sprawnym. Błędne odczyty mają 60 sekund okresu ochronnego; potem działa czujnik zapasowy lub strefa zostaje wyłączona.",
  "Backup water": "Woda z czujnika zapasowego", "Backup sensor active": "Sterowanie czujnikiem zapasowym",
  "Backup explanation": "{zone} korzysta z odczytów czujnika {source}. Własny czujnik jest niedostępny lub podaje błędne odczyty. Powrót nastąpi automatycznie po poprawnym odczycie.",
  "No usable water sensor": "Brak sprawnego czujnika wody",
  "Add a thermostat to heating": "Dodaj termostat do ogrzewania",
  "Add thermostat instruction": "Dodaj termostat przez",
  "Settings → Devices & services → Tuya Local": "Ustawienia → Urządzenia i usługi → Tuya Local",
  "and assign its room/area.": "i przypisz mu pokój/obszar.",
  "Open": "Otwórz", "Entities": "Encje",
  "select its": "wybierz jego encję", "entity, and add exactly one label:": "i dodaj dokładnie jedną etykietę:", "for": "dla",
  "Room appears instruction": "Pokój i jego sterowanie pojawią się automatycznie i odziedziczą temperaturę centralną. Bieżący tryb systemu obowiązuje od razu.",
  "Choose the pipe instruction": "Wybierz strefę według rury zasilającej wodą dany klimakonwektor, a nie według piętra, na którym stoi termostat.",
  "Thermostat requirements": "Termostat musi podawać temperaturę pokoju i obsługiwać fan_only z niskim i średnim biegiem. Etykietę dodaj do samej encji climate.",
  "Accepted water": "Woda po filtracji", "Zone label missing": "Brak etykiety strefy",
  "Water rule bypassed": "Temperatura wody pomijana", "System off": "System wyłączony",
  "Zone fans off": "Wentylatory tej strefy w trybie adaptacyjnym otrzymują polecenie wyłączenia.",
  "Using the last good reading temporarily.": "Tymczasowo używany jest ostatni poprawny odczyt.",
  "Mode bypasses water": "Tryb {mode} nie korzysta z temperatury wody.",
  "Sensor readings & details": "Odczyty i szczegóły czujnika", "Raw": "Surowy", "accepted": "po filtracji",
  "Raw report": "Odczyt surowy", "last accepted": "ostatni poprawny",
  "Rejected readings since startup": "Odrzucone odczyty od uruchomienia", "Shelly settings ↗": "Ustawienia Shelly ↗",
  "No rooms instruction": "Brak przypisanych pokoi. Użyj przycisku Dodaj pokój, aby dołączyć termostat.",
  "Footer": "Po zakończeniu odliczania tymczasowa temperatura wraca do bieżącej wartości centralnej. Limit temperatury pokoju i tryb wyłączony zawsze mają pierwszeństwo.",
  "awaiting device confirmation": "oczekiwanie na potwierdzenie urządzenia",
  "Until cancelled": "Do anulowania", "Temporary override": "Tymczasowa temperatura", "Custom target": "Własna temperatura",
  "Cancel": "Anuluj", "Change override & details": "Zmień temperaturę i szczegóły", "Override target & details": "Własna temperatura i szczegóły",
  "Room target": "Temperatura dla pokoju", "Duration": "Czas obowiązywania", "Override duration for": "Czas własnej temperatury dla",
  "30 minutes": "30 minut", "1 hour": "1 godzina", "2 hours": "2 godziny",
  "Apply / restart timer": "Zastosuj / rozpocznij odliczanie", "Apply override": "Zastosuj własną temperaturę",
  "Medium": "Średni", "Thermostat settings ↗": "Ustawienia termostatu ↗",
  "Decrease": "Zmniejsz", "Increase": "Zwiększ",
  "Starting": "Ogrzewanie centralne uruchamia się. Jeśli ten komunikat nie znika, sprawdź integrację w Ustawieniach.",
  "Select the central heating status entity": "Wybierz encję stanu ogrzewania centralnego",
  "Ending · restoring central target": "Koniec · powrót do temperatury centralnej",
  "{time} remaining": "Pozostało: {time}", "{time} ago": "{time} temu",
  "Thermostat unavailable": "Termostat niedostępny", "Room temperature unavailable": "Temperatura pokoju niedostępna",
  "Room limit reached": "Osiągnięto limit temperatury pokoju", "Unsupported thermostat fan modes": "Termostat nie obsługuje wymaganych biegów",
  "Manual · low": "Tryb ręczny · niski bieg", "Water temperature unavailable": "Temperatura wody niedostępna",
  "Water too cold": "Woda zbyt chłodna", "Cold room · medium": "Chłodny pokój · średni bieg",
  "Near target · low": "Blisko temperatury docelowej · niski bieg", "Water sensor unavailable": "Czujnik wody niedostępny",
  "Suspect 85°C sensor reset": "Podejrzany odczyt 85°C po resecie czujnika", "Isolated temperature spike": "Pojedynczy skok temperatury",
  "Sensor fault": "Błąd czujnika", "Reading rejected · last good retained": "Odczyt odrzucony · zachowano ostatni poprawny",
  "Water ready": "Woda wystarczająco ciepła", "Confirming warm water": "Potwierdzanie temperatury wody",
  "Waiting for warm water": "Oczekiwanie na ciepłą wodę", "Initializing": "Uruchamianie",
  "Conflicting zone labels": "Sprzeczne etykiety stref", "Device command timed out": "Upłynął czas oczekiwania na polecenie urządzenia",
  "Stored settings were invalid; reset to defaults and Off": "Zapisane ustawienia były błędne; przywrócono wartości domyślne i wyłączono system",
};
const english = {
  "Restart explanation": "Rooms restart at {temperature} after reaching the maximum. Fan speed keeps its previous value between target −1.5°C and target −0.5°C. Water uses a median of three reports, with 30 seconds of confirmation before enabling. An unavailable sensor is automatically replaced by the other healthy reader. Invalid readings retain the 60-second grace, then use backup or stop the zone.",
  "Backup explanation": "{zone} uses readings from {source}. Its own sensor is unavailable or has invalid readings. The primary returns automatically after a valid reading.",
  "Add thermostat instruction": "Add the thermostat through", "Room appears instruction": "The room and its controls appear automatically and inherit the central target. The current system mode applies immediately.",
  "Choose the pipe instruction": "Choose the zone by the water pipe supplying the fan-coil, regardless of the thermostat's floor.",
  "Thermostat requirements": "The thermostat must report room temperature and support fan_only with low and medium speeds. Apply the label to the climate entity itself.",
  "Zone fans off": "Adaptive fans in this zone are requested off.", "Mode bypasses water": "{mode} mode does not use water permission.",
  "No rooms instruction": "No rooms assigned. Use Add room to connect a thermostat.",
  "Footer": "Temporary overrides return to the current central target when their timer ends. The room limit and Off mode always take priority.",
  "Starting": "Central heating is starting. If this persists, check the integration in Settings.",
};

class CentralHeatingCard extends HTMLElement {
  constructor() {
    super();
    this._drafts = new Map();
    this._open = new Set();
    this._pending = false;
  }

  setConfig(config) {
    this._config = config;
    if (config.language && !["pl", "en"].includes(config.language)) throw new Error("Supported languages: pl, en");
    try { this._language = localStorage.getItem(`central-heating-language:${config.entity}`); } catch (_) { /* WebViews may restrict storage. */ }
    if (!["pl", "en"].includes(this._language)) this._language = config.language || "pl";
    if (!config.entity) throw new Error(this._t("Select the central heating status entity"));
    // Keep the host's width stable while a render replaces its shadow styles.
    this.style.display = "block";
    if (!this.shadowRoot) this.attachShadow({mode: "open"});
    this._render();
  }

  connectedCallback() { clearInterval(this._timer); this._timer = setInterval(() => this._clock(), 1000); }
  disconnectedCallback() { clearInterval(this._timer); }

  set hass(hass) {
    this._hass = hass;
    const signature = JSON.stringify(hass.states[this._config?.entity]?.attributes);
    if (signature === this._signature) return;
    this._signature = signature;
    this._render();
  }

  getCardSize() { return 12; }

  _t(key, values = {}) {
    const text = (this._language === "pl" ? polish[key] : english[key]) || english[key] || key || "";
    return text.replace(/\{(\w+)\}/g, (_, name) => values[name] ?? "");
  }

  _degrees(value) {
    return value == null || !Number.isFinite(Number(value)) ? this._t("Unavailable") : `${new Intl.NumberFormat(this._language, {minimumFractionDigits:1, maximumFractionDigits:1}).format(Number(value))}°C`;
  }

  async _call(domain, service, data) {
    if (this._pending) return false;
    this._pending = true;
    this._error = null;
    this._render(true);
    let success = false;
    try {
      await this._hass.callService(domain, service, data);
      success = true;
    } catch (error) {
      this._error = error.message || String(error);
    } finally {
      this._pending = false;
      this._render(true);
    }
    return success;
  }

  _number(key, label, value, minimum, maximum, step, entity, draft = false) {
    const disabled = this._pending || (!draft && !entity);
    return `<label class="setting"><span>${esc(label)}</span><div class="stepper">
      <button type="button" data-step="-${step}" aria-label="${this._t("Decrease")} ${esc(label)}" ${disabled ? "disabled" : ""}>−</button>
      <input type="number" aria-label="${esc(label)}" data-number="${esc(entity || "")}" data-key="${esc(key)}" ${draft ? 'data-draft="true"' : ""} min="${minimum}" max="${maximum}" step="${step}" value="${esc(value)}" ${disabled ? "disabled" : ""}>
      <button type="button" data-step="${step}" aria-label="${this._t("Increase")} ${esc(label)}" ${disabled ? "disabled" : ""}>+</button><span>°C</span>
    </div></label>`;
  }

  _room(room) {
    const fault = room.error || !room.confirmed || room.reported_mode === "unavailable";
    const speed = room.reported_mode === "off" ? "Off" : room.reported_mode === "fan_only" ? room.reported_fan || "Fan" : room.reported_mode;
    const draft = this._drafts.get(room.key) || {target: room.custom_target, duration: room.override ? room.override_duration : "2 hours"};
    const expiry = room.override_expires_at ? `<span data-expires="${room.override_expires_at}"></span>` : this._t("Until cancelled");
    return `<article class="room ${fault ? "unconfirmed" : ""}" data-room="${esc(room.key)}">
      <div class="room-head"><h3>${esc(room.name)}</h3><span class="pill ${room.reported_mode === "fan_only" ? "running" : ""}">${esc(this._t(speed))}</span></div>
      <div class="room-temperature">${this._degrees(room.temperature)}</div>
      <div class="target-summary">${this._t("Target")} <strong>${this._degrees(room.effective_target)}</strong><span class="source">${esc(this._t(room.target_source))}</span></div>
      <p class="reason">${esc(this._t(room.reason))}${!room.confirmed ? ` · ${this._t("awaiting device confirmation")}` : ""}</p>
      ${room.error ? `<p class="error" role="alert">${esc(this._t(room.error))}</p>` : ""}
      ${room.override ? `<div class="override-active"><span>${this._t(room.override_expires_at ? "Temporary override" : "Custom target")} · ${expiry}</span><button type="button" data-cancel="${esc(room.controls?.override || "")}" ${this._pending ? "disabled" : ""}>${this._t("Cancel")}</button></div>` : ""}
      <details data-panel="room-${esc(room.key)}" ${this._open.has(`room-${room.key}`) ? "open" : ""}>
        <summary>${this._t(room.override ? "Change override & details" : "Override target & details")}</summary>
        <div class="override-form">
          ${this._number("room_target", this._t("Room target"), draft.target, 5, 35, 0.5, null, true)}
          <label class="setting"><span>${this._t("Duration")}</span><select data-duration aria-label="${this._t("Override duration for")} ${esc(room.name)}" ${this._pending ? "disabled" : ""}>${durations.map(duration => `<option value="${duration}" ${duration === draft.duration ? "selected" : ""}>${this._t(duration)}</option>`).join("")}</select></label>
          <button class="primary" type="button" data-apply ${this._pending ? "disabled" : ""}>${this._t(room.override ? "Apply / restart timer" : "Apply override")}</button>
        </div>
        <p class="small thresholds">${this._t("Medium")} ≤${this._degrees(room.medium_at_or_below)} · ${this._t("low")} ≥${this._degrees(room.low_at_or_above)}</p>
        <a class="device-settings" href="${esc(room.configuration_url || '/config/entities')}">${this._t("Thermostat settings ↗")}</a>
      </details>
    </article>`;
  }

  _render(force = false) {
    if (!this.shadowRoot || !this._config) return;
    if (!force && this.shadowRoot.activeElement?.matches('input, select')) return;
    const model = this._hass?.states[this._config.entity]?.attributes;
    if (!model?.settings || !model?.zones) {
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:24px">${this._t("Starting")}</div></ha-card>`;
      return;
    }
    const settings = model.settings;
    const controls = model.controls || {};
    const rooms = model.rooms || [];
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);font-family:var(--ha-font-family,inherit)}
        *{box-sizing:border-box}ha-card{display:block;padding:24px;border-radius:18px;max-width:1180px;margin:8px auto;background:var(--ha-card-background,var(--card-background-color))}
        h1,h2,h3,p{margin:0}h1{font-size:28px}h2{font-size:20px}h3{font-size:17px}
        button,input,select{font:inherit;color:inherit}button,select,summary{cursor:pointer}button:disabled,input:disabled,select:disabled{opacity:.55;cursor:wait}
        button{min-height:44px;border:1px solid var(--divider-color);border-radius:9px;background:var(--card-background-color);padding:8px 14px}
        button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--primary-color);outline-offset:2px}
        .primary,.modes .active{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:var(--primary-color)}
        .hero,.zone-head,.room-head,.override-active{display:flex;align-items:center;justify-content:space-between;gap:12px}.hero{margin-bottom:18px}.hero-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
        .subtitle,.small,.reason,.explanation{color:var(--secondary-text-color);font-size:13px;line-height:1.6}.subtitle{margin-top:5px}.count{font-size:13px;white-space:nowrap}.explanation{margin:14px 0}
        .main-controls{display:flex;gap:24px;align-items:flex-end;flex-wrap:wrap}.modes{display:flex;gap:8px}.modes button{min-width:86px}.setting{display:block;font-size:13px;line-height:1.5}
        .stepper{display:flex;align-items:center;gap:5px;margin-top:6px}.stepper input{width:78px;min-width:0;text-align:center;appearance:textfield;-moz-appearance:textfield}.stepper input::-webkit-inner-spin-button{appearance:none}.stepper button{width:44px;padding:8px}.stepper span{font-size:12px}
        input,select{min-height:44px;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:8px;padding:8px}select{width:100%;margin-top:6px}
        details{margin-top:14px}summary{min-height:36px;padding:8px 0;font-size:13px;font-weight:600}.settings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;background:var(--secondary-background-color);border-radius:10px;padding:16px}.settings .small{grid-column:1/-1}
        .zones{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin-top:22px}.zone{min-width:0}.zone-head{margin-bottom:12px;align-items:flex-start;flex-wrap:wrap}.water{margin-top:6px;font-size:15px}.zone-badge,.pill,.source{font-size:12px;border-radius:20px;padding:6px 10px;background:var(--secondary-background-color)}.ready,.running{color:var(--success-color,#2e7d32);background:color-mix(in srgb,var(--success-color,#2e7d32) 12%,transparent)}
        .room-list{display:grid;gap:12px}.room{border:1px solid var(--divider-color);border-radius:12px;padding:18px}.room-temperature{font-size:38px;font-weight:650;margin:8px 0}.target-summary{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px}.reason{margin-top:10px}.thresholds{margin-top:12px;font-size:12px}
        .override-active{margin-top:14px;border-radius:8px;background:var(--secondary-background-color);padding:9px;font-size:12px;flex-wrap:wrap}.override-active button{min-height:36px;padding:5px 10px}.override-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.override-form .primary{grid-column:1/-1}.setting{min-width:0}select{min-width:0}
        .diagnostics{background:var(--secondary-background-color);padding:10px 12px;border-radius:9px;margin-bottom:12px}.diagnostics p{font-size:12px;line-height:1.7}.alert{border-left:4px solid var(--error-color,#b3261e);padding:10px 12px;margin:12px 0;background:var(--secondary-background-color)}.error{color:var(--error-color,#b3261e);font-size:13px}.unconfirmed{border-color:var(--warning-color,#b26a00)}.backup-alert{border-color:var(--warning-color,#b26a00);font-size:14px;line-height:1.6;overflow-wrap:anywhere}.backup-alert p{margin-top:4px}
        .guide{padding:14px;background:var(--secondary-background-color);border-radius:10px;font-size:14px;line-height:1.7}.guide ol{padding-left:22px}.guide code{white-space:normal;overflow-wrap:anywhere}a{color:var(--primary-color)}.device-settings{display:inline-block;font-size:12px;color:var(--secondary-text-color);margin-top:10px;padding:6px 0}.empty{padding:18px;font-size:13px;border:1px dashed var(--divider-color);border-radius:9px}.footer{margin-top:20px;font-size:12px;color:var(--secondary-text-color)}
        @media(max-width:800px){.zones{grid-template-columns:1fr}.hero{align-items:flex-start}.hero-actions{justify-content:flex-end}ha-card{padding:20px}}
        @media(max-width:480px){ha-card{padding:16px;margin:4px auto}.hero{display:block}h1{font-size:24px}.hero-actions{justify-content:flex-start;margin-top:12px}.main-controls{display:block}.modes{margin-bottom:14px}.modes button{flex:1;min-width:0}.settings,.override-form{grid-template-columns:1fr}.room{padding:14px}.settings{padding:12px}}
      </style>
      <ha-card aria-busy="${this._pending}" lang="${this._language}">
        <div class="hero"><div><h1>${this._t("Central heating")}</h1><p class="subtitle">${this._t("Two water zones · individual room control")}</p></div><div class="hero-actions"><span class="count">${this._t("Fans running")}: ${model.running_fans || 0} / ${rooms.length}</span><button type="button" data-add-room>${this._t("Add room")}</button></div></div>
        ${this._error ? `<p class="error alert" role="alert">${esc(this._error)}</p>` : ""}
        ${model.startup_fault ? `<p class="error alert" role="alert">${esc(this._t(model.startup_fault))}</p>` : ""}
        ${model.mode === "Adaptive" ? model.zones.filter(zone => zone.backup_active).map(zone => `<div class="alert backup-alert" role="alert" data-backup-zone="${esc(zone.id)}"><strong>${this._t("Backup sensor active")}</strong><p>${esc(this._t("Backup explanation", {zone:zone.name, source:zone.backup_source_name}))}</p></div>`).join("") : ""}
        <div class="main-controls"><div class="modes" role="group" aria-label="${this._t("Heating mode")}">${["Off", "Manual", "Adaptive"].map(mode => `<button type="button" data-mode="${mode}" class="${model.mode === mode ? "active" : ""}" aria-pressed="${model.mode === mode}" ${this._pending ? "disabled" : ""}>${this._t(mode)}</button>`).join("")}</div>${this._number("target", this._t("Central target"), settings.target, 5, 35, 0.5, controls.target)}</div>
        <p class="explanation">${this._t(model.mode === "Off" ? "All managed fans are requested off." : model.mode === "Manual" ? "Fans run at low speed; the room temperature limit still applies." : "Warm water enables fans. Cold rooms use medium speed, then low as they warm.")} ${this._t("Targets choose speed; the room maximum stops the fan.")}</p>
        <details data-panel="advanced" ${this._open.has("advanced") ? "open" : ""}><summary>${this._t("Advanced settings")}</summary><div class="settings">
          <label class="setting"><span>${this._t("Language")}</span><select data-language aria-label="${this._t("Language")}"><option value="pl" ${this._language === "pl" ? "selected" : ""}>Polski</option><option value="en" ${this._language === "en" ? "selected" : ""}>English</option></select></label>
          ${this._number("water_on", this._t("Water ON"), settings.water_on, 0, 100, 0.5, controls.water_on)}
          ${this._number("water_off", this._t("Water OFF"), settings.water_off, 0, 100, 0.5, controls.water_off)}
          ${this._number("room_maximum", this._t("Room maximum"), settings.room_maximum, 5, 35, 0.5, controls.room_maximum)}
          ${this._number("room_hysteresis", this._t("Room restart gap"), settings.room_hysteresis, 0.5, 5, 0.5, controls.room_hysteresis)}
          <p class="small">${this._t("Restart explanation", {temperature:this._degrees(settings.room_maximum - settings.room_hysteresis)})}</p>
        </div></details>
        <details data-panel="add-room" ${this._open.has("add-room") ? "open" : ""}><summary>${this._t("Add a thermostat to heating")}</summary><div class="guide"><ol>
          <li>${this._t("Add thermostat instruction")} <a href="/config/integrations">${esc(this._t("Settings → Devices & services → Tuya Local"))}</a> ${this._t("and assign its room/area.")}</li>
          <li>${this._t("Open")} <a href="/config/entities">${this._t("Entities")}</a>, ${this._t("select its")} <code>climate.…</code> ${this._t("entity, and add exactly one label:")} ${model.zones.map(zone => `<code>${esc(zone.label)}</code> ${this._t("for")} ${esc(zone.name)}`).join("; ")}.</li>
          <li>${this._t("Room appears instruction")}</li>
        </ol><p>${this._t("Thermostat requirements")} ${this._t("Choose the pipe instruction")}</p></div></details>
        <div class="zones">${model.zones.map(zone => {
          const members = rooms.filter(room => room.zone_ids.includes(zone.id));
          const text = zone.label_missing ? "Zone label missing" : model.mode === "Manual" ? "Water rule bypassed" : model.mode === "Off" ? "System off" : zone.sensor_status;
          return `<section class="zone"><div class="zone-head"><div><h2>${esc(zone.name)}</h2><div class="water">${this._t(zone.backup_active ? "Backup water" : "Accepted water")} <strong>${this._degrees(zone.backup_active ? zone.operating_water_temperature : zone.water_temperature)}</strong></div></div><span class="zone-badge ${zone.allowed ? "ready" : ""}">${esc(this._t(text))}</span></div>
            ${zone.sensor_issue && !zone.backup_active ? `<p class="alert error" role="alert">${esc(this._t(zone.sensor_issue))}. ${this._t(zone.sensor_fault ? "Zone fans off" : "Using the last good reading temporarily.")}${model.mode !== "Adaptive" ? ` ${this._t("Mode bypasses water", {mode:this._t(model.mode)})}` : ""}</p>` : ""}
            <details class="diagnostics" data-panel="water-${esc(zone.id)}" ${this._open.has(`water-${zone.id}`) ? "open" : ""}><summary>${this._t("Sensor readings & details")}</summary><p>${this._t("Raw")} ${this._degrees(zone.raw_temperature)} · ${this._t("accepted")} ${this._degrees(zone.water_temperature)}</p>${zone.sensor_issue ? `<p class="error">${esc(this._t(zone.sensor_issue))}</p>` : ""}<p>${this._t("Raw report")} <span data-age="${zone.raw_reported_at || 0}"></span> · ${this._t("last accepted")} <span data-age="${zone.accepted_reported_at || 0}"></span></p><p>${this._t("Rejected readings since startup")}: ${zone.rejected_readings || 0}</p><a class="device-settings" href="${esc(zone.configuration_url || '/config/entities')}">${this._t("Shelly settings ↗")}</a></details>
            <div class="room-list">${members.length ? members.map(room => this._room(room)).join("") : `<div class="empty">${this._t("No rooms instruction")}</div>`}</div></section>`;
        }).join("")}</div>
        <p class="footer">${this._t("Footer")}</p>
      </ha-card>`;
    this._bind(model);
    this._clock();
  }

  _bind(model) {
    this.shadowRoot.querySelector("[data-language]").onchange = event => {
      this._language = event.target.value;
      try { localStorage.setItem(`central-heating-language:${this._config.entity}`, this._language); } catch (_) { /* Keep the selection for this session. */ }
      this._render(true);
    };
    this.shadowRoot.querySelectorAll("details[data-panel]").forEach(panel => {
      panel.ontoggle = () => panel.open ? this._open.add(panel.dataset.panel) : this._open.delete(panel.dataset.panel);
    });
    this.shadowRoot.querySelector("[data-add-room]").onclick = () => {
      this._open.add("add-room");
      const guide = this.shadowRoot.querySelector('[data-panel="add-room"]');
      guide.open = true;
      guide.scrollIntoView({behavior: "smooth", block: "nearest"});
    };
    this.shadowRoot.querySelectorAll("[data-mode]").forEach(button => {
      button.onclick = () => this._call("select", "select_option", {entity_id: model.controls.mode, option: button.dataset.mode});
    });
    this.shadowRoot.querySelectorAll("[data-number]").forEach(input => {
      const save = () => {
        if (!input.reportValidity()) return;
        if (input.dataset.draft) this._draft(input.closest("[data-room]")).target = Number(input.value);
        else this._call("number", "set_value", {entity_id: input.dataset.number, value: Number(input.value)});
      };
      input.oninput = () => { if (input.dataset.draft) this._draft(input.closest("[data-room]")).target = Number(input.value); };
      input.onchange = save;
      input.onblur = () => setTimeout(() => this._render(), 0);
      input.closest(".stepper").querySelectorAll("[data-step]").forEach(button => {
        button.onclick = () => {
          input.value = Math.min(Number(input.max), Math.max(Number(input.min), Number((Number(input.value) + Number(button.dataset.step)).toFixed(1))));
          save();
        };
      });
    });
    this.shadowRoot.querySelectorAll("[data-room]").forEach(article => {
      const room = model.rooms.find(room => room.key === article.dataset.room);
      const select = article.querySelector("[data-duration]");
      select.onchange = () => { this._draft(article).duration = select.value; };
      select.onblur = () => setTimeout(() => this._render(), 0);
      article.querySelector("[data-apply]").onclick = async () => {
        const input = article.querySelector("[data-draft]");
        if (!input.reportValidity()) return;
        const success = await this._call("central_heating", "set_room_override", {entity_id: room.entity_id, target: Number(input.value), duration: select.value});
        if (success) { this._drafts.delete(room.key); this._render(true); }
      };
      const cancel = article.querySelector("[data-cancel]");
      if (cancel) cancel.onclick = () => this._call("switch", "turn_off", {entity_id: cancel.dataset.cancel});
    });
  }

  _draft(article) {
    if (!this._drafts.has(article.dataset.room)) this._drafts.set(article.dataset.room, {
      target: Number(article.querySelector("[data-draft]").value), duration: article.querySelector("[data-duration]").value,
    });
    return this._drafts.get(article.dataset.room);
  }

  _clock() {
    if (!this.shadowRoot) return;
    const now = Date.now() / 1000;
    this.shadowRoot.querySelectorAll("[data-expires]").forEach(element => {
      const remaining = Math.ceil(Number(element.dataset.expires) - now);
      element.textContent = remaining <= 0 ? this._t("Ending · restoring central target") : this._t("{time} remaining", {time:remaining < 60 ? `${remaining} s` : `${Math.ceil(remaining / 60)} min`});
    });
    this.shadowRoot.querySelectorAll("[data-age]").forEach(element => {
      const timestamp = Number(element.dataset.age);
      const age = Math.max(0, Math.floor(now - timestamp));
      element.textContent = !timestamp ? this._t("unavailable") : this._t("{time} ago", {time:age < 60 ? `${age} s` : age < 3600 ? `${Math.floor(age / 60)} min` : `${Math.floor(age / 3600)} h`});
    });
  }
}

if (!customElements.get("central-heating-card")) customElements.define("central-heating-card", CentralHeatingCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(card => card.type === "central-heating-card")) window.customCards.push({type: "central-heating-card", name: "Central Heating", description: "Heating zones, timed room overrides, and sensor quality"});
})();
