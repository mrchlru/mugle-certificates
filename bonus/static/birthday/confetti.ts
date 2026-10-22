/**
 * confetti.ts — мягкое конфетти на canvas. Без зависимостей.
 * Запускается один раз, сам останавливается, когда все частицы погасли.
 */
export interface ConfettiOptions {
  /** Точка вылета, px внутри canvas (CSS-пиксели) */
  origin: { x: number; y: number };
  /** Ширина области вылета (разброс по X), px */
  spread: number;
  /** Размер canvas в CSS-пикселях */
  width: number;
  height: number;
  colors?: string[];
  count?: number;
}

export const DEFAULT_CONFETTI_COLORS = ['#19B8A3', '#D81B60', '#F4EBDD', '#F2B544', '#E04040', '#8FE3D6'];

interface Particle {
  x: number; y: number; vx: number; vy: number;
  r: number; vr: number; ph: number;
  col: string; kind: 0 | 1 | 2; // 0 квадрат, 1 прямоугольник, 2 лента
  w: number; h: number; delay: number; age: number; max: number;
}

/** Возвращает функцию остановки (вызывать при размонтировании). */
export function burstConfetti(canvas: HTMLCanvasElement, o: ConfettiOptions): () => void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return () => {};
  const colors = o.colors?.length ? o.colors : DEFAULT_CONFETTI_COLORS;
  const count = o.count ?? 120;
  const P: Particle[] = [];
  for (let i = 0; i < count; i++) {
    const a = -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.1;
    const v = 4 + Math.random() * 7;
    P.push({
      x: o.origin.x + (Math.random() - 0.5) * o.spread,
      y: o.origin.y,
      vx: Math.cos(a) * v, vy: Math.sin(a) * v,
      r: Math.random() * 6.28, vr: (Math.random() - 0.5) * 0.16, ph: Math.random() * 6.28,
      col: colors[i % colors.length],
      kind: i % 5 === 0 ? 2 : ((i % 2) as 0 | 1),
      w: 5 + Math.random() * 5, h: 8 + Math.random() * 7,
      delay: Math.random() * 450, age: 0, max: 3600 + Math.random() * 1200,
    });
  }

  let raf = 0;
  let stopped = false;
  const t0 = performance.now();
  let last = t0;

  const step = (now: number) => {
    if (stopped) return;
    const dt = Math.min(40, now - last) / 16.67; // независимо от частоты экрана
    last = now;
    const el = now - t0;
    let alive = 0;
    ctx.clearRect(0, 0, o.width, o.height);
    for (const p of P) {
      if (el < p.delay) { alive++; continue; }
      p.age += dt * 16.67;
      if (p.age > p.max) continue;
      alive++;
      p.vy += 0.09 * dt;
      const d = Math.pow(0.985, dt);
      p.vx *= d; p.vy *= d;
      p.x += p.vx * dt + Math.sin(p.ph * 0.7) * 0.35 * dt;
      p.y += p.vy * dt + Math.min(p.age / 1200, 1) * 0.35 * dt;
      p.r += p.vr * dt; p.ph += 0.06 * dt;
      const fadeIn = Math.min(1, p.age / 180);
      const fadeOut = p.age > p.max - 900 ? (p.max - p.age) / 900 : 1;
      ctx.globalAlpha = Math.max(0, Math.min(fadeIn, fadeOut));
      ctx.fillStyle = p.col; ctx.strokeStyle = p.col;
      ctx.save();
      ctx.translate(p.x, p.y); ctx.rotate(p.r);
      if (p.kind === 2) {
        ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.beginPath();
        for (let k = 0; k <= 10; k++) ctx.lineTo(k * 2.6 - 13, Math.sin(k * 0.9 + p.ph) * 4);
        ctx.stroke();
      } else {
        ctx.scale(1, Math.abs(Math.cos(p.ph)) * 0.8 + 0.2); // «переворот» листка
        ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.kind ? p.h : p.w);
      }
      ctx.restore();
    }
    ctx.globalAlpha = 1;
    if (alive) raf = requestAnimationFrame(step);
    else ctx.clearRect(0, 0, o.width, o.height);
  };
  raf = requestAnimationFrame(step);

  return () => {
    stopped = true;
    cancelAnimationFrame(raf);
    ctx.clearRect(0, 0, o.width, o.height);
  };
}
