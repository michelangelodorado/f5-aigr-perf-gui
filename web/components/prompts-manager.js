import { app } from '../app.js';
export async function renderPrompts() {
  app.innerHTML = '<div class="empty-state"><h3>Prompts</h3><p>Coming soon</p></div>';
}
