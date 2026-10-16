(function (global) {
  "use strict";

  var STORE_DEFAULT = "teams-bonus-card-colors";

  function normalizeHex(raw) {
    var value = String(raw || "").trim().toUpperCase();
    if (/^[0-9A-F]{6}$/.test(value)) value = "#" + value;
    return /^#[0-9A-F]{6}$/.test(value) ? value : "";
  }

  function clamp(n, min, max) {
    return Math.min(max, Math.max(min, n));
  }

  function hexToRgb(hex) {
    var clean = normalizeHex(hex);
    if (!clean) return null;
    return {
      r: parseInt(clean.slice(1, 3), 16),
      g: parseInt(clean.slice(3, 5), 16),
      b: parseInt(clean.slice(5, 7), 16),
    };
  }

  function rgbToHex(r, g, b) {
    function part(n) {
      var h = clamp(Math.round(n), 0, 255).toString(16).toUpperCase();
      return h.length === 1 ? "0" + h : h;
    }
    return "#" + part(r) + part(g) + part(b);
  }

  function rgbToHsv(r, g, b) {
    r /= 255;
    g /= 255;
    b /= 255;
    var max = Math.max(r, g, b);
    var min = Math.min(r, g, b);
    var d = max - min;
    var h = 0;
    var s = max === 0 ? 0 : d / max;
    var v = max;
    if (d !== 0) {
      if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
      else if (max === g) h = ((b - r) / d + 2) / 6;
      else h = ((r - g) / d + 4) / 6;
    }
    return { h: h * 360, s: s, v: v };
  }

  function hsvToRgb(h, s, v) {
    h = ((h % 360) + 360) % 360;
    var c = v * s;
    var x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    var m = v - c;
    var r = 0;
    var g = 0;
    var b = 0;
    if (h < 60) {
      r = c;
      g = x;
    } else if (h < 120) {
      r = x;
      g = c;
    } else if (h < 180) {
      g = c;
      b = x;
    } else if (h < 240) {
      g = x;
      b = c;
    } else if (h < 300) {
      r = x;
      b = c;
    } else {
      r = c;
      b = x;
    }
    return {
      r: Math.round((r + m) * 255),
      g: Math.round((g + m) * 255),
      b: Math.round((b + m) * 255),
    };
  }

  function loadSaved(storeKey) {
    try {
      var raw = localStorage.getItem(storeKey || STORE_DEFAULT);
      var list = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(list)) return [];
      return list.map(normalizeHex).filter(Boolean);
    } catch (err) {
      return [];
    }
  }

  function writeSaved(storeKey, list) {
    localStorage.setItem(storeKey || STORE_DEFAULT, JSON.stringify(list));
  }

  function createPicker(root, options) {
    var opts = options || {};
    var storeKey = root.getAttribute("data-color-store") || STORE_DEFAULT;
    var savedWrap = root.querySelector("[data-color-saved]");
    var openBtn = root.querySelector("[data-color-open]");
    var popover = root.querySelector("[data-color-popover]");
    var sv = root.querySelector("[data-color-sv]");
    var knob = root.querySelector("[data-color-sv-knob]");
    var hue = root.querySelector("[data-color-hue]");
    var preview = root.querySelector("[data-color-preview]");
    var eyedrop = root.querySelector("[data-color-eyedrop]");
    var hexInput = root.querySelector("[data-color-hex]");
    var rInput = root.querySelector("[data-color-r]");
    var gInput = root.querySelector("[data-color-g]");
    var bInput = root.querySelector("[data-color-b]");
    var applyBtn = root.querySelector("[data-color-apply]");
    var cancelBtn = root.querySelector("[data-color-cancel]");
    var onChange = typeof opts.onChange === "function" ? opts.onChange : null;
    var state = { h: 180, s: 1, v: 1 };
    var draftHex = "#00A8A8";
    var openBefore = "";

    function radioName() {
      var first = root.querySelector("input[type='radio'][data-color-radio]");
      return first ? first.name : "color";
    }

    function presetValues() {
      return Array.prototype.map
        .call(
          root.querySelectorAll(
            ".color-option:not(.color-saved-option) input[data-color-radio]"
          ),
          function (input) {
            return normalizeHex(input.value);
          }
        )
        .filter(Boolean);
    }

    function currentValue() {
      var checked = root.querySelector("input[data-color-radio]:checked");
      return checked ? normalizeHex(checked.value) : "";
    }

    function emit(hex) {
      if (onChange) onChange(hex, root);
      root.dispatchEvent(
        new CustomEvent("colorchange", { detail: { hex: hex }, bubbles: true })
      );
    }

    function setDraftFromHex(hex, skipInputs) {
      var clean = normalizeHex(hex) || "#00A8A8";
      var rgb = hexToRgb(clean);
      var hsv = rgbToHsv(rgb.r, rgb.g, rgb.b);
      state.h = hsv.h;
      state.s = hsv.s;
      state.v = hsv.v;
      draftHex = clean;
      paintPanel(skipInputs);
    }

    function setDraftFromHsv(skipInputs) {
      var rgb = hsvToRgb(state.h, state.s, state.v);
      draftHex = rgbToHex(rgb.r, rgb.g, rgb.b);
      paintPanel(skipInputs);
    }

    function paintPanel(skipInputs) {
      if (sv) {
        sv.style.background =
          "linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, hsl(" +
          state.h +
          " 100% 50%))";
      }
      if (knob && sv) {
        knob.style.left = state.s * 100 + "%";
        knob.style.top = (1 - state.v) * 100 + "%";
      }
      if (hue) hue.value = String(Math.round(state.h));
      if (preview) preview.style.background = draftHex;
      if (!skipInputs) {
        if (hexInput && document.activeElement !== hexInput) hexInput.value = draftHex;
        var rgb = hexToRgb(draftHex);
        if (rgb) {
          if (rInput && document.activeElement !== rInput) rInput.value = String(rgb.r);
          if (gInput && document.activeElement !== gInput) gInput.value = String(rgb.g);
          if (bInput && document.activeElement !== bInput) bInput.value = String(rgb.b);
        }
      }
    }

    function bindRadio(radio) {
      radio.addEventListener("change", function () {
        if (radio.checked) emit(radio.value);
      });
    }

    function selectColor(hex, skipSave) {
      var clean = normalizeHex(hex);
      if (!clean) return;
      var radio = root.querySelector('input[data-color-radio][value="' + clean + '"]');
      if (!radio && !skipSave) {
        saveCustomColor(clean);
        return;
      }
      if (!radio) return;
      radio.checked = true;
      emit(clean);
    }

    function renderSaved(selectHex) {
      if (!savedWrap) return;
      var list = loadSaved(storeKey);
      savedWrap.innerHTML = "";
      list.forEach(function (hex) {
        var label = document.createElement("label");
        label.className = "color-option color-saved-option";
        label.title = hex;
        var radio = document.createElement("input");
        radio.type = "radio";
        radio.name = radioName();
        radio.value = hex;
        radio.setAttribute("data-color-radio", "");
        bindRadio(radio);
        var swatch = document.createElement("span");
        swatch.style.background = hex;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "color-remove";
        remove.setAttribute("aria-label", "Удалить цвет " + hex);
        remove.textContent = "×";
        remove.addEventListener("click", function (event) {
          event.preventDefault();
          event.stopPropagation();
          removeSaved(hex);
        });
        label.appendChild(radio);
        label.appendChild(swatch);
        label.appendChild(remove);
        savedWrap.appendChild(label);
      });
      if (selectHex) selectColor(selectHex, true);
    }

    function saveCustomColor(raw) {
      var hex = normalizeHex(raw);
      if (!hex) return;
      if (presetValues().indexOf(hex) !== -1) {
        selectColor(hex, true);
        return;
      }
      var list = loadSaved(storeKey);
      if (list.indexOf(hex) === -1) {
        list.push(hex);
        writeSaved(storeKey, list);
      }
      renderSaved(hex);
      emit(hex);
    }

    function removeSaved(hex) {
      var cleaned = normalizeHex(hex);
      writeSaved(
        storeKey,
        loadSaved(storeKey).filter(function (item) {
          return item !== cleaned;
        })
      );
      var wasSelected = currentValue() === cleaned;
      renderSaved();
      if (wasSelected) {
        var first = root.querySelector(".color-option input[data-color-radio]");
        if (first) {
          first.checked = true;
          emit(first.value);
        }
      }
    }

    function closePopover(restore) {
      if (!popover || popover.hidden) return;
      popover.hidden = true;
      if (openBtn) openBtn.setAttribute("aria-expanded", "false");
      if (restore && openBefore) selectColor(openBefore);
    }

    function openPopover() {
      if (!popover) return;
      openBefore = currentValue() || draftHex || "#00A8A8";
      setDraftFromHex(openBefore);
      popover.hidden = false;
      if (openBtn) openBtn.setAttribute("aria-expanded", "true");
    }

    function applyPopover() {
      saveCustomColor(draftHex);
      closePopover(false);
    }

    function readSvFromEvent(event) {
      if (!sv) return;
      var rect = sv.getBoundingClientRect();
      var x = clamp((event.clientX - rect.left) / rect.width, 0, 1);
      var y = clamp((event.clientY - rect.top) / rect.height, 0, 1);
      state.s = x;
      state.v = 1 - y;
      setDraftFromHsv();
    }

    Array.prototype.forEach.call(root.querySelectorAll("input[data-color-radio]"), bindRadio);

    if (openBtn) {
      openBtn.addEventListener("click", function (event) {
        event.preventDefault();
        if (popover && !popover.hidden) closePopover(true);
        else openPopover();
      });
    }

    if (sv) {
      sv.addEventListener("pointerdown", function (event) {
        sv.setPointerCapture(event.pointerId);
        readSvFromEvent(event);
      });
      sv.addEventListener("pointermove", function (event) {
        if (event.buttons) readSvFromEvent(event);
      });
    }

    if (hue) {
      hue.addEventListener("input", function () {
        state.h = Number(hue.value) || 0;
        setDraftFromHsv();
      });
    }

    if (eyedrop) {
      eyedrop.addEventListener("click", function () {
        if (!global.EyeDropper) {
          alert("Пипетка недоступна в этом браузере");
          return;
        }
        var dropper = new global.EyeDropper();
        dropper
          .open()
          .then(function (result) {
            setDraftFromHex(result.sRGBHex);
          })
          .catch(function () {});
      });
    }

    if (hexInput) {
      hexInput.addEventListener("input", function () {
        var typed = String(hexInput.value || "").trim().toUpperCase();
        if (typed && typed.charAt(0) !== "#") typed = "#" + typed;
        hexInput.value = typed.slice(0, 7);
        var hex = normalizeHex(hexInput.value);
        if (hex) setDraftFromHex(hex, true);
      });
      hexInput.addEventListener("change", function () {
        setDraftFromHex(hexInput.value);
      });
    }

    function bindRgb() {
      var r = clamp(Number(rInput && rInput.value) || 0, 0, 255);
      var g = clamp(Number(gInput && gInput.value) || 0, 0, 255);
      var b = clamp(Number(bInput && bInput.value) || 0, 0, 255);
      setDraftFromHex(rgbToHex(r, g, b), true);
      if (hexInput) hexInput.value = draftHex;
    }

    [rInput, gInput, bInput].forEach(function (input) {
      if (!input) return;
      input.addEventListener("input", bindRgb);
      input.addEventListener("change", bindRgb);
    });

    if (applyBtn) applyBtn.addEventListener("click", applyPopover);
    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        closePopover(true);
      });
    }

    document.addEventListener("pointerdown", function (event) {
      if (!popover || popover.hidden) return;
      if (root.contains(event.target)) return;
      closePopover(true);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closePopover(true);
    });

    renderSaved();
    setDraftFromHex(currentValue() || "#00A8A8");

    return {
      root: root,
      apply: function (hex) {
        saveCustomColor(hex);
      },
      value: currentValue,
    };
  }

  function initAll(options) {
    var map = {};
    Array.prototype.forEach.call(document.querySelectorAll("[data-color-picker]"), function (root) {
      if (root._teamsColorPicker) return;
      var api = createPicker(root, options);
      root._teamsColorPicker = api;
      map[root.getAttribute("data-color-picker") || "main"] = api;
    });
    return map;
  }

  global.TeamsBonusColorPicker = {
    initAll: initAll,
    normalizeHex: normalizeHex,
  };
})(window);
