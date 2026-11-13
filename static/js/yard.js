/* WNT-05 yard placer - the client half of "Raspored u storage".
 *
 * The rules a placement must satisfy (bounds, reserved ground, collision,
 * clearance, exit order) all depend on facts the browser already holds:
 * the other outlines' rectangles and exit dates, the obstacles, the zone
 * extent and its clearance. Before this file existed the page knew none
 * of them, so the only way to find out whether a spot was legal was to
 * submit and read the refusal.
 *
 * This module moves the refusal to the moment of aiming. It is a preview
 * and never a gate: services.validate_free_placement() re-runs on every
 * POST, and it is the authority. Where the two disagree the server wins -
 * which is why every rule below mirrors one named rule there, in the same
 * order, so a divergence shows up as a spot that looked legal and was
 * refused rather than the reverse.
 *
 * THE AIM FOLLOWS THE POINTER. An earlier version froze the outline on
 * the first click and only moved it on the next one, so sliding across
 * the yard left a stationary ghost behind - the placement was invisible
 * right when it was being chosen. Aiming is continuous; only "Place"
 * writes. Leaving the yard deliberately KEEPS the last aim, because the
 * planner has to move the pointer off the plan to reach the button.
 *
 * ONE FILE, AND NO document.currentScript. The page lives inside #main,
 * which htmx swaps wholesale on every boosted navigation. Scripts
 * inserted that way are async: they can run in either order (so a
 * separate binding file could look for an engine that had not loaded
 * yet) and currentScript is null inside them (so reading config off the
 * script tag threw). Both failures were silent, and both left the yard
 * with no outline at all until a hard reload. Config now comes from the
 * DOM, and the binding lives beside the engine.
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  // Legal / illegal. Deliberately NOT the colours the exit ramp uses -
  // green and red mean one thing on this page and one thing only.
  var OK_FILL = "#18702a";
  var BAD_FILL = "#c62828";

  function num(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function fmt(template, values) {
    return String(template).replace(/\{(\w+)\}/g, function (match, key) {
      return Object.prototype.hasOwnProperty.call(values, key)
        ? values[key]
        : match;
    });
  }

  /* Mirrors services._rectangles_overlap: touching edges do not overlap. */
  function overlaps(a, b) {
    return !(a.x1 <= b.x0 || b.x1 <= a.x0 || a.y1 <= b.y0 || b.y1 <= a.y0);
  }

  /* Mirrors services.inflate: only the candidate grows, so the resulting
   * gap between two outlines is exactly the configured clearance. */
  function inflate(r, m) {
    if (!m) return r;
    return { x0: r.x0 - m, y0: r.y0 - m, x1: r.x1 + m, y1: r.y1 + m };
  }

  /* Mirrors services.outline_path: a generic outline inside the footprint,
   * bow towards the exit (lengthwise) or the left edge (crosswise).
   * Same control points as the server so the ghost and the placed asset
   * are the same drawing. */
  function outlinePath(r, crosswise) {
    var length = crosswise ? r.x1 - r.x0 : r.y1 - r.y0;
    var breadth = crosswise ? r.y1 - r.y0 : r.x1 - r.x0;
    var half = breadth / 2;
    function pt(u, v) {
      var x = crosswise ? r.x0 + u : r.x0 + half + v;
      var y = crosswise ? r.y0 + half + v : r.y0 + u;
      return x.toFixed(2) + " " + y.toFixed(2);
    }
    var L = length;
    return [
      "M", pt(0, 0),
      "C", pt(0.12 * L, -0.62 * half), pt(0.36 * L, -half), pt(0.58 * L, -half),
      "L", pt(0.92 * L, -half),
      "Q", pt(L, -half), pt(L, -0.55 * half),
      "L", pt(L, 0.55 * half),
      "Q", pt(L, half), pt(0.92 * L, half),
      "L", pt(0.58 * L, half),
      "C", pt(0.36 * L, half), pt(0.12 * L, 0.62 * half), pt(0, 0),
      "Z",
    ].join(" ");
  }

  function rectOf(item) {
    return {
      x0: num(item.x),
      y0: num(item.y),
      x1: num(item.x) + num(item.w),
      y1: num(item.y) + num(item.h),
      case: item.case,
      name: item.name,
      exit: item.exit,
    };
  }

  function snap(v) {
    return Math.round(v * 2) / 2;
  }

  function attach(options) {
    var labels = options.labels || {};
    var zones = {};
    (options.data || []).forEach(function (z) {
      zones[String(z.pk)] = {
        width: num(z.width),
        length: num(z.length),
        clearance: num(z.clearance),
        assets: (z.assets || []).map(rectOf),
        obstacles: (z.obstacles || []).map(rectOf),
      };
    });

    /* The candidate footprint in metres, honouring the orientation.
     * Matches services.zone_footprint: crosswise turns the outline
     * broadside, so the length runs along the exit edge. */
    function footprint() {
      var dims = options.asset();
      var L = num(dims.length);
      var W = num(dims.width);
      if (dims.crosswise) return { w: L, h: W };
      return { w: W, h: L };
    }

    /* The whole rule set, in the server's order. Returns {ok, msg}. */
    function judge(zone, rect, excludeCase, exitDate) {
      var i;
      if (rect.x1 - rect.x0 > zone.width || rect.y1 - rect.y0 > zone.length) {
        return { ok: false, msg: labels.tooBig };
      }
      if (
        rect.x0 < -1e-6 ||
        rect.y0 < -1e-6 ||
        rect.x1 > zone.width + 1e-6 ||
        rect.y1 > zone.length + 1e-6
      ) {
        return { ok: false, msg: labels.outside };
      }
      for (i = 0; i < zone.obstacles.length; i++) {
        if (overlaps(rect, zone.obstacles[i])) {
          return {
            ok: false,
            msg: fmt(labels.onObstacle, { name: zone.obstacles[i].name }),
          };
        }
      }
      var others = zone.assets.filter(function (b) {
        return String(b.case) !== String(excludeCase);
      });
      for (i = 0; i < others.length; i++) {
        if (overlaps(rect, others[i])) {
          return { ok: false, msg: fmt(labels.collides, { case: others[i].case }) };
        }
      }
      var padded = inflate(rect, zone.clearance);
      for (i = 0; i < others.length; i++) {
        if (overlaps(padded, others[i])) {
          return {
            ok: false,
            msg: fmt(labels.tooClose, {
              case: others[i].case,
              gap: zone.clearance,
            }),
          };
        }
      }
      // Exit order needs a planned exit date to compare against, so the
      // what-if check (which has none) stops here - exactly as the
      // server does when planned_exit_date is absent from the payload.
      if (exitDate === null || exitDate === undefined) {
        return { ok: true, msg: labels.fits };
      }
      // "" means no date recorded, which the server reads as "leaves
      // last". ISO strings compare correctly as strings.
      var mine = exitDate || "9999-12-31";
      for (i = 0; i < others.length; i++) {
        var other = others[i];
        var xOverlap = !(rect.x1 <= other.x0 || other.x1 <= rect.x0);
        if (!xOverlap) continue;
        var theirs = other.exit || "9999-12-31";
        if (other.y0 < rect.y0 && theirs > mine) {
          return { ok: false, msg: fmt(labels.trappedBy, { case: other.case }) };
        }
        if (rect.y0 < other.y0 && mine > theirs) {
          return { ok: false, msg: fmt(labels.wouldTrap, { case: other.case }) };
        }
      }
      return { ok: true, msg: labels.fits };
    }

    var controller = {};

    document.querySelectorAll("svg.yard-svg").forEach(function (svg) {
      var zoneId = svg.getAttribute("data-zone-id");
      var zone = zones[String(zoneId)];
      if (!zone) return;

      var ghost = null;
      var outline = null;
      var halo = null;
      var tag = null;
      var aim = null;

      function ensureGhost() {
        if (ghost) return;
        halo = document.createElementNS(SVG_NS, "rect");
        halo.setAttribute("fill", "none");
        halo.setAttribute("stroke-dasharray", "0.4 0.3");
        halo.setAttribute("stroke-width", "0.08");
        halo.setAttribute("pointer-events", "none");
        svg.appendChild(halo);
        // The footprint (what the rules judge) stays visible under the
        // outline, faintly, exactly as it does for a placed asset.
        ghost = document.createElementNS(SVG_NS, "rect");
        ghost.setAttribute("pointer-events", "none");
        ghost.setAttribute("stroke-dasharray", "0.5 0.3");
        svg.appendChild(ghost);
        outline = document.createElementNS(SVG_NS, "path");
        outline.setAttribute("pointer-events", "none");
        outline.setAttribute("stroke-linejoin", "round");
        svg.appendChild(outline);
        // The outline carries its own name. Unlabelled, it reads as a
        // second, unidentified asset appearing on the plan rather than as
        // the asset you are holding.
        tag = document.createElementNS(SVG_NS, "text");
        tag.setAttribute("pointer-events", "none");
        tag.setAttribute("text-anchor", "middle");
        tag.setAttribute("dominant-baseline", "central");
        tag.setAttribute("font-weight", "600");
        svg.appendChild(tag);
      }

      function clearGhost() {
        [halo, ghost, outline, tag].forEach(function (el) {
          if (el) el.remove();
        });
        halo = ghost = outline = tag = null;
      }

      function draw(rect, verdict) {
        ensureGhost();
        var colour = verdict.ok ? OK_FILL : BAD_FILL;
        var w = rect.x1 - rect.x0;
        var h = rect.y1 - rect.y0;
        ghost.setAttribute("x", rect.x0);
        ghost.setAttribute("y", rect.y0);
        ghost.setAttribute("width", w);
        ghost.setAttribute("height", h);
        ghost.setAttribute("fill", colour);
        ghost.setAttribute("fill-opacity", "0.14");
        ghost.setAttribute("stroke", colour);
        ghost.setAttribute("stroke-width", "0.1");
        outline.setAttribute("d", outlinePath(rect, options.asset().crosswise));
        outline.setAttribute("fill", colour);
        outline.setAttribute("fill-opacity", "0.55");
        outline.setAttribute("stroke", colour);
        outline.setAttribute("stroke-width", "0.22");
        // The clearance the placement is buying, drawn so the planner can
        // see the gap rather than discover it in a refusal.
        var padded = inflate(rect, zone.clearance);
        halo.setAttribute("x", padded.x0);
        halo.setAttribute("y", padded.y0);
        halo.setAttribute("width", padded.x1 - padded.x0);
        halo.setAttribute("height", padded.y1 - padded.y0);
        halo.setAttribute("stroke", colour);
        if (options.label) {
          tag.textContent = options.label;
          tag.setAttribute("x", (rect.x0 + rect.x1) / 2);
          tag.setAttribute("y", (rect.y0 + rect.y1) / 2);
          tag.setAttribute("fill", "#ffffff");
          tag.setAttribute(
            "font-size",
            Math.max(0.3, Math.min(0.85, Math.min(w, h) / 4))
          );
        }
      }

      /* Build the candidate from its lower-left corner, clamped inside
       * the zone. Used by the pointer (which aims at a centre), by typed
       * coordinates, and by a refused submission handing its own back. */
      function fromCorner(x, y, fp) {
        fp = fp || footprint();
        var cx = Math.max(0, Math.min(snap(x), zone.width - fp.w));
        var cy = Math.max(0, Math.min(snap(y), zone.length - fp.h));
        return { x0: cx, y0: cy, x1: cx + fp.w, y1: cy + fp.h };
      }

      function cornerAt(px, py) {
        var fp = footprint();
        return fromCorner(px - fp.w / 2, py - fp.h / 2, fp);
      }

      function toMetres(evt) {
        var pt = svg.createSVGPoint();
        pt.x = evt.clientX;
        pt.y = evt.clientY;
        return pt.matrixTransform(svg.getScreenCTM().inverse());
      }

      /* Take aim. Nothing is written here - this draws the candidate,
       * judges it, and reports it so the form can track it. */
      function setAim(rect) {
        aim = rect;
        var verdict = judge(
          zone,
          rect,
          options.excludeCase,
          options.exitDate ? options.exitDate() : null
        );
        draw(rect, verdict);
        if (options.onAim) options.onAim(zoneId, rect, verdict);
        return verdict;
      }

      function clearAim() {
        aim = null;
        clearGhost();
        if (options.onAim) options.onAim(zoneId, null, null);
      }

      svg.siteClearAim = clearAim;
      svg.siteAimCorner = function (x, y) {
        setAim(fromCorner(x, y));
      };
      /* Re-judge where we already are: the orientation and the exit date
       * both change the answer without moving the asset. */
      svg.siteRefresh = function () {
        if (aim) setAim(fromCorner(aim.x0, aim.y0));
      };

      // pointer* rather than mouse*: the yard manager is standing in the
      // yard with a tablet, not sitting at the desk the old handlers
      // assumed. touch-action:none on .yard-svg keeps a drag from
      // scrolling the page out from under the aim.
      svg.addEventListener("pointermove", function (evt) {
        var p = toMetres(evt);
        setAim(cornerAt(p.x, p.y));
      });

      // No pointerleave handler on purpose. The aim has to survive the
      // trip from the plan to the Place button.

      svg.addEventListener("click", function (evt) {
        var p = toMetres(evt);
        setAim(cornerAt(p.x, p.y));
        svg.focus({ preventScroll: true });
      });

      // Keyboard placement. Nothing in the SVG used to be focusable, so a
      // keyboard user could type coordinates but never see an outline -
      // they were aiming blind. Arrows nudge by the same 0.5 m the
      // pointer snaps to; Escape drops the aim.
      svg.setAttribute("tabindex", "0");
      svg.addEventListener("keydown", function (evt) {
        var step = evt.shiftKey ? 0.5 : 1;
        var dx = 0;
        var dy = 0;
        if (evt.key === "Escape") {
          clearAim();
          return;
        }
        if (evt.key === "ArrowLeft") dx = -step;
        else if (evt.key === "ArrowRight") dx = step;
        else if (evt.key === "ArrowUp") dy = -step;
        else if (evt.key === "ArrowDown") dy = step;
        else if (evt.key === "Enter" || evt.key === " ") {
          if (!aim) setAim(cornerAt(zone.width / 2, zone.length / 2));
          else if (options.onCommit) options.onCommit(zoneId, aim);
          evt.preventDefault();
          return;
        } else return;
        evt.preventDefault();
        var base = aim || cornerAt(zone.width / 2, zone.length / 2);
        setAim(
          cornerAt((base.x0 + base.x1) / 2 + dx, (base.y0 + base.y1) / 2 + dy)
        );
      });
    });

    controller.refresh = function () {
      document.querySelectorAll("svg.yard-svg").forEach(function (svg) {
        if (svg.siteRefresh) svg.siteRefresh();
      });
    };
    controller.aimCorner = function (zoneId, x, y) {
      var svg = document.querySelector(
        'svg.yard-svg[data-zone-id="' + zoneId + '"]'
      );
      if (svg && svg.siteAimCorner) svg.siteAimCorner(x, y);
    };
    controller.clearOthers = function (keepZoneId) {
      document.querySelectorAll("svg.yard-svg").forEach(function (svg) {
        if (
          svg.getAttribute("data-zone-id") !== String(keepZoneId) &&
          svg.siteClearAim
        ) {
          svg.siteClearAim();
        }
      });
    };
    return controller;
  }

  // ------------------------------------------------------------------
  // Bindings. Which one runs is decided by what is in the DOM, so the
  // two yard pages share every rule above and differ only in what they
  // do with a verdict.
  // ------------------------------------------------------------------

  function verdictInto(el, verdict, base) {
    if (!el) return;
    el.textContent = verdict ? verdict.msg : "";
    el.className = base + (verdict ? (verdict.ok ? " is-ok" : " is-bad") : "");
  }

  /* The yard plan: aiming fills the real form, and only Place writes. */
  function bindPlacer(data, labels) {
    var form = document.getElementById("place-form");
    var zoneField = form.querySelector('[name="zone"]');
    var orientField = form.querySelector('[name="orientation"]');
    var xField = form.querySelector('[name="pos_x_m"]');
    var yField = form.querySelector('[name="pos_y_m"]');
    var exitField = form.querySelector('[name="planned_exit_date"]');
    var rotateBtn = document.getElementById("rotate-btn");
    var placeBtn = document.getElementById("place-btn");
    var verdictEl = document.getElementById("place-verdict");

    var LENGTH = parseFloat(form.getAttribute("data-length"));
    var WIDTH = parseFloat(form.getAttribute("data-width"));
    var EXCLUDE = form.getAttribute("data-case");

    var activeZone = null;
    var controller = null;

    // Disabled here rather than in the markup: a page with no JavaScript
    // keeps the old type-the-coordinates-and-press-Place path instead of
    // losing the button entirely.
    function arm(ready) {
      if (placeBtn) placeBtn.disabled = !ready;
    }

    controller = attach({
      data: data,
      labels: labels,
      excludeCase: EXCLUDE,
      label: EXCLUDE ? "#" + EXCLUDE : "",
      asset: function () {
        return {
          length: LENGTH,
          width: WIDTH,
          crosswise: orientField.value === "CROSSWISE",
        };
      },
      // "" (no date) is a real answer the server reads as "leaves last",
      // so it is passed through rather than treated as "do not check".
      exitDate: function () {
        return exitField ? exitField.value : "";
      },
      onAim: function (zoneId, rect, verdict) {
        var zoneEl = document.querySelector(
          '[data-verdict-for="' + zoneId + '"]'
        );
        verdictInto(zoneEl, verdict, "yard-verdict small flex-grow-1 text-end");
        if (!rect) {
          if (String(zoneId) === String(activeZone)) {
            activeZone = null;
            arm(false);
            verdictInto(verdictEl, null, "yard-verdict mb-0 mt-2");
          }
          return;
        }
        activeZone = zoneId;
        zoneField.value = zoneId;
        xField.value = rect.x0.toFixed(2);
        yField.value = rect.y0.toFixed(2);
        arm(true);
        verdictInto(verdictEl, verdict, "yard-verdict mb-0 mt-2");
        controller.clearOthers(zoneId);
      },
      onCommit: function () {
        if (placeBtn && !placeBtn.disabled) form.requestSubmit(placeBtn);
      },
    });

    if (rotateBtn) {
      rotateBtn.addEventListener("click", function () {
        orientField.value =
          orientField.value === "CROSSWISE" ? "LENGTHWISE" : "CROSSWISE";
        controller.refresh();
      });
    }

    // Turning the outline or moving the exit date changes the answer, so the
    // aim is re-judged rather than left showing a stale verdict.
    [orientField, exitField].forEach(function (field) {
      if (field) field.addEventListener("change", controller.refresh);
    });

    if (zoneField) {
      zoneField.addEventListener("change", function () {
        controller.clearOthers(null);
        activeZone = null;
        arm(false);
      });
    }

    // Typed coordinates get an outline too. Before, they were the one
    // path with no preview at all - which is what a keyboard user was
    // left with.
    function aimFromFields() {
      var x = parseFloat(xField.value);
      var y = parseFloat(yField.value);
      if (isNaN(x) || isNaN(y) || !zoneField.value) return false;
      controller.aimCorner(zoneField.value, x, y);
      return true;
    }
    [xField, yField].forEach(function (field) {
      if (field) field.addEventListener("change", aimFromFields);
    });

    // A refused submission comes back with the planner's own numbers
    // still in the form (the server re-renders instead of redirecting).
    // Draw them as a live candidate so the correction is aimed, not
    // retyped.
    if (!aimFromFields()) arm(false);
  }

  /* The pre-contract space check: same rules, nothing to write. */
  function bindCheck(data, labels) {
    var lengthInput = document.getElementById("fit-length");
    var widthInput = document.getElementById("fit-width");
    var rotateBtn = document.getElementById("fit-rotate");
    var orientLabel = document.getElementById("fit-orient");
    var crosswise = false;

    var controller = attach({
      data: data,
      labels: labels,
      // No exit date exists before a contract does, so exit order is left
      // out here exactly as the server leaves it out when no planned exit
      // date is supplied.
      exitDate: null,
      asset: function () {
        return {
          length: parseFloat(lengthInput.value) || 0,
          width: parseFloat(widthInput.value) || 0,
          crosswise: crosswise,
        };
      },
      onAim: function (zoneId, rect, verdict) {
        verdictInto(
          document.querySelector('[data-verdict-for="' + zoneId + '"]'),
          verdict,
          "yard-verdict small"
        );
        if (rect) controller.clearOthers(zoneId);
      },
    });

    if (rotateBtn) {
      rotateBtn.addEventListener("click", function () {
        crosswise = !crosswise;
        if (orientLabel) {
          orientLabel.textContent = crosswise
            ? orientLabel.getAttribute("data-crosswise")
            : orientLabel.getAttribute("data-lengthwise");
        }
        controller.refresh();
      });
    }

    [lengthInput, widthInput].forEach(function (field) {
      if (field) field.addEventListener("change", controller.refresh);
    });
  }

  function boot() {
    var dataEl = document.getElementById("yard-data");
    var labelsEl = document.getElementById("yard-labels");
    if (!dataEl || !labelsEl) return;
    var data = JSON.parse(dataEl.textContent);
    var labels = JSON.parse(labelsEl.textContent);
    if (document.getElementById("place-form")) bindPlacer(data, labels);
    else if (document.getElementById("fit-length")) bindCheck(data, labels);
  }

  boot();
})();
