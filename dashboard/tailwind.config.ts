import type { Config } from "tailwindcss";

export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:    "#0a0a0c",
        panel: "#13131a",
        text:  "#e7e7ec",
        muted: "#7a7a85",
        accent:"#22d39e",
        danger:"#ff5c6f",
      },
    },
  },
  plugins: [],
} satisfies Config;
