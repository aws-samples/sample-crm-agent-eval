// SPDX-License-Identifier: MIT
/**
 * MarkdownAnswer renders GFM, never turns model output into live HTML (checked by rendering and
 * inspecting the DOM), and only emits safe links.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MARKDOWN_ANSWER_TESTID, MARKDOWN_IMAGE_TESTID, MarkdownAnswer, safeUrl } from "./MarkdownAnswer";

afterEach(cleanup);

describe("MarkdownAnswer renders GFM", () => {
  it("renders a container with the stable test id", () => {
    render(<MarkdownAnswer text="hello" />);
    expect(screen.getByTestId(MARKDOWN_ANSWER_TESTID)).toBeInTheDocument();
  });

  it("renders a GFM pipe table as a real table with header and body cells", () => {
    const table = ["| Model | Cost |", "| --- | --- |", "| astra | high |"].join("\n");
    const { container } = render(<MarkdownAnswer text={table} />);

    expect(container.querySelector("table")).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "Model" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "astra" })).toBeInTheDocument();
  });

  it("renders lists, inline code, and fenced code blocks", () => {
    const md = ["- one", "- two", "", "Inline `x` here.", "", "```", "block", "```"].join("\n");
    const { container } = render(<MarkdownAnswer text={md} />);

    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("code")).not.toBeNull();
    expect(container.querySelector("pre")).not.toBeNull();
  });
});

describe("MarkdownAnswer never renders raw HTML (executed, not source-asserted)", () => {
  it("leaves an injected tag as inert text and creates no element for it", () => {
    const injected = 'before <b>bold</b> <img src="x" onerror="alert(1)"> after';
    const { container } = render(<MarkdownAnswer text={injected} />);

    // No live element was created from the raw HTML.
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    // The tag text survives verbatim as content rather than being parsed away.
    expect(container.textContent).toContain("<b>bold</b>");
  });

  it("does not execute a raw <script> tag from the answer", () => {
    const { container } = render(
      <MarkdownAnswer text={'text <script>window.__x = 1</script> more'} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>");
  });
});

describe("MarkdownAnswer never fetches an image", () => {
  it("renders a markdown image as inert alt text with no <img> element", () => {
    const { container } = render(
      <MarkdownAnswer text="![the Q3 chart](https://evil.example.com/pixel.png)" />,
    );

    // No <img> at all — nothing to fetch, no src to inspect.
    expect(container.querySelector("img")).toBeNull();
    const rendered = screen.getByTestId(MARKDOWN_IMAGE_TESTID);
    expect(rendered).toHaveTextContent("Image: the Q3 chart");
    // The URL never reaches the DOM in any attribute.
    expect(container.innerHTML).not.toContain("evil.example.com");
  });

  it("labels an image with no alt text without fetching it either", () => {
    const { container } = render(<MarkdownAnswer text="![](https://evil.example.com/x.png)" />);

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByTestId(MARKDOWN_IMAGE_TESTID)).toHaveTextContent("Image omitted");
    expect(container.innerHTML).not.toContain("evil.example.com");
  });
});

describe("MarkdownAnswer emits only safe links", () => {
  it("renders an https link with rel=noopener noreferrer", () => {
    render(<MarkdownAnswer text="[site](https://example.com/x)" />);
    const link = screen.getByRole("link", { name: "site" });
    expect(link).toHaveAttribute("href", "https://example.com/x");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("keeps a relative link", () => {
    render(<MarkdownAnswer text="[accounts](/accounts/acc-1)" />);
    expect(screen.getByRole("link", { name: "accounts" })).toHaveAttribute(
      "href",
      "/accounts/acc-1",
    );
  });

  it("drops a javascript: link's href rather than rendering it", () => {
    const { container } = render(<MarkdownAnswer text="[x](javascript:alert(1))" />);
    // The href is dropped, so the anchor has no link role; the element is queried directly.
    const anchor = container.querySelector("a");
    expect(anchor).not.toBeNull();
    expect(anchor?.textContent).toBe("x");
    const href = anchor?.getAttribute("href");
    expect(href === null || href === "").toBe(true);
    expect(container.innerHTML).not.toContain("javascript");
  });
});

describe("safeUrl", () => {
  it("passes https: and relative references", () => {
    expect(safeUrl("https://example.com")).toBe("https://example.com");
    expect(safeUrl("/accounts")).toBe("/accounts");
    expect(safeUrl("#anchor")).toBe("#anchor");
    expect(safeUrl("?q=1")).toBe("?q=1");
  });

  it("drops every non-https scheme and protocol-relative urls", () => {
    for (const url of [
      "javascript:alert(1)",
      "data:text/html,x",
      "http://example.com",
      "mailto:a@b.com",
      "tel:+1",
      "//evil.example.com",
    ]) {
      expect(safeUrl(url)).toBe("");
    }
  });
});
