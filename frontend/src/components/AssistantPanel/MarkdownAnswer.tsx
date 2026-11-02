// SPDX-License-Identifier: MIT
/**
 * The assistant's answer, rendered from GFM markdown.
 *
 * - No raw HTML: `rehype-raw` is not used, so an injected tag renders as inert text.
 * - Safe links only: {@link safeUrl} passes `https:` and relative URLs and drops every other scheme
 *   and protocol-relative `//host`. Every link carries `rel="noopener noreferrer"`.
 * - No image is fetched: the `img` renderer shows the alt text as inert markup with no `src`.
 *
 * Styling uses token classes only (`frontend/src/styles/tokens.css`), with block rhythm from one
 * `space-y` on the root.
 */

import type { JSX } from "react";
import Markdown, { type Components, type UrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "../ui/utils";

export const MARKDOWN_ANSWER_TESTID = "markdown-answer";

/** Marks where an image in the answer was rendered as inert text instead of fetched. */
export const MARKDOWN_IMAGE_TESTID = "markdown-image";

export interface MarkdownAnswerProps {
  /** The assistant's answer text, as raw GFM markdown. */
  text: string;
  /** Optional extra classes merged onto the root. Token classes only. */
  className?: string;
}

/**
 * Return `url` if it is safe to render as a link/target, else `""` (which drops the attribute).
 * Safe means `https:` or a relative reference that is not protocol-relative.
 */
export function safeUrl(url: string): string {
  const trimmed = url.trim();
  if (trimmed.startsWith("//")) return "";
  const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(trimmed);
  if (scheme) return scheme[1].toLowerCase() === "https" ? trimmed : "";
  return trimmed;
}

const urlTransform: UrlTransform = (url) => safeUrl(url);

const COMPONENTS: Components = {
  // An `img` in model output is never fetched; its alt text is shown as inert markup.
  img: ({ alt }) => (
    <span data-testid={MARKDOWN_IMAGE_TESTID} className="text-text-secondary italic">
      {alt && alt.trim() !== "" ? `Image: ${alt}` : "Image omitted"}
    </span>
  ),
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-text-link underline">
      {children}
    </a>
  ),
  h1: ({ children }) => <h1 className="text-base font-semibold text-text-primary">{children}</h1>,
  h2: ({ children }) => <h2 className="text-sm font-semibold text-text-primary">{children}</h2>,
  h3: ({ children }) => <h3 className="text-sm font-medium text-text-primary">{children}</h3>,
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-6">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal space-y-1 pl-6">{children}</ol>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-hairline border-border-default pl-4 text-text-secondary">
      {children}
    </blockquote>
  ),
  code: ({ className, children }) => {
    const isBlock = /language-/.test(className ?? "");
    return isBlock ? (
      <code className={cn("font-mono text-2xs", className)}>{children}</code>
    ) : (
      <code className="rounded-control bg-surface-inset px-1 font-mono text-2xs">{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="overflow-x-auto rounded-surface bg-surface-inset px-4 py-3">{children}</pre>
  ),
  table: ({ children }) => <table className="w-full border-collapse text-2xs">{children}</table>,
  th: ({ children }) => (
    <th className="border border-border-subtle px-cell-x py-cell-y text-left font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-border-subtle px-cell-x py-cell-y">{children}</td>
  ),
};

/** Render `text` as GFM markdown: no raw HTML, safe links, token-styled block elements. */
export function MarkdownAnswer({ text, className }: MarkdownAnswerProps): JSX.Element {
  return (
    <div
      data-testid={MARKDOWN_ANSWER_TESTID}
      className={cn("space-y-3 text-sm leading-relaxed text-text-primary", className)}
    >
      <Markdown remarkPlugins={[remarkGfm]} urlTransform={urlTransform} components={COMPONENTS}>
        {text}
      </Markdown>
    </div>
  );
}
