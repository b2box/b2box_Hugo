import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Badge: chips soft estilo Supabase (mismo que Pro/App).
const badgeVariants = cva(
  "inline-flex items-center justify-center gap-1 rounded-full border font-medium whitespace-nowrap uppercase tracking-[0.07em] text-[9px] leading-none px-[5.5px] py-[3px] transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        // Estilo Paco: pill con borde de color, fondo transparente, texto del color.
        default: "bg-transparent text-muted-foreground border-border",
        secondary: "bg-transparent text-muted-foreground border-border",
        destructive: "bg-transparent text-destructive border-destructive",
        success: "bg-transparent text-success border-success",
        warning: "bg-transparent text-warning border-warning",
        outline: "bg-transparent text-foreground border-border",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
