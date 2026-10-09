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
  if (!navigator.mediaDevices || typeof jsQR !== "function") {
    note.hidden = false;
    note.textContent = "Камера здесь недоступна. Введите код вручную или наведите обычную камеру телефона на QR.";
    return;
  }
  opener.disabled = true;
  navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (stream) {
    const idle = document.getElementById("scan-idle");
    if (idle) {
      idle.hidden = true;
    }
    box.classList.add("is-live");
    opener.hidden = true;
    video.srcObject = stream;
    video.play();
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });
    let locked = false;
    window.setInterval(function () {
      if (locked || !context || video.readyState < 2) {
        return;
      }
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      context.drawImage(video, 0, 0);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      const found = jsQR(pixels.data, pixels.width, pixels.height);
      if (found && found.data) {
        locked = true;
        box.classList.add("is-found");
        stream.getTracks().forEach(function (track) { track.stop(); });
        window.location.href = "/desk?code=" + encodeURIComponent(found.data);
      }
    }, 250);
  }).catch(function () {
    opener.disabled = false;
    note.hidden = false;
    note.textContent = "Камера не открылась. Введите код с карточки или отсканируйте QR обычной камерой — откроется страница списания.";
  });
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
