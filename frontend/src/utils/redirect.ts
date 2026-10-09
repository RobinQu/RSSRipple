// Post-login redirect target. Only same-origin absolute paths are accepted
// (must start with '/' but not '//') to prevent open redirects.
export function resolveLoginRedirect(search: string): string {
  const redirect = new URLSearchParams(search).get('redirect');
  if (redirect && redirect.startsWith('/') && !redirect.startsWith('//')) {
    return redirect;
  }
  return '/';
}
