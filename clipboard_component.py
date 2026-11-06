"""Small inline Streamlit v2 clipboard control; no network or server callback."""
import streamlit as st


CLIPBOARD_JS = """
export default function(component) {
  const {data, parentElement} = component;
  const button = parentElement.querySelector('button');
  const status = parentElement.querySelector('[role="status"]');
  button.textContent = data.label;
  button.disabled = !data.text && !data.editor_label;
  status.textContent = '';
  button.onclick = async () => {
    // Read the currently visible native editor, including a last keystroke before its Python blur rerun.
    // Labels include language, so this cannot select another language/animal editor.
    const editor = data.editor_label ? Array.from(document.querySelectorAll('textarea')).find(
      element => element.getAttribute('aria-label') === data.editor_label) : null;
    const text = editor ? editor.value : data.text;
    if (!text) { status.textContent = 'Nothing to copy.'; return; }
    try {
      await navigator.clipboard.writeText(text);
      status.textContent = 'Copied';
    } catch (error) {
      status.textContent = 'Clipboard access was blocked. Select the text and press Ctrl+C.';
    }
  };
  return () => { button.onclick = null; };
}
"""

_CLIPBOARD = st.components.v2.component('rescue_copy_text',
    html='<button type="button"></button><span role="status" aria-live="polite"></span>',
    css='''
button {font:inherit; color:var(--st-text-color); background:var(--st-secondary-background-color);
border:1px solid var(--st-border-color); border-radius:0.5rem; padding:0.4rem 0.75rem; cursor:pointer;}
button:disabled {opacity:0.5; cursor:default;} span {margin-left:0.6rem; font:inherit;}
''', js=CLIPBOARD_JS)


def copy_button(label, text, key, editor_label=None):
    return _CLIPBOARD(data={'label': label, 'text': text, 'editor_label': editor_label}, key=key, height=44)
