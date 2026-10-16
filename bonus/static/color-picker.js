(function (global) {
  "use strict";

  var STORE_DEFAULT = "teams-bonus-card-colors";

  function normalizeHex(raw) {
    var value = String(raw || "").trim().toUpperCase();
    if (/^[0-9A-F]{6}$/.test(value)) value = "#" + value;
    return /^#[0-9A-F]{6}$/.test(value) ? value : "";
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

  function formatRgb(hex) {
    var rgb = hexToRgb(hex);
    if (!rgb) return "";
    return "R " + rgb.r + " · G " + rgb.g + " · B " + rgb.b;
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
    var grid = root.querySelector("[data-color-grid]");
    var savedWrap = root.querySelector("[data-color-saved]");
    var native = root.querySelector("[data-color-native]");
    var face = root.querySelector("[data-color-face]");
    var hexInput = root.querySelector("[data-color-hex]");
    var rgbLabel = root.querySelector("[data-color-rgb]");
    var eyedrop = root.querySelector("[data-color-eyedrop]");
    var onChange = typeof opts.onChange === "function" ? opts.onChange : null;

    function radioName() {
      var first = root.querySelector('input[type="radio"][data-color-radio]');
      return first ? first.name : "color";
    }

    function presetValues() {
      return Array.prototype.map
        .call(root.querySelectorAll(".color-option:not(.color-picker-option):not(.color-saved-option) input[data-color-radio]"), function (input) {
          return normalizeHex(input.value);
        })
        .filter(Boolean);
    }

    function currentValue() {
      var checked = root.querySelector('input[data-color-radio]:checked');
      return checked ? normalizeHex(checked.value) : "";
    }

    function syncCodes(hex) {
      var value = normalizeHex(hex) || currentValue() || "#00A8A8";
      if (hexInput && document.activeElement !== hexInput) hexInput.value = value;
      if (rgbLabel) rgbLabel.textContent = formatRgb(value);
      if (native) native.value = value;
    }

    function emit(hex) {
      syncCodes(hex);
      if (onChange) onChange(hex, root);
      root.dispatchEvent(new CustomEvent("colorchange", { detail: { hex: hex }, bubbles: true }));
    }

    function selectColor(value, skipSave) {
      var hex = normalizeHex(value);
      if (!hex) return;
      var radio = root.querySelector('input[data-color-radio][value="' + hex + '"]');
      if (!radio && !skipSave) {
        saveCustomColor(hex);
        return;
      }
      if (!radio) return;
      radio.checked = true;
      emit(hex);
    }

    function bindRadio(radio) {
      radio.addEventListener("change", function () {
        if (radio.checked) emit(radio.value);
      });
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
          removeSavedColor(hex);
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
      if (native) native.value = hex;
      var presets = presetValues();
      if (presets.indexOf(hex) !== -1) {
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

    function removeSavedColor(hex) {
      var cleaned = normalizeHex(hex);
      var list = loadSaved(storeKey).filter(function (item) {
        return item !== cleaned;
      });
      writeSaved(storeKey, list);
      var wasSelected = currentValue() === cleaned;
      renderSaved();
      if (wasSelected) {
        var first = root.querySelector(".color-option:not(.color-picker-option) input[data-color-radio]");
        if (first) {
          first.checked = true;
          emit(first.value);
        }
      }
    }

    Array.prototype.forEach.call(root.querySelectorAll("input[data-color-radio]"), bindRadio);

    if (native) {
      native.addEventListener("input", function () {
        var hex = normalizeHex(native.value);
        if (!hex) return;
        syncCodes(hex);
        if (onChange) onChange(hex, root);
      });
      native.addEventListener("change", function () {
        saveCustomColor(native.value);
      });
    }

    if (hexInput) {
      hexInput.addEventListener("input", function () {
        var typed = String(hexInput.value || "").trim().toUpperCase();
        if (typed && typed.charAt(0) !== "#") typed = "#" + typed;
        hexInput.value = typed.slice(0, 7);
        var hex = normalizeHex(hexInput.value);
        if (!hex) return;
        if (rgbLabel) rgbLabel.textContent = formatRgb(hex);
        if (native) native.value = hex;
      });
      hexInput.addEventListener("change", function () {
        saveCustomColor(hexInput.value);
      });
      hexInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          saveCustomColor(hexInput.value);
          hexInput.blur();
        }
      });
    }

    if (eyedrop) {
      if (!global.EyeDropper) {
        eyedrop.title = "Пипетка недоступна в этом браузере — откройте палитру";
      }
      eyedrop.addEventListener("click", function () {
        if (global.EyeDropper) {
          var picker = new global.EyeDropper();
          picker
            .open()
            .then(function (result) {
              saveCustomColor(result.sRGBHex);
            })
            .catch(function () {});
          return;
        }
        if (native) native.click();
      });
    }

    renderSaved();
    syncCodes(currentValue() || (native && native.value) || "#00A8A8");

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
    formatRgb: formatRgb,
  };
})(window);
