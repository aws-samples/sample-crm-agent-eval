// SPDX-License-Identifier: MIT
/**
 * Pipeline page — routed at `/pipeline`. Owns the page frame: the `main` landmark, the `h1`, and the
 * container rhythm. `PipelineBoard` renders a `section` inside it and supplies neither, so the page
 * has exactly one main landmark and one level-1 heading.
 */
import type { JSX } from "react";

import { PipelineBoard } from "./PipelineBoard";

export function PipelinePage(): JSX.Element {
  return (
    <main aria-label="Sales pipeline" className="mx-auto w-full max-w-content space-y-7 px-6 py-8">
      <h1 className="text-2xl font-semibold tracking-tight text-text-primary">Pipeline</h1>
      <PipelineBoard />
    </main>
  );
}
