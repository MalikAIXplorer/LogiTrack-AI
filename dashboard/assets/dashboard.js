(() => {
  "use strict";
  const GROUPS = [
    { title: "Shipment Information", icon: "▤", fields: ["shipment_priority", "shipment_weight_kg", "weight_missing"] },
    { title: "Carrier and Service", icon: "▱", fields: ["service_mode", "service_target_transit_hours", "service_reference_missing", "carrier_type", "carrier_reference_missing", "carrier_historical_on_time_rate", "carrier_historical_delivery_count", "carrier_history_missing"] },
    { title: "Route and Journey", icon: "⌁", fields: ["journey_elapsed_minutes", "planned_route_distance_km", "planned_route_hours", "planned_leg_count", "remaining_scheduled_leg_count", "remaining_scheduled_hours", "remaining_scheduled_distance_km"] },
    { title: "Tracking Events", icon: "◷", fields: ["eligible_event_count", "latest_event_type", "latest_event_missing", "last_observed_state", "latest_event_age_minutes", "latest_event_leg_number", "pickup_scan_seen", "minutes_since_pickup_scan"] },
    { title: "Weather Conditions", icon: "☁", fields: ["weather_observation_count", "active_weather_disruption_count", "max_active_weather_severity", "forecast_weather_count", "max_forecast_weather_severity", "forecast_precipitation_mm", "forecast_wind_speed_kph", "forecast_weather_missing", "weather_observation_missing"] },
    { title: "Facility Queue Conditions", icon: "▥", fields: ["queue_observation_count", "max_observed_queue_length", "mean_observed_wait_minutes", "queue_wait_missing", "queue_observation_missing"] },
  ];
  const LABELS = {
    service_mode: "Service mode", service_target_transit_hours: "Service target transit", service_reference_missing: "Service reference missing",
    carrier_type: "Carrier type", carrier_reference_missing: "Carrier reference missing", carrier_historical_on_time_rate: "Historical on-time rate",
    carrier_historical_delivery_count: "Historical delivery count", carrier_history_missing: "Carrier history missing",
    shipment_priority: "Shipment priority", shipment_weight_kg: "Shipment weight", weight_missing: "Weight missing",
    journey_elapsed_minutes: "Journey elapsed time", planned_route_distance_km: "Planned route distance", planned_route_hours: "Planned route duration",
    planned_leg_count: "Planned leg count", remaining_scheduled_leg_count: "Remaining scheduled legs",
    remaining_scheduled_hours: "Remaining scheduled time", remaining_scheduled_distance_km: "Remaining scheduled distance",
    eligible_event_count: "Eligible event count", latest_event_type: "Latest event type", latest_event_missing: "Latest event missing",
    last_observed_state: "Last observed state", latest_event_age_minutes: "Latest event age", latest_event_leg_number: "Latest event leg number",
    pickup_scan_seen: "Pickup scan seen", minutes_since_pickup_scan: "Time since pickup scan",
    weather_observation_count: "Weather observations", active_weather_disruption_count: "Active disruptions",
    max_active_weather_severity: "Observed weather severity", forecast_weather_count: "Available forecasts",
    max_forecast_weather_severity: "Forecast severity", forecast_precipitation_mm: "Forecast precipitation",
    forecast_wind_speed_kph: "Forecast wind speed", forecast_weather_missing: "Forecast unavailable",
    weather_observation_missing: "Weather observation missing", queue_observation_count: "Queue observations",
    max_observed_queue_length: "Observed queue length", mean_observed_wait_minutes: "Estimated facility wait",
    queue_wait_missing: "Queue wait unavailable", queue_observation_missing: "Queue observation missing",
  };
  const UNITS = {
    service_target_transit_hours: "hours", shipment_weight_kg: "kg", carrier_historical_on_time_rate: "0–1 rate",
    journey_elapsed_minutes: "minutes", planned_route_distance_km: "km", planned_route_hours: "hours",
    remaining_scheduled_hours: "hours", remaining_scheduled_distance_km: "km", latest_event_age_minutes: "minutes",
    minutes_since_pickup_scan: "minutes", max_active_weather_severity: "0–5 scale", max_forecast_weather_severity: "0–5 scale",
    forecast_precipitation_mm: "mm", forecast_wind_speed_kph: "km/h", mean_observed_wait_minutes: "minutes",
  };
  const HINTS = {
    carrier_historical_on_time_rate: "Historical carrier performance available before this prediction.",
    forecast_weather_missing: "One means no eligible forecast was available at prediction time.",
    weather_observation_missing: "One means no weather observation was available at prediction time.",
    queue_wait_missing: "One means an estimated queue wait was unavailable.",
    queue_observation_missing: "One means no eligible queue observation was available.",
  };
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const state = { metadata: null, demo: null };

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
  function humanize(value) { return value.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase()); }
  function setPage(page) {
    const titles = { overview: "Overview", prediction: "ETA Prediction", model: "Model Information", system: "System Status" };
    $$(".page-section").forEach(el => el.classList.toggle("active", el.id === "page-" + page));
    $$(".nav-link").forEach(el => el.classList.toggle("active", el.dataset.pageLink === page));
    $("#page-title").textContent = titles[page] || titles.overview;
    $("#page-crumb").textContent = titles[page] || titles.overview;
    $("#sidebar").classList.remove("open");
    $("#mobile-scrim").classList.remove("visible");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  async function getJson(path) {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.status));
    return body;
  }
  function fieldControl(name) {
    const type = state.metadata.predictor_types[name];
    const nullable = Number(state.metadata.missing_value_counts?.[name] || 0) > 0;
    const label = LABELS[name] || humanize(name);
    const id = "feature-" + name;
    if (type === "boolean") {
      return '<div class="field switch-field" data-field="' + name + '"><div class="switch-copy"><strong>' + label + '</strong><small>Binary operational indicator</small></div><label class="switch" aria-label="' + label + '"><input id="' + id + '" name="' + name + '" type="checkbox" role="switch"><span class="switch-track"></span></label></div>';
    }
    if (type === "category") {
      const options = state.metadata.categories[name] || [];
      const nullOption = nullable ? '<option value="__null__">Not observed</option>' : "";
      return '<div class="field" data-field="' + name + '"><label for="' + id + '"><span>' + label + '</span><span class="field-unit">' + (nullable ? "optional" : "required") + '</span></label><select id="' + id + '" name="' + name + '" ' + (nullable ? "" : "required") + '><option value="">Select ' + label.toLowerCase() + '</option>' + nullOption + options.map(v => '<option value="' + escapeHtml(v) + '">' + escapeHtml(humanize(v)) + '</option>').join("") + '</select><small class="field-hint">Choose a category represented in the fitted model.</small></div>';
    }
    const integer = type === "integer";
    const max = name.includes("severity") ? "5" : name === "carrier_historical_on_time_rate" ? "1" : "";
    const unit = UNITS[name] || (integer ? "count" : "numeric");
    const required = !nullable;
    return '<div class="field" data-field="' + name + '"><label for="' + id + '"><span>' + label + '</span><span class="field-unit">' + unit + (required ? "" : " · optional") + '</span></label><input id="' + id + '" name="' + name + '" type="number" step="' + (integer ? "1" : "any") + '" min="0" ' + (max ? 'max="' + max + '" ' : "") + (required ? "required " : "") + 'inputmode="decimal" placeholder="' + (nullable ? "Leave blank if unavailable" : "Enter value") + '"><small class="field-hint">' + (HINTS[name] || (integer ? "Whole number." : "Use a finite numeric value.")) + '</small></div>';
  }
  function renderFields() {
    const fields = GROUPS.flatMap(group => group.fields);
    const expected = state.metadata.predictor_columns;
    if (new Set(fields).size !== fields.length || fields.length !== expected.length || expected.some(n => !fields.includes(n))) {
      throw new Error("Dashboard groups do not match the saved 40-field model contract.");
    }
    $("#feature-groups").innerHTML = GROUPS.map((group, i) =>
      '<details class="feature-group" ' + (i === 0 ? "open" : "") + '><summary><span class="chevron">›</span><span class="group-icon">' + group.icon + '</span><span>' + group.title + '</span><span class="group-count">' + group.fields.length + ' fields</span></summary><div class="field-grid">' +
      group.fields.map(fieldControl).join("") + '</div></details>'
    ).join("");
  }
  async function refreshHealth() {
    const chip = $("#api-status");
    try {
      const health = await getJson("/health");
      const good = health.status === "ok" && health.model_loaded === true;
      chip.dataset.state = good ? "healthy" : "error";
      $(".status-copy", chip).textContent = good ? "API connected" : "Model unavailable";
      $("#health-dot").className = "large-status-dot " + (good ? "healthy" : "error");
      $("#health-label").textContent = good ? "Healthy · model loaded" : "Degraded · model unavailable";
      $("#health-description").textContent = good ? "GET /health confirms that the saved model is loaded." : (health.load_error || "The API is responding, but the model is unavailable.");
      $("#health-icon").textContent = good ? "✓" : "!";
    } catch (error) {
      chip.dataset.state = "error";
      $(".status-copy", chip).textContent = "API unavailable";
      $("#health-dot").className = "large-status-dot error";
      $("#health-label").textContent = "Connection unavailable";
      $("#health-description").textContent = error.message || "Unable to reach GET /health.";
      $("#health-icon").textContent = "!";
    }
  }
  function renderModelInfo(info) {
    const period = info.training_period_utc;
    const rows = [["Backend model", info.model_name], ["Target", info.target + " (" + info.target_units + ")"],
      ["Expected predictors", String(info.expected_predictor_count)], ["Training dataset", info.training_dataset],
      ["Training samples", Number(info.training_rows).toLocaleString()],
      ["Training period", period ? period.min + " → " + period.max : "Not supplied"]];
    $("#model-metadata").innerHTML = rows.map(pair => '<div class="metadata-item"><span>' + escapeHtml(pair[0]) + '</span><strong>' + escapeHtml(pair[1]) + '</strong></div>').join("");
  }
  async function refreshSystem() {
    await refreshHealth();
    try { renderModelInfo(await getJson("/model-info")); }
    catch (error) { $("#model-metadata").innerHTML = '<div class="metadata-loading error-text">' + escapeHtml(error.message) + '</div>'; }
  }
  async function loadMetadata() {
    try { state.metadata = await getJson("/dashboard/metadata"); renderFields(); }
    catch (error) {
      $("#feature-groups").innerHTML = '<div class="form-error">' + escapeHtml(error.message) + '</div>';
      $("#load-demo").disabled = true; $("#submit-prediction").disabled = true;
    }
  }
  function setDemoStatus(loaded, title, detail) {
    $(".demo-label").dataset.loaded = loaded ? "true" : "false";
    $("#sample-status").textContent = title; $("#sample-detail").textContent = detail;
  }
  function fillForm(values) {
    Object.entries(values).forEach(([name, value]) => {
      const input = document.querySelector('[name="' + CSS.escape(name) + '"]');
      if (!input) return;
      if (input.type === "checkbox") input.checked = value === true || value === 1;
      else if (input.tagName === "SELECT") {
        const desired = value === null ? "__null__" : String(value);
        input.value = [...input.options].some(option => option.value === desired) ? desired : "";
      } else input.value = value === null ? "" : String(value);
    });
  }
  function clearError() { const el = $("#form-error"); el.hidden = true; el.textContent = ""; }
  function showError(message) { const el = $("#form-error"); el.textContent = message; el.hidden = false; el.scrollIntoView({ behavior: "smooth", block: "center" }); }
  async function loadDemo() {
    const button = $("#load-demo"); button.disabled = true; button.textContent = "Loading sample…";
    try {
      const sample = await getJson("/dashboard/demo");
      fillForm(sample.features); state.demo = sample.features;
      setDemoStatus(true, "Synthetic demo features loaded", "An actual row from data/realistic_v2. Edit values before predicting.");
      clearError(); $("#prediction-result").hidden = true;
    } catch (error) {
      showError("Demo sample unavailable: " + error.message);
      setDemoStatus(false, "Demo sample unavailable", "Enter a feature snapshot manually.");
    } finally { button.disabled = false; button.innerHTML = "<span>▣</span> Load Demo Shipment"; }
  }
  function resetForm() {
    $("#prediction-form").reset();
    $$(".field.invalid").forEach(el => el.classList.remove("invalid"));
    $$(".field-error").forEach(el => el.remove());
    state.demo = null; $("#prediction-result").hidden = true; clearError();
    setDemoStatus(false, "No sample loaded", "Load a real feature row from the realistic_v2 dataset or enter your own snapshot.");
  }
  function collectPayload() {
    const payload = {}; let firstInvalid = null;
    for (const name of state.metadata.predictor_columns) {
      const input = document.querySelector('[name="' + CSS.escape(name) + '"]');
      const wrapper = input.closest(".field"); wrapper.classList.remove("invalid");
      const oldError = $(".field-error", wrapper); if (oldError) oldError.remove();
      const type = state.metadata.predictor_types[name]; let value, error = "";
      if (type === "boolean") value = input.checked ? 1 : 0;
      else if (type === "category") {
        value = input.value === "__null__" || input.value === "" ? null : input.value;
        if (input.required && value === null) error = "Choose a category.";
      } else if (!input.value.trim()) {
        value = null; if (input.required) error = "Enter a value.";
      } else {
        value = Number(input.value);
        if (!Number.isFinite(value)) error = "Enter a finite number.";
        else if (input.min && value < Number(input.min)) error = "Must be at least " + input.min + ".";
        else if (input.max && value > Number(input.max)) error = "Must be no greater than " + input.max + ".";
        else if (type === "integer" && !Number.isInteger(value)) error = "Enter a whole number.";
      }
      if (error) {
        wrapper.classList.add("invalid"); const message = document.createElement("small");
        message.className = "field-error"; message.textContent = error; wrapper.append(message); firstInvalid ||= input;
      }
      payload[name] = value;
    }
    if (firstInvalid) { firstInvalid.focus(); return null; }
    return payload;
  }
  function showResult(result, payload) {
    const hours = Number(result.predicted_remaining_hours);
    if (!Number.isFinite(hours)) throw new Error("The API returned a non-numeric ETA estimate.");
    const total = Math.max(0, Math.round(hours * 60));
    $("#result-hours").textContent = hours.toFixed(2);
    $("#result-conversion").textContent = Math.floor(total / 60) + " hr " + String(total % 60).padStart(2, "0") + " min";
    const priority = payload.shipment_priority || "Unspecified";
    const mode = payload.service_mode || "Unspecified";
    const distance = payload.remaining_scheduled_distance_km;
    $("#result-summary").textContent = humanize(priority) + " priority · " + humanize(mode) + " service · " +
      (distance == null ? "remaining distance unavailable" : Number(distance).toLocaleString() + " km remaining");
    $("#prediction-result").hidden = false; $("#prediction-result").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  async function submitPrediction(event) {
    event.preventDefault(); clearError();
    const payload = collectPayload(); if (!payload) return;
    const button = $("#submit-prediction"); button.disabled = true;
    button.innerHTML = '<span class="spinner" aria-hidden="true"></span> Calculating…'; $("#prediction-result").hidden = true;
    try {
      const response = await fetch("/predict", { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(payload) });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : JSON.stringify(result.detail || "Prediction request failed."));
      showResult(result, payload);
    } catch (error) { showError(error.message || "Prediction failed. Check the API connection and try again."); }
    finally { button.disabled = false; button.innerHTML = '<span class="button-icon">⌁</span> Generate ETA <span class="arrow">→</span>'; }
  }
  function wireNavigation() {
    $$("[data-page-link]").forEach(link => link.addEventListener("click", event => {
      event.preventDefault(); const page = link.dataset.pageLink; setPage(page); history.replaceState(null, "", "#" + page);
    }));
    const initial = location.hash.slice(1); if (initial && $("#page-" + CSS.escape(initial))) setPage(initial);
    $("#menu-toggle").addEventListener("click", () => {
      const open = $("#sidebar").classList.toggle("open"); $("#mobile-scrim").classList.toggle("visible", open);
      $("#menu-toggle").setAttribute("aria-expanded", String(open));
    });
    $("#mobile-scrim").addEventListener("click", () => {
      $("#sidebar").classList.remove("open"); $("#mobile-scrim").classList.remove("visible"); $("#menu-toggle").setAttribute("aria-expanded", "false");
    });
  }
  async function boot() {
    wireNavigation();
    $("#load-demo").addEventListener("click", loadDemo); $("#reset-form").addEventListener("click", resetForm);
    $("#reset-form-bottom").addEventListener("click", resetForm); $("#prediction-form").addEventListener("submit", submitPrediction);
    $("#refresh-status").addEventListener("click", refreshSystem); $$("[data-refresh]").forEach(button => button.addEventListener("click", refreshHealth));
    await Promise.all([loadMetadata(), refreshSystem()]);
  }
  document.addEventListener("DOMContentLoaded", boot);
})();
