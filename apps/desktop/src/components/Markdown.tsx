import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { cn } from '@/lib/utils';

// Safe markdown for chat bubbles. react-markdown does NOT render raw HTML (no
// rehype-raw) → no XSS, and the default URL transform strips dangerous protocols.
// All styling is theme-tokened Tailwind here (no index.css edit), so it works in
// light + dark and won't collide with the theme-token file. Streaming-safe:
// it re-parses the (growing) text on each render.
//
// react-markdown v10 dropped the `inline` prop on `code`; we detect a fenced
// block by the `language-*` class the renderer adds and let <pre> style blocks,
// rendering everything else as an inline pill.
//
// memo'd on its primitive props: markdown parsing is expensive, and the parent
// ChatPane re-renders on every composer keystroke. Without this, each bubble
// re-parsed its markdown per keystroke (the 2.0.11 typing-lag regression). Both
// props are primitives, so the default shallow compare re-parses ONLY when a
// message's text actually changes (i.e. streaming) — not when the draft does.
export const MarkdownBody = memo(function MarkdownBody({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn('break-words text-[12px] leading-normal', className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props: any) => (
            <a {...props} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-2 hover:text-primary/80" />
          ),
          code: ({ className: c, children, ...props }: any) => {
            const isBlock = typeof c === 'string' && /language-/.test(c);
            return isBlock ? (
              <code className={cn('font-mono text-[11.5px]', c)} {...props}>{children}</code>
            ) : (
              <code className="rounded bg-input/60 px-1 py-0.5 font-mono text-[11.5px]" {...props}>{children}</code>
            );
          },
          pre: (props: any) => (
            <pre className="my-1.5 overflow-x-auto scrollbar-thin rounded-md border border-border/60 bg-input/40 p-2.5 font-mono text-[11.5px] leading-normal" {...props} />
          ),
          ul: (props: any) => <ul className="my-1 ml-4 list-disc space-y-0.5" {...props} />,
          ol: (props: any) => <ol className="my-1 ml-4 list-decimal space-y-0.5" {...props} />,
          li: (props: any) => <li className="leading-normal" {...props} />,
          h1: (props: any) => <h1 className="mb-1 mt-2 text-[13.5px] font-semibold first:mt-0" {...props} />,
          h2: (props: any) => <h2 className="mb-1 mt-2 text-[13px] font-semibold first:mt-0" {...props} />,
          h3: (props: any) => <h3 className="mb-1 mt-2 text-[12.5px] font-semibold first:mt-0" {...props} />,
          p: (props: any) => <p className="my-1 whitespace-pre-wrap first:mt-0 last:mb-0" {...props} />,
          blockquote: (props: any) => <blockquote className="my-1.5 border-l-2 border-border pl-3 text-muted-foreground" {...props} />,
          table: (props: any) => <table className="my-1.5 w-full border-collapse text-[11.5px]" {...props} />,
          th: (props: any) => <th className="border border-border px-2 py-0.5 text-left font-semibold" {...props} />,
          td: (props: any) => <td className="border border-border px-2 py-0.5" {...props} />,
          hr: () => <hr className="my-2 border-border" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
