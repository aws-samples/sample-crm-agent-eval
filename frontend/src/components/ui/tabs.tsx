// SPDX-License-Identifier: MIT
/** Tabs primitive over Radix, which supplies the roving-tabindex keyboard behaviour. */

import * as TabsPrimitive from "@radix-ui/react-tabs";

import { cn, focusRing } from "./utils";

export const Tabs = TabsPrimitive.Root;

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn(
        "inline-flex items-center gap-6 border-b-hairline border-border-default",
        className,
      )}
      {...props}
    />
  );
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "-mb-px inline-flex h-control-lg items-center border-b-emphasis border-transparent",
        "font-sans text-sm font-medium text-text-muted",
        "transition-colors duration-fast ease-standard hover:text-text-primary",
        "data-[state=active]:border-accent-emphasis data-[state=active]:text-text-primary",
        "disabled:pointer-events-none disabled:opacity-50",
        focusRing,
        className,
      )}
      {...props}
    />
  );
}

export function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn("pt-6", focusRing, className)} {...props} />;
}
