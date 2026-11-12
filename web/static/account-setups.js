(function () {
  "use strict";

  function validShape(setup) {
    if (!setup || typeof setup !== "object" || Array.isArray(setup)) return false;
    const fields = ["id", "name", "account", "strategy", "exchange", "budget", "drawdown"];
    return Object.keys(setup).length === fields.length && fields.every((key) => Object.hasOwn(setup, key)) &&
      typeof setup.id === "string" && setup.id.length > 0 && setup.id.length <= 80 &&
      typeof setup.name === "string" && setup.name.trim().length > 0 && setup.name.length <= 80 &&
      ["account", "strategy", "exchange"].every((key) => typeof setup[key] === "string") &&
      setup.strategy.length > 0 && setup.exchange.length > 0 &&
      typeof setup.budget === "number" && Number.isFinite(setup.budget) && setup.budget > 0 &&
      typeof setup.drawdown === "number" && Number.isFinite(setup.drawdown) && setup.drawdown > 0 && setup.drawdown <= 100;
  }

  function parseSaved(raw) {
    if (raw === null) return [];
    const setups = JSON.parse(raw);
    if (!Array.isArray(setups) || !setups.every(validShape) || new Set(setups.map((s) => s.id)).size !== setups.length) {
      throw new Error("Saved research setups have an invalid format.");
    }
    return setups;
  }

  function storageUnchanged(loaded, current) {
    return loaded !== undefined && loaded === current;
  }

  function validate(setup, research) {
    if (!setup.name.trim() || setup.name.length > 80) return "Enter a setup name of 1 to 80 characters.";
    if (!Number.isFinite(setup.budget) || setup.budget <= 0) return "Enter a hypothetical budget greater than zero.";
    if (!Number.isFinite(setup.drawdown) || setup.drawdown <= 0 || setup.drawdown > 100) return "Enter a drawdown greater than zero and at most 100 percent.";
    const strategy = research.strategies.find((s) => s.name === setup.strategy);
    if (!strategy) return "Choose a currently available strategy.";
    if (!strategy.venues.includes(setup.exchange)) return "Choose a currently available venue for this strategy.";
    if (setup.account && !research.accounts.some((a) => a.id === setup.account)) return "Choose a current observed account, or remove the account reference.";
    return "";
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {validShape, parseSaved, validate, storageUnchanged};
  if (typeof document === "undefined") return;
  const metadata = document.getElementById("research-data");
  if (!metadata) return;
  const research = JSON.parse(metadata.textContent);
  const byId = (id) => document.getElementById(id);
  const form = byId("setup-form");
  const fields = Object.fromEntries(["name", "account", "strategy", "exchange", "budget", "drawdown"].map((key) => [key, byId("setup-" + key)]));
  const key = "trader.research-setups.v1." + encodeURIComponent(research.user);
  let setups = [];
  let editing = null;
  let corrupted = false;
  let evidence = null;
  let evidenceRequest = 0;

  function option(select, value, label) {
    const item = document.createElement("option");
    item.value = value;
    item.textContent = label;
    select.append(item);
  }

  research.accounts.forEach((a) => option(fields.account, a.id, a.label));
  research.strategies.forEach((s) => option(fields.strategy, s.name, s.name));
  byId("setup-editor").hidden = false;

  function status(message) {
    byId("setup-status").textContent = message;
  }

  function obsoleteOption(select, value) {
    if (value && !Array.from(select.options).some((o) => o.value === value)) {
      option(select, value, value + " (no longer available)");
      select.lastElementChild.dataset.obsolete = "true";
    }
    select.value = value;
  }

  function clearObsolete() {
    form.querySelectorAll("option[data-obsolete]").forEach((item) => item.remove());
  }

  function percent(value) {
    return value === null || value === undefined ? "not available" : (value * 100).toFixed(2) + "%";
  }

  function showRisk() {
    const limit = Number(fields.drawdown.value);
    byId("setup-risk").textContent = evidence && evidence.max_drawdown !== null &&
      evidence.max_drawdown !== undefined && limit > 0 && limit <= 100 && evidence.max_drawdown * 100 > limit
      ? "Observed historical drawdown exceeds your " + limit + "% preference. Saving this setup does not limit losses."
      : "";
  }

  async function showEvidence() {
    const request = ++evidenceRequest;
    evidence = null;
    showRisk();
    const strategy = research.strategies.find((s) => s.name === fields.strategy.value);
    byId("setup-method").hidden = !strategy;
    if (strategy) {
      byId("setup-description").textContent = "Family: " + strategy.family + ". " + strategy.description;
      byId("setup-inputs").textContent = "Inputs: " + (strategy.inputs.join(", ") || "Not classified") + ". Family descriptions explain the method, not each strategy's exact entry and exit rules.";
      byId("setup-note").textContent = strategy.note || "";
      byId("setup-strategy-link").href = "/strategies/" + encodeURIComponent(strategy.name) + "?exchange=" + encodeURIComponent(fields.exchange.value);
    }
    if (!strategy || !strategy.venues.includes(fields.exchange.value)) {
      byId("setup-evidence").textContent = strategy && !strategy.venues.length
        ? "No venue has been observed for this strategy yet. Analysis must record a venue before this setup can be saved."
        : "Choose a currently available strategy and venue to see historical results.";
      return;
    }
    byId("setup-evidence").textContent = "Loading historical results...";
    try {
      const params = new URLSearchParams({strategy: strategy.name, exchange: fields.exchange.value});
      const response = await fetch("/accounts/evidence?" + params, {headers: {Accept: "application/json"}});
      if (!response.ok || response.redirected) throw new Error("Evidence unavailable");
      const found = await response.json();
      if (request !== evidenceRequest) return;
      evidence = found;
      const dates = found.start_day ? found.start_day + " to " + (found.end_day || "unknown end date") : "dates not available";
      const results = found.available
        ? "Historical " + found.window + " results for " + found.strategy + " on " + found.exchange + ": " + dates + "; " + found.trades + " trades; return " + percent(found.total_return) + "; maximum drawdown " + percent(found.max_drawdown) + ". "
        : "No historical results for " + found.strategy + " on " + found.exchange + " in the " + found.window + " window. ";
      byId("setup-evidence").textContent = results + "Lookahead bias: " + found.bias + ". Recursive stability: " + found.stability + ". Confidence: " + found.confidence + ". Last computed: " + (found.computed_at || "not available") + ".";
      showRisk();
    } catch (error) {
      if (request === evidenceRequest) byId("setup-evidence").textContent = "Could not load historical results. Reload, or sign in again if your session expired. Your setup fields are unchanged.";
    }
  }

  function venues(selected = "") {
    fields.exchange.replaceChildren();
    option(fields.exchange, "", "Choose a venue");
    const strategy = research.strategies.find((s) => s.name === fields.strategy.value);
    byId("save-setup").disabled = corrupted || Boolean(strategy && !strategy.venues.length);
    if (strategy) strategy.venues.forEach((venue) => option(fields.exchange, venue, venue));
    if (selected) obsoleteOption(fields.exchange, selected);
    else if (strategy && strategy.venues.length === 1) fields.exchange.value = strategy.venues[0];
    showEvidence();
  }

  function create() {
    editing = null;
    clearObsolete();
    form.reset();
    byId("setup-form-heading").textContent = "Create setup";
    byId("setup-errors").textContent = "";
    venues();
    fields.name.focus();
  }

  function edit(setup) {
    clearObsolete();
    editing = setup.id;
    byId("setup-form-heading").textContent = "Edit setup";
    byId("setup-errors").textContent = "";
    ["name", "budget", "drawdown"].forEach((field) => { fields[field].value = setup[field]; });
    obsoleteOption(fields.account, setup.account);
    obsoleteOption(fields.strategy, setup.strategy);
    venues(setup.exchange);
    fields.name.focus();
  }

  function persist(next, message) {
    if (corrupted) {
      byId("setup-errors").textContent = "Saved data is malformed. Remove saved data explicitly before saving a setup.";
      return false;
    }
    try {
      if (!storageUnchanged(raw, localStorage.getItem(key))) {
        byId("setup-errors").textContent = "Reload to load current saved setups before saving. Your edited fields have been preserved.";
        return false;
      }
      const serialized = JSON.stringify(next);
      localStorage.setItem(key, serialized);
      raw = serialized;
      setups = next;
      render();
      status(message);
      return true;
    } catch (error) {
      byId("setup-errors").textContent = "Browser storage is unavailable or full. This setup is unsaved; keep this page open and allow storage before trying Save setup again.";
      return false;
    }
  }

  function render() {
    const rows = byId("setup-rows");
    rows.replaceChildren();
    byId("saved-setups").hidden = setups.length === 0;
    byId("setup-empty").hidden = setups.length > 0 || corrupted;
    setups.forEach((setup) => {
      const tr = document.createElement("tr");
      [setup.name, setup.strategy, setup.exchange].forEach((value, index) => {
        const td = document.createElement("td");
        td.dataset.label = ["Name", "Strategy", "Venue"][index];
        td.textContent = value;
        tr.append(td);
      });
      const actions = document.createElement("td");
      const editButton = document.createElement("button");
      editButton.type = "button";
      editButton.textContent = "Edit";
      editButton.setAttribute("aria-label", "Edit " + setup.name);
      editButton.addEventListener("click", () => edit(setup));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.textContent = "Delete";
      removeButton.setAttribute("aria-label", "Delete " + setup.name);
      removeButton.addEventListener("click", () => {
        if (persist(setups.filter((s) => s.id !== setup.id), "Setup deleted from this browser.") && editing === setup.id) create();
      });
      actions.append(editButton, removeButton);
      tr.append(actions);
      rows.append(tr);
    });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const setup = {id: editing || crypto.randomUUID(), name: fields.name.value.trim(), account: fields.account.value,
      strategy: fields.strategy.value, exchange: fields.exchange.value,
      budget: Number(fields.budget.value), drawdown: Number(fields.drawdown.value)};
    const error = validate(setup, research);
    byId("setup-errors").textContent = error;
    if (error) return;
    const next = editing ? setups.map((s) => s.id === editing ? setup : s) : [...setups, setup];
    if (persist(next, "Setup saved in this browser. Running accounts are unchanged.")) {
      editing = setup.id;
      byId("setup-form-heading").textContent = "Edit setup";
    }
  });
  byId("create-setup").addEventListener("click", create);
  fields.strategy.addEventListener("change", () => venues());
  fields.exchange.addEventListener("change", showEvidence);
  fields.drawdown.addEventListener("input", showRisk);
  byId("remove-saved-setups").addEventListener("click", () => {
    try {
      if (!storageUnchanged(raw, localStorage.getItem(key))) {
        status("Reload to load current saved setups before removing saved data. The existing data has been preserved.");
        return;
      }
      localStorage.removeItem(key);
      raw = null;
      corrupted = false;
      setups = [];
      byId("remove-saved-setups").hidden = true;
      byId("save-setup").disabled = false;
      byId("setup-errors").textContent = "";
      render();
      status("Malformed saved data removed from this browser. You can save a new setup.");
    } catch (error) {
      status("Could not remove saved data. Allow browser storage and try again.");
    }
  });

  let raw;
  try {
    raw = localStorage.getItem(key);
  } catch (error) {
    status("Browser storage is unavailable. You can explore an unsaved setup here. Allow storage, then reload to load current saved setups before saving.");
  }
  if (raw !== undefined) {
    try {
      setups = parseSaved(raw);
    } catch (error) {
      corrupted = true;
      status("Saved research data is malformed and has been preserved. Use Remove saved data to discard it before saving again.");
      byId("remove-saved-setups").hidden = false;
      byId("save-setup").disabled = true;
    }
  }
  render();
})();
