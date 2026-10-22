'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { burstConfetti, DEFAULT_CONFETTI_COLORS } from './confetti';

/** Шаги анимации. Классы накапливаются: на шаге 4 действуют is-seal … is-card. */
const STEPS = ['', 'is-seal', 'is-flap', 'is-flapback', 'is-card', 'is-reveal'] as const;

/** Тайминги, мс от нажатия «Открыть». Менять только вместе с CSS-длительностями. */
export const UNWRAP_TIMING = {
  flap: 250,      // клапан начинает откидываться
  flapBack: 650,  // клапан уходит за карту
  card: 650,      // карта выезжает и растёт, конверт растворяется (одновременно)
  reveal: 2350,   // карта на месте: конфетти, новый заголовок, кнопка
} as const;

export interface BirthdayUnwrapProps {
  /** Готовый компонент карты (тот же, что показывается в приложении, с рабочим штрихкодом). */
  children: ReactNode;
  /** Натуральный размер карты в px — в нём карта вёрстана сейчас. Нужен, чтобы вписать её в 270px. */
  cardNativeWidth: number;
  cardNativeHeight: number;
  /** Имя для поздравления. Нет имени — «С днём рождения!» */
  firstName?: string;
  /** Название заведения для подписи на первом экране */
  brandName?: string;
  /** Буквы на печати (2–3 символа, строчные) */
  sealText?: string;
  openLabel?: string;
  goLabel?: string;
  /** Цвет карты. Подсвечивает фон и печать. */
  accentColor?: string;
  confettiColors?: string[];
  /** Нажали «Перейти к карте» (или ×). Здесь пометить подарок открытым и показать обычную карту. */
  onGo: () => void;
}

export function BirthdayUnwrap({
  children, cardNativeWidth, cardNativeHeight, firstName, brandName = 'Mugle Rest', sealText = 'mr',
  openLabel = 'Открыть', goLabel = 'Перейти к карте', accentColor = '#B91C1C', confettiColors, onGo,
}: BirthdayUnwrapProps) {
  const [step, setStep] = useState(0);
  const stageRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const timers = useRef<number[]>([]);
  const stopConfetti = useRef<(() => void) | null>(null);
  const [k, setK] = useState(1);

  const CARD_W = 270;
  const cardH = Math.round((CARD_W * cardNativeHeight) / cardNativeWidth);
  const fitScale = CARD_W / cardNativeWidth;

  // уменьшаем сцену на низких экранах, чтобы карта не пересекалась с кнопкой
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const fit = () => {
      setK(Math.min(1, Math.max(0.5, (el.clientHeight - 190 - 120) / cardH)));
      const c = canvasRef.current;
      if (c) {
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        c.width = el.clientWidth * dpr;
        c.height = el.clientHeight * dpr;
        c.getContext('2d')?.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [cardH]);

  useEffect(() => () => {
    timers.current.forEach(clearTimeout);
    stopConfetti.current?.();
  }, []);

  const at = (ms: number, fn: () => void) => { timers.current.push(window.setTimeout(fn, ms)); };

  const launchConfetti = useCallback(() => {
    const stage = stageRef.current, card = cardRef.current, canvas = canvasRef.current;
    if (!stage || !card || !canvas) return;
    const s = stage.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    stopConfetti.current = burstConfetti(canvas, {
      origin: { x: c.left - s.left + c.width / 2, y: c.top - s.top + c.height * 0.5 },
      spread: c.width * 0.6,
      width: s.width, height: s.height,
      colors: confettiColors ?? DEFAULT_CONFETTI_COLORS,
    });
  }, [confettiColors]);

  const open = useCallback(() => {
    if (step !== 0) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) { setStep(5); return; } // без анимации и конфетти
    try { navigator.vibrate?.(12); } catch { /* не поддерживается */ }
    setStep(1);
    at(UNWRAP_TIMING.flap, () => setStep(2));
    at(UNWRAP_TIMING.flapBack, () => setStep(3));
    at(UNWRAP_TIMING.card, () => setStep(4));
    at(UNWRAP_TIMING.reveal, () => { setStep(5); launchConfetti(); });
  }, [step, launchConfetti]);

  const cls = ['bday', step === 0 ? 'is-idle' : '', ...STEPS.slice(1, step + 1)].filter(Boolean).join(' ');
  const revealTitle = firstName ? `С днём рождения, ${firstName}!` : 'С днём рождения!';

  return (
    <div
      ref={stageRef}
      className={cls}
      style={{ ['--bday-accent' as string]: accentColor, ['--bday-k' as string]: k, ['--bday-card-h' as string]: `${cardH}px` }}
    >
      <div className="bday__bar">
        <span className="bday__tag">Подарок</span>
        <button className="bday__x" type="button" aria-label="Закрыть" onClick={onGo}>×</button>
      </div>

      <div className="bday__heads">
        <div className="bday__head bday__head--a">
          <h1>Вам подарок!</h1>
          <p>{brandName} приготовил кое-что ко дню рождения</p>
        </div>
        <div className="bday__head bday__head--b" aria-live="polite">
          <h1>{revealTitle}</h1>
          <p>Ваша подарочная карта уже ждёт</p>
        </div>
      </div>

      <div className="bday__giftwrap">
        <div className="bday__gift">
          <div className="bday__part bday__env-back" />
          <div className="bday__card" ref={cardRef} style={{ width: CARD_W, height: cardH }}>
            <div
              className="bday__card-fit"
              style={{ width: cardNativeWidth, height: cardNativeHeight, transform: `scale(${fitScale})` }}
            >
              {children}
            </div>
          </div>
          <div className="bday__part bday__env-front" />
          <div className="bday__part bday__flap"><i className="bday__flap-out" /><i className="bday__flap-in" /></div>
          <div className="bday__seal"><span>{sealText}</span></div>
          <button className="bday__env-hit" type="button" aria-label="Открыть подарок" onClick={open} />
        </div>
      </div>

      <div className="bday__foot">
        <div className="bday__layer bday__layer--open">
          <button className="bday__btn bday__btn--pulse" type="button" onClick={open}>{openLabel}</button>
        </div>
        <div className="bday__layer bday__layer--cta">
          <button className="bday__btn" type="button" onClick={onGo}>
            <span>{goLabel}</span>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          </button>
        </div>
      </div>

      <canvas ref={canvasRef} className="bday__fx" aria-hidden="true" />
    </div>
  );
}
