import { cn } from '@sentrix/ui';
import type { ReactNode } from 'react';

export function Panel({ title, children, className, actions }: { title: ReactNode; children: ReactNode; className?: string; actions?: ReactNode }) {
  return (
    <section className={cn('grid content-start gap-3 rounded-2xl border border-line bg-paper p-4', className)}>
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-sans text-[15px] font-bold">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  if (!rows.length) return <p className="text-[13px] text-slate">Nothing reported.</p>;
  return (
    <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1.5 text-[13px]">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-slate">{k}</dt>
          <dd className="m-0 text-right font-semibold tabular">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Bar({ label, value, max = 1, hint }: { label: string; value: number | null | undefined; max?: number; hint?: string }) {
  const v = typeof value === 'number' && Number.isFinite(value) ? value : null;
  const pct = v == null ? 0 : Math.max(0, Math.min(100, (v / max) * 100));
  const tone = pct > 60 ? 'bg-chilli-fill' : pct > 30 ? 'bg-haldi-fill' : 'bg-indigo-fill';
  return (
    <div className="grid grid-cols-[110px_1fr_64px] items-center gap-2 text-[12.5px]">
      <span className="truncate text-slate">{label}</span>
      <span className="h-2 overflow-hidden rounded-full bg-hatch">
        <i className={cn('block h-full rounded-full', tone)} style={{ width: `${pct}%` }} />
      </span>
      <span className="text-right font-semibold tabular">{hint ?? (v == null ? 'n/a' : `${Math.round(pct)}%`)}</span>
    </div>
  );
}

export function yesNo(v: unknown): string {
  if (v === true) return 'yes';
  if (v === false) return 'no';
  if (v == null || v === '') return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
