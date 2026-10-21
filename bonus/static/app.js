function boot() {
  bindCopy();
  bindOnce();
  bindAmounts();
  bindCamera();
  bindPartnersDialog();
  bindTiltCards();
}

function bindTiltCards() {
  if (typeof createTilt !== "function") {
    return;
  }
  document.querySelectorAll(".wallet-card.tilt-card").forEach(function (card) {
    createTilt(card, { maxAngle: 9, gyro: false });
  });
}

function bindPartnersDialog() {
  document.querySelectorAll("[data-partners-dialog]").forEach(function (dialog) {
    if (typeof dialog.showModal !== "function") {
      return;
    }
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) {
        dialog.close();
      }
    });
    dialog.addEventListener("close", function () {
      document.body.classList.remove("partners-dialog-open");
    });
  });
  document.querySelectorAll("[data-partners-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      const targetId = button.getAttribute("data-partners-target") || "partners-dialog";
      const dialog = document.getElementById(targetId);
      if (dialog && typeof dialog.showModal === "function") {
        document.body.classList.add("partners-dialog-open");
        dialog.showModal();
      }
    });
  });
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
  if (!window.isSecureContext) {
    showCameraNote(note, "Камера работает только по HTTPS. Введите код вручную.");
    return;
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
    showCameraNote(note, "Браузер не даёт доступ к камере. Введите код вручную.");
    return;
  }
  opener.disabled = true;
  note.hidden = true;
  navigator.mediaDevices
    .getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
    })
    .then(function (stream) {
      const idle = document.getElementById("scan-idle");
      if (idle) {
        idle.hidden = true;
      }
      box.classList.add("is-live");
      opener.hidden = true;
      video.srcObject = stream;
      video.setAttribute("playsinline", "true");
      video.muted = true;
      return video.play().then(function () {
        return stream;
      });
    })
    .then(function (stream) {
      return watchBarcode(video, stream, box);
    })
    .catch(function (error) {
      opener.disabled = false;
      opener.hidden = false;
      box.classList.remove("is-live");
      const idle = document.getElementById("scan-idle");
      if (idle) {
        idle.hidden = false;
      }
      showCameraNote(note, cameraErrorText(error));
    });
}

function watchBarcode(video, stream, box) {
  if (typeof BarcodeDetector === "function") {
    return watchWithBarcodeDetector(video, stream, box);
  }
  return watchWithZxing(video, stream, box);
}

function watchWithBarcodeDetector(video, stream, box) {
  let locked = false;
  const detector = new BarcodeDetector({
    formats: ["code_128", "code_39", "ean_13", "ean_8"],
  });
  return new Promise(function (resolve, reject) {
    const timer = window.setInterval(function () {
      if (locked || video.readyState < 2) {
        return;
      }
      detector
        .detect(video)
        .then(function (codes) {
          if (locked || !codes || !codes.length) {
            return;
          }
          const code = normalizeScanCode(codes[0].rawValue || "");
          if (!code) {
            return;
          }
          locked = true;
          window.clearInterval(timer);
          finishScan(box, stream, code);
          resolve(code);
        })
        .catch(function (error) {
          window.clearInterval(timer);
          stream.getTracks().forEach(function (track) {
            track.stop();
          });
          reject(error);
        });
    }, 250);
  });
}

function watchWithZxing(video, stream, box) {
  const ZXing = window.ZXing;
  if (!ZXing || typeof ZXing.BrowserMultiFormatReader !== "function") {
    stream.getTracks().forEach(function (track) {
      track.stop();
    });
    return Promise.reject(new Error("zxing-missing"));
  }
  const hints = new Map();
  hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
    ZXing.BarcodeFormat.CODE_128,
    ZXing.BarcodeFormat.CODE_39,
    ZXing.BarcodeFormat.EAN_13,
    ZXing.BarcodeFormat.EAN_8,
  ]);
  hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
  const reader = new ZXing.BrowserMultiFormatReader(hints, 250);
  let locked = false;
  return reader.decodeFromStream(stream, video, function (result) {
    if (locked || !result) {
      return;
    }
    const code = normalizeScanCode(result.getText());
    if (!code) {
      return;
    }
    locked = true;
    try {
      reader.reset();
    } catch (_err) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
    finishScan(box, null, code);
  });
}

function finishScan(box, stream, code) {
  box.classList.add("is-found");
  if (stream) {
    stream.getTracks().forEach(function (track) {
      track.stop();
    });
  }
  window.location.href = "/desk?code=" + encodeURIComponent(code);
}

function showCameraNote(note, text) {
  note.hidden = false;
  note.textContent = text;
}

function cameraErrorText(error) {
  const name = error && error.name ? error.name : "";
  const message = error && error.message ? String(error.message) : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "Нет разрешения на камеру. Разрешите доступ в браузере и нажмите ещё раз.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "Камера не найдена. Введите код вручную.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "Камера занята другим приложением. Закройте его и попробуйте снова.";
  }
  if (message === "zxing-missing") {
    return "Не загрузилась библиотека сканера. Обновите страницу и попробуйте снова.";
  }
  return "Камера не открылась. Обновите страницу или введите код вручную.";
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
