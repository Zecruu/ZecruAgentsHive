import { useState } from 'react';
import { ChevronRight, FileText, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import { basename, toolFileTarget, type ToolCallData } from '@/lib/agentshive';

interface Props {
  call: ToolCallData;
}

export function ToolCallCard({ call }: Props) {
  const [open, setOpen] = useState(false);
  const status = !call.completed ? 'running' : call.isError ? 'err' : 'ok';
  const variant = status === 'ok' ? 'ok' : status === 'err' ? 'err' : 'muted';
  const file = toolFileTarget(call);
  const inputPreview = oneLine(call.input);
  const resultText = typeof call.result === 'string' ? call.result : JSON.stringify(call.result ?? null, null, 2);
  const inputJson = JSON.stringify(call.input || {}, null, 2);

  return (
    <div className="overflow-hidden rounded-md border border-border/70 bg-card/70 shadow-[0_1px_0_hsl(0_0%_100%/0.03)_inset]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-secondary/30"
      >
        <ChevronRight className={cn('h-3 w-3 transition-transform', open && 'rotate-90')} />
        <span className="font-mono text-[11.5px] font-semibold text-accent">{call.name}</span>
        {file && (
          <span
            className={cn(
              'inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]',
              file.changed ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-input/40 text-muted-foreground',
            )}
            title={`${file.changed ? 'edited' : 'read'} · ${file.path}`}
          >
            <FileText className="h-2.5 w-2.5" />
            {basename(file.path)}
          </span>
        )}
        <span className="flex-1 truncate font-mono text-[11px] text-muted-foreground">{file ? '' : inputPreview}</span>
        {status === 'running' && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
        <Badge variant={variant} className="ml-auto">{status === 'running' ? 'running…' : status}</Badge>
      </button>
      {open && (
        <div className="max-h-72 overflow-y-auto scrollbar-thin border-t border-border/60 bg-input/30 p-3 font-mono text-[11px] whitespace-pre-wrap break-words">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">input</div>
          <pre className="m-0 rounded-md border border-border/50 bg-background/60 p-2 font-mono text-[11px] leading-relaxed text-foreground/90">{inputJson}</pre>
          {call.completed && (
            <>
              <div className="mt-3 mb-1 text-muted-foreground">result{call.isError ? ' (error)' : ''}:</div>
              <pre className="m-0 rounded-md border border-border/50 bg-background/60 p-2 font-mono text-[11px] leading-relaxed text-foreground/90">{resultText}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function oneLine(input: unknown): string {
  if (!input) return '';
  const s = JSON.stringify(input);
  return s.length > 100 ? s.slice(0, 97) + '…' : s;
}
