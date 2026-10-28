import { clsx } from 'clsx';
import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from 'react';
import { Delete, Loader2, X } from 'lucide-react';
import { formatCountdown } from './format';

export const cn = clsx;

/* ------------------------------------------------------------------ buttons */

type Variant = 'primary' | 'alarm' | 'secondary' | 'quiet' | 'ghost' | 'dark';

const variants: Record<Variant, string> = {
  primary: 'bg-indigo-fill text-on-fill border-indigo-fill hover:brightness-110',
  alarm: 'bg-chilli-fill text-on-fill border-chilli-fill hover:bg-chilli-deep',
  secondary: 'bg-paper text-graphite border-line hover:border-slate',
  quiet: 'bg-paper text-graphite border-line border-dashed hover:border-slate',
  ghost: 'bg-transparent text-indigo border-transparent underline underline-offset-2 px-0 min-h-0',
  dark: 'bg-graphite text-paper border-graphite',
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: 'md' | 'lg' | 'sm';
  sub?: ReactNode;
  busy?: boolean;
  block?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', sub, busy, block = true, icon, className, children, disabled, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cn(
        'inline-flex flex-col items-center justify-center rounded-xl border-[1.5px] font-bold transition-[filter,background-color,border-color] disabled:cursor-not-allowed disabled:opacity-50',
        size === 'lg' && 'min-h-14 px-3 py-2.5 text-[15px]',
        size === 'md' && 'min-h-12 px-3 py-2 text-[15px]',
        size === 'sm' && 'min-h-9 px-3 py-1 text-[13px]',
        block && variant !== 'ghost' ? 'w-full' : 'w-auto',
        variants[variant],
        className,
      )}
      {...rest}
    >
      <span className="inline-flex items-center gap-2">
        {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : icon}
        {children}
      </span>
      {sub ? <span className="mt-0.5 text-[11.5px] font-medium opacity-85">{sub}</span> : null}
    </button>
  );
});

/* ------------------------------------------------------------------ status */

export type Tone = 'ok' | 'warn' | 'alarm' | 'info' | 'off';

const toneWash: Record<Tone, string> = {
  ok: 'bg-neem-wash',
  warn: 'bg-haldi-wash',
  alarm: 'bg-chilli-wash',
  info: 'bg-indigo-wash',
  off: 'bg-hatch',
};
const toneText: Record<Tone, string> = {
  ok: 'text-neem',
  warn: 'text-haldi',
  alarm: 'text-chilli',
  info: 'text-indigo',
  off: 'text-graphite',
};

export function StatusCard({ tone, title, children, className }: { tone: Tone; title: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <div className={cn('grid gap-1 rounded-2xl px-3.5 py-3', toneWash[tone], className)}>
      <strong className={cn('font-display text-[25px] leading-[1.05] font-bold stretch-90', toneText[tone])}>{title}</strong>
      {children ? <span className="text-[13px]">{children}</span> : null}
    </div>
  );
}

export function Strip({ tone = 'info', children, className, role }: { tone?: Tone; children: ReactNode; className?: string; role?: string }) {
  return (
    <div role={role} className={cn('rounded-xl px-3 py-2 text-[13px] leading-snug text-graphite', toneWash[tone], className)}>
      {children}
    </div>
  );
}

export function Pill({ tone = 'info', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[12.5px] font-semibold', toneWash[tone], toneText[tone])}>
      <span aria-hidden className="size-2 rounded-full bg-current" />
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ alert band */

export type BandTone = 'alarm' | 'help' | 'calm' | 'quiet';
const bandBg: Record<BandTone, string> = {
  alarm: 'bg-chilli-fill',
  help: 'bg-band-help',
  calm: 'bg-band-calm',
  quiet: 'bg-band-quiet',
};

export function Band({ tone, title, right, children, className }: { tone: BandTone; title: ReactNode; right?: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <div className={cn('grid gap-1.5 px-4 pb-3 pt-2.5 text-on-fill safe-x', bandBg[tone], className)}>
      <div className="flex items-center justify-between gap-2.5">
        <span className="text-[15px] font-bold">{title}</span>
        {right}
      </div>
      {children}
    </div>
  );
}

export function BandButton(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className, type = 'button', ...rest } = props;
  return (
    <button
      type={type}
      className={cn('rounded-lg border-[1.5px] border-on-fill bg-transparent px-3 py-1 text-[13px] font-bold text-on-fill hover:bg-white/15', className)}
      {...rest}
    />
  );
}

/** Live countdown to an ISO time; re-renders once a second. */
export function Countdown({ until, className, onDone }: { until: string | number; className?: string; onDone?: () => void }) {
  const target = typeof until === 'number' ? until : Date.parse(until);
  const [now, setNow] = useState(() => Date.now());
  const fired = useRef(false);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!fired.current && now >= target && onDone) {
      fired.current = true;
      onDone();
    }
  }, [now, target, onDone]);
  return <span className={cn('tabular', className)}>{formatCountdown(target - now)}</span>;
}

/* ------------------------------------------------------------------ lists */

export function List({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('overflow-hidden rounded-xl border border-line bg-paper', className)}>{children}</div>;
}

export function ListRow({
  label,
  sub,
  value,
  tone,
  onClick,
  children,
}: {
  label: ReactNode;
  sub?: ReactNode;
  value?: ReactNode;
  tone?: 'ok' | 'warn' | 'bad';
  onClick?: () => void;
  children?: ReactNode;
}) {
  const inner = (
    <>
      <span className="min-w-0 font-semibold">
        {label}
        {sub ? <small className="block text-[12px] font-normal text-slate">{sub}</small> : null}
      </span>
      {value != null ? (
        <em
          className={cn(
            'max-w-[58%] shrink-0 text-right text-[12.5px] not-italic',
            tone === 'ok' && 'font-semibold text-neem',
            tone === 'warn' && 'font-semibold text-haldi',
            tone === 'bad' && 'font-semibold text-chilli',
            !tone && 'text-slate',
          )}
        >
          {value}
        </em>
      ) : null}
      {children}
    </>
  );
  const cls = 'flex w-full items-center justify-between gap-2.5 border-t border-line px-3 py-2.5 text-left text-[13.5px] first:border-t-0';
  return onClick ? (
    <button type="button" onClick={onClick} className={cn(cls, 'bg-paper hover:bg-chalk')}>
      {inner}
    </button>
  ) : (
    <div className={cls}>{inner}</div>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h3 className="mt-1.5 font-sans text-[13px] font-bold text-slate">{children}</h3>;
}

/* ------------------------------------------------------------------ forms */

export function Field({
  label,
  help,
  error,
  optional,
  children,
  htmlFor,
}: {
  label: ReactNode;
  help?: ReactNode;
  error?: string;
  optional?: boolean;
  children: ReactNode;
  htmlFor: string;
}) {
  return (
    <div className="grid gap-1">
      <label htmlFor={htmlFor} className="text-[13px] font-semibold">
        {label}
        {optional ? <span className="font-normal text-slate"> (optional)</span> : null}
      </label>
      {children}
      {error ? (
        <span id={`${htmlFor}-error`} className="text-[12px] font-semibold text-chilli">
          {error}
        </span>
      ) : help ? (
        <span id={`${htmlFor}-help`} className="text-[12px] text-slate">
          {help}
        </span>
      ) : null}
    </div>
  );
}

const inputCls =
  'w-full min-h-11 rounded-[10px] border-[1.5px] border-line bg-paper px-2.5 py-2 text-[15px] text-graphite placeholder:text-slate/70 aria-[invalid=true]:border-chilli';

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }>(function TextInput(
  { className, invalid, id, ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      id={id}
      aria-invalid={invalid || undefined}
      aria-describedby={id ? (invalid ? `${id}-error` : `${id}-help`) : undefined}
      spellCheck={false}
      className={cn(inputCls, className)}
      {...rest}
    />
  );
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className, ...rest }, ref) {
  return <select ref={ref} className={cn(inputCls, className)} {...rest} />;
});

export function Check({
  id,
  label,
  sub,
  checked,
  onChange,
  disabled,
  error,
}: {
  id: string;
  label: ReactNode;
  sub?: ReactNode;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  error?: string;
}) {
  return (
    <label htmlFor={id} className="flex items-start gap-2.5 py-1.5 text-[13.5px]">
      <input id={id} type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="mt-0.5 size-5 shrink-0 accent-[var(--indigo-fill)]" />
      <span>
        {label}
        {sub ? <small className="block text-[12px] text-slate">{sub}</small> : null}
        {error ? <small className="block text-[12px] font-semibold text-chilli">{error}</small> : null}
      </span>
    </label>
  );
}

export function RadioCards<T extends string | number>({
  name,
  value,
  options,
  onChange,
}: {
  name: string;
  value: T;
  options: { value: T; label: ReactNode; sub?: ReactNode }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="grid gap-1.5" role="radiogroup">
      {options.map((o) => {
        const id = `${name}-${o.value}`;
        return (
          <label key={String(o.value)} htmlFor={id} className={cn('flex items-center gap-2.5 rounded-[10px] border-[1.5px] px-2.5 py-2 text-[14px]', value === o.value ? 'border-indigo bg-indigo-wash' : 'border-line')}>
            <input id={id} type="radio" name={name} checked={value === o.value} onChange={() => onChange(o.value)} className="size-[18px] accent-[var(--indigo-fill)]" />
            <span>
              <b>{o.label}</b>
              {o.sub ? <small className="block text-[12px] text-slate">{o.sub}</small> : null}
            </span>
          </label>
        );
      })}
    </div>
  );
}

export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
  className,
}: {
  label: string;
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <div role="group" aria-label={label} className={cn('grid overflow-hidden rounded-[10px] border-[1.5px] border-line', className)} style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          disabled={disabled}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn('px-2 py-2 text-[14px] font-bold disabled:opacity-50', value === o.value ? 'bg-graphite text-paper' : 'bg-paper text-slate')}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Stepper({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (v: number) => void }) {
  return (
    <div className="flex items-center justify-between py-1.5 text-[14px]">
      <span>{label}</span>
      <div className="flex items-center gap-2.5 font-bold tabular">
        <button type="button" aria-label={`One fewer: ${label}`} disabled={value <= min} onClick={() => onChange(value - 1)} className="grid size-9 place-items-center rounded-full border-[1.5px] border-line bg-paper text-lg disabled:opacity-40">
          −
        </button>
        <span aria-live="polite">{value}</span>
        <button type="button" aria-label={`One more: ${label}`} disabled={value >= max} onClick={() => onChange(value + 1)} className="grid size-9 place-items-center rounded-full border-[1.5px] border-line bg-paper text-lg disabled:opacity-40">
          +
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ sheet */

export function Sheet({ open, onClose, title, children, labelledBy }: { open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; labelledBy?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const autoId = useId();
  const titleId = labelledBy ?? `sheet-${autoId}`;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const first = node?.querySelector<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'Tab' && node) {
        const items = Array.from(node.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'));
        if (!items.length) return;
        const firstEl = items[0];
        const lastEl = items[items.length - 1];
        if (e.shiftKey && document.activeElement === firstEl) {
          e.preventDefault();
          lastEl.focus();
        } else if (!e.shiftKey && document.activeElement === lastEl) {
          e.preventDefault();
          firstEl.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      previous?.focus?.();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-[var(--scrim)] sm:items-center" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={title ? titleId : undefined} className="anim-sheet grid max-h-[90dvh] w-full max-w-lg gap-3 overflow-y-auto overscroll-contain rounded-t-[20px] bg-paper px-4 pb-[max(18px,env(safe-area-inset-bottom))] pt-4 shadow-float sm:rounded-[20px]">
        {title ? (
          <div className="flex items-start justify-between gap-3">
            <h2 id={titleId} className="text-[19px] font-bold">
              {title}
            </h2>
            <button type="button" onClick={onClose} aria-label="Close" className="-mr-1 grid size-9 place-items-center rounded-full text-slate hover:bg-chalk">
              <X className="size-5" aria-hidden />
            </button>
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ toasts */

interface ToastItem {
  id: number;
  text: string;
}
const ToastCtx = createContext<(text: string) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((text: string) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs.slice(-2), { id, text }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-[max(80px,calc(env(safe-area-inset-bottom)+72px))] z-[60] grid justify-items-center gap-2 px-4" aria-live="polite" role="status">
        {items.map((t) => (
          <div key={t.id} className="anim-in pointer-events-auto max-w-md rounded-xl bg-graphite px-3.5 py-2.5 text-[13.5px] text-paper shadow-float">
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return useContext(ToastCtx);
}

/* ------------------------------------------------------------------ PIN pad */

export function PinPad({
  title,
  length = 6,
  error,
  busy,
  onSubmit,
  onCancel,
  large,
}: {
  title: ReactNode;
  length?: number;
  error?: string;
  busy?: boolean;
  onSubmit: (pin: string) => void;
  onCancel: () => void;
  large?: boolean;
}) {
  const [pin, setPin] = useState('');
  const press = useCallback(
    (k: string) => {
      if (busy) return;
      setPin((p) => {
        if (k === 'del') return p.slice(0, -1);
        if (p.length >= length) return p;
        const next = p + k;
        if (next.length === length) {
          setTimeout(() => {
            onSubmit(next);
            setPin('');
          }, 60);
        }
        return next;
      });
    },
    [busy, length, onSubmit],
  );
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (/^[0-9]$/.test(e.key)) press(e.key);
      else if (e.key === 'Backspace') press('del');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [press]);
  const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '', '0', 'del'];
  return (
    <div className="grid gap-3">
      <h3 className={cn('font-sans font-bold', large ? 'text-[17px]' : 'text-[18px]')}>{title}</h3>
      <div className="flex justify-center gap-3 py-1" aria-label={`${pin.length} of ${length} digits entered`} role="img">
        {Array.from({ length }).map((_, i) => (
          <i key={i} className={cn('size-3.5 rounded-full border-2 border-graphite', i < pin.length && 'bg-graphite')} />
        ))}
      </div>
      <div className="min-h-5 text-center text-[13px] font-semibold text-chilli" role="alert">
        {error}
      </div>
      <div className="grid grid-cols-3 gap-2">
        {keys.map((k, i) =>
          k === '' ? (
            <span key={i} />
          ) : (
            <button
              key={i}
              type="button"
              onClick={() => press(k)}
              aria-label={k === 'del' ? 'Delete' : k}
              className={cn('grid place-items-center rounded-xl border border-line bg-chalk font-display font-semibold', large ? 'h-12 text-[22px]' : 'h-12 text-[20px]')}
            >
              {k === 'del' ? <Delete aria-hidden className="size-5" /> : k}
            </button>
          ),
        )}
      </div>
      <Button onClick={onCancel}>Cancel</Button>
    </div>
  );
}

/* ------------------------------------------------------------------ misc */

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="grid place-items-center gap-2 p-8 text-[13px] text-slate" role="status">
      <Loader2 aria-hidden className="size-6 animate-spin" />
      {label}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="grid justify-items-center gap-2 rounded-2xl border border-dashed border-line px-5 py-8 text-center">
      <strong className="font-display text-[19px]">{title}</strong>
      {children ? <p className="max-w-sm text-[13.5px] text-slate">{children}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({ title = 'Something went wrong', message, onRetry }: { title?: string; message?: string; onRetry?: () => void }) {
  return (
    <div className="grid gap-2 rounded-2xl bg-chilli-wash p-4 text-[13.5px]" role="alert">
      <strong>{title}</strong>
      {message ? <span>{message}</span> : null}
      {onRetry ? (
        <Button size="sm" block={false} onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 font-display text-[19px] font-bold tracking-[0.04em] stretch-90', className)}>
      <svg aria-hidden viewBox="0 0 20 20" className="size-4">
        <path d="M10 1.5 17.5 5.75v8.5L10 18.5 2.5 14.25v-8.5z" fill="none" stroke="currentColor" strokeWidth="2.2" />
      </svg>
      SENTRIX
    </span>
  );
}

/** Announces a short text to screen readers when it changes. */
export function Announcer({ text, assertive }: { text: string; assertive?: boolean }) {
  return (
    <div aria-live={assertive ? 'assertive' : 'polite'} className="sr-only">
      {text}
    </div>
  );
}
