import { API, app, showToast } from '../app.js';

export async function renderPrompts() {
  const prompts = await API.get('/prompts');

  const listHtml = prompts.map(p => {
    const cats = Object.entries(p.categories || {}).map(([k, v]) => `${k}: ${v}`).join(', ');
    return `
      <div class="run-card" data-name="${p.name}" style="cursor:default">
        <div class="run-card-left">
          <div>
            <div style="font-weight:600;font-size:14px">${p.name}</div>
            <div class="text-muted">${p.rows} prompts · ${(p.size_bytes / 1024).toFixed(1)} KB · ${cats || 'no categories'}</div>
          </div>
        </div>
        <div class="run-card-right" style="gap:8px">
          <button class="btn preview-btn" data-name="${p.name}">Preview</button>
          <button class="btn btn-danger delete-btn" data-name="${p.name}">Delete</button>
        </div>
      </div>`;
  }).join('');

  app.innerHTML = `
    <h1 style="font-size:24px;font-weight:700;margin-bottom:20px">Prompt Files</h1>

    <div class="card">
      <div class="drop-zone" id="drop-zone">
        <div style="font-size:16px;font-weight:600;margin-bottom:4px">Drop CSV here or click to upload</div>
        <div class="text-muted">Format: prompt, expected, category</div>
        <input type="file" id="file-input" accept=".csv" style="display:none">
      </div>
    </div>

    <div id="prompt-list">${listHtml || '<div class="empty-state"><h3>No prompt files</h3><p class="text-muted">Upload a CSV to get started.</p></div>'}</div>

    <div class="card" id="preview-card" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h2 id="preview-title" style="margin:0">Preview</h2>
        <button class="btn" id="close-preview">Close</button>
      </div>
      <div style="overflow-x:auto">
        <table id="preview-table"><thead></thead><tbody></tbody></table>
      </div>
    </div>`;

  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');

  dropZone.addEventListener('click', () => fileInput.click());
  dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', async (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) await uploadFile(file);
  });
  fileInput.addEventListener('change', async () => {
    if (fileInput.files[0]) await uploadFile(fileInput.files[0]);
  });

  async function uploadFile(file) {
    try {
      await API.upload('/prompts', file);
      showToast(`Uploaded: ${file.name}`);
      renderPrompts();
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  document.querySelectorAll('.delete-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const name = btn.dataset.name;
      if (!confirm(`Delete ${name}?`)) return;
      try {
        await API.del(`/prompts/${name}`);
        showToast(`Deleted: ${name}`);
        renderPrompts();
      } catch (e) {
        showToast(`Error: ${e.message}`);
      }
    });
  });

  document.querySelectorAll('.preview-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const name = btn.dataset.name;
      try {
        const data = await API.get(`/prompts/${name}/preview`);
        document.getElementById('preview-title').textContent = `Preview: ${name}`;
        const thead = document.querySelector('#preview-table thead');
        const tbody = document.querySelector('#preview-table tbody');
        thead.innerHTML = '<tr>' + (data.headers || []).map(h => `<th>${h}</th>`).join('') + '</tr>';
        tbody.innerHTML = data.rows.map(row =>
          '<tr>' + (data.headers || []).map(h => {
            const val = row[h] || '';
            const truncated = val.length > 80 ? val.slice(0, 80) + '...' : val;
            return `<td title="${val.replace(/"/g, '&quot;')}">${truncated}</td>`;
          }).join('') + '</tr>'
        ).join('');
        document.getElementById('preview-card').style.display = '';
      } catch (e) {
        showToast(`Error: ${e.message}`);
      }
    });
  });

  document.getElementById('close-preview').addEventListener('click', () => {
    document.getElementById('preview-card').style.display = 'none';
  });
}
