/* Site-wide htmx glue: htmx (hx-boost on <body>) turns every link and
 * form into a background request that swaps #main in place, so pages
 * never fully reload. This file adds the browser-like touches htmx
 * does not do on its own. */
(function () {
  if (typeof htmx === "undefined") return;

  /* Flash messages auto-dismiss after a few seconds; re-armed after
   * every swap because swapped-in content brings fresh alerts. */
  function armFlashes() {
    document.querySelectorAll("[data-flash]:not([data-flash-armed])").forEach(function (el) {
      el.setAttribute("data-flash-armed", "");
      setTimeout(function () {
        el.classList.remove("show");
        setTimeout(function () { el.remove(); }, 300);
      }, 6000);
    });
  }

  /* Generic failure toast for network errors and non-2xx responses,
   * which htmx leaves unswapped (the page would otherwise sit silent). */
  function showError() {
    var stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      stack.setAttribute("aria-live", "polite");
      (document.getElementById("main") || document.body).appendChild(stack);
    }
    var alert = document.createElement("div");
    alert.className = "alert alert-danger fade show shadow";
    alert.setAttribute("role", "alert");
    alert.setAttribute("data-flash", "");
    alert.textContent = document.body.dataset.htmxError || "Error";
    stack.appendChild(alert);
    armFlashes();
  }

  /* History snapshots saved before #main carried hx-history-elt hold the
   * whole <body>, the three <script src> tags at its foot included, and
   * they live in localStorage across deploys. Restoring one of those into
   * #main re-runs Bootstrap and htmx, and every section toggle then opens
   * twice and never closes - the very bug hx-history-elt fixed, back for
   * one browser Back per stale entry. Purge the cache once per format. */
  try {
    if (window.localStorage.getItem("app-history-format") !== "main") {
      window.localStorage.removeItem("htmx-history-cache");
      window.localStorage.setItem("app-history-format", "main");
    }
  } catch (e) { /* storage blocked: nothing to purge */ }

  /* Swapping #main re-renders Bootstrap accordions collapsed; remember
   * which panels were open so a form submit doesn't fold the section the
   * user is working in. */
  var openPanels = [];
  document.body.addEventListener("htmx:beforeSwap", function () {
    openPanels = Array.prototype.map.call(
      document.querySelectorAll(".collapse.show[id]"),
      function (el) { return el.id; }
    );
  });

  /* Reconcile each collapse toggle's arrow/aria with whether its panel is
   * actually shown. Panels are server-rendered open (class="collapse show")
   * while their button still reads aria-expanded="false"; Bootstrap only
   * fixes that on click, so we sync it on load and after every swap. Skip
   * panels mid-transition (.collapsing) - Bootstrap owns their aria then. */
  function syncToggles() {
    document.querySelectorAll('[data-bs-toggle="collapse"][data-bs-target]').forEach(function (btn) {
      var target = document.querySelector(btn.getAttribute("data-bs-target"));
      if (!target || target.classList.contains("collapsing")) return;
      var shown = target.classList.contains("show");
      btn.setAttribute("aria-expanded", shown ? "true" : "false");
      btn.classList.toggle("collapsed", !shown);
    });
  }

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var cfg = evt.detail.requestConfig;
    if (cfg && cfg.verb === "get") {
      /* Links should land at the top like real navigation. */
      window.scrollTo(0, 0);
    }
    armFlashes();
  });

  /* afterSettle, not afterSwap: during the settle window (20ms) htmx keeps
   * the OLD attributes on every id-matched node so CSS transitions can run,
   * and only then writes the server-rendered ones. At afterSwap a panel the
   * user had open therefore still reads class="collapse show", the reopen
   * below saw nothing to do, and the settle folded it anyway - with the
   * toggle's aria left saying "expanded". */
  document.body.addEventListener("htmx:afterSettle", function (evt) {
    var cfg = evt.detail.requestConfig;
    if (!cfg || cfg.verb !== "get") {
      /* Form posts keep scroll position (hx-swap show:none) and their open
       * accordion panels. A freshly swapped panel has no Collapse instance
       * yet, and Bootstrap reads its state from the class on first use, so
       * adding .show here is exact and instant. Only an element that already
       * has an instance must go through show() - toggling it by hand left
       * Bootstrap thinking it was still closed, so the next click needed two
       * tries or did nothing. */
      openPanels.forEach(function (id) {
        var el = document.getElementById(id);
        if (!el || !el.classList.contains("collapse") || el.classList.contains("show")) return;
        var inst = typeof bootstrap !== "undefined" && bootstrap.Collapse
          && bootstrap.Collapse.getInstance(el);
        if (inst) { inst.show(); } else { el.classList.add("show"); }
      });
    }
    syncToggles();
  });
  /* Unsaved input survives a section save. The case page is a dozen-odd
   * independent forms, and saving any one of them redirects back to the
   * page and swaps #main, which rebuilds every other form from the
   * database - whatever was typed but not yet saved in them was gone.
   * Snapshot every dirty control before the swap (except in the form that
   * was submitted: the server renders that one from what it saved, or
   * with its errors), put the values back once the new DOM has settled,
   * and say so beside that form's save button. Keyed by the form's
   * resolved action (so a form with no action attribute keys on the page
   * URL, like the browser does), its ordinal among forms sharing that
   * action, and the field name - never by id: the shared field include
   * repeats ids such as id_note across forms. Files are skipped - a File
   * cannot be re-set. */
  var UNSAVED_SKIP = { file: 1, hidden: 1, submit: 1, button: 1, reset: 1, image: 1 };
  var unsavedFields = [];

  function isDirty(el) {
    if (el.tagName === "SELECT") {
      return Array.prototype.some.call(el.options, function (o) {
        return o.selected !== o.defaultSelected;
      });
    }
    if (el.type === "checkbox" || el.type === "radio") {
      return el.checked !== el.defaultChecked;
    }
    return el.value !== el.defaultValue;
  }

  function fieldValue(el) {
    if (el.tagName === "SELECT") {
      return Array.prototype.filter.call(el.options, function (o) { return o.selected; })
        .map(function (o) { return o.value; });
    }
    if (el.type === "checkbox" || el.type === "radio") { return el.checked; }
    return el.value;
  }

  /* [{form, key}] for every form in #main, key = resolved action + ordinal. */
  function formKeys() {
    var seen = {};
    return Array.prototype.map.call(document.querySelectorAll("#main form"), function (form) {
      var action = form.action || "";
      seen[action] = (seen[action] || 0) + 1;
      return { form: form, key: action + "#" + seen[action] };
    });
  }

  function collectUnsaved(submitted) {
    var found = [];
    formKeys().forEach(function (entry) {
      var form = entry.form;
      if (form === submitted) { return; }
      Array.prototype.forEach.call(form.elements, function (el) {
        if (!el.name || el.disabled || UNSAVED_SKIP[el.type]) { return; }
        if (el.closest("fieldset:disabled")) { return; }
        if (!isDirty(el)) { return; }
        found.push({
          key: entry.key, name: el.name,
          type: el.type, value: fieldValue(el),
          radio: el.type === "radio" ? el.value : null
        });
      });
    });
    return found;
  }

  function markUnsaved(form) {
    if (form.classList.contains("has-unsaved")) { return; }
    form.classList.add("has-unsaved");
    var submit = form.querySelector('[type="submit"]');
    if (!submit) { return; }
    var hint = document.createElement("span");
    hint.className = "form-unsaved-hint";
    hint.setAttribute("role", "status");
    hint.textContent = document.body.dataset.unsavedHint || "Not saved yet";
    submit.insertAdjacentElement("afterend", hint);
  }

  function restoreUnsaved(fields) {
    var byKey = {};
    formKeys().forEach(function (entry) { byKey[entry.key] = entry.form; });
    fields.forEach(function (f) {
      var form = byKey[f.key];
      if (!form) { return; }
      var el;
      if (f.type === "radio") {
        el = form.querySelector('input[type="radio"][name="' + f.name + '"][value="' + f.radio + '"]');
      } else {
        el = form.elements[f.name];
        if (el && el.length !== undefined && el.tagName === undefined) { el = el[0]; }
      }
      if (!el || el.disabled || el.closest("fieldset:disabled")) { return; }
      if (el.tagName === "SELECT") {
        Array.prototype.forEach.call(el.options, function (o) {
          o.selected = f.value.indexOf(o.value) !== -1;
        });
      } else if (el.type === "checkbox" || el.type === "radio") {
        el.checked = f.value;
      } else {
        el.value = f.value;
      }
      markUnsaved(form);
      /* The section holding it was open when the user typed there; make
       * sure it is open again. No Collapse instance exists on a freshly
       * swapped panel, so the class alone is exact (see afterSettle). */
      var panel = form.closest(".collapse");
      if (panel && !panel.classList.contains("show")) { panel.classList.add("show"); }
    });
  }

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var cfg = evt.detail.requestConfig;
    var submitted = cfg && cfg.elt && cfg.elt.closest ? cfg.elt.closest("form") : null;
    unsavedFields = collectUnsaved(submitted);
  });

  document.body.addEventListener("htmx:afterSettle", function () {
    if (!unsavedFields.length) { return; }
    var fields = unsavedFields;
    unsavedFields = [];
    restoreUnsaved(fields);
    syncToggles();
  });

  document.body.addEventListener("htmx:responseError", showError);
  document.body.addEventListener("htmx:sendError", showError);

  syncToggles();
  armFlashes();
})();

/* Live suggestion for the service price-list card on the quote-create form.
 *
 * Deliberately its own IIFE with no htmx guard, and delegated on
 * document.body: <body hx-boost="true"> re-swaps #main on every boosted
 * navigation and every quote submit, so a listener bound to the form
 * itself would die after the first post.
 *
 * ACT-02: the price list only suggests. An amount or description the
 * user owns - typed now, or server-rendered from a previous quote - is
 * never overwritten. */
(function () {
  var BOX = 'input[name="catalog_items"]';

  /* Fields that already carry a value when they arrive from the server
   * are the user's, not ours; tick them before the first change event. */
  function markOwned(root) {
    (root || document)
      .querySelectorAll('input[name="amount"], textarea[name="description"]')
      .forEach(function (el) {
        if (el.value) { el.dataset.touched = "1"; }
      });
  }

  /* Hours live in a sibling input, so read them off the row, not the box. */
  function hoursOf(box) {
    if (box.dataset.hourly !== "1") { return null; }
    var row = box.closest(".price-item-row");
    var input = row && row.querySelector(".price-item-hours");
    var raw = input && input.value ? input.value.trim() : "";
    return raw && parseFloat(raw) > 0 ? raw : "1";
  }

  function lineTotal(box) {
    var price = parseFloat(box.dataset.price || "0") || 0;
    var hours = hoursOf(box);
    return hours === null ? price : price * parseFloat(hours);
  }

  /* Mirrors QuoteCreateForm.catalog_line so a JS-filled and a
   * server-filled quote read identically. */
  function lineText(box) {
    var hours = hoursOf(box);
    if (hours === null) {
      return box.dataset.name + " \u2014 " + box.dataset.price + " \u20ac";
    }
    return box.dataset.name + " \u2014 " + hours + " h \u00d7 "
         + box.dataset.price + " \u20ac/h";
  }

  function retotal(form) {
    var picked = Array.prototype.slice.call(
      form.querySelectorAll(BOX + ":checked")
    );
    var amount = form.querySelector('input[name="amount"]');
    if (amount && amount.dataset.touched !== "1") {
      /* "" rather than "0": the field has min_value=0.01, and an empty
       * amount is what re-arms the authoritative server-side sum in
       * QuoteCreateForm.clean() for clients without JS. */
      amount.value = picked.length
        ? picked.reduce(function (sum, box) { return sum + lineTotal(box); }, 0).toFixed(2)
        : "";
    }
    var description = form.querySelector('textarea[name="description"]');
    if (description && description.dataset.touched !== "1") {
      description.value = picked.map(lineText).join("\n");
    }
  }

  /* Scoped by name within the submitting form, never by #id_amount:
   * the work-log and transport-offer forms on the same case page emit
   * the same auto-ids. */
  document.body.addEventListener("input", function (event) {
    var name = event.target.name;
    if (name === "amount" || name === "description") {
      event.target.dataset.touched = "1";
    } else if (event.target.classList.contains("price-item-hours")) {
      if (event.target.form) { retotal(event.target.form); }
    }
  });

  document.body.addEventListener("change", function (event) {
    var box = event.target.closest && event.target.closest(BOX);
    if (box && box.form) { retotal(box.form); }
  });

  markOwned(document);
  document.body.addEventListener("htmx:afterSwap", function (event) {
    markOwned(event.target);
  });
})();

/* SVC-04 scheduling pickers: narrow the five-minute slot menu to the
 * opening hours of the weekday the date half is showing, and empty it on
 * a closed day (Sunday). The wrapper carries the windows as
 * data-working-hours; fields that are not the site's own time to keep
 * (TRP-02 carrier windows) simply do not have the attribute and are left
 * on the full 24-hour grid.
 *
 * Courtesy layer only: scheduling.services.book_appointment refuses an
 * out-of-hours time whatever this file does or does not manage to do. */
(function () {
  var WRAP = "[data-working-hours]";

  function narrow(wrap) {
    var date = wrap.querySelector('input[type="date"]');
    var select = wrap.querySelector("select");
    if (!date || !select) return;
    var windows;
    try {
      windows = JSON.parse(wrap.dataset.workingHours);
    } catch (e) {
      return;  /* leave the full menu rather than a broken one */
    }
    /* A time already on file may sit outside today's hours (booked
     * before these rules, or moved by hand). It stays selectable, or
     * re-saving the record would silently move the appointment - the
     * same reason the widget injects it into the choices server-side. */
    if (wrap.dataset.initialSlot === undefined) {
      wrap.dataset.initialSlot = select.value || "";
    }
    var keep = wrap.dataset.initialSlot;

    var parts = (date.value || "").split("-");
    var known = parts.length === 3 && parts[0].length === 4;
    var window_ = null;
    if (known) {
      /* getUTCDay() counts from Sunday, Python's weekday() from Monday. */
      var day = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
      window_ = windows[String((day.getUTCDay() + 6) % 7)] || null;
    }
    var closed = known && window_ === null;

    Array.prototype.forEach.call(select.options, function (option) {
      if (!option.value) return;  /* the "--:--" placeholder always stays */
      var ok =
        !known ||
        option.value === keep ||
        (window_ !== null &&
          option.value >= window_[0] &&
          option.value <= window_[1]);
      option.hidden = !ok;
      option.disabled = !ok;
    });
    if (select.value && select.options[select.selectedIndex].disabled) {
      select.value = "";
    }
    date.classList.toggle("is-invalid", closed);
    date.title = closed ? date.dataset.closedHint || "" : "";
  }

  function narrowAll(root) {
    (root.querySelectorAll ? root : document)
      .querySelectorAll(WRAP)
      .forEach(narrow);
  }

  document.body.addEventListener("change", function (event) {
    var wrap = event.target.closest && event.target.closest(WRAP);
    if (wrap) narrow(wrap);
  });

  narrowAll(document);
  if (typeof htmx !== "undefined") {
    document.body.addEventListener("htmx:afterSwap", function (event) {
      narrowAll(event.target);
    });
  }
})();

/* SVC-10 parts picker: filter the catalogue as you type, and say which
 * route the current selection is heading for before the form is posted.
 * The refusal lives in the guard; this only stops it being a surprise. */
(function () {
  "use strict";

  function matches(row, needle) {
    return !needle || (row.dataset.search || "").indexOf(needle) !== -1;
  }

  function filter(picker) {
    var needle = picker.querySelector(".part-filter").value
      .trim().toLowerCase();
    var shown = 0;
    Array.prototype.forEach.call(
      picker.querySelectorAll(".part-item-row"),
      function (row) {
        /* A ticked part stays visible whatever the filter says - hiding a
         * selection the user cannot see them make is how a form posts
         * something nobody intended. */
        var picked = row.querySelector(".part-pick");
        var visible = (picked && picked.checked) || matches(row, needle);
        row.hidden = !visible;
        if (visible) { shown += 1; }
      }
    );
    return shown;
  }

  function hint(picker) {
    var picked = Array.prototype.slice.call(
      picker.querySelectorAll(".part-pick:checked")
    );
    var target = picker.querySelector(".part-route-hint");
    if (!target) { return; }
    if (!picked.length) { target.textContent = ""; return; }
    var blocked = picked.filter(function (box) {
      var row = box.closest(".part-item-row");
      var qty = row && row.querySelector(".part-qty");
      var need = qty && qty.value ? parseInt(qty.value, 10) : 1;
      return box.dataset.consumable !== "1"
          || parseInt(box.dataset.stock, 10) < need;
    });
    target.textContent = blocked.length
      ? target.dataset.quote || ""
      : target.dataset.shortcut || "";
    target.classList.toggle("text-warning", blocked.length > 0);
  }

  document.body.addEventListener("input", function (event) {
    var picker = event.target.closest && event.target.closest(".part-picker");
    if (!picker) { return; }
    if (event.target.classList.contains("part-filter")) { filter(picker); }
    if (event.target.classList.contains("part-qty")) { hint(picker); }
  });

  document.body.addEventListener("change", function (event) {
    var picker = event.target.closest && event.target.closest(".part-picker");
    if (picker && event.target.classList.contains("part-pick")) {
      hint(picker);
    }
  });
})();
