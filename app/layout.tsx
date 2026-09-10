import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  metadataBase: new URL('https://s-chenhao.github.io'),
  title: 'Chenhao Si 司辰昊 — Scientific Machine Learning',
  description:
    'Chenhao Si (司辰昊) is a PhD candidate at CUHK-Shenzhen working on scientific machine learning, AI for Science, and physics-informed neural networks.',
  alternates: {
    canonical: '/',
  },
  openGraph: {
    title: 'Chenhao Si 司辰昊 — Scientific Machine Learning',
    description:
      'PhD candidate at CUHK-Shenzhen working on scientific machine learning, AI for Science, and physics-informed neural networks.',
    url: '/',
    siteName: 'Chenhao Si 司辰昊',
    images: [{ url: '/og.png', width: 1200, height: 630, alt: 'Chenhao Si 司辰昊 — Scientific Machine Learning' }],
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Chenhao Si 司辰昊 — Scientific Machine Learning',
    description:
      'PhD candidate at CUHK-Shenzhen working on scientific machine learning, AI for Science, and physics-informed neural networks.',
    images: ['/og.png'],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
