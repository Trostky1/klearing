(function () {
  "use strict";

  var root = document.querySelector(".tax-workspace[data-view]");
  if (!root) return;

  var moneyFormatter = new Intl.NumberFormat(undefined, {
    style: "currency", currency: "USD"
  });

  function dollars(cents) {
    return moneyFormatter.format(Number(cents || 0) / 100);
  }

  function cents(value) {
    return Math.max(0, Math.round(Number(value || 0) * 100));
  }

  function isoDate(value) {
    var year = value.getFullYear();
    var month = String(value.getMonth() + 1).padStart(2, "0");
    var day = String(value.getDate()).padStart(2, "0");
    return year + "-" + month + "-" + day;
  }

  function request(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var detail = payload.detail;
          var message = detail && typeof detail === "object" ? detail.message : detail;
          var error = new Error(message || "This Tax action could not be completed.");
          error.detail = detail;
          error.status = response.status;
          throw error;
        }
        return payload;
      });
    });
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function metric(container, label, value, tone, mixed) {
    var row = element("div", "planning-result");
    row.appendChild(element("span", "label", label));
    row.appendChild(element("span", "value" + (tone ? " " + tone : "") + (mixed ? " mixed" : ""), value));
    container.appendChild(row);
  }

  function setStatus(node, text, isError) {
    node.textContent = text;
    node.classList.toggle("error", Boolean(isError));
    node.hidden = !text;
  }

  function setExportLink(link, href, enabled, disabledTitle) {
    if (enabled) {
      link.href = href;
      link.removeAttribute("aria-disabled");
      link.removeAttribute("title");
    } else {
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
      link.title = disabledTitle || "Categorize every used revenue and expense account to enable this tax export.";
    }
  }

  function query(parameters) {
    return new URLSearchParams(parameters).toString();
  }

  function setupArchive() {
    var status = document.getElementById("tax-archive-status");
    var filter = document.getElementById("tax-archive-status-filter");
    var tbody = document.querySelector("#tax-legacy-table tbody");
    var empty = document.getElementById("tax-archive-empty");
    var counts = document.getElementById("tax-archive-counts");

    function categoryLabel(value) {
      return value ? String(value).replaceAll("_", " ") : "Not recorded";
    }

    function archiveRow(transaction) {
      var row = document.createElement("tr");
      var detailCell = document.createElement("td");
      detailCell.appendChild(element("div", "tax-row-title", transaction.merchant || transaction.description || "Unknown merchant"));
      detailCell.appendChild(element("div", "tax-row-detail", transaction.date + (transaction.description ? " - " + transaction.description : "")));
      row.appendChild(detailCell);
      row.appendChild(element("td", "", transaction.raw_wave_category || "Not recorded"));
      row.appendChild(element("td", "", categoryLabel(transaction.effective_category)));
      var statusCell = document.createElement("td");
      var tone = transaction.user_approved === 1 ? "high" : transaction.user_approved === 2 ? "low" : "none";
      statusCell.appendChild(element("span", "tax-confidence " + tone, transaction.approval_status));
      row.appendChild(statusCell);
      var amountCell = element("td", "num");
      amountCell.appendChild(element("span", "tax-amount " + (transaction.amount_cents >= 0 ? "income" : "expense"), dollars(transaction.amount_cents)));
      row.appendChild(amountCell);
      return row;
    }

    function load() {
      setStatus(status, "Loading legacy records...", false);
      request("/api/tax/legacy-transactions?status=" + encodeURIComponent(filter.value)).then(function (data) {
        var records = data.transactions || [];
        tbody.replaceChildren();
        records.forEach(function (transaction) { tbody.appendChild(archiveRow(transaction)); });
        empty.hidden = records.length !== 0;
        counts.replaceChildren();
        metric(counts, "Records shown", String(records.length));
        metric(counts, "Approved preserved", String(data.summary.approved), "positive");
        metric(counts, "Pending archived", String(data.summary.pending));
        metric(counts, "Rejected archived", String(data.summary.rejected));
        setStatus(status, records.length ? "Showing read-only legacy records." : "", false);
      }).catch(function (error) {
        setStatus(status, error.message, true);
      });
    }

    filter.addEventListener("change", load);
    load();
  }

  function setupStatements() {
    var period = document.getElementById("tax-statement-period");
    var start = document.getElementById("tax-statement-start");
    var end = document.getElementById("tax-statement-end");
    var status = document.getElementById("tax-statements-status");
    var results = document.getElementById("tax-statement-results");
    var balanceResults = document.getElementById("tax-balance-results");
    var expenseRows = document.getElementById("tax-expense-rows");
    var expenseEmpty = document.getElementById("tax-expense-empty");
    var incomeExport = document.getElementById("tax-income-export");
    var scheduleExport = document.getElementById("tax-schedule-export");
    var statementCsv = document.getElementById("tax-statement-csv");
    expenseEmpty.textContent = "No Ledger operating expenses in this period.";

    function setPeriod() {
      if (period.value === "custom") return;
      var today = new Date();
      var from;
      var to = today;
      if (period.value === "last_month") {
        from = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        to = new Date(today.getFullYear(), today.getMonth(), 0);
      } else if (period.value === "ytd") {
        from = new Date(today.getFullYear(), 0, 1);
      } else {
        from = new Date(today.getFullYear(), today.getMonth(), 1);
      }
      start.value = isoDate(from);
      end.value = isoDate(to);
    }

    function updateExports(taxCategoriesComplete) {
      var range = query({ start_date: start.value, end_date: end.value });
      setExportLink(incomeExport, "/api/tax/exports/income-statement.pdf?" + range, true);
      setExportLink(statementCsv, "/api/tax/exports/transactions.csv?" + range, taxCategoriesComplete);
      setExportLink(scheduleExport, "/api/tax/exports/schedule-c.pdf?" + query({ as_of: end.value }), taxCategoriesComplete);
    }

    function load() {
      if (!start.value || !end.value || start.value > end.value) {
        var invalidRangeTitle = "Choose a valid statement period to enable this export.";
        setExportLink(incomeExport, "", false, invalidRangeTitle);
        setExportLink(scheduleExport, "", false, invalidRangeTitle);
        setExportLink(statementCsv, "", false, invalidRangeTitle);
        setStatus(status, "Choose a valid statement period.", true);
        return;
      }
      results.replaceChildren();
      balanceResults.replaceChildren();
      expenseRows.replaceChildren();
      expenseEmpty.hidden = true;
      document.getElementById("tax-balance-note").textContent = "";
      setStatus(status, "Loading statement...", false);
      updateExports(false);
      var range = query({ start_date: start.value, end_date: end.value });
      Promise.all([
        request("/api/tax/statements/income?" + range),
        request("/api/tax/statements/balance-sheet?" + query({ as_of: end.value }))
      ]).then(function (data) {
        var statement = data[0];
        var balance = data[1];
        updateExports(statement.is_tax_complete);
        results.replaceChildren();
        metric(results, "Net revenue", dollars(statement.income.net_revenue_cents), "positive");
        metric(results, "Gross profit", dollars(statement.gross_profit_cents), statement.gross_profit_cents >= 0 ? "positive" : "nonpositive");
        metric(results, "Gross margin", (statement.gross_margin * 100).toFixed(1) + "%");
        metric(results, "Operating expenses", dollars(statement.operating_expenses.total_cents), statement.operating_expenses.total_cents ? "nonpositive" : "positive");
        metric(results, "Net income", dollars(statement.net_income_cents), statement.net_income_cents >= 0 ? "positive" : "nonpositive");
        metric(results, "Net margin", (statement.net_margin * 100).toFixed(1) + "%");
        expenseRows.replaceChildren();
        var categories = statement.operating_expenses.categories || [];
        categories.forEach(function (category) {
          var row = document.createElement("tr");
          row.appendChild(element("td", "", category.label));
          row.appendChild(element("td", "num", dollars(category.amount_cents)));
          expenseRows.appendChild(row);
        });
        expenseEmpty.hidden = categories.length !== 0;
        balanceResults.replaceChildren();
        metric(balanceResults, "Total assets", dollars(balance.assets_total_cents), balance.assets_total_cents >= 0 ? "positive" : "nonpositive");
        metric(balanceResults, "Total liabilities", dollars(balance.liabilities_total_cents), balance.liabilities_total_cents ? "nonpositive" : "positive");
        metric(balanceResults, "Current earnings", dollars(balance.current_earnings_cents), balance.current_earnings_cents >= 0 ? "positive" : "nonpositive");
        metric(balanceResults, "Total equity", dollars(balance.equity_total_cents), balance.equity_total_cents >= 0 ? "positive" : "nonpositive");
        metric(balanceResults, "Balance check", balance.is_balanced ? "Balanced" : dollars(balance.difference_cents) + " difference", balance.is_balanced ? "positive" : "nonpositive", true);
        document.getElementById("tax-balance-note").textContent = balance.note;
        var entryWord = statement.source_entry_count === 1 ? "entry" : "entries";
        if (statement.is_tax_complete) {
          setStatus(status, statement.source_entry_count + " Ledger " + entryWord + " included. Tax categories are complete for this period.", false);
        } else {
          var accountWord = statement.unresolved_account_count === 1 ? "account" : "accounts";
          var accountVerb = statement.unresolved_account_count === 1 ? "needs" : "need";
          setStatus(status, statement.source_entry_count + " Ledger " + entryWord + " included. " + statement.unresolved_account_count + " " + accountWord + " still " + accountVerb + " categorization before tax projections or category-sensitive exports are available.", true);
        }
      }).catch(function (error) {
        setStatus(status, error.message, true);
      });
    }

    period.addEventListener("change", function () { setPeriod(); load(); });
    start.addEventListener("change", function () { period.value = "custom"; });
    end.addEventListener("change", function () { period.value = "custom"; });
    document.getElementById("tax-statements-apply").addEventListener("click", load);
    setPeriod();
    load();
  }

  function setupProjections() {
    var asOf = document.getElementById("tax-projection-date");
    var section179 = document.getElementById("tax-section-179");
    var homeOffice = document.getElementById("tax-home-office");
    var status = document.getElementById("tax-projection-status");
    var results = document.getElementById("tax-projection-results");
    var deductions = document.getElementById("tax-deduction-results");
    var quarters = document.getElementById("tax-quarterly-rows");
    var warnings = document.getElementById("tax-projection-warnings");
    var output = document.getElementById("tax-projection-output");
    var blocked = document.getElementById("tax-projection-blocked");
    var blockedTitle = document.getElementById("tax-projection-blocked-title");
    var blockedMessage = document.getElementById("tax-projection-blocked-message");
    var blockedAccounts = document.getElementById("tax-projection-blocked-accounts");
    var blockedAction = document.getElementById("tax-projection-blocked-action");
    var exportLink = document.getElementById("tax-projection-export");

    function resetOutput() {
      results.replaceChildren();
      deductions.replaceChildren();
      quarters.replaceChildren();
      warnings.replaceChildren();
      output.hidden = true;
      blocked.hidden = true;
      blockedTitle.textContent = "Categorization needed";
      blockedMessage.textContent = "";
      blockedAccounts.replaceChildren();
      blockedAccounts.hidden = false;
      blockedAction.hidden = false;
      blockedAction.href = "/ledger?tab=accounts";
      blockedAction.textContent = "Categorize Ledger accounts";
      exportLink.removeAttribute("href");
      exportLink.setAttribute("aria-disabled", "true");
    }

    function showProjectionBlock(detail) {
      blockedTitle.textContent = detail.title;
      blockedMessage.textContent = detail.message;
      (detail.unresolved_accounts || []).forEach(function (account) {
        blockedAccounts.appendChild(element(
          "li", "", account.code + " " + account.name
        ));
      });
      blockedAccounts.hidden = !(detail.unresolved_accounts || []).length;
      if (detail.action) {
        blockedAction.href = detail.action.href;
        blockedAction.textContent = detail.action.label;
        blockedAction.hidden = false;
      } else {
        blockedAction.hidden = true;
      }
      blocked.hidden = false;
    }

    function parameters() {
      return {
        as_of: asOf.value,
        section_179_cents: cents(section179.value),
        home_office_square_feet: Math.max(0, Math.floor(Number(homeOffice.value || 0)))
      };
    }

    function load() {
      resetOutput();
      if (!asOf.value) {
        setStatus(status, "Choose an as-of date.", true);
        return;
      }
      setStatus(status, "Calculating projection...", false);
      var values = parameters();
      request("/api/tax/projection?" + query(values)).then(function (data) {
        metric(results, "Annualized net income", dollars(data.income.annualized_net_income_cents), data.income.annualized_net_income_cents >= 0 ? "positive" : "nonpositive");
        metric(results, "Federal income tax", dollars(data.taxes.federal_income_tax_cents));
        metric(results, "Self-employment tax", dollars(data.taxes.self_employment_tax_cents));
        var state = data.state || { name: "State", available: data.taxes.iowa_income_tax_cents !== null };
        var stateCents = data.taxes.state_income_tax_cents;
        if (stateCents === undefined) stateCents = data.taxes.iowa_income_tax_cents;
        metric(results, state.name + " income tax", state.available ? dollars(stateCents) : "Not available", "", !state.available);
        var totalLabel = state.available ? "Total estimated tax" : "Federal + self-employment only";
        metric(results, totalLabel, dollars(data.taxes.total_estimated_tax_cents), "nonpositive");
        metric(results, "Effective tax rate", data.income.annualized_net_income_cents > 0 ? (data.taxes.total_estimated_tax_cents / data.income.annualized_net_income_cents * 100).toFixed(1) + "%" : "0.0%");
        metric(deductions, "Section 179", dollars(data.deductions.section_179_allowed_cents));
        metric(deductions, "Home office", dollars(data.deductions.home_office_deduction_cents));
        metric(deductions, "QBI", dollars(data.deductions.qbi_deduction_cents));
        metric(deductions, "Half SE tax", dollars(data.deductions.half_self_employment_tax_deduction_cents));
        data.quarterly_estimates.forEach(function (quarter) {
          var row = document.createElement("tr");
          row.appendChild(element("td", "", quarter.due_date));
          row.appendChild(element("td", "num", dollars(quarter.amount_cents)));
          row.appendChild(element("td", "", quarter.is_past_due ? "Past due" : "Upcoming"));
          quarters.appendChild(row);
        });
        data.warnings.forEach(function (warning) { warnings.appendChild(element("li", "", warning)); });
        output.hidden = false;
        exportLink.href = "/api/tax/exports/projection.pdf?" + query(values);
        exportLink.removeAttribute("aria-disabled");
        setStatus(status, data.is_estimate_complete ? "Projection updated." : "Projection updated with entity-specific limitations.", false);
      }).catch(function (error) {
        if (error.status === 409 && error.detail && error.detail.code === "tax_categorization_required") {
          showProjectionBlock({
            title: "Categorization needed",
            message: error.detail.message,
            unresolved_accounts: error.detail.unresolved_accounts,
            action: {
              href: "/ledger?tab=accounts",
              label: "Categorize Ledger accounts"
            }
          });
          setStatus(status, "Projection unavailable until Ledger categorization is complete.", true);
        } else if (error.status === 409 && error.detail && error.detail.code === "tax_year_stale") {
          showProjectionBlock(error.detail);
          setStatus(status, error.detail.title + ".", true);
        } else {
          setStatus(status, error.message, true);
        }
      });
    }

    document.getElementById("tax-projection-apply").addEventListener("click", load);
    load();
  }

  if (root.dataset.view === "archive") setupArchive();
  if (root.dataset.view === "statements") setupStatements();
  if (root.dataset.view === "projections") setupProjections();
})();
