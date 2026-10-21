'use client';

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  type HTMLAttributes,
  type ReactNode,
  type RefObject,
} from 'react';
import { createTilt, type TiltHandle, type TiltOptions } from './tilt-core';

export interface TiltCardProps
  extends Omit<HTMLAttributes<HTMLDivElement>, 'children'>,
    Omit<TiltOptions, 'surface'> {
  children: ReactNode;
  /** Тень, которая смещается при наклоне. Включайте, если у карты нет своей тени. */
  shadow?: boolean;
  /** Элемент, который принимает касания (например, весь экран карты). По умолчанию сама карта. */
  surfaceRef?: RefObject<HTMLElement | null>;
}

export interface TiltCardHandle {
  /** Включить наклон телефоном. На iOS вызывать из обработчика нажатия. */
  enableGyro(): Promise<boolean>;
  readonly el: HTMLDivElement | null;
}

/**
 * Обёртка наклона. Применяйте классы вашей карточки прямо сюда через className,
 * не добавляйте ещё один контейнер: размеры и расположение элементов не меняются.
 */
export const TiltCard = forwardRef<TiltCardHandle, TiltCardProps>(function TiltCard(
  {
    children,
    className,
    shadow,
    surfaceRef,
    maxAngle,
    stiffness,
    damping,
    perspective,
    gyro,
    gyroRange,
    gyroInvert,
    disabled,
    onActiveChange,
    ...rest
  },
  ref,
) {
  const elRef = useRef<HTMLDivElement>(null);
  const tiltRef = useRef<TiltHandle | null>(null);
  const cbRef = useRef(onActiveChange);
  cbRef.current = onActiveChange;

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const t = createTilt(el, {
      surface: surfaceRef?.current ?? null,
      onActiveChange: (a) => cbRef.current?.(a),
    });
    tiltRef.current = t;
    return () => {
      t.destroy();
      tiltRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    tiltRef.current?.update({
      maxAngle,
      stiffness,
      damping,
      perspective,
      gyro,
      gyroRange,
      gyroInvert,
      disabled,
    });
  }, [maxAngle, stiffness, damping, perspective, gyro, gyroRange, gyroInvert, disabled]);

  useImperativeHandle(
    ref,
    () => ({
      enableGyro: () => tiltRef.current?.enableGyro() ?? Promise.resolve(false),
      get el() {
        return elRef.current;
      },
    }),
    [],
  );

  const classes = ['tilt-card', shadow ? 'tilt-card--shadow' : '', className ?? '']
    .filter(Boolean)
    .join(' ');

  return (
    <div ref={elRef} className={classes} {...rest}>
      {children}
      <span className="tilt-card__sheen" data-tilt-sheen aria-hidden="true" />
    </div>
  );
});
