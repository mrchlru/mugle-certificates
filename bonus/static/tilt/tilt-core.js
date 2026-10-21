/**
 * tilt-core.js — наклон карты (порт tilt-core.ts без зависимостей).
 * Пишет CSS-переменные на элемент; стили в tilt-card.css.
 */
(function (global) {
  "use strict";

  var DEFAULTS = {
    maxAngle: 10,
    stiffness: 140,
    damping: 13,
    perspective: 900,
    gyro: false,
    gyroRange: 25,
    gyroInvert: false,
    surface: null,
    disabled: false,
    onActiveChange: undefined,
  };

  function clamp(v, a, b) {
    return Math.max(a, Math.min(b, v));
  }

  function merge(base, patch) {
    var out = {};
    var key;
    for (key in base) {
      if (Object.prototype.hasOwnProperty.call(base, key)) {
        out[key] = base[key];
      }
    }
    for (key in patch) {
      if (Object.prototype.hasOwnProperty.call(patch, key) && patch[key] !== undefined) {
        out[key] = patch[key];
      }
    }
    return out;
  }

  function gyroNeedsPermission() {
    var DOE =
      typeof window !== "undefined" ? window.DeviceOrientationEvent : undefined;
    return !!DOE && typeof DOE.requestPermission === "function";
  }

  function createTilt(el, options) {
    var o = merge(DEFAULTS, options || {});
    var reduceMq = window.matchMedia("(prefers-reduced-motion: reduce)");

    var rx = 0;
    var ry = 0;
    var vx = 0;
    var vy = 0;
    var tx = 0;
    var ty = 0;
    var raf = 0;
    var last = 0;
    var active = false;
    var gyroOn = false;
    var base = null;
    var surf = o.surface || el;

    function off() {
      return o.disabled || reduceMq.matches;
    }

    function setActive(next) {
      if (active === next) {
        return;
      }
      active = next;
      if (o.onActiveChange) {
        o.onActiveChange(next);
      }
    }

    function apply() {
      var m = o.maxAngle || 1;
      var lim = m * 1.15;
      var ax = clamp(rx, -lim, lim);
      var ay = clamp(ry, -lim, lim);
      var s = el.style;
      s.setProperty("--tilt-perspective", o.perspective + "px");
      s.setProperty("--tilt-rx", ax.toFixed(3) + "deg");
      s.setProperty("--tilt-ry", ay.toFixed(3) + "deg");
      s.setProperty("--tilt-sx", (50 - (ay / m) * 40).toFixed(2) + "%");
      s.setProperty("--tilt-sy", (50 + (ax / m) * 40).toFixed(2) + "%");
      s.setProperty("--tilt-px", (ay / m).toFixed(3));
      s.setProperty("--tilt-py", (-ax / m).toFixed(3));
      s.setProperty("--tilt-ox", (-ay / m).toFixed(3));
      s.setProperty("--tilt-oy", (ax / m).toFixed(3));
      s.setProperty("--tilt-mag", Math.min(1, Math.hypot(ax, ay) / m).toFixed(3));
    }

    function frame(t) {
      var dt = Math.min(0.032, (t - last) / 1000 || 0.016);
      last = t;
      var n = 4;
      var h = dt / n;
      var i;
      for (i = 0; i < n; i++) {
        vx += (o.stiffness * (tx - rx) - o.damping * vx) * h;
        rx += vx * h;
        vy += (o.stiffness * (ty - ry) - o.damping * vy) * h;
        ry += vy * h;
      }
      apply();
      var busy = Math.abs(rx - tx) + Math.abs(ry - ty) + Math.abs(vx) + Math.abs(vy);
      if (!active && busy < 0.08) {
        rx = tx;
        ry = ty;
        vx = 0;
        vy = 0;
        apply();
        raf = 0;
        if (tx === 0 && ty === 0) {
          el.removeAttribute("data-tilting");
        }
        return;
      }
      raf = requestAnimationFrame(frame);
    }

    function go() {
      if (raf) {
        return;
      }
      last = performance.now();
      raf = requestAnimationFrame(frame);
    }

    function aim(e) {
      var c = el.getBoundingClientRect();
      var cx = c.left + c.width / 2;
      var cy = c.top + c.height / 2;
      var hw =
        (surf === el ? el.offsetWidth : surf.getBoundingClientRect().width) / 2;
      var hh =
        (surf === el ? el.offsetHeight : surf.getBoundingClientRect().height) / 2;
      var nx = clamp((e.clientX - cx) / (hw || 1), -1, 1);
      var ny = clamp((e.clientY - cy) / (hh || 1), -1, 1);
      ty = nx * o.maxAngle;
      tx = -ny * o.maxAngle;
    }

    function blocked(e) {
      if (off()) {
        return true;
      }
      var target = e.target;
      return !!(target && target.closest && target.closest("[data-tilt-ignore]"));
    }

    function release() {
      if (!active && tx === 0 && ty === 0) {
        return;
      }
      setActive(false);
      tx = 0;
      ty = 0;
      go();
    }

    function begin() {
      setActive(true);
      el.setAttribute("data-tilting", "");
    }

    function onDown(e) {
      if (blocked(e)) {
        return;
      }
      begin();
      aim(e);
      go();
    }

    function onMove(e) {
      if (blocked(e)) {
        release();
        return;
      }
      if (e.pointerType === "mouse") {
        if (!active) {
          begin();
        }
        aim(e);
        go();
      } else if (active) {
        aim(e);
        go();
      }
    }

    function onUp() {
      release();
    }

    function bind() {
      surf.addEventListener("pointerdown", onDown);
      surf.addEventListener("pointermove", onMove);
      surf.addEventListener("pointerup", onUp);
      surf.addEventListener("pointercancel", onUp);
      surf.addEventListener("pointerleave", onUp);
    }

    function unbind() {
      surf.removeEventListener("pointerdown", onDown);
      surf.removeEventListener("pointermove", onMove);
      surf.removeEventListener("pointerup", onUp);
      surf.removeEventListener("pointercancel", onUp);
      surf.removeEventListener("pointerleave", onUp);
    }

    function onVisibility() {
      if (document.hidden) {
        setActive(false);
        rx = 0;
        ry = 0;
        vx = 0;
        vy = 0;
        tx = 0;
        ty = 0;
        apply();
        el.removeAttribute("data-tilting");
      }
    }

    function onOrient(e) {
      if (active || off() || e.beta == null || e.gamma == null) {
        return;
      }
      if (!base) {
        base = { b: e.beta, g: e.gamma };
      }
      base.b += (e.beta - base.b) * 0.004;
      base.g += (e.gamma - base.g) * 0.004;
      var k = o.gyroInvert ? -1 : 1;
      var db = clamp((e.beta - base.b) / o.gyroRange, -1, 1);
      var dg = clamp((e.gamma - base.g) / o.gyroRange, -1, 1);
      ty = k * dg * o.maxAngle;
      tx = k * db * o.maxAngle;
      el.setAttribute("data-tilting", "");
      go();
    }

    function enableGyro() {
      if (gyroOn) {
        return Promise.resolve(true);
      }
      if (typeof window === "undefined" || !("DeviceOrientationEvent" in window)) {
        return Promise.resolve(false);
      }
      var DOE = window.DeviceOrientationEvent;
      var request =
        typeof DOE.requestPermission === "function"
          ? DOE.requestPermission()
          : Promise.resolve("granted");
      return Promise.resolve(request)
        .then(function (res) {
          if (res !== "granted") {
            return false;
          }
          window.addEventListener("deviceorientation", onOrient);
          gyroOn = true;
          return true;
        })
        .catch(function () {
          return false;
        });
    }

    function disableGyro() {
      if (!gyroOn) {
        return;
      }
      window.removeEventListener("deviceorientation", onOrient);
      gyroOn = false;
      base = null;
      if (!active) {
        tx = 0;
        ty = 0;
        go();
      }
    }

    function update(next) {
      var prevSurface = surf;
      var wasGyro = o.gyro;
      o = merge(o, next || {});
      surf = o.surface || el;
      if (surf !== prevSurface) {
        prevSurface.removeEventListener("pointerdown", onDown);
        prevSurface.removeEventListener("pointermove", onMove);
        prevSurface.removeEventListener("pointerup", onUp);
        prevSurface.removeEventListener("pointercancel", onUp);
        prevSurface.removeEventListener("pointerleave", onUp);
        bind();
      }
      if (o.disabled) {
        release();
        disableGyro();
      }
      if (o.gyro && !wasGyro && !gyroNeedsPermission()) {
        enableGyro();
      }
      if (!o.gyro && wasGyro) {
        disableGyro();
      }
      apply();
    }

    bind();
    document.addEventListener("visibilitychange", onVisibility);
    apply();
    if (o.gyro && !gyroNeedsPermission()) {
      enableGyro();
    }

    return {
      update: update,
      enableGyro: enableGyro,
      disableGyro: disableGyro,
      destroy: function () {
        unbind();
        document.removeEventListener("visibilitychange", onVisibility);
        disableGyro();
        if (raf) {
          cancelAnimationFrame(raf);
        }
        raf = 0;
        el.removeAttribute("data-tilting");
        [
          "--tilt-perspective",
          "--tilt-rx",
          "--tilt-ry",
          "--tilt-sx",
          "--tilt-sy",
          "--tilt-px",
          "--tilt-py",
          "--tilt-ox",
          "--tilt-oy",
          "--tilt-mag",
        ].forEach(function (p) {
          el.style.removeProperty(p);
        });
      },
    };
  }

  global.createTilt = createTilt;
  global.gyroNeedsPermission = gyroNeedsPermission;
})(typeof window !== "undefined" ? window : this);
