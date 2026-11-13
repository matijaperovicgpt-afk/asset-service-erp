/* WNT-09 yard editor - the preview beside the zone form.
 *
 * Nothing is drawn by hand here. The preview follows the form: typing a
 * new width or length re-scales the plan, and choosing a picture shows it
 * under the grid before it is saved (FileReader, so no round trip). The
 * parked assets and the reserved ground come from the server as JSON and
 * are drawn read-only, so the planner sees what a smaller rectangle
 * would cut off.
 *
 * Same htmx caveat as yard.js: this runs inside #main, which hx-boost
 * swaps, so config is read from the DOM and there is no currentScript.
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  function num(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function fmt(v) {
    return (Math.round(v * 100) / 100).toFixed(2);
  }

  function el(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    return node;
  }

  function boot() {
    var form = document.getElementById("zone-form");
    var svg = document.getElementById("yard-editor-svg");
    var dataEl = document.getElementById("yard-editor-data");
    if (!form || !svg || !dataEl) return;

    var data = JSON.parse(dataEl.textContent);
    var widthField = form.querySelector('[name="width_m"]');
    var lengthField = form.querySelector('[name="length_m"]');
    var imageField = form.querySelector('[name="plan_image"]');

    var boxRect = document.getElementById("editor-box");
    var gridRect = document.getElementById("editor-grid-rect");
    var planImage = document.getElementById("editor-plan-image");
    var assetsG = document.getElementById("editor-assets");
    var obstaclesG = document.getElementById("editor-obstacles");
    var exitRect = document.getElementById("editor-exit");
    var exitLabel = document.getElementById("editor-exit-label");

    function box() {
      return {
        w: Math.max(1, num(widthField.value)),
        h: Math.max(1, num(lengthField.value)),
      };
    }

    function drawBox() {
      var b = box();
      svg.setAttribute("viewBox", "0 0 " + fmt(b.w) + " " + fmt(b.h));
      [boxRect, gridRect, planImage].forEach(function (node) {
        node.setAttribute("width", fmt(b.w));
        node.setAttribute("height", fmt(b.h));
      });
      exitRect.setAttribute("width", fmt(b.w));
      exitLabel.setAttribute("x", fmt(b.w));
    }

    function labelSize(w, h) {
      return fmt(Math.max(0.3, Math.min(0.85, Math.min(w, h) / 4)));
    }

    function drawObstacles() {
      (data.obstacles || []).forEach(function (o) {
        var x = num(o.x);
        var y = num(o.y);
        var w = num(o.w);
        var h = num(o.h);
        var g = el("g", { class: "yard-obstacle" });
        g.appendChild(
          el("rect", {
            x: fmt(x),
            y: fmt(y),
            width: fmt(w),
            height: fmt(h),
            fill: "url(#editor-hatch)",
            stroke: "#5d6a74",
            "stroke-width": "0.12",
            "stroke-dasharray": "0.6 0.4",
          })
        );
        var t = el("text", {
          x: fmt(x + w / 2),
          y: fmt(y + h / 2),
          "font-size": labelSize(w, h),
          fill: "#3c4750",
          "text-anchor": "middle",
          "dominant-baseline": "central",
        });
        t.textContent = o.name;
        g.appendChild(t);
        obstaclesG.appendChild(g);
      });
    }

    function drawAssets() {
      (data.assets || []).forEach(function (b) {
        var x = num(b.x);
        var y = num(b.y);
        var w = num(b.w);
        var h = num(b.h);
        var g = el("g", { class: "yard-asset" });
        g.appendChild(
          el("rect", {
            x: fmt(x),
            y: fmt(y),
            width: fmt(w),
            height: fmt(h),
            fill: "#2a788e",
            "fill-opacity": "0.14",
            stroke: "#2a788e",
            "stroke-width": "0.06",
            "stroke-dasharray": "0.5 0.3",
          })
        );
        g.appendChild(
          el("path", {
            d: b.d,
            fill: "#2a788e",
            "fill-opacity": "0.55",
            stroke: "#22303a",
            "stroke-width": "0.1",
          })
        );
        var t = el("text", {
          x: fmt(x + w / 2),
          y: fmt(y + h / 2),
          "font-size": labelSize(w, h),
          fill: "#ffffff",
          "text-anchor": "middle",
          "dominant-baseline": "central",
        });
        t.textContent = "#" + b.case;
        g.appendChild(t);
        assetsG.appendChild(g);
      });
    }

    function showImage(url) {
      if (url) {
        planImage.setAttribute("href", url);
        planImage.removeAttribute("visibility");
      } else {
        planImage.removeAttribute("href");
        planImage.setAttribute("visibility", "hidden");
      }
    }

    [widthField, lengthField].forEach(function (f) {
      if (f) f.addEventListener("input", drawBox);
    });

    showImage(data.planImage || "");
    if (imageField) {
      imageField.addEventListener("change", function () {
        var file = imageField.files && imageField.files[0];
        if (!file || !window.FileReader) {
          showImage(data.planImage || "");
          return;
        }
        var reader = new FileReader();
        reader.onload = function () {
          showImage(reader.result);
        };
        reader.readAsDataURL(file);
      });
    }

    drawBox();
    drawObstacles();
    drawAssets();
  }

  boot();
})();
