import "katex/dist/katex.min.css";

import { Children, isValidElement, type ReactNode, useEffect, useId, useState } from "react";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkFrontmatter from "remark-frontmatter";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

/**
 * 文档预览和搜索回答共用同一套 Markdown 能力：GFM、换行、数学公式。
 * raw HTML 不启用 rehype-raw，避免导入文档中的 HTML 直接执行。
 */
export function normalizePreviewMarkdown(content: string): string {
  const source = content.replace(/\r\n?/g, "\n");
  const lines = source.split("\n");
  let inFence = false;
  const normalized: string[] = [];
  lines.forEach((line, index) => {
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      normalized.push(line);
      return;
    }
    if (inFence) {
      normalized.push(line);
      return;
    }
    const callout = /^\s*>\s*\[![a-z]+\]/i.test(line);
    normalized.push(line
      .replace(/!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, target: string, alias?: string) => (
        `![${(alias ?? target).trim()}](kemo-embed:${encodeURIComponent(target.trim())})`
      ))
      .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, target: string, alias?: string) => (
        `[${(alias ?? target).trim()}](kemo-node:${encodeURIComponent(target.trim())})`
      )));
    if (callout && lines[index + 1]?.trim().startsWith(">")) normalized.push(">");
  });
  return transformOutsideFences(normalized.join("\n"), normalizeMathDelimiters);
}

function transformOutsideFences(source: string, transform: (text: string) => string): string {
  const lines = source.split("\n");
  const output: string[] = [];
  const outside: string[] = [];
  let inFence = false;
  const flush = () => {
    if (outside.length) {
      output.push(transform(outside.join("\n")));
      outside.length = 0;
    }
  };
  lines.forEach((line) => {
    if (/^\s*(```|~~~)/.test(line)) {
      flush();
      output.push(line);
      inFence = !inFence;
      return;
    }
    if (inFence) output.push(line);
    else outside.push(line);
  });
  flush();
  return output.join("\n");
}

function normalizeMathDelimiters(source: string): string {
  return source
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, expression: string) => (
      `\n$$\n${expression.trim()}\n$$\n`
    ))
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, expression: string) => (
      `$${expression.trim()}$`
    ))
    .replace(/^\s*\[\s*((?:\\text|\\begin|\\frac|\\xrightarrow|\\mathrm|\\sum|\\int)[\s\S]*?)\s*\]\s*$/gm, (
      _match,
      expression: string,
    ) => `\n$$\n${expression.trim()}\n$$\n`);
}

type MermaidDiagramProps = { chart: string };

function MermaidDiagram({ chart }: MermaidDiagramProps) {
  const id = `kemo-mermaid-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setSvg(null);
    setError(null);
    void import("mermaid")
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base" });
        const rendered = await mermaid.render(id, chart);
        if (active) setSvg(rendered.svg);
      })
      .catch((caught: unknown) => {
        if (active) setError(caught instanceof Error ? caught.message : "Mermaid 图表无法渲染");
      });
    return () => { active = false; };
  }, [chart, id]);

  if (svg) {
    return <div className="markdown-mermaid" role="img" aria-label="Mermaid 图表" dangerouslySetInnerHTML={{ __html: svg }} />;
  }
  if (error) {
    return <div className="markdown-mermaid markdown-mermaid--error"><strong>Mermaid 图表语法错误</strong><small>{error}</small><pre>{chart}</pre></div>;
  }
  return <div className="markdown-mermaid markdown-mermaid--loading" aria-busy="true">正在绘制图表…</div>;
}

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node)) return nodeText(node.props.children as ReactNode);
  return "";
}

const CALLOUT_TYPES = new Set([
  "abstract", "attention", "bug", "caution", "danger", "example", "failure",
  "faq", "fail", "help", "hint", "important", "info", "note", "question",
  "success", "summary", "tip", "todo", "warning", "quote",
]);

function CalloutBlockquote({ children, node: _node, ...properties }: { children?: ReactNode; node?: unknown }) {
  const parts = Children.toArray(children).filter((part) => typeof part !== "string" || part.trim());
  const first = parts[0];
  const marker = nodeText(first).trim().match(/^\[!([a-z]+)\]\s*(.*)$/i);
  if (!marker || !CALLOUT_TYPES.has(marker[1].toLowerCase())) {
    return <blockquote {...properties}>{children}</blockquote>;
  }
  const type = marker[1].toLowerCase();
  const title = marker[2].trim() || type[0].toUpperCase() + type.slice(1);
  const bodyParts = parts.slice(1);
  return (
    <aside className={`markdown-callout is-${type}`} data-callout={type}>
      <div className="markdown-callout__title"><span aria-hidden="true">◆</span>{title}</div>
      {bodyParts.length ? <div className="markdown-callout__body">{bodyParts}</div> : null}
    </aside>
  );
}

function MarkdownLink({ href, children, node: _node, ...properties }: { href?: string; children?: ReactNode; node?: unknown }) {
  if (href?.startsWith("kemo-node:")) {
    const target = decodeURIComponent(href.slice("kemo-node:".length));
    return (
      <button
        {...properties}
        className="markdown-knowledge-link"
        type="button"
        data-node-key={target}
        onClick={() => window.dispatchEvent(new CustomEvent("kemo:open-node", { detail: { target } }))}
      >
        {children}
      </button>
    );
  }
  if (href?.startsWith("kemo-embed:")) {
    const target = decodeURIComponent(href.slice("kemo-embed:".length));
    return <span className="markdown-embed-placeholder">嵌入内容：{target}</span>;
  }
  return <a {...properties} href={href} target="_blank" rel="noreferrer">{children}</a>;
}

export function MarkdownPreview({ content }: { content: string }) {
  return (
    <article className="markdown-preview">
      <ReactMarkdown
        components={{
          a: MarkdownLink,
          blockquote: CalloutBlockquote,
          code: ({ className, children, node: _node, ...properties }) => {
            const language = /language-([\w-]+)/.exec(className ?? "")?.[1];
            const text = String(children).replace(/\n$/, "");
            if (language === "mermaid") return <MermaidDiagram chart={text} />;
            return <code {...properties} className={className}>{children}</code>;
          },
          img: ({ alt, node: _node, ...properties }) => (
            <img {...properties} alt={alt ?? "文档图片"} loading="lazy" />
          ),
          pre: ({ children, node: _node, ...properties }) => (
            <pre {...properties} tabIndex={0}>
              {children}
            </pre>
          ),
        }}
        remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, [remarkFrontmatter, ["yaml", "toml"]]]}
        rehypePlugins={[
          [rehypeHighlight, { detect: false, plainText: ["mermaid"] }],
          [rehypeKatex, { strict: "ignore", throwOnError: false }],
        ]}
      >
        {normalizePreviewMarkdown(content)}
      </ReactMarkdown>
    </article>
  );
}
