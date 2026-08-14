// Built-in Plex-compatible path-template presets — keep in sync with
// app/services/organize_template.py (PRESET_TV / PRESET_MOVIE).
export const ORGANIZE_PRESET_TV =
  '{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}[ - {episode_title}]{ext}';
export const ORGANIZE_PRESET_MOVIE = '{category}/{title} ({year})/{title} ({year}){ext}';
