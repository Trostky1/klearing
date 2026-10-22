(function () {
  "use strict";

  var workspace = document.getElementById("financial-insights");
  if (!workspace) return;

  var form = document.getElementById("insights-scenario-form");
  var cashBalance = null;
  var baselineRevenue = Number(workspace.dataset.monthlyRevenue || 0);
  var baselineExpenses = Number(workspace.dataset.monthlyExpenses || 0);
  var money = new Intl.NumberFormat(undefined, {
    style: "currency", currency: "USD", maximumFractionDigits: 0
  });

  function setText(id, value) {
    document.getElementById(id).textContent = value;
  }

  function setValue(id, value, mixed) {
    var node = document.getElementById(id);
    node.textContent = value;
    node.classList.toggle("mixed", Boolean(mixed));
  }

  function inputNumber(field) {
    var value = Number(field.value || 0);
    return Number.isFinite(value) ? value : 0;
  }

  function scenario() {
    var revenueChange = inputNumber(form.elements.revenue_change);
    var expenseChange = inputNumber(form.elements.expense_change);
    var oneTimeCost = Math.max(0, Math.round(
      inputNumber(form.elements.one_time_cost) * 100));
    var revenue = Math.max(0, Math.round(
      baselineRevenue * (1 + revenueChange / 100)));
    var recurringExpenses = Math.max(0, Math.round(
      baselineExpenses * (1 + expenseChange / 100)));
    var totalExpenses = recurringExpenses + oneTimeCost;
    var netIncome = revenue - totalExpenses;
    var margin = revenue > 0 ? netIncome / revenue * 100 : null;

    setValue("scenario-revenue", money.format(revenue / 100), false);
    setValue("scenario-expenses", money.format(totalExpenses / 100), false);
    setValue("scenario-net", money.format(netIncome / 100), false);
    setValue("scenario-margin", margin === null ? "Not available" : margin.toFixed(1) + "%", margin === null);

    var runway = null;
    if (cashBalance !== null && recurringExpenses > 0) {
      runway = Math.max(0, cashBalance - oneTimeCost) / (recurringExpenses / 30);
    }
    setValue("scenario-runway", runway === null ? "Not available" : Math.round(runway) + " days", true);
  }

  form.addEventListener("input", scenario);
  form.addEventListener("reset", function () {
    window.setTimeout(scenario, 0);
  });

  fetch("/api/insights/cash").then(function (response) {
    return response.json();
  }).then(function (data) {
    var status = document.getElementById("insights-cash-status");
    if (data.state === "ok") {
      cashBalance = Number(data.balance_cents);
      setValue("insights-cash-balance", money.format(cashBalance / 100), false);
      setValue("insights-runway", data.runway_days === null
        ? "No approved burn yet" : Math.round(data.runway_days) + " days", true);
      status.textContent = data.note;
    } else {
      setValue("insights-cash-balance", "Not available", true);
      setValue("insights-runway", "Not available", true);
      status.textContent = data.note;
    }
    scenario();
  }).catch(function () {
    setValue("insights-cash-balance", "Not available", true);
    setValue("insights-runway", "Not available", true);
    document.getElementById("insights-cash-status").textContent =
      "Cash accounts could not be checked. The approved-ledger insights are still available.";
  });

  scenario();
})();
