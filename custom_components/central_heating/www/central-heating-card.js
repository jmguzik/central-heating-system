/* Central heating dashboard; all changes use native Home Assistant services. */
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]));
const degrees = (value) => value == null ? "Unavailable" : `${Number(value).toFixed(1)}°C`;

class CentralHeatingCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("Select the central heating status entity");
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({mode: "open"});
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const model = hass.states[this._config?.entity]?.attributes;
    const signature = JSON.stringify(model);
    if (signature === this._signature) return;
    this._signature = signature;
    const focused = this.shadowRoot?.activeElement;
    if (focused?.matches('input[type="number"]')) return;
    this._render();
  }

  getCardSize() { return 10; }

  async _call(domain, service, data) {
    try {
      this._error = null;
      await this._hass.callService(domain, service, data);
    } catch (error) {
      this._error = error.message || String(error);
    }
    this._render();
  }

  _number(key, label, value, minimum, maximum, step, entity) {
    return `<label class="setting"><span>${esc(label)}</span><div class="number-wrap"><input type="number" aria-label="${esc(label)}" data-number="${esc(entity || "")}" data-key="${esc(key)}" min="${minimum}" max="${maximum}" step="${step}" value="${esc(value)}" ${entity ? "" : "disabled"}><span>°C</span></div></label>`;
  }

  _room(room) {
    const fault = room.error || !room.confirmed || room.reported_mode === "unavailable";
    const speed = room.reported_mode === "off" ? "Off" : room.reported_mode === "fan_only" ? room.reported_fan || "Fan" : room.reported_mode;
    return `<article class="room ${fault ? "unconfirmed" : ""}">
      <div class="room-head"><h3>${esc(room.name)}</h3><span class="pill ${room.reported_mode === "fan_only" ? "running" : ""}">${esc(speed)}</span></div>
      <div class="room-temperature">${degrees(room.temperature)}</div>
      <p class="reason">${esc(room.reason)}${!room.confirmed ? " · awaiting confirmation" : ""}</p>
      ${room.error ? `<p class="error">${esc(room.error)}</p>` : ""}
      <div class="target-summary"><span>Target <strong>${degrees(room.effective_target)}</strong></span><span class="source">${esc(room.target_source)}</span></div>
      <div class="small">Medium below ${degrees(room.medium_below)}</div>
      <label class="override"><span>Use custom target</span><input type="checkbox" role="switch" aria-label="Custom target for ${esc(room.name)}" data-override="${esc(room.controls?.override || "")}" ${room.override ? "checked" : ""} ${room.controls?.override ? "" : "disabled"}></label>
      ${room.override ? this._number("room_target", `Custom target · ${room.name}`, room.custom_target, 5, 35, 0.5, room.controls?.target) : ""}
    </article>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const model = this._hass?.states[this._config.entity]?.attributes;
    if (!model?.settings || !model?.zones) {
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:24px">Central heating is starting. If this persists, check the integration in Settings.</div></ha-card>`;
      return;
    }
    const settings = model.settings;
    const controls = model.controls || {};
    const rooms = model.rooms || [];
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);font-family:var(--ha-font-family,inherit)}
        *{box-sizing:border-box}ha-card{padding:28px;border-radius:20px;max-width:1180px;margin:12px auto;background:var(--ha-card-background,var(--card-background-color))}
        h1,h2,h3,p{margin:0}h1{font-size:28px;font-weight:650;letter-spacing:-.5px}h2{font-size:20px;font-weight:600}h3{font-size:16px;font-weight:600}
        .hero{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px}.subtitle,.small{color:var(--secondary-text-color);font-size:13px;line-height:1.5}.subtitle{margin-top:6px}
        .count{white-space:nowrap;background:var(--secondary-background-color);border-radius:12px;padding:12px 16px;font-size:14px}
        .modes{display:flex;gap:8px;margin-bottom:12px}.modes button{font:inherit;min-height:46px;border:1px solid var(--divider-color);border-radius:10px;padding:10px 24px;background:transparent;color:var(--primary-text-color);cursor:pointer}.modes button.active{background:var(--primary-color);border-color:var(--primary-color);color:var(--text-primary-color,#fff)}button:focus-visible,input:focus-visible{outline:2px solid var(--primary-color);outline-offset:3px}
        .explanation{font-size:13px;color:var(--secondary-text-color);line-height:1.6;margin-bottom:22px}
        .settings{display:grid;grid-template-columns:repeat(5,minmax(110px,1fr));gap:12px;padding:16px;border:1px solid var(--divider-color);border-radius:14px;margin-bottom:28px}
        .setting{display:block;font-size:12px;line-height:1.5}.number-wrap{display:flex;align-items:center;gap:6px;margin-top:6px}.number-wrap input{font:inherit;font-size:16px;background:var(--card-background-color);color:var(--primary-text-color);border:1px solid var(--divider-color);border-radius:8px;padding:9px;min-width:0;width:85px;min-height:42px}.number-wrap span{font-size:12px;color:var(--secondary-text-color)}
        .zones{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}.zone{min-width:0}.zone-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:14px}.water{margin-top:5px;color:var(--secondary-text-color);font-size:14px}.zone-badge{font-size:11px;white-space:nowrap;border-radius:20px;padding:6px 10px;background:var(--secondary-background-color)}.zone-badge.ready{color:var(--success-color,#219653)}
        .room-list{display:grid;gap:12px}.room{padding:18px;border:1px solid var(--divider-color);border-radius:14px}.room.unconfirmed{border-color:var(--warning-color,#dba32a)}.room-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.pill{font-size:12px;background:var(--secondary-background-color);border-radius:20px;padding:5px 10px;text-transform:capitalize}.pill.running{background:color-mix(in srgb,var(--primary-color) 15%,transparent);color:var(--primary-color)}
        .room-temperature{font-size:31px;letter-spacing:-1px;font-weight:550;margin:12px 0 4px}.reason{font-size:13px;color:var(--secondary-text-color);margin-bottom:16px}.target-summary{display:flex;align-items:center;justify-content:space-between;font-size:13px;margin-bottom:3px}.source{font-size:11px;color:var(--secondary-text-color);border:1px solid var(--divider-color);border-radius:5px;padding:2px 6px}.override{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px;border-top:1px solid var(--divider-color);margin-top:16px;padding-top:14px;min-height:40px;cursor:pointer}.override input{width:21px;height:21px;accent-color:var(--primary-color)}.room .setting{margin-top:10px}
        .empty{border:1px dashed var(--divider-color);border-radius:14px;padding:20px;color:var(--secondary-text-color);font-size:14px}.error{color:var(--error-color,#db4437);font-size:13px;line-height:1.5}.alert{padding:12px;margin-bottom:16px;background:var(--secondary-background-color);border-radius:8px}.footer{margin-top:24px;padding-top:16px;border-top:1px solid var(--divider-color);font-size:12px;color:var(--secondary-text-color);line-height:1.6}.footer a{color:var(--primary-color)}
        @media(max-width:800px){ha-card{padding:20px}.settings{grid-template-columns:repeat(3,minmax(0,1fr))}.zones{grid-template-columns:1fr}.hero{align-items:flex-start}.count{font-size:12px;padding:10px}h1{font-size:24px}}
        @media(max-width:450px){ha-card{padding:16px;margin:4px auto}.settings{grid-template-columns:repeat(2,minmax(0,1fr));padding:12px}.modes button{flex:1;padding:10px}.hero{display:block}.count{display:inline-block;margin-top:12px}.zone-head{flex-wrap:wrap}}
      </style>
      <ha-card>
        <div class="hero"><div><h1>Central heating</h1><p class="subtitle">Two water zones · individual room control</p></div><div class="count">${model.running_fans || 0} / ${rooms.length} fans running</div></div>
        ${this._error ? `<p class="error alert" role="alert">${esc(this._error)}</p>` : ""}
        ${model.startup_fault ? `<p class="error alert" role="alert">${esc(model.startup_fault)}</p>` : ""}
        <div class="modes" role="group" aria-label="Heating mode">${["Off", "Manual", "Adaptive"].map(mode => `<button data-mode="${mode}" class="${model.mode === mode ? "active" : ""}" aria-pressed="${model.mode === mode}">${mode}</button>`).join("")}</div>
        <p class="explanation">${model.mode === "Off" ? "All managed fans are off." : model.mode === "Manual" ? "Fans run at low speed. The room temperature limit still applies." : "Fans run while heating water is warm. Cold rooms use medium speed; rooms near their target use low."} Reaching the target reduces speed; the room maximum stops the fan.</p>
        <div class="settings">
          ${this._number("target", "Central target", settings.target, 5, 35, 0.5, controls.target)}
          ${this._number("water_on", "Water ON", settings.water_on, 0, 100, 0.5, controls.water_on)}
          ${this._number("water_off", "Water OFF", settings.water_off, 0, 100, 0.5, controls.water_off)}
          ${this._number("room_maximum", "Room maximum", settings.room_maximum, 5, 35, 0.5, controls.room_maximum)}
          ${this._number("room_hysteresis", "Room restart gap", settings.room_hysteresis, 0.1, 5, 0.1, controls.room_hysteresis)}
        </div>
        <div class="zones">${model.zones.map(zone => {
          const members = rooms.filter(room => room.zone_ids.includes(zone.id));
          const text = zone.label_missing ? "Zone label missing" : model.mode === "Manual" ? "Water rule bypassed" : model.mode === "Off" ? "System off" : zone.allowed ? "Water ready" : "Waiting for warm water";
          return `<section class="zone"><div class="zone-head"><div><h2>${esc(zone.name)}</h2><div class="water">Water ${degrees(zone.water_temperature)}</div></div><span class="zone-badge ${zone.allowed ? "ready" : ""}">${text}</span></div><div class="room-list">${members.length ? members.map(room => this._room(room)).join("") : '<div class="empty">No rooms assigned to this zone.</div>'}</div></section>`;
        }).join("")}</div>
        <div class="footer">Adaptive medium speed applies below each room’s target minus 1°C. Rooms restart at ${degrees(settings.room_maximum - settings.room_hysteresis)} after reaching the maximum.<br>Add a room in <a href="/config/entities">Devices &amp; services</a>: give its climate entity exactly one label, <strong>heating_poddasze</strong> or <strong>heating_piwnica</strong>. Its controls will appear automatically.</div>
      </ha-card>`;
    this.shadowRoot.querySelectorAll("[data-mode]").forEach(button => {
      button.onclick = () => this._call("select", "select_option", {entity_id: controls.mode, option: button.dataset.mode});
    });
    this.shadowRoot.querySelectorAll("[data-number]").forEach(input => {
      input.onchange = () => {
        if (!input.reportValidity()) return;
        this._call("number", "set_value", {entity_id: input.dataset.number, value: Number(input.value)});
      };
      input.onblur = () => setTimeout(() => this._render(), 0);
    });
    this.shadowRoot.querySelectorAll("[data-override]").forEach(input => {
      input.onchange = () => this._call("switch", input.checked ? "turn_on" : "turn_off", {entity_id: input.dataset.override});
    });
  }
}

if (!customElements.get("central-heating-card")) customElements.define("central-heating-card", CentralHeatingCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(card => card.type === "central-heating-card")) {
  window.customCards.push({type: "central-heating-card", name: "Central Heating", description: "Zone controls and room target overrides"});
}
