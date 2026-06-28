import type { SyntheticEvent } from 'react';

export const DEFAULT_POSTER_URL = '/default-poster.svg';

export function posterUrl(url: string | null | undefined): string {
  return url || DEFAULT_POSTER_URL;
}

export function useDefaultPoster(event: SyntheticEvent<HTMLImageElement>) {
  const img = event.currentTarget;
  if (img.getAttribute('src') === DEFAULT_POSTER_URL) return;
  img.src = DEFAULT_POSTER_URL;
}
