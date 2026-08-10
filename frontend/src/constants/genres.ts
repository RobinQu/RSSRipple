// Canonical work genre names — kept in sync BY HAND with the backend
// registry in ``app/services/genre_registry.py``. The database stores only
// these English canonical names (TMDB closed set); display names come from
// the ``genre`` i18n namespace keyed by ``genreSlug(name)``.
export const GENRE_NAMES: string[] = [
  'Action',
  'Adventure',
  'Animation',
  'Comedy',
  'Crime',
  'Documentary',
  'Drama',
  'Family',
  'Fantasy',
  'History',
  'Horror',
  'Music',
  'Mystery',
  'Romance',
  'Science Fiction',
  'TV Movie',
  'Thriller',
  'War',
  'Western',
  'Action & Adventure',
  'Kids',
  'News',
  'Reality',
  'Sci-Fi & Fantasy',
  'Soap',
  'Talk',
  'War & Politics',
];

// Slug used as the i18n key: lowercase, ``&``/spaces to ``-``, other
// symbols dropped ("Sci-Fi & Fantasy" -> "sci-fi-fantasy", "TV Movie" ->
// "tv-movie"). Keeps i18next keys free of special characters.
export function genreSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/&/g, ' ')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}
