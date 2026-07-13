/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Fira Sans"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"Fira Code"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        navy: {
          50: "#F8FAFC", 100: "#F1F5F9", 200: "#E2E8F0", 300: "#CBD5E1",
          400: "#94A3B8", 500: "#64748B", 600: "#475569", 700: "#334155",
          800: "#1E293B", 900: "#0F172A", 950: "#020617",
        },
        brand: {
          50: "#F0F9FF", 100: "#E0F2FE", 200: "#BAE6FD", 300: "#7DD3FC",
          400: "#38BDF8", 500: "#0284C7", 600: "#0369A1", 700: "#075985",
          800: "#0C4A6E", 900: "#082F49",
        },
      },
      boxShadow: {
        card: "0 1px 2px 0 rgba(15,23,42,.04), 0 1px 3px 0 rgba(15,23,42,.06)",
        "card-hover": "0 4px 12px -2px rgba(15,23,42,.08), 0 2px 4px -2px rgba(15,23,42,.06)",
      },
    },
  },
  plugins: [],
};
