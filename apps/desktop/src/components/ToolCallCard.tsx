import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import type { ToolCallData } from '@/lib/agentshive';

interface Props {
  call: ToolCallData;
}

export function ToolCallCard({ call }: Props) {
  const [open, setOpen] = useState(false);
  const status = !call.completed ? 'running' : call.isError ? 'err' : 'ok';
  const variant = status === 'ok' ? 'ok' : status === 'err' ? 'err' : 'muted';
  const inputPreview = oneLine(call.input);
  const resultText = typeof call.result === 'string' ? call.result : JSON.stringify(call.result ?? null, null, 2);
  const inputJson = JSON.stringify(call.input || {}, null, 2);

  return (
    <div className="overflow-hidden rounded-md border border-border/70 bg-input/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-secondary/30"
      >
        <ChevronRight className={cn('h-3 w-3 transition-transform', open && 'rotate-90')} />
        <span className="font-mono text-[11.5px] font-semibold text-accent">{call.name}</span>
        <span className="flex-1 truncate font-mono text-[11px] text-muted-foreground">{inputPreview}</span>
        <Badge variant={variant} className="ml-auto">{status === 'running' ? 'running…' : status}</Badge>
      </button>
      {open && (
        <div className="max-h-72 overflow-y-auto scrollbar-thin border-t border-border/60 bg-[hsl(222_50%_3%)] p-3 font-mono text-[11px] whitespace-pre-wrap break-words">
          <div className="mb-1 text-muted-foreground">input:</div>
          {inputJson}
          {call.completed && (
            <>
              <div className="mt-3 mb-1 text-muted-foreground">result{call.isError ? ' (error)' : ''}:</div>
              {resultText}
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
