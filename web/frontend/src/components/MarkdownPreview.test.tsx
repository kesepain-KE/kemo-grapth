import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { MarkdownPreview, normalizePreviewMarkdown } from "./MarkdownPreview";

describe("MarkdownPreview", () => {
  it("normalizes Windows and classic Mac line endings without changing Markdown", () => {
    expect(normalizePreviewMarkdown("# 标题\r\n\r\n[[数据结构|结构]]\r---\r"))
      .toBe("# 标题\n\n[结构](kemo-node:%E6%95%B0%E6%8D%AE%E7%BB%93%E6%9E%84)\n---\n");
  });

  it("keeps GFM constructs available to the shared React Markdown renderer", () => {
    const markdown = normalizePreviewMarkdown([
      "- [x] 已完成",
      "- [ ] 待处理",
      "",
      "> 引用内容",
      "",
      "| A | B |",
      "| --- | --- |",
      "| 1 | 2 |",
      "",
      "---",
      "",
      "```python",
      "print('ok')",
      "```",
    ].join("\r\n"));

    expect(markdown).toContain("> 引用内容");
    expect(markdown).toContain("| --- | --- |");
    expect(markdown).toContain("---\n\n```python");
    expect(markdown).toContain("- [x] 已完成");
  });

  it("renders block quotes, rules and tables as HTML instead of literal Markdown", () => {
    const html = renderToStaticMarkup(
      <MarkdownPreview
        content={["> 引用内容", "", "---", "", "| A | B |", "| --- | --- |", "| 1 | 2 |"].join("\n")}
      />,
    );

    expect(html).toContain("<blockquote>");
    expect(html).toContain("<hr/>");
    expect(html).toContain("<table>");
    expect(html).not.toContain("> 引用内容</p>");
  });

  it("renders Obsidian callouts as themed knowledge-document panels", () => {
    const html = renderToStaticMarkup(
      <MarkdownPreview content={"> [!WARNING] 注意\n> 这是一条重要信息。"} />,
    );

    expect(html).toContain("markdown-callout is-warning");
    expect(html).toContain("注意");
    expect(html).toContain("这是一条重要信息");
  });

  it("normalizes legacy LaTeX delimiters and renders complex KaTeX formulas", () => {
    const normalized = normalizePreviewMarkdown([
      "\\[",
      "\\begin{aligned}",
      "f(x) &= \\begin{cases} x^2 & x > 0 \\\\ -x & x \\le 0 \\end{cases} \\\\",
      "A &= \\begin{bmatrix} 1 & 2 \\\\ 3 & 4 \\end{bmatrix} + \\frac{1}{2}",
      "\\end{aligned}",
      "\\]",
      "",
      "内联：\\(a \\xrightarrow{关系} b\\)。",
    ].join("\n"));
    expect(normalized).toContain("$$");
    expect(normalized).toContain("$a \\xrightarrow{关系} b$");
    const html = renderToStaticMarkup(<MarkdownPreview content={normalized} />);
    expect(html).toContain("katex");
    expect(html).toContain("mfrac");
  });
});
