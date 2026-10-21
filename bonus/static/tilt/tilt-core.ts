/**
 * tilt-core.ts — наклон карты за пальцем/мышью с возвратом пружиной.
 * Без зависимостей. Ничего не меняет в вёрстке: только пишет CSS-переменные на элемент
 * (стили лежат в tilt-card.css).
 *
 * Переменные, которые выставляются на элементе:
 *   --tilt-rx, --tilt-ry   углы поворота (deg)
 *   --tilt-sx, --tilt-sy   положение блика (%)
 *   --tilt-px, --tilt-py   смещение слоёв, от -1 до 1
 *   --tilt-ox, --tilt-oy   смещение тени, от -1 до 1
 *   --tilt-mag             сила наклона, от 0 до 1
 */

export interface TiltOptions {
  /** Предел наклона, градусы. Для карты со штрихкодом держите 8–10. */
  maxAngle?: number;
  /** Жёсткость пружины. Больше — быстрее возвращается. */
  stiffness?: number;
  /** Затухание. Меньше — заметнее покачивание у нуля. */
  damping?: number;
  /** Перспектива, px. */
  perspective?: number;
  /** Наклон телефоном (deviceorientation). На iOS нужно разрешение: см. enableGyro(). */
  gyro?: boolean;
  /** На сколько градусов поворота телефона достигается предел наклона. */
  gyroRange?: number;
  /** Поменять направление наклона от датчика, если на устройстве получилось наоборот. */
  gyroInvert?: boolean;
  /** Элемент, который принимает касания. По умолчанию сама карта. */
  surface?: HTMLElement | null;
  /** Полностью выключить эффект (карта плоская). */
  disabled?: boolean;
  /** Вызывается, когда карту начали/перестали наклонять. */
  onActiveChange?: (active: boolean) => void;
}

export interface TiltHandle {
  update(next: Partial<TiltOptions>): void;
  /** Включить датчики. На iOS вызывать только из обработчика нажатия. */
  enableGyro(): Promise<boolean>;
  disableGyro(): void;
  destroy(): void;
}

const DEFAULTS = {
  maxAngle: 10,
  stiffness: 140,
  damping: 13,
  perspective: 900,
  gyro: false,
  gyroRange: 25,
  gyroInvert: false,
  surface: null as HTMLElement | null,
  disabled: false,
  onActiveChange: undefined as ((active: boolean) => void) | undefined,
};

type Resolved = typeof DEFAULTS;

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));

function merge(base: Resolved, patch: Partial<TiltOptions>): Resolved {
  const out = { ...base } as Record<string, unknown>;
  (Object.keys(patch) as (keyof TiltOptions)[]).forEach((k) => {
    if (patch[k] !== undefined) out[k] = patch[k];
  });
  return out as Resolved;
}

/** true, если для датчиков нужно явное разрешение (iOS Safari). */
export function gyroNeedsPermission(): boolean {
  const DOE = (typeof window !== 'undefined' ? (window as any).DeviceOrientationEvent : undefined) as any;
  return !!DOE && typeof DOE.requestPermission === 'function';
}

export function createTilt(el: HTMLElement, options: TiltOptions = {}): TiltHandle {
  let o = merge(DEFAULTS, options);
  const reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)');

  let rx = 0, ry = 0, vx = 0, vy = 0; // текущие углы и скорости
  let tx = 0, ty = 0;                 // целевые углы
  let raf = 0, last = 0;
  let active = false;
  let gyroOn = false;
  let base: { b: number; g: number } | null = null;
  let surf: HTMLElement = o.surface ?? el;

  const off = () => o.disabled || reduceMq.matches;

  function setActive(next: boolean) {
    if (active === next) return;
    active = next;
    o.onActiveChange?.(next);
  }

  function apply() {
    const m = o.maxAngle || 1;
    const lim = m * 1.15; // пружина может чуть перелететь предел
    const ax = clamp(rx, -lim, lim);
    const ay = clamp(ry, -lim, lim);
    const s = el.style;
    s.setProperty('--tilt-perspective', o.perspective + 'px');
    s.setProperty('--tilt-rx', ax.toFixed(3) + 'deg');
    s.setProperty('--tilt-ry', ay.toFixed(3) + 'deg');
    s.setProperty('--tilt-sx', (50 - (ay / m) * 40).toFixed(2) + '%');
    s.setProperty('--tilt-sy', (50 + (ax / m) * 40).toFixed(2) + '%');
    s.setProperty('--tilt-px', (ay / m).toFixed(3));
    s.setProperty('--tilt-py', (-ax / m).toFixed(3));
    s.setProperty('--tilt-ox', (-ay / m).toFixed(3));
    s.setProperty('--tilt-oy', (ax / m).toFixed(3));
    s.setProperty('--tilt-mag', Math.min(1, Math.hypot(ax, ay) / m).toFixed(3));
  }

  function frame(t: number) {
    const dt = Math.min(0.032, (t - last) / 1000 || 0.016);
    last = t;
    const n = 4;
    const h = dt / n;
    for (let i = 0; i < n; i++) {
      vx += (o.stiffness * (tx - rx) - o.damping * vx) * h;
      rx += vx * h;
      vy += (o.stiffness * (ty - ry) - o.damping * vy) * h;
      ry += vy * h;
    }
    apply();
    const busy = Math.abs(rx - tx) + Math.abs(ry - ty) + Math.abs(vx) + Math.abs(vy);
    if (!active && busy < 0.08) {
      rx = tx; ry = ty; vx = vy = 0;
      apply();
      raf = 0;
      if (tx === 0 && ty === 0) el.removeAttribute('data-tilting');
      return;
    }
    raf = requestAnimationFrame(frame);
  }

  function go() {
    if (raf) return;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }

  function aim(e: PointerEvent) {
    const c = el.getBoundingClientRect();
    const cx = c.left + c.width / 2;
    const cy = c.top + c.height / 2;
    // offsetWidth не зависит от текущего поворота, поэтому нет обратной связи
    const hw = (surf === el ? el.offsetWidth : surf.getBoundingClientRect().width) / 2;
    const hh = (surf === el ? el.offsetHeight : surf.getBoundingClientRect().height) / 2;
    const nx = clamp((e.clientX - cx) / (hw || 1), -1, 1);
    const ny = clamp((e.clientY - cy) / (hh || 1), -1, 1);
    // нажатая точка «уходит вглубь»
    ty = nx * o.maxAngle;
    tx = -ny * o.maxAngle;
  }

  function blocked(e: Event) {
    if (off()) return true;
    const target = e.target as Element | null;
    return !!target?.closest?.('[data-tilt-ignore]');
  }

  function release() {
    if (!active && tx === 0 && ty === 0) return;
    setActive(false);
    tx = 0; ty = 0;
    go();
  }

  function begin() {
    setActive(true);
    el.setAttribute('data-tilting', '');
  }

  const onDown = (e: PointerEvent) => {
    if (blocked(e)) return;
    begin(); aim(e); go();
  };
  const onMove = (e: PointerEvent) => {
    if (blocked(e)) { release(); return; }
    if (e.pointerType === 'mouse') {
      if (!active) begin(); // мышь: наклон при наведении
      aim(e); go();
    } else if (active) {
      aim(e); go();
    }
  };
  const onUp = () => release();

  function bind() {
    surf.addEventListener('pointerdown', onDown);
    surf.addEventListener('pointermove', onMove);
    surf.addEventListener('pointerup', onUp);
    surf.addEventListener('pointercancel', onUp);
    surf.addEventListener('pointerleave', onUp);
  }
  function unbind() {
    surf.removeEventListener('pointerdown', onDown);
    surf.removeEventListener('pointermove', onMove);
    surf.removeEventListener('pointerup', onUp);
    surf.removeEventListener('pointercancel', onUp);
    surf.removeEventListener('pointerleave', onUp);
  }

  const onVisibility = () => {
    if (document.hidden) {
      setActive(false);
      rx = ry = vx = vy = tx = ty = 0;
      apply();
      el.removeAttribute('data-tilting');
    }
  };

  const onOrient = (e: DeviceOrientationEvent) => {
    if (active || off() || e.beta == null || e.gamma == null) return;
    if (!base) base = { b: e.beta, g: e.gamma };
    // «нулевое» положение медленно подстраивается под то, как держат телефон
    base.b += (e.beta - base.b) * 0.004;
    base.g += (e.gamma - base.g) * 0.004;
    const k = o.gyroInvert ? -1 : 1;
    const db = clamp((e.beta - base.b) / o.gyroRange, -1, 1);
    const dg = clamp((e.gamma - base.g) / o.gyroRange, -1, 1);
    ty = k * dg * o.maxAngle;
    tx = k * db * o.maxAngle;
    el.setAttribute('data-tilting', '');
    go();
  };

  async function enableGyro(): Promise<boolean> {
    if (gyroOn) return true;
    if (typeof window === 'undefined' || !('DeviceOrientationEvent' in window)) return false;
    try {
      const DOE = (window as any).DeviceOrientationEvent as any;
      if (typeof DOE.requestPermission === 'function') {
        const res = await DOE.requestPermission();
        if (res !== 'granted') return false;
      }
      window.addEventListener('deviceorientation', onOrient);
      gyroOn = true;
      return true;
    } catch {
      return false;
    }
  }

  function disableGyro() {
    if (!gyroOn) return;
    window.removeEventListener('deviceorientation', onOrient);
    gyroOn = false;
    base = null;
    if (!active) { tx = 0; ty = 0; go(); }
  }

  function update(next: Partial<TiltOptions>) {
    const prevSurface = surf;
    const wasGyro = o.gyro;
    o = merge(o, next);
    surf = o.surface ?? el;
    if (surf !== prevSurface) {
      prevSurface.removeEventListener('pointerdown', onDown);
      prevSurface.removeEventListener('pointermove', onMove);
      prevSurface.removeEventListener('pointerup', onUp);
      prevSurface.removeEventListener('pointercancel', onUp);
      prevSurface.removeEventListener('pointerleave', onUp);
      bind();
    }
    if (o.disabled) { release(); disableGyro(); }
    // авто-подключение датчиков там, где не нужно разрешение (Android, десктоп)
    if (o.gyro && !wasGyro && !gyroNeedsPermission()) void enableGyro();
    if (!o.gyro && wasGyro) disableGyro();
    apply();
  }

  // старт
  bind();
  document.addEventListener('visibilitychange', onVisibility);
  apply();
  if (o.gyro && !gyroNeedsPermission()) void enableGyro();

  return {
    update,
    enableGyro,
    disableGyro,
    destroy() {
      unbind();
      document.removeEventListener('visibilitychange', onVisibility);
      disableGyro();
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      el.removeAttribute('data-tilting');
      ['--tilt-perspective', '--tilt-rx', '--tilt-ry', '--tilt-sx', '--tilt-sy',
       '--tilt-px', '--tilt-py', '--tilt-ox', '--tilt-oy', '--tilt-mag']
        .forEach((p) => el.style.removeProperty(p));
    },
  };
}
