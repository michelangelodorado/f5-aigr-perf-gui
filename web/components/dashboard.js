import { app } from '../app.js';
export async function renderDashboard() {
  app.innerHTML = '<div class="empty-state"><h3>Dashboard</h3><p>Coming soon</p></div>';
}
