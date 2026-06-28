import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "WelfareFlow India — Apply for welfare schemes",
  description:
    "Simple, voice-first help to apply for PM-Kisan, Ayushman Bharat and other welfare schemes.",
};

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#fdf8f1] text-stone-900 antialiased">
        {children}
      </body>
    </html>
  );
}
