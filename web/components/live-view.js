import { app } from '../app.js';
export async function renderLiveView() {
  app.innerHTML = '<div class="empty-state"><h3>Live View</h3><p>Coming soon</p></div>';
}
