function boot() {
  bindCopy();
  bindOnce();
  bindAmounts();
  bindCamera();
}

function bindCopy() {
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      const value = button.getAttribute("data-copy") || "";
      navigator.clipboard.writeText(value).then(function () {
        button.textContent = "Скопировано";
      });
    });
  });
}

function bindOnce() {
  document.querySelectorAll("form[data-once]").forEach(function (form) {
    let sent = false;
    form.addEventListener("submit", function (event) {
      if (sent) {
        event.preventDefault();
        return;
      }
      sent = true;
      const button = form.querySelector("[type=submit]");
      if (button) {
        button.disabled = true;
      }
    });
  });
}

function bindAmounts() {
  const input = document.getElementById("amount");
  if (!input) {
    return;
  }
  document.querySelectorAll("[data-add]").forEach(function (button) {
    button.addEventListener("click", function () {
      input.value = formatKopecks(Number(button.getAttribute("data-add")));
      input.focus();
    });
  });
  const all = document.querySelector("[data-all]");
  if (all) {
    all.addEventListener("click", function () {
      input.value = formatKopecks(Number(input.getAttribute("data-balance")));
      input.focus();
    });
  }
}

function bindCamera() {
  const opener = document.getElementById("open-camera");
  const box = document.getElementById("camera-box");
  const video = document.getElementById("camera");
  const note = document.getElementById("camera-note");
  if (!opener || !box || !video || !note) {
    return;
  }
  opener.addEventListener("click", function () {
    startCamera(opener, box, video, note);
  });
}

function startCamera(opener, box, video, note) {
  const ZXing = window.ZXing;
  if (!navigator.mediaDevices || !ZXing || typeof ZXing.BrowserMultiFormatReader !== "function") {
    note.hidden = false;
    note.textContent = "Камера здесь недоступна. Введите код вручную.";
    return;
  }
  opener.disabled = true;
  const hints = new Map();
  hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
    ZXing.BarcodeFormat.CODE_128,
    ZXing.BarcodeFormat.CODE_39,
    ZXing.BarcodeFormat.EAN_13,
    ZXing.BarcodeFormat.EAN_8,
  ]);
  hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
  const reader = new ZXing.BrowserMultiFormatReader(hints);
  let locked = false;
  reader.decodeFromConstraints(
    { audio: false, video: { facingMode: { ideal: "environment" } } },
    video,
    function (result) {
      if (locked || !result) {
        return;
      }
      const code = normalizeScanCode(result.getText());
      if (!code) {
        return;
      }
      locked = true;
      box.classList.add("is-found");
      try {
        reader.reset();
      } catch (_err) {
        /* поток уже останавливается при уходе со страницы */
      }
      window.location.href = "/desk?code=" + encodeURIComponent(code);
    }
  ).then(function () {
    const idle = document.getElementById("scan-idle");
    if (idle) {
      idle.hidden = true;
    }
    box.classList.add("is-live");
    opener.hidden = true;
  }).catch(function () {
    opener.disabled = false;
    note.hidden = false;
    note.textContent = "Камера не открылась. Введите код с карточки.";
  });
}

function normalizeScanCode(raw) {
  const text = String(raw || "").trim();
  if (!text) {
    return "";
  }
  const digits = text.replace(/\D/g, "");
  if (digits.length >= 6) {
    return digits;
  }
  return text.replace(/\s+/g, "").toUpperCase();
}

function formatKopecks(kopecks) {
  const whole = Math.floor(kopecks / 100);
  const frac = kopecks % 100;
  if (frac === 0) {
    return String(whole);
  }
  return whole + "," + String(frac).padStart(2, "0");
}

boot();
