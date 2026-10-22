(function () {
  var root = document.getElementById("tax-planning");
  if (!root) return;

  var shopifyOnly = root.querySelectorAll("[data-shopify-only]");
  var manualOnly = root.querySelectorAll("[data-manual-only]");
  var sourceButtons = root.querySelectorAll("[data-source]");
  var rangeButtons = root.querySelectorAll("[data-range]");
  var table = document.getElementById("planning-sku-table");
  var body = table.querySelector("tbody");
  var empty = document.getElementById("planning-sku-empty");
  var blended = document.getElementById("planning-blended");
  var results = document.getElementById("planning-results");
  var manualSp = document.getElementById("planning-manual-sp");
  var manualQ = document.getElementById("planning-manual-q");
  var manualVc = document.getElementById("planning-manual-vc");
  var target = document.getElementById("planning-target-income");
  var manualSave = document.getElementById("planning-manual-save");
  var status = document.getElementById("planning-status");
  var costBasis = document.getElementById("planning-cost-basis");

  function dollars(cents) {
    var value = Number(cents || 0) / 100;
    return value.toLocaleString(undefined, { style: "currency", currency: "USD" });
  }
  function inputDollars(cents) { return (Number(cents || 0) / 100).toFixed(2); }
  function cents(value) { return Math.max(0, Math.round(Number(value || 0) * 100)); }
  function number(value) { return Number(value || 0).toLocaleString(); }
  function setVisible(elements, show) {
    for (var i = 0; i < elements.length; i++) elements[i].hidden = !show;
  }
  function setStatus(text, isError, actionUrl) {
    status.replaceChildren();
    status.classList.toggle("error", Boolean(isError));
    status.hidden = !text;
    if (!text) return;
    status.appendChild(document.createTextNode(text));
    if (actionUrl) {
      status.appendChild(document.createTextNode(" "));
      var link = document.createElement("a");
      link.href = actionUrl;
      link.textContent = "Categorize Ledger accounts";
      status.appendChild(link);
    }
  }
  function cell(row, text, numeric) {
    var td = document.createElement("td");
    td.textContent = text;
    if (numeric) td.className = "num";
    row.appendChild(td);
  }
  function result(label, value, positive) {
    var row = document.createElement("div");
    row.className = "planning-result";
    var name = document.createElement("span");
    name.className = "label";
    name.textContent = label;
    var amount = document.createElement("span");
    amount.className = "value " + (positive ? "positive" : "nonpositive");
    amount.textContent = value;
    row.appendChild(name);
    row.appendChild(amount);
    results.appendChild(row);
  }
  function render(data) {
    var config = data.config;
    var source = config.source;
    setVisible(shopifyOnly, source === "shopify");
    setVisible(manualOnly, source === "manual");
    for (var i = 0; i < sourceButtons.length; i++) {
      sourceButtons[i].classList.toggle("active", sourceButtons[i].dataset.source === source);
    }
    for (var j = 0; j < rangeButtons.length; j++) {
      rangeButtons[j].classList.toggle("active", Number(rangeButtons[j].dataset.range) === Number(config.date_range_days));
    }
    manualSp.value = inputDollars(config.manual_sp_cents);
    manualQ.value = Number(config.manual_q || 0);
    manualVc.value = inputDollars(config.manual_vc_cents);
    target.value = inputDollars(config.target_income_cents);
    body.replaceChildren();
    var rows = data.per_sku || [];
    table.hidden = rows.length === 0;
    empty.hidden = rows.length !== 0;
    for (var k = 0; k < rows.length; k++) {
      var row = document.createElement("tr");
      cell(row, rows[k].title, false);
      cell(row, number(rows[k].units_sold), true);
      cell(row, dollars(rows[k].avg_sp_cents), true);
      cell(row, dollars(rows[k].net_revenue_cents), true);
      body.appendChild(row);
    }
    blended.textContent = "Blended: " + number(data.blended.total_units) + " units @ " + dollars(data.blended.blended_avg_sp_cents) + " avg SP";
    results.replaceChildren();
    var inputs = data.inputs;
    var outputs = data.outputs;
    result("Contribution margin", dollars(outputs.contribution_margin_cents) + " / unit (" + (outputs.contribution_margin_pct * 100).toFixed(1) + "%)", outputs.contribution_margin_cents > 0);
    result("Fixed costs", dollars(inputs.fc_cents), inputs.fc_cents > 0);
    result("Break-even", number(outputs.breakeven_q) + " units", outputs.breakeven_q > 0);
    result("Target volume", number(outputs.target_q) + " units", outputs.target_q > 0);
    result("Current profit", dollars(outputs.current_profit_cents), outputs.current_profit_cents > 0);
    result("Units still needed", number(outputs.units_to_target), outputs.units_to_target > 0);
    result("Min price to hit target", dollars(outputs.min_sp_for_target_cents), outputs.min_sp_for_target_cents > 0);
    var period = data.cost_basis.period;
    if (inputs.source_used === "shopify") {
      costBasis.textContent = "Ledger costs for " + period.start_date + " through " + period.end_date + ": " + dollars(data.cost_basis.variable_cost_cents) + " variable and " + dollars(data.cost_basis.fixed_cost_cents) + " fixed.";
    } else {
      costBasis.textContent = "Ledger fixed costs for " + period.start_date + " through " + period.end_date + ": " + dollars(data.cost_basis.fixed_cost_cents) + ". Variable cost per unit uses your manual input.";
    }
    setStatus("Planning data updated.", false);
  }
  function refresh() {
    setStatus("Loading planning data...", false);
    return fetch("/api/tax/planning").then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var detail = payload.detail;
          var message = detail && typeof detail === "object" ? detail.message : detail;
          var error = new Error(message || "Planning data could not load.");
          error.detail = detail;
          throw error;
        }
        return payload;
      });
    }).then(render).catch(function (error) {
      results.replaceChildren();
      costBasis.textContent = "";
      setStatus(error.message + (error.detail && error.detail.action_url ? "" : " Try again."), true, error.detail && error.detail.action_url);
    });
  }
  function update(values) {
    return fetch("/api/tax/planning/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values)
    }).then(function (response) {
      if (!response.ok) throw new Error("Planning settings could not save.");
      return refresh();
    });
  }
  for (var i = 0; i < sourceButtons.length; i++) {
    sourceButtons[i].addEventListener("click", function () { update({ source: this.dataset.source }); });
  }
  for (var j = 0; j < rangeButtons.length; j++) {
    rangeButtons[j].addEventListener("click", function () { update({ date_range_days: Number(this.dataset.range) }); });
  }
  manualSave.addEventListener("click", function () {
    manualSave.disabled = true;
    manualSave.textContent = "Saving...";
    update({ manual_sp_cents: cents(manualSp.value), manual_q: Math.max(0, Math.floor(Number(manualQ.value || 0))), manual_vc_cents: cents(manualVc.value) })
      .then(function () { manualSave.textContent = "Saved"; })
      .catch(function () { manualSave.textContent = "Try again"; })
      .then(function () { manualSave.disabled = false; });
  });
  function saveManualOnBlur() {
    update({ manual_sp_cents: cents(manualSp.value), manual_q: Math.max(0, Math.floor(Number(manualQ.value || 0))), manual_vc_cents: cents(manualVc.value) });
  }
  manualSp.addEventListener("blur", saveManualOnBlur);
  manualQ.addEventListener("blur", saveManualOnBlur);
  manualVc.addEventListener("blur", saveManualOnBlur);
  target.addEventListener("blur", function () { update({ target_income_cents: cents(target.value) }); });
  refresh();
})();
