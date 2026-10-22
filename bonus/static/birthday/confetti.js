/**
 * confetti.js — порт confetti.ts без зависимостей.
 */
(function (global) {
  "use strict";

  var DEFAULT_CONFETTI_COLORS = [
    "#19B8A3",
    "#D81B60",
    "#F4EBDD",
    "#F2B544",
    "#E04040",
    "#8FE3D6",
  ];

  function burstConfetti(canvas, o) {
    var ctx = canvas.getContext("2d");
    if (!ctx) {
      return function () {};
    }
    var colors = o.colors && o.colors.length ? o.colors : DEFAULT_CONFETTI_COLORS;
    var count = o.count != null ? o.count : 120;
    var P = [];
    var i;
    for (i = 0; i < count; i++) {
      var a = -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.1;
      var v = 4 + Math.random() * 7;
      P.push({
        x: o.origin.x + (Math.random() - 0.5) * o.spread,
        y: o.origin.y,
        vx: Math.cos(a) * v,
        vy: Math.sin(a) * v,
        r: Math.random() * 6.28,
        vr: (Math.random() - 0.5) * 0.16,
        ph: Math.random() * 6.28,
        col: colors[i % colors.length],
        kind: i % 5 === 0 ? 2 : i % 2,
        w: 5 + Math.random() * 5,
        h: 8 + Math.random() * 7,
        delay: Math.random() * 450,
        age: 0,
        max: 3600 + Math.random() * 1200,
      });
    }

    var raf = 0;
    var stopped = false;
    var t0 = performance.now();
    var last = t0;

    function step(now) {
      if (stopped) {
        return;
      }
      var dt = Math.min(40, now - last) / 16.67;
      last = now;
      var el = now - t0;
      var alive = 0;
      ctx.clearRect(0, 0, o.width, o.height);
      for (var pi = 0; pi < P.length; pi++) {
        var p = P[pi];
        if (el < p.delay) {
          alive++;
          continue;
        }
        p.age += dt * 16.67;
        if (p.age > p.max) {
          continue;
        }
        alive++;
        p.vy += 0.09 * dt;
        var d = Math.pow(0.985, dt);
        p.vx *= d;
        p.vy *= d;
        p.x += p.vx * dt + Math.sin(p.ph * 0.7) * 0.35 * dt;
        p.y += p.vy * dt + Math.min(p.age / 1200, 1) * 0.35 * dt;
        p.r += p.vr * dt;
        p.ph += 0.06 * dt;
        var fadeIn = Math.min(1, p.age / 180);
        var fadeOut = p.age > p.max - 900 ? (p.max - p.age) / 900 : 1;
        ctx.globalAlpha = Math.max(0, Math.min(fadeIn, fadeOut));
        ctx.fillStyle = p.col;
        ctx.strokeStyle = p.col;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.r);
        if (p.kind === 2) {
          ctx.lineWidth = 3;
          ctx.lineCap = "round";
          ctx.beginPath();
          for (var k = 0; k <= 10; k++) {
            ctx.lineTo(k * 2.6 - 13, Math.sin(k * 0.9 + p.ph) * 4);
          }
          ctx.stroke();
        } else {
          ctx.scale(1, Math.abs(Math.cos(p.ph)) * 0.8 + 0.2);
          ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.kind ? p.h : p.w);
        }
        ctx.restore();
      }
      ctx.globalAlpha = 1;
      if (alive) {
        raf = requestAnimationFrame(step);
      } else {
        ctx.clearRect(0, 0, o.width, o.height);
      }
    }
    raf = requestAnimationFrame(step);

    return function () {
      stopped = true;
      cancelAnimationFrame(raf);
      ctx.clearRect(0, 0, o.width, o.height);
    };
  }

  global.burstConfetti = burstConfetti;
  global.DEFAULT_CONFETTI_COLORS = DEFAULT_CONFETTI_COLORS;
})(typeof window !== "undefined" ? window : this);
