// SPDX-License-Identifier: MIT
/**
 * The shared run-spend description: a live figure, the stub wording, the unrecognised-mode wording,
 * and "not measured" for a live run whose items all failed before any was measured.
 */
import { describe, expect, it } from "vitest";

import { formatUsd } from "../../lib/format";
import { jobPayload } from "./testFixtures";
import { describeRunSpend } from "./useRunHistory";

describe("describeRunSpend", () => {
  it("shows a live run's metered figure", () => {
    const run = jobPayload({ mode: "live", tasks_done: 3, tasks_failed: 1, cost_spent_usd: 1.25 });
    expect(describeRunSpend(run)).toBe(formatUsd(1.25));
    expect(describeRunSpend(run)).not.toBe("not measured");
  });

  it("says 'not measured' for a live run with no item done and at least one failed", () => {
    const run = jobPayload({ mode: "live", tasks_done: 0, tasks_failed: 2, cost_spent_usd: 0 });
    expect(describeRunSpend(run)).toBe("not measured");
  });

  it("shows the live figure when nothing has failed yet, even with no item done", () => {
    const run = jobPayload({ status: "running", mode: "live", tasks_done: 0, tasks_failed: 0 });
    expect(describeRunSpend(run)).toBe(formatUsd(0));
  });

  it("states a stub run incurs no inference cost, even when every item failed", () => {
    const run = jobPayload({ mode: "stub", tasks_done: 0, tasks_failed: 40 });
    expect(describeRunSpend(run)).toBe("no inference cost (stub)");
  });

  it("shows no figure for an unrecognised mode, even when every item failed", () => {
    const run = jobPayload({ mode: "replay", tasks_done: 0, tasks_failed: 40 });
    expect(describeRunSpend(run)).toBe("not displayed (unrecognised mode)");
  });
});
