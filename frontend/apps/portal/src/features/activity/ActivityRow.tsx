import { cn, formatDuration, formatTime } from '@sentrix/ui';
import type { ActivityItem } from '../../lib/models';

export function activityState(a: ActivityItem): string {
  if (a.live) return a.kind === 'unusual' ? 'Still in view' : 'Open now';
  if (a.kind === 'unusual') {
    return ({ guest: 'Marked as a guest', resident: 'Someone who lives here', unknown: 'You don’t know them' } as Record<string, string>)[a.review ?? ''] ?? 'Not looked at yet';
  }
  if (a.kind === 'alert') return a.verdict === 'false' ? 'False alarm' : a.verdict === 'true' ? 'Real' : 'Was this right?';
  return '';
}

export function needsLook(a: ActivityItem): boolean {
  return !a.live && ((a.kind === 'unusual' && !a.review) || (a.kind === 'alert' && !a.verdict));
}

export function ActivityRow({ item, timezone, onOpen }: { item: ActivityItem; timezone: string; onOpen: (item: ActivityItem) => void }) {
  const sub = [item.where, !item.live && item.durationS ? formatDuration(item.durationS) : null, activityState(item)].filter(Boolean).join('. ');
  return (
    <button
      type="button"
      onClick={() => onOpen(item)}
      className={cn('grid w-full grid-cols-[10px_1fr_auto] items-start gap-x-2.5 gap-y-0.5 rounded-xl border bg-paper px-3 py-2.5 text-left text-[13.5px] hover:bg-chalk', item.live ? 'border-haldi-fill' : 'border-line')}
    >
      <span aria-hidden className={cn('mt-1.5 size-2.5 rounded-full', item.kind === 'alert' ? 'bg-chilli-fill' : item.kind === 'unusual' ? 'bg-haldi-fill' : 'bg-slate')} />
      <b className="font-semibold">{item.title}</b>
      <time className="whitespace-nowrap text-[12px] text-slate tabular">{formatTime(item.at, timezone)}</time>
      {sub ? <span className="col-start-2 col-end-4 text-[12px] text-slate">{sub}</span> : null}
    </button>
  );
}
