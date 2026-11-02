// SPDX-License-Identifier: MIT
/**
 * Table primitives for CRM list views. `TableHead` takes `sort` and renders `aria-sort`.
 */

import { cn, focusRing } from "./utils";

export function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div className="w-full overflow-x-auto">
      <table
        className={cn("w-full border-collapse font-sans text-sm text-text-primary", className)}
        {...props}
      />
    </div>
  );
}

export function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      className={cn("border-b-hairline border-border-default", className)}
      {...props}
    />
  );
}

export function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}

export function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      className={cn(
        "border-t-hairline border-border-default bg-surface-inset font-medium",
        className,
      )}
      {...props}
    />
  );
}

export function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      className={cn(
        "border-b-hairline border-border-subtle transition-colors duration-instant ease-standard",
        "hover:bg-surface-hover data-[selected=true]:bg-surface-selected",
        className,
      )}
      {...props}
    />
  );
}

export type SortDirection = "ascending" | "descending" | "none";

export interface TableHeadProps extends Omit<React.ComponentProps<"th">, "aria-sort"> {
  /** Omit for a column that is not sortable; `aria-sort` is only emitted when this is supplied. */
  sort?: SortDirection;
}

export function TableHead({ className, sort, ...props }: TableHeadProps) {
  return (
    <th
      scope="col"
      aria-sort={sort}
      className={cn(
        "h-row-dense px-cell-x text-left align-middle",
        "font-sans text-2xs font-semibold text-text-secondary",
        "[&:has(button)]:p-0",
        className,
      )}
      {...props}
    />
  );
}

/** The clickable target inside a sortable `TableHead`. */
export function TableSortButton({ className, ...props }: React.ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(
        "flex h-row-dense w-full items-center gap-2 px-cell-x text-left",
        "font-semibold text-text-secondary hover:text-text-primary",
        focusRing,
        className,
      )}
      {...props}
    />
  );
}

export function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return <td className={cn("px-cell-x py-cell-y align-middle", className)} {...props} />;
}

/** Right-aligned tabular-figure cell for currency, counts, and timestamps. */
export function TableNumericCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      className={cn(
        "px-cell-x py-cell-y text-right align-middle font-numeric text-xs tabular-nums",
        className,
      )}
      {...props}
    />
  );
}

export function TableCaption({ className, ...props }: React.ComponentProps<"caption">) {
  return (
    <caption className={cn("mt-4 text-left text-xs text-text-muted", className)} {...props} />
  );
}
