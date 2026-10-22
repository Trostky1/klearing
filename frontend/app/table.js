/* Klearing shared interaction layer.
 *
 * Three things, all progressive enhancement over server-rendered HTML:
 *   1. Sortable table columns
 *   2. Row selection + a floating bulk-action bar
 *   3. Confirmation dialogs replacing window.confirm()
 *
 * No framework, no build step, no client-side routing. Every mutation
 * still goes through a real form POST; this only changes what the user
 * sees before the POST happens, and how they read what is already on
 * the page.
 *
 * If this file fails to load, every table still renders in its
 * server-provided order, every form still submits, and every confirm
 * falls back to window.confirm(). Nothing here is load-bearing for
 * correctness.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------
   * 1. Sorting
   * ---------------------------------------------------------------- */

  // Money, counts and dates all arrive as display strings ("$1,240.00",
  // "-$40.00", "2026-07-14", "94 days"). Parse to something orderable
  // rather than sorting the string, which would put "$9" after "$100".
  function numericValue(text) {
    var cleaned = text.replace(/[^0-9.\-]/g, "");
    if (cleaned === "" || cleaned === "-" || cleaned === ".") return null;
    var n = parseFloat(cleaned);
    return isNaN(n) ? null : n;
  }

  function cellValue(row, index, kind) {
    var cell = row.cells[index];
    if (!cell) return kind === "text" ? "" : null;
    // An explicit data-sort-value always wins — it lets the server hand
    // us the true value when the display string is lossy (a truncated
    // vendor name, "—" for null, a relative date).
    var explicit = cell.getAttribute("data-sort-value");
    var text = explicit !== null ? explicit : cell.textContent.trim();
    if (kind === "text") return text.toLowerCase();
    if (kind === "date") {
      var t = Date.parse(text);
      return isNaN(t) ? null : t;
    }
    return numericValue(text);
  }

  function compare(a, b, kind) {
    // Nulls always sort last regardless of direction: an empty cell is
    // not "smaller", it is absent, and burying it keeps the rows that
    // actually carry data at the top where they can be read.
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    if (kind === "text") return a < b ? -1 : a > b ? 1 : 0;
    return a - b;
  }

  function sortTable(table, th) {
    var head = th.parentNode;
    var index = Array.prototype.indexOf.call(head.cells, th);
    var kind = th.getAttribute("data-sort") || "text";
    var current = th.getAttribute("aria-sort");
    var dir = current === "ascending" ? "descending" : "ascending";

    Array.prototype.forEach.call(head.cells, function (cell) {
      cell.removeAttribute("aria-sort");
    });
    th.setAttribute("aria-sort", dir);

    var body = table.tBodies[0];
    if (!body) return;
    var rows = Array.prototype.slice.call(body.rows);
    var sign = dir === "ascending" ? 1 : -1;

    // Decorate-sort-undecorate, so cellValue runs once per row rather
    // than once per comparison.
    rows.map(function (row, i) {
      return { row: row, key: cellValue(row, index, kind), i: i };
    }).sort(function (x, y) {
      var c = compare(x.key, y.key, kind);
      // Stable: fall back to original document order on ties, so
      // re-sorting never reshuffles equal rows.
      return c !== 0 ? c * sign : x.i - y.i;
    }).forEach(function (item) {
      body.appendChild(item.row);
    });

    announce(th.textContent.trim() + ", sorted " +
             (dir === "ascending" ? "ascending" : "descending"));
  }

  function initSorting(table) {
    var head = table.tHead;
    if (!head || !head.rows.length) return;
    Array.prototype.forEach.call(head.rows[0].cells, function (th) {
      if (!th.hasAttribute("data-sort")) return;
      th.classList.add("sortable");
      var label = th.textContent.trim();
      var button = document.createElement("button");
      button.type = "button";
      button.innerHTML = "";
      button.appendChild(document.createTextNode(label));
      var arrow = document.createElement("span");
      arrow.className = "sort-arrow";
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "↕";
      button.appendChild(arrow);
      button.addEventListener("click", function () { sortTable(table, th); });
      th.textContent = "";
      th.appendChild(button);
    });
  }

  /* ------------------------------------------------------------------
   * 2. Selection + bulk bar
   * ---------------------------------------------------------------- */

  function initSelection(table) {
    var bar = document.querySelector(table.getAttribute("data-select"));
    if (!bar) return;
    var boxes = function () {
      return Array.prototype.slice.call(
        table.querySelectorAll("tbody input[type=checkbox][data-row]"));
    };
    var all = table.querySelector("thead input[type=checkbox][data-all]");
    var countEl = bar.querySelector(".count");
    var last = null;

    function selected() {
      return boxes().filter(function (b) { return b.checked; });
    }

    function sync() {
      var chosen = selected();
      var n = chosen.length;
      boxes().forEach(function (b) {
        b.closest("tr").classList.toggle("is-selected", b.checked);
      });
      bar.classList.toggle("is-open", n > 0);
      if (countEl) {
        countEl.textContent = n + (n === 1 ? " selected" : " selected");
      }
      if (all) {
        all.checked = n > 0 && n === boxes().length;
        all.indeterminate = n > 0 && n < boxes().length;
      }
      // Hand the selection to every form in the bar, so a bulk submit
      // is a normal POST with normal form data and needs no fetch().
      Array.prototype.forEach.call(bar.querySelectorAll("form"), function (form) {
        Array.prototype.forEach.call(
          form.querySelectorAll("input[data-selection]"), function (n) {
            n.parentNode.removeChild(n);
          });
        chosen.forEach(function (b) {
          var hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = b.getAttribute("data-name") || "selected";
          hidden.value = b.value;
          hidden.setAttribute("data-selection", "");
          form.appendChild(hidden);

          // Carry along anything the row wants included with it —
          // Review uses this so a bulk approve posts each row's own
          // chosen category, not one category applied to everything.
          // An input can only belong to one form via the form
          // attribute, and these already belong to their per-row form,
          // so their values are copied rather than moved.
          //
          // The fields live in the row's detail panel, which moves in
          // and out of the drawer, so follow the pointer rather than
          // looking inside the <tr>: a row whose drawer is currently
          // open would otherwise contribute nothing to a bulk submit.
          var scope = b.getAttribute("data-detail");
          var row = scope ? document.querySelector(scope) : b.closest("tr");
          if (!row) return;
          Array.prototype.forEach.call(
            row.querySelectorAll("[data-bulk-name]"), function (field) {
              var extra = document.createElement("input");
              extra.type = "hidden";
              extra.name = field.getAttribute("data-bulk-name");
              extra.value = field.value;
              extra.setAttribute("data-selection", "");
              form.appendChild(extra);
            });
        });
      });
    }

    table.addEventListener("click", function (event) {
      var box = event.target;
      if (!box.matches || !box.matches("input[type=checkbox][data-row]")) return;
      // Shift-click selects a range, the way every list this borrows
      // from behaves. Without it, clearing a 200-item backlog is 200
      // clicks — which is the problem this whole feature exists to fix.
      if (event.shiftKey && last) {
        var list = boxes();
        var from = list.indexOf(last), to = list.indexOf(box);
        if (from > -1 && to > -1) {
          var lo = Math.min(from, to), hi = Math.max(from, to);
          for (var i = lo; i <= hi; i++) list[i].checked = box.checked;
        }
      }
      last = box;
      sync();
    });

    if (all) {
      all.addEventListener("change", function () {
        boxes().forEach(function (b) { b.checked = all.checked; });
        sync();
      });
    }

    var clear = bar.querySelector("[data-clear]");
    if (clear) {
      clear.addEventListener("click", function () {
        boxes().forEach(function (b) { b.checked = false; });
        sync();
      });
    }

    sync();
  }

  /* ------------------------------------------------------------------
   * 3. Confirmation dialogs
   * ---------------------------------------------------------------- */

  var dialog = null;

  function ensureDialog() {
    if (dialog) return dialog;
    if (!window.HTMLDialogElement) return null;
    dialog = document.createElement("dialog");
    dialog.className = "kl-dialog";
    dialog.innerHTML =
      '<h2 data-title></h2><p data-message></p>' +
      '<div class="dialog-actions">' +
      '<button type="button" class="check" data-cancel>Cancel</button>' +
      '<button type="button" class="approve" data-ok>Confirm</button>' +
      "</div>";
    document.body.appendChild(dialog);
    return dialog;
  }

  function confirmDialog(opts, onConfirm) {
    var el = ensureDialog();
    if (!el) return onConfirm(window.confirm(opts.message));
    el.querySelector("[data-title]").textContent = opts.title || "Confirm";
    el.querySelector("[data-message]").textContent = opts.message;
    var ok = el.querySelector("[data-ok]");
    ok.textContent = opts.confirmLabel || "Confirm";
    ok.classList.toggle("danger", !!opts.danger);

    var done = false;
    function finish(result) {
      if (done) return;
      done = true;
      el.close();
      onConfirm(result);
    }
    el.querySelector("[data-cancel]").onclick = function () { finish(false); };
    ok.onclick = function () { finish(true); };
    el.onclose = function () { finish(false); };
    el.showModal();
    ok.focus();
  }

  // Forms opt in with data-confirm. This keeps ui.py's confirm_onsubmit()
  // signature intact — 22 call sites keep working untouched — while
  // swapping what actually happens from a native confirm to this.
  function initConfirms(root) {
    Array.prototype.forEach.call(
      root.querySelectorAll("form[data-confirm]"), function (form) {
        if (form.hasAttribute("data-confirm-bound")) return;
        form.setAttribute("data-confirm-bound", "");
        form.addEventListener("submit", function (event) {
          if (form.hasAttribute("data-confirmed")) return;
          event.preventDefault();
          confirmDialog({
            title: form.getAttribute("data-confirm-title") || "Confirm",
            message: form.getAttribute("data-confirm"),
            confirmLabel: form.getAttribute("data-confirm-label") || "Confirm",
            danger: form.hasAttribute("data-confirm-danger"),
          }, function (ok) {
            if (!ok) return;
            form.setAttribute("data-confirmed", "");
            var busy = form.getAttribute("data-busy-label");
            if (busy) {
              var btn = form.querySelector("button[type=submit], button:not([type])");
              if (btn) { btn.disabled = true; btn.textContent = busy; }
            }
            form.submit();
          });
        });
      });
  }

  /* Confirm on one BUTTON rather than the whole form.
   *
   * A form with several submit buttons (save / add a row / send) needs the
   * prompt on the one irreversible action, not on every submit. The
   * form-level binding above cannot express that.
   *
   * Uses requestSubmit(button) rather than form.submit(): the latter
   * ignores the submitter, so the button's formaction — the entire reason
   * these buttons exist — would be discarded and every action would post
   * to the form's own action instead.
   */
  function initButtonConfirms(root) {
    Array.prototype.forEach.call(
      root.querySelectorAll("button[data-confirm]"), function (button) {
        if (button.hasAttribute("data-confirm-bound")) return;
        button.setAttribute("data-confirm-bound", "");
        button.addEventListener("click", function (event) {
          var form = button.form;
          if (!form || button.hasAttribute("data-confirmed")) return;
          event.preventDefault();
          confirmDialog({
            title: button.getAttribute("data-confirm-title") || "Confirm",
            message: button.getAttribute("data-confirm"),
            confirmLabel: button.getAttribute("data-confirm-label") || "Confirm",
            danger: button.hasAttribute("data-confirm-danger"),
          }, function (ok) {
            if (!ok) return;
            button.setAttribute("data-confirmed", "");
            var busy = button.getAttribute("data-busy-label");
            if (form.requestSubmit) {
              form.requestSubmit(button);
            } else {
              // Ancient fallback: carry the formaction across by hand.
              var action = button.getAttribute("formaction");
              if (action) form.setAttribute("action", action);
              form.submit();
            }
            if (busy) { button.disabled = true; button.textContent = busy; }
          });
        });
      });
  }

  /* ------------------------------------------------------------------
   * 3b. Review drawer
   * ---------------------------------------------------------------- */

  /* One transaction at a time, with the queue still visible behind it.
   *
   * The detail panel is MOVED into the drawer rather than copied. Its
   * inputs carry form="approve-<id>", which associates by element id
   * regardless of where the node sits, so relocating it keeps a single
   * source of truth for the field values — a bulk submit and a drawer
   * submit always read the same state.
   */
  function initDrawer() {
    var drawer = document.getElementById("review-drawer");
    if (!drawer) return;
    var scrim = document.querySelector("[data-drawer-scrim]");
    var slot = drawer.querySelector("[data-drawer-slot]");
    var vendorEl = drawer.querySelector("[data-drawer-vendor]");
    var metaEl = drawer.querySelector("[data-drawer-meta]");
    var home = document.querySelector(".review-panels");
    var openPanel = null, lastFocus = null;

    function close() {
      if (!openPanel) return;
      // Put the panel back where it came from so its fields stay in the
      // document and keep contributing to bulk submits. The drawer chrome
      // is shared across every review queue, but .review-panels belongs to
      // whichever queue rendered — so tolerate its absence rather than
      // throwing and leaving the drawer stuck open. The panel stays in the
      // slot, still in the document, so form= association survives either way.
      if (home) home.appendChild(openPanel);
      openPanel.classList.add("is-hidden");
      var row = document.querySelector("tr.is-open-in-drawer");
      if (row) row.classList.remove("is-open-in-drawer");
      openPanel = null;
      drawer.classList.remove("is-open");
      scrim.classList.remove("is-open");
      drawer.hidden = true;
      if (lastFocus && lastFocus.isConnected) lastFocus.focus();
    }

    function open(selector, trigger) {
      var panel = document.querySelector(selector);
      if (!panel) return;
      if (openPanel) close();
      lastFocus = trigger || document.activeElement;
      vendorEl.textContent = panel.getAttribute("data-vendor") || "";
      metaEl.innerHTML = panel.getAttribute("data-meta") || "";
      panel.classList.remove("is-hidden");
      slot.appendChild(panel);
      openPanel = panel;
      drawer.hidden = false;
      // Force a reflow so the transition has an off-screen start state,
      // then open synchronously. This used to wait for
      // requestAnimationFrame, which does not fire in a window that is
      // not compositing — and because .drawer is visibility:hidden until
      // .is-open lands, a dropped frame left the drawer invisible AND
      // unfocusable rather than merely un-animated.
      void drawer.offsetWidth;
      drawer.classList.add("is-open");
      scrim.classList.add("is-open");
      var id = selector.replace("#detail-", "");
      var row = document.getElementById("row-" + id);
      if (row) row.classList.add("is-open-in-drawer");
      // Focus the category select — the decision this drawer exists for
      // — rather than Approve. Landing on the irreversible button is
      // how misclicks happen.
      var first = panel.querySelector("select, input[type=text]");
      if (first) first.focus();
    }

    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-review]");
      if (trigger) {
        event.preventDefault();
        open(trigger.getAttribute("data-review"), trigger);
        return;
      }
      if (event.target.closest("[data-drawer-close]")
          || event.target === scrim) {
        close();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && openPanel) {
        event.preventDefault();
        close();
        return;
      }
      // Keyboard queue traversal. Deliberately no single-key approve:
      // approving posts to the ledger and cannot be retracted from Klearing,
      // so it stays behind an explicit click and a confirmation.
      var tag = (document.activeElement || {}).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (openPanel) return;
      var isNext = event.key === "j" || event.key === "ArrowDown";
      var isPrev = event.key === "k" || event.key === "ArrowUp";
      if (!isNext && !isPrev && event.key !== "Enter") return;
      var buttons = Array.prototype.slice.call(
        document.querySelectorAll("[data-review]"));
      if (!buttons.length) return;
      var at = buttons.indexOf(document.activeElement);
      if (event.key === "Enter") {
        if (at > -1) {
          event.preventDefault();
          open(buttons[at].getAttribute("data-review"), buttons[at]);
        }
        return;
      }
      event.preventDefault();
      var next = at < 0 ? 0 : at + (isNext ? 1 : -1);
      next = Math.max(0, Math.min(buttons.length - 1, next));
      buttons[next].focus();
    });

    // Trap focus inside the drawer while it is open.
    drawer.addEventListener("keydown", function (event) {
      if (event.key !== "Tab" || !openPanel) return;
      var focusable = drawer.querySelectorAll(
        "button, select, input, textarea, a[href]");
      if (!focusable.length) return;
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    // A failed post comes back with ?review=<id>; reopen where we were.
    var table = document.querySelector("[data-review-table]");
    var reopen = table && table.getAttribute("data-open-detail");
    if (reopen) open(reopen, null);
  }

  /* ------------------------------------------------------------------
   * 4. Command palette
   * ---------------------------------------------------------------- */

  var palette = null, paletteItems = [], paletteIndex = 0;

  // Subsequence match, the same rule every palette uses: "leg sync"
  // matches "Ledger — Sync". Scored so that earlier and tighter matches
  // rank first, which is what makes two keystrokes enough.
  function fuzzy(needle, hay) {
    if (!needle) return 0;
    var n = needle.toLowerCase(), h = hay.toLowerCase();
    var i = 0, score = 0, streak = 0, firstAt = -1;
    for (var j = 0; j < h.length && i < n.length; j++) {
      if (n[i] === " ") { i++; continue; }
      if (h[j] === n[i]) {
        if (firstAt < 0) firstAt = j;
        streak++;
        score += 1 + streak;
        // A match at a word boundary is worth more than one mid-word.
        if (j === 0 || h[j - 1] === " " || h[j - 1] === "—") score += 3;
        i++;
      } else {
        streak = 0;
      }
    }
    if (i < n.replace(/ /g, "").length) return -1;
    return score - firstAt * 0.1;
  }

  function buildPalette() {
    if (palette) return palette;
    var data = window.KLEARING_PALETTE || [];
    palette = document.createElement("dialog");
    palette.className = "palette";
    palette.setAttribute("aria-label", "Search and jump to");
    palette.innerHTML =
      '<input class="palette-input" type="text" autocomplete="off" ' +
      'spellcheck="false" placeholder="Jump to a page, or run an action…" ' +
      'aria-controls="palette-list" aria-expanded="true" role="combobox">' +
      '<div class="palette-list" id="palette-list" role="listbox"></div>' +
      '<div class="palette-foot"><span>↑↓ move</span><span>↵ open</span>' +
      "<span>esc close</span></div>";
    document.body.appendChild(palette);

    var input = palette.querySelector(".palette-input");
    var list = palette.querySelector(".palette-list");

    function render(query) {
      var matches = data.map(function (entry) {
        if (!query) return { entry: entry, score: 0 };
        // Score against the label first, since a label hit should always
        // outrank an alias hit. Aliases only rescue queries the label
        // cannot satisfy at all — "pnl" has no n in "Profit & Loss".
        var best = fuzzy(query, entry.label + " " + (entry.group || ""));
        if (best < 0 && entry.kw) {
          var viaAlias = fuzzy(query, entry.kw);
          if (viaAlias >= 0) best = viaAlias * 0.5;
        }
        return { entry: entry, score: best };
      }).filter(function (m) { return m.score >= 0; });

      if (query) {
        matches.sort(function (a, b) { return b.score - a.score; });
      }
      matches = matches.slice(0, 40);

      list.innerHTML = "";
      paletteItems = [];
      if (!matches.length) {
        list.innerHTML = '<div class="palette-empty">Nothing matches that.</div>';
        return;
      }
      var lastGroup = null;
      matches.forEach(function (m) {
        // Group headings only make sense in the unfiltered list; once
        // results are ranked by score, grouping would fight the ranking.
        if (!query && m.entry.group !== lastGroup) {
          lastGroup = m.entry.group;
          var head = document.createElement("div");
          head.className = "palette-group";
          head.textContent = m.entry.group;
          list.appendChild(head);
        }
        var row = document.createElement("div");
        row.className = "palette-item";
        row.setAttribute("role", "option");
        row.appendChild(document.createTextNode(m.entry.label));
        if (query && m.entry.group) {
          var where = document.createElement("span");
          where.className = "where";
          where.textContent = m.entry.group;
          row.appendChild(where);
        }
        row.addEventListener("click", function () { go(m.entry); });
        list.appendChild(row);
        paletteItems.push({ el: row, entry: m.entry });
      });
      select(0);
    }

    function select(i) {
      if (!paletteItems.length) return;
      paletteIndex = (i + paletteItems.length) % paletteItems.length;
      paletteItems.forEach(function (item, n) {
        item.el.setAttribute("aria-selected", n === paletteIndex ? "true" : "false");
      });
      var el = paletteItems[paletteIndex].el;
      if (el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
    }

    function go(entry) {
      palette.close();
      if (entry.method === "post") {
        // A few palette entries are verbs, not places. Submitting a real
        // form keeps them on the same POST-redirect-banner path as the
        // button that would otherwise run them, so they get the same
        // confirmation and the same error handling.
        var form = document.createElement("form");
        form.method = "post";
        form.action = entry.href;
        document.body.appendChild(form);
        form.submit();
        return;
      }
      window.location.href = entry.href;
    }

    input.addEventListener("input", function () { render(input.value.trim()); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") { event.preventDefault(); select(paletteIndex + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); select(paletteIndex - 1); }
      else if (event.key === "Enter") {
        event.preventDefault();
        if (paletteItems[paletteIndex]) go(paletteItems[paletteIndex].entry);
      }
    });
    palette.addEventListener("close", function () { input.value = ""; });
    palette.render = render;
    return palette;
  }

  function openPalette() {
    var el = buildPalette();
    if (el.open) return;
    el.render("");
    el.showModal();
    el.querySelector(".palette-input").focus();
  }

  function initPalette() {
    document.addEventListener("keydown", function (event) {
      // Ctrl+K everywhere, Cmd+K on mac. "/" is a nice shortcut but it
      // would swallow the character in every text field, so it is only
      // bound when focus is not in an input.
      var mod = event.ctrlKey || event.metaKey;
      var tag = (document.activeElement || {}).tagName;
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (mod && event.key.toLowerCase() === "k") {
        event.preventDefault();
        openPalette();
      } else if (event.key === "/" && !typing && !mod) {
        event.preventDefault();
        openPalette();
      }
    });
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-palette-open]"), function (button) {
        button.addEventListener("click", openPalette);
      });
  }

  /* ------------------------------------------------------------------
   * 8. Mobile "More" sheet
   * ---------------------------------------------------------------- */

  // A native <dialog>, same idiom as the confirm dialog and the palette
  // above — Escape-to-close and focus handling come from the browser,
  // not from hand-rolled code.
  function initMoreSheet() {
    var sheet = document.getElementById("more-sheet");
    var opener = document.querySelector("[data-more-open]");
    if (!sheet || !opener) return;
    opener.addEventListener("click", function () { sheet.showModal(); });
    var closeBtn = sheet.querySelector("[data-more-close]");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () { sheet.close(); });
    }
  }

  /* ------------------------------------------------------------------
   * Shared
   * ---------------------------------------------------------------- */

  var liveRegion = null;
  function announce(text) {
    if (!liveRegion) {
      liveRegion = document.createElement("div");
      liveRegion.setAttribute("aria-live", "polite");
      liveRegion.className = "sr-only";
      document.body.appendChild(liveRegion);
    }
    liveRegion.textContent = text;
  }

  function init() {
    Array.prototype.forEach.call(
      document.querySelectorAll("table"), function (table) {
        initSorting(table);
        if (table.hasAttribute("data-select")) initSelection(table);
      });
    initConfirms(document);
    initButtonConfirms(document);
    initDrawer();
    initPalette();
    initMoreSheet();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.klearing = window.klearing || {};
  window.klearing.confirmDialog = confirmDialog;
})();
