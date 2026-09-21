import { app } from '../app.js';
export async function renderTestForm() {
  app.innerHTML = '<div class="empty-state"><h3>New Test</h3><p>Coming soon</p></div>';
}
