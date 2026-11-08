// SPDX-License-Identifier: MIT
/**
 * Run artefacts open through a presigned URL from `artefact_url`: the click asks the run API for the
 * one key, opens the returned URL in a new tab, and states a refusal instead of failing silently.
 * `HttpEvalRunClient.getArtefactUrl` sends exactly the handler's keys and refuses a reply it
 * cannot trust.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CrmApiError } from "../../api/CrmApiClient";
import { EvalArtifactList } from "./EvalArtifactList";
import { HttpEvalRunClient } from "./EvalRunClient";
import type { ArtefactUrlOptions, EvalArtefactUrlResponse } from "./EvalRunClient";
import { BaseFakeEvalRunClient } from "./testFixtures";
import type { EvalArtifactLinks } from "./types";

const RAW_KEY = "raw/anthropic.claude-opus-5-5_2026-10-12T18-20-12.008916+00-00.json";
const LINKS: EvalArtifactLinks = {
  matrixJson: "matrix.json",
  matrixMd: "matrix.md",
  matrixCsv: "matrix.csv",
  raw: [RAW_KEY],
};

interface FakeTab {
  opener: unknown;
  location: { href: string };
  close: ReturnType<typeof vi.fn>;
}

function stubWindowOpen(): { tab: FakeTab; open: ReturnType<typeof vi.fn> } {
  const tab: FakeTab = { opener: "the dashboard", location: { href: "" }, close: vi.fn() };
  const open = vi.fn().mockReturnValue(tab);
  vi.stubGlobal("open", open);
  return { tab, open };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("EvalArtifactList — presigned artefact URLs", () => {
  it.each<[string, RegExp | string, string]>([
    ["matrix.json", /matrix\.json/i, "matrix.json"],
    ["matrix.md", /^matrix\.md/i, "matrix.md"],
    ["matrix.csv", /matrix\.csv/i, "matrix.csv"],
    ["a raw archive", RAW_KEY, RAW_KEY],
  ])("clicking %s requests that one key and opens the presigned URL in a new tab", async (_l, name, key) => {
    const { tab, open } = stubWindowOpen();
    const client = new BaseFakeEvalRunClient();
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={client} />);

    await userEvent.click(screen.getByRole("button", { name }));

    await waitFor(() =>
      expect(tab.location.href).toBe(`https://artefacts.example.test/run-9/${key}?X-Amz-Expires=300`),
    );
    expect(client.artefactRequests).toEqual([{ runId: "run-9", key }]);
    expect(open).toHaveBeenCalledWith("", "_blank");
    expect(tab.opener).toBeNull();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("states a refusal, names the artefact, and closes the tab it opened", async () => {
    const { tab } = stubWindowOpen();
    const client = new BaseFakeEvalRunClient([], {
      artefactError: new CrmApiError("scope_denied", 403, {
        message: "Reading evaluation artefacts requires full tenant visibility.",
      }),
    });
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={client} />);

    await userEvent.click(screen.getByRole("button", { name: /matrix\.json/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not open matrix\.json/i);
    expect(alert).toHaveTextContent(/full tenant visibility/i);
    expect(tab.close).toHaveBeenCalledTimes(1);
    expect(tab.location.href).toBe("");
  });

  it("navigates this tab when the browser refuses a new one", async () => {
    vi.stubGlobal("open", vi.fn().mockReturnValue(null));
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const client = new BaseFakeEvalRunClient();
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={client} />);

    await userEvent.click(screen.getByRole("button", { name: /matrix\.csv/i }));

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://artefacts.example.test/run-9/matrix.csv?X-Amz-Expires=300"),
    );
  });

  it("renders no raw path as a link, so nothing reaches the private bucket unsigned", () => {
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={new BaseFakeEvalRunClient()} />);
    expect(screen.queryAllByRole("link")).toEqual([]);
  });
});

/** Records each request's `download` option beside the base fake's key log. */
class DownloadRecordingClient extends BaseFakeEvalRunClient {
  public readonly downloadOptions: (ArtefactUrlOptions | undefined)[] = [];

  override getArtefactUrl(
    runId: string,
    key: string,
    options?: ArtefactUrlOptions,
  ): Promise<EvalArtefactUrlResponse> {
    this.downloadOptions.push(options);
    return super.getArtefactUrl(runId, key);
  }
}

describe("EvalArtifactList — Download matrix.md", () => {
  it("asks for matrix.md as an attachment and navigates to the returned URL", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const client = new DownloadRecordingClient();
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={client} />);

    await userEvent.click(screen.getByTestId("download-matrix-md"));

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://artefacts.example.test/run-9/matrix.md?X-Amz-Expires=300"),
    );
    expect(client.artefactRequests).toEqual([{ runId: "run-9", key: "matrix.md" }]);
    expect(client.downloadOptions).toEqual([{ download: true }]);
    expect(open).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("is named for what it does and sits beside the open control", () => {
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={new BaseFakeEvalRunClient()} />);
    const button = screen.getByTestId("download-matrix-md");
    expect(button).toHaveAccessibleName("Download matrix.md");
    expect(button.closest("li")).toBe(screen.getByRole("button", { name: /^matrix\.md/i }).closest("li"));
  });

  it("opening matrix.md still asks for no attachment", async () => {
    stubWindowOpen();
    const client = new DownloadRecordingClient();
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={client} />);
    await userEvent.click(screen.getByRole("button", { name: /^matrix\.md/i }));
    await waitFor(() => expect(client.downloadOptions).toEqual([undefined]));
  });

  it("states a refusal in the existing alert", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const client = new BaseFakeEvalRunClient([], {
      artefactError: new CrmApiError("scope_denied", 403, {
        message: "Reading evaluation artefacts requires full tenant visibility.",
      }),
    });
    render(<EvalArtifactList artifacts={LINKS} runId="run-9" client={client} />);

    await userEvent.click(screen.getByTestId("download-matrix-md"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not download matrix\.md/i);
    expect(alert).toHaveTextContent(/full tenant visibility/i);
    expect(assign).not.toHaveBeenCalled();
  });

  it("is absent when the run recorded no matrix.md", () => {
    render(
      <EvalArtifactList
        artifacts={{ ...LINKS, matrixMd: undefined }}
        runId="run-9"
        client={new BaseFakeEvalRunClient()}
      />,
    );
    expect(screen.queryByTestId("download-matrix-md")).not.toBeInTheDocument();
    expect(screen.getByText(/matrix\.md — not available/)).toBeInTheDocument();
  });
});

describe("HttpEvalRunClient.getArtefactUrl", () => {
  function clientWith(response: unknown): { client: HttpEvalRunClient; fetchMock: ReturnType<typeof vi.fn> } {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(response) });
    vi.stubGlobal("fetch", fetchMock);
    return {
      client: new HttpEvalRunClient("/api", { getAccessToken: () => Promise.resolve("tok") }),
      fetchMock,
    };
  }

  const reply = {
    run_id: "run-7",
    key: "matrix.json",
    url: "https://bucket.s3.us-east-1.amazonaws.com/eval-runs/run-7/matrix.json?X-Amz-Expires=300",
    expires_in: 300,
  };

  it("posts exactly the handler's keys to /eval_trigger", async () => {
    const { client, fetchMock } = clientWith(reply);
    await expect(client.getArtefactUrl("run-7", "matrix.json")).resolves.toEqual(reply);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/eval_trigger");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      action: "artefact_url",
      run_id: "run-7",
      key: "matrix.json",
    });
  });

  it("adds download: true only when asked", async () => {
    const { client, fetchMock } = clientWith(reply);
    await client.getArtefactUrl("run-7", "matrix.json", { download: true });
    await client.getArtefactUrl("run-7", "matrix.json", { download: false });
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse((init as RequestInit).body as string));
    expect(bodies).toEqual([
      { action: "artefact_url", run_id: "run-7", key: "matrix.json", download: true },
      { action: "artefact_url", run_id: "run-7", key: "matrix.json" },
    ]);
  });

  it.each<[string, unknown]>([
    ["a plain-http URL", { ...reply, url: "http://bucket.s3.amazonaws.com/x" }],
    ["no URL", { ...reply, url: undefined }],
    ["no expiry", { ...reply, expires_in: undefined }],
    ["a different key", { ...reply, key: "matrix.md" }],
    ["a different run", { ...reply, run_id: "run-8" }],
  ])("refuses a reply with %s rather than opening it", async (_label, response) => {
    const { client } = clientWith(response);
    await expect(client.getArtefactUrl("run-7", "matrix.json")).rejects.toMatchObject({
      body: { code: "eval_response_unreadable" },
    });
  });
});
