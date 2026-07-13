import * as React from "react";
import { cn } from "@/lib/utils";

// Input: control plano border-driven estilo Supabase (mismo que Pro/App).
const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => (
    <input
      type={type}
      className={cn(
        "flex h-10 w-full rounded-md border border-input read-only:border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground transition-colors",
        "focus:border-input focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        "aria-[invalid=true]:border-destructive",
        className,
      )}
      ref={ref}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export { Input };
