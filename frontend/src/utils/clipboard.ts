// Clipboard helper. navigator.clipboard.writeText requires a secure context;
// on plain HTTP we fall back to a hidden textarea + execCommand('copy').
// Returns true only when the copy actually happened — callers must not show a
// success message for a false result.

function copyViaClipboardApi(text: string): Promise<boolean> {
  // Do not attempt the API on insecure contexts: waiting for its rejection
  // can consume the transient user activation the legacy copy command needs.
  if (
    typeof window !== 'undefined' &&
    window.isSecureContext &&
    typeof navigator !== 'undefined' &&
    navigator.clipboard?.writeText
  ) {
    return navigator.clipboard.writeText(text).then(
      () => true,
      () => false,
    );
  }
  return Promise.resolve(false);
}

function copyViaExecCommand(text: string): boolean {
  if (typeof document === 'undefined') return false;
  const textarea = document.createElement('textarea');
  const previouslyFocused = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  let copyEventHandled = false;
  const handleCopy = (event: ClipboardEvent) => {
    if (!event.clipboardData) return;
    event.clipboardData.setData('text/plain', text);
    event.preventDefault();
    copyEventHandled = true;
  };

  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.addEventListener('copy', handleCopy);
  textarea.focus();
  textarea.select();

  let commandSucceeded: boolean;
  try {
    commandSucceeded = document.execCommand('copy');
  } catch {
    commandSucceeded = false;
  } finally {
    textarea.removeEventListener('copy', handleCopy);
    textarea.remove();
    previouslyFocused?.focus();
  }

  // execCommand may return true without changing the clipboard. Requiring
  // the copy event confirms that this page actually supplied the payload.
  return commandSucceeded && copyEventHandled;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (await copyViaClipboardApi(text)) return true;
  return copyViaExecCommand(text);
}
