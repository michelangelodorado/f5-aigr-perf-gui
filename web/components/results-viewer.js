import { app } from '../app.js';
export async function renderResults() {
  app.innerHTML = '<div class="empty-state"><h3>Results</h3><p>Coming soon</p></div>';
}
