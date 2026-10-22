/**
 * birthday-unwrap.js — порт BirthdayUnwrap.tsx для Jinja-страницы.
 */
(function (global) {
  "use strict";

  var STEPS = ["", "is-seal", "is-flap", "is-flapback", "is-card", "is-reveal"];
  var UNWRAP_TIMING = { flap: 250, flapBack: 650, card: 650, reveal: 2350 };
  var CARD_W = 270;

  function storageKey(cardId) {
    return "gift-opened:" + String(cardId);
  }

  function readLocalOpened(cardId) {
    try {
      return window.localStorage.getItem(storageKey(cardId)) === "1";
    } catch (err) {
      return false;
    }
  }

  function writeLocalOpened(cardId) {
    try {
      window.localStorage.setItem(storageKey(cardId), "1");
    } catch (err) {
      /* ignore */
    }
  }

  function mountBirthdayUnwrap(root) {
    if (!root) {
      return null;
    }
    var preview = root.getAttribute("data-preview") === "1";
    var cardId = root.getAttribute("data-card-id") || "";
    var markUrl = root.getAttribute("data-mark-url") || "";
    var nativeW = Number(root.getAttribute("data-native-w") || "360");
    var nativeH = Number(root.getAttribute("data-native-h") || "560");
    var step = 0;
    var timers = [];
    var stopConfetti = null;
    var stage = root.querySelector("[data-bday-stage]");
    var cardBox = root.querySelector("[data-bday-card]");
    var cardFit = root.querySelector("[data-bday-fit]");
    var canvas = root.querySelector("[data-bday-fx]");
    var mainEl = document.getElementById("card-main");
    var wallet = cardFit ? cardFit.querySelector(".wallet-card") : null;

    if (cardFit) {
      cardFit.style.width = nativeW + "px";
      if (wallet) {
        wallet.style.width = nativeW + "px";
        wallet.style.maxWidth = "none";
        wallet.style.margin = "0";
        // Высота реальной карты после раскладки.
        nativeH = Math.max(wallet.offsetHeight || nativeH, 400);
      }
      cardFit.style.height = nativeH + "px";
    }
    var cardH = Math.round((CARD_W * nativeH) / nativeW);
    var fitScale = CARD_W / nativeW;
    if (cardFit) {
      cardFit.style.transform = "scale(" + fitScale + ")";
    }
    if (cardBox) {
      cardBox.style.width = CARD_W + "px";
      cardBox.style.height = cardH + "px";
    }
    stage.style.setProperty("--bday-card-h", cardH + "px");

    function setK() {
      var h = stage.clientHeight || window.innerHeight;
      var k = Math.min(1, Math.max(0.5, (h - 190 - 120) / cardH));
      stage.style.setProperty("--bday-k", String(k));
      if (canvas) {
        var dpr = Math.min(2, window.devicePixelRatio || 1);
        canvas.width = stage.clientWidth * dpr;
        canvas.height = stage.clientHeight * dpr;
        var ctx = canvas.getContext("2d");
        if (ctx) {
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
      }
    }

    function paintStep() {
      var cls = ["bday"];
      if (step === 0) {
        cls.push("is-idle");
      }
      for (var i = 1; i <= step; i++) {
        if (STEPS[i]) {
          cls.push(STEPS[i]);
        }
      }
      stage.className = cls.join(" ");
    }

    function at(ms, fn) {
      timers.push(window.setTimeout(fn, ms));
    }

    function launchConfetti() {
      if (typeof burstConfetti !== "function" || !canvas || !cardBox) {
        return;
      }
      var s = stage.getBoundingClientRect();
      var c = cardBox.getBoundingClientRect();
      stopConfetti = burstConfetti(canvas, {
        origin: {
          x: c.left - s.left + c.width / 2,
          y: c.top - s.top + c.height * 0.5,
        },
        spread: c.width * 0.6,
        width: s.width,
        height: s.height,
      });
    }

    function openGift() {
      if (step !== 0) {
        return;
      }
      var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduce) {
        step = 5;
        paintStep();
        return;
      }
      try {
        if (navigator.vibrate) {
          navigator.vibrate(12);
        }
      } catch (err) {
        /* ignore */
      }
      step = 1;
      paintStep();
      at(UNWRAP_TIMING.flap, function () {
        step = 2;
        paintStep();
      });
      at(UNWRAP_TIMING.flapBack, function () {
        step = 3;
        paintStep();
      });
      at(UNWRAP_TIMING.card, function () {
        step = 4;
        paintStep();
      });
      at(UNWRAP_TIMING.reveal, function () {
        step = 5;
        paintStep();
        launchConfetti();
      });
    }

    function markOpened() {
      if (preview) {
        return;
      }
      if (cardId) {
        writeLocalOpened(cardId);
      }
      if (markUrl) {
        try {
          fetch(markUrl, { method: "POST", credentials: "same-origin" });
        } catch (err) {
          /* ignore */
        }
      }
    }

    function goToCard() {
      markOpened();
      timers.forEach(clearTimeout);
      timers = [];
      if (stopConfetti) {
        stopConfetti();
        stopConfetti = null;
      }
      if (preview) {
        window.history.back();
        return;
      }
      root.hidden = true;
      root.remove();
      if (mainEl) {
        mainEl.hidden = false;
        if (typeof bindTiltCards === "function") {
          bindTiltCards();
        }
      } else {
        window.location.reload();
      }
    }

    root.querySelectorAll("[data-bday-open]").forEach(function (btn) {
      btn.addEventListener("click", openGift);
    });
    root.querySelectorAll("[data-bday-go]").forEach(function (btn) {
      btn.addEventListener("click", goToCard);
    });

    setK();
    paintStep();
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(setK).observe(stage);
    } else {
      window.addEventListener("resize", setK);
    }

    return {
      destroy: function () {
        timers.forEach(clearTimeout);
        if (stopConfetti) {
          stopConfetti();
        }
      },
    };
  }

  function bootBirthdayUnwrap() {
    var root = document.querySelector("[data-bday-root]");
    if (!root) {
      return;
    }
    if (!root.getAttribute("data-preview") && root.getAttribute("data-card-id")) {
      if (readLocalOpened(root.getAttribute("data-card-id"))) {
        root.remove();
        var mainEl = document.getElementById("card-main");
        if (mainEl) {
          mainEl.hidden = false;
        }
        return;
      }
    }
    mountBirthdayUnwrap(root);
  }

  global.mountBirthdayUnwrap = mountBirthdayUnwrap;
  global.bootBirthdayUnwrap = bootBirthdayUnwrap;
  global.readLocalGiftOpened = readLocalOpened;
})(typeof window !== "undefined" ? window : this);
