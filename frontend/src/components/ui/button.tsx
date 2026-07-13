import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Botón alineado al design system B2BOX (look Supabase: plano, con borde, sin
// sombra de elevación). Mismos variantes que Pro/App.
const buttonVariants = cva(
  "relative inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md border text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "border-primary bg-primary text-primary-foreground hover:bg-primary/90 hover:border-primary",
        destructive:
          "border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/15 hover:border-destructive/60",
        outline: "border-input bg-transparent text-foreground hover:bg-muted",
        secondary: "border-border bg-background text-foreground hover:bg-muted hover:border-input",
        ghost: "border-transparent text-foreground shadow-none hover:bg-muted",
        link: "border-transparent text-primary shadow-none underline-offset-4 hover:underline",
        success:
          "border-success/30 bg-success/10 text-success hover:bg-success/15 hover:border-success/50",
        warning:
          "border-warning/30 bg-warning/10 text-warning hover:bg-warning/15 hover:border-warning/50",
      },
      size: {
        default: "h-9 px-3 py-2",
        sm: "h-8 px-2.5 text-xs",
        lg: "h-10 px-4",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
  ),
);
Button.displayName = "Button";

export { Button, buttonVariants };
