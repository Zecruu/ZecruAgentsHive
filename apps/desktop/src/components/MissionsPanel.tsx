import { useCallback, useEffect, useState } from 'react';
import { Compass, RefreshCw, Target, X as XIcon } from 'lucide-react';
import { ah, type ExportedMission, type MissionsExport } from '@/lib/agentshive';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface Props {
  projectSlug: string;
  refreshKey?: number; // bump to force an immediate refresh (e.g. after a turn)
  onClose: () => void;
}

const SPEC_PREVIEW = 240;

export function MissionsPanel({ projectSlug, refreshKey, onClose }: Props) {
  const [data, setData] = useState<MissionsExport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await ah().missions.export(projectSlug);
      setData(d);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message || String(e));
    }
  }, [projectSlug]);

  useEffect(() => {
    load();
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
  }, [load]);

  // External nudge (e.g. a turn finished) → refresh promptly.
  useEffect(() => { if (refreshKey !== undefined) load(); }, [refreshKey, load]);

  const missions = data?.missions || [];
  const active = missions.find((m) => m.status === 'active') || null;
  const recent = missions.filter((m) => m.status !== 'active').slice().reverse();

  return (
    <aside className="relative flex w-80 flex-none flex-col overflow-hidden border-l border-border/60 glass">
      <div className="flex flex-none items-center justify-between border-b border-border/60 px-3 py-2.5">
        <div className="flex items-center gap-1.5 text-[13px] font-semibold tracking-tight">
          <Compass className="h-4 w-4 text-accent" /> Missions
        </div>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={load} title="Refresh">
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose} title="Hide panel">
            <XIcon className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-4">
        {err && <p className="text-xs text-destructive">Failed to load: {err}</p>}

        {data?.foundation && (
          <section>
            <h4 className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              <Target className="h-3 w-3 text-primary" /> Foundation
            </h4>
            <div className="rounded-md border border-primary/30 bg-gradient-to-b from-primary/10 to-primary/0 px-3 py-2">
              <div className="text-[13px] font-medium">{data.foundation.name}</div>
              {data.foundation.spec && (
                <p className="mt-1 whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-muted-foreground">
                  {preview(data.foundation.spec)}
                </p>
              )}
            </div>
          </section>
        )}

        <section>
          <h4 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Active</h4>
          {active ? (
            <MissionCard m={active} highlight />
          ) : (
            <p className="text-[12px] text-muted-foreground">No active mission.</p>
          )}
        </section>

        {recent.length > 0 && (
          <section>
            <h4 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              Recent ({recent.length})
            </h4>
            <div className="space-y-2">
              {recent.map((m) => <MissionCard key={m.mission_id} m={m} />)}
            </div>
          </section>
        )}

        {!data && !err && <p className="text-xs text-muted-foreground">Loading…</p>}
      </div>
    </aside>
  );
}

function MissionCard({ m, highlight }: { m: ExportedMission; highlight?: boolean }) {
  const tone =
    m.status === 'active' ? 'ok'
    : m.status === 'done' ? 'muted'
    : 'warn'; // superseded
  const reports = (m.summaries || []).length;
  return (
    <div className={cn('rounded-md border px-3 py-2', highlight ? 'border-accent/40 bg-card/70' : 'border-border/60 bg-input/30')}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 text-[13px] font-medium tracking-tight">{m.name}</div>
        <Badge variant={tone as 'ok' | 'muted' | 'warn'} className="shrink-0">{m.status}</Badge>
      </div>
      {m.spec && (
        <p className="mt-1 whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-muted-foreground">
          {preview(m.spec)}
        </p>
      )}
      <div className="mt-1 text-[10px] text-muted-foreground">
        {reports} report{reports === 1 ? '' : 's'}
        {m.done_at ? ' · done' : ''}
      </div>
    </div>
  );
}

function preview(s: string): string {
  return s.length > SPEC_PREVIEW ? s.slice(0, SPEC_PREVIEW - 1) + '…' : s;
}
