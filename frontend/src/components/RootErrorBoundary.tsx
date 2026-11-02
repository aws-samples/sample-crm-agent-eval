// SPDX-License-Identifier: MIT
/**
 * Top-level error boundary: a render or commit error shows a stated, reportable error with the thrown
 * message and a reload affordance, instead of an unmounted, blank root. It does not retry the subtree.
 */
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

export interface RootErrorBoundaryProps {
  readonly children: ReactNode;
}

interface RootErrorBoundaryState {
  readonly error: Error | undefined;
}

export class RootErrorBoundary extends Component<RootErrorBoundaryProps, RootErrorBoundaryState> {
  constructor(props: RootErrorBoundaryProps) {
    super(props);
    this.state = { error: undefined };
  }

  static getDerivedStateFromError(error: unknown): RootErrorBoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // The component stack identifies which subtree threw.
    console.error("Unhandled render error", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      // Plain token utilities and no imported component, so this renders even when the failure is
      // inside the component layer.
      <div
        role="alert"
        data-testid="root-error-boundary"
        className="mx-auto my-8 max-w-prose space-y-4 rounded-surface border border-hairline border-danger-border bg-danger-surface p-6 text-danger-text"
      >
        <h1 className="text-xl font-semibold tracking-tight">Something went wrong</h1>
        <p className="text-sm leading-relaxed">
          The page could not be displayed. This is a bug in the application, not a problem with your
          account or your access.
        </p>
        {/* The quotable message, in a monospace face so it survives being copied into a report. */}
        <p
          data-testid="root-error-message"
          className="rounded-control border border-hairline border-danger-border bg-surface-raised px-4 py-3 font-numeric text-xs text-text-primary"
        >
          {error.message}
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="inline-flex h-control-md items-center rounded-control border border-hairline border-border-control bg-surface-raised px-5 text-sm font-medium text-text-primary outline-none hover:bg-surface-hover focus-visible:ring-focus focus-visible:ring-focus-ring focus-visible:ring-offset-focus focus-visible:ring-offset-surface-base"
        >
          Reload the page
        </button>
      </div>
    );
  }
}
