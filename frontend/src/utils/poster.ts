import type { SyntheticEvent } from 'react';

export const DEFAULT_POSTER_URL = '/default-poster.svg';
export const DEFAULT_POSTER_LIGHT_URL = '/default-poster-light.svg';
export const DEFAULT_POSTER_DARK_URL = '/default-poster-dark.svg';

export function defaultPosterUrl(): string {
  if (typeof document !== 'undefined') {
    const theme = document.documentElement.dataset.theme;
    if (theme === 'dark') return DEFAULT_POSTER_DARK_URL;
    if (theme === 'light') return DEFAULT_POSTER_LIGHT_URL;
  }
  if (
    typeof window !== 'undefined'
    && window.matchMedia('(prefers-color-scheme: dark)').matches
  ) {
    return DEFAULT_POSTER_DARK_URL;
  }
  return DEFAULT_POSTER_LIGHT_URL;
}

export function posterUrl(url: string | null | undefined): string {
  return url || defaultPosterUrl();
}

export function useDefaultPoster(event: SyntheticEvent<HTMLImageElement>) {
  const img = event.currentTarget;
  const fallback = defaultPosterUrl();
  if (img.getAttribute('src') === fallback) return;
  img.src = fallback;
}
