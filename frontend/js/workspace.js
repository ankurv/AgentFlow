// ── Workspace ─────────────────────────────────────────────────────────────────
let currentFileContent = '';

function getFileIcon(f) {
  const ext = f.split('.').pop().toLowerCase();
  if (['py'].includes(ext)) return '<span class="file-icon" style="color:#38bdf8">🐍</span>';
  if (['js','ts'].includes(ext)) return '<span class="file-icon" style="color:#f59e0b">⚡</span>';
  if (['html'].includes(ext)) return '<span class="file-icon" style="color:#f97316">🌐</span>';
  if (['css'].includes(ext)) return '<span class="file-icon" style="color:#6366f1">🎨</span>';
  if (['json'].includes(ext)) return '<span class="file-icon" style="color:#14b8a6">📦</span>';
  if (['md'].includes(ext)) return '<span class="file-icon" style="color:#818cf8">📝</span>';
  if (['db','sqlite'].includes(ext)) return '<span class="file-icon" style="color:#94a3b8">💾</span>';
  return '<span class="file-icon" style="color:var(--muted)">📄</span>';
}

function renderFileContent(filename, content) {
  currentFileContent = content;
  const container = document.getElementById('fileViewContainer');
  if (!container) return;

  if (content === undefined || content === null) {
    container.innerHTML = '<div style="color:var(--muted);font-size:12.5px;font-style:italic">Select a file to view its contents.</div>';
    return;
  }

  const lines = content.split('\n');
  let html = `
    <div class="ws-content-header">
      <span class="ws-content-path">${escHtml(filename)}</span>
      <div style="display:flex; gap:8px;">
        <button class="btn btn-primary" onclick="startFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Edit</button>
        <button class="btn btn-secondary" onclick="navigator.clipboard.writeText(currentFileContent);notify('Copied file contents to clipboard!')" style="padding: 4px 10px; font-size: 11px">Copy</button>
      </div>
    </div>
    <div class="code-editor">
  `;

  lines.forEach((line, index) => {
    const isReview = line.includes('# REVIEW:') || line.includes('// REVIEW:') || line.includes('/* REVIEW:');
    const reviewClass = isReview ? 'code-line review-highlight' : 'code-line';
    html += `
      <div class="${reviewClass}">
        <span class="line-number">${index + 1}</span>
        <span class="line-code">${escHtml(line) || '&nbsp;'}</span>
      </div>
    `;
  });

  html += '</div>';
  container.innerHTML = html;
}

function startFileEdit(filename) {
  const container = document.getElementById('fileViewContainer');
  if (!container) return;

  container.innerHTML = `
    <div class="ws-content-header">
      <span class="ws-content-path">Editing: ${escHtml(filename)}</span>
      <div style="display:flex; gap:8px">
        <button class="btn btn-primary" onclick="saveFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Save</button>
        <button class="btn btn-secondary" onclick="cancelFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Cancel</button>
      </div>
    </div>
    <div class="code-editor" style="padding:0; overflow:hidden;">
      <textarea id="fileEditArea" style="width:100%; height:100%; min-height:400px; padding:16px; border:none; background:transparent; color:var(--text); font-family:var(--mono); font-size:13px; resize:none; outline:none;" spellcheck="false"></textarea>
    </div>
  `;
  document.getElementById('fileEditArea').value = currentFileContent;
}

function cancelFileEdit(filename) {
  renderFileContent(filename, currentFileContent);
}

async function saveFileEdit(filename) {
  const newContent = document.getElementById('fileEditArea').value;
  const isRootFile = ['design', 'plan', 'consensus', 'tests', 'questions'].includes(filename);
  
  const encodedName = filename.split('/').map(encodeURIComponent).join('/');
  const url = isRootFile ? `/workspace/file/${filename}` : `/workspace/src/${encodedName}`;
  
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: newContent })
    });
    
    if (res.ok) {
      notify('File saved successfully!');
      currentFileContent = newContent;
      renderFileContent(filename, currentFileContent);
    } else {
      const err = await res.json();
      alert('Failed to save file: ' + (err.detail || JSON.stringify(err)));
    }
  } catch (err) {
    console.error(err);
    alert('Failed to save file: ' + err.message);
  }
}

async function loadWsFile(key) {
  currentWsKey = key;
  document.querySelectorAll('.ws-file-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('wsbtn-'+key);
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.ws-file-btn').forEach(b => {
    if (b.textContent.includes(key)) b.classList.add('active');
  });

  const dashboardView = document.getElementById('dashboardView');
  const fileViewContainer = document.getElementById('fileViewContainer');

  if (key === 'dashboard') {
    if (dashboardView) dashboardView.style.display = 'flex';
    if (fileViewContainer) fileViewContainer.style.display = 'none';

    try {
      const designRes = await fetch('/workspace/file/design').then(r=>r.json());
      const planRes = await fetch('/workspace/file/plan').then(r=>r.json());
      
      const designViewer = document.getElementById('designDocViewer');
      const planViewer = document.getElementById('planDocViewer');
      
      if (designViewer) designViewer.textContent = designRes.content || 'No design details written in DESIGN.md yet.';
      if (planViewer) planViewer.textContent = planRes.content || 'No plan details written in PLAN.md yet.';

      // Parse and Render Mermaid Diagram if present in DESIGN.md
      const mermaidTarget = document.getElementById('mermaidTarget');
      const mapContainer = document.getElementById('mermaidMapContainer');
      const designContent = designRes.content || '';
      const mermaidMatch = designContent.match(/```mermaid([\s\S]*?)```/);
      if (mermaidMatch && mermaidTarget && mapContainer) {
        const rawGraph = mermaidMatch[1].trim();
        lastMermaidCode = rawGraph;
        mermaidTarget.removeAttribute('data-processed');
        mermaidTarget.textContent = rawGraph;
        mapContainer.style.display = 'flex';
        if (window.mermaid) {
          try {
            mermaid.run({ nodes: [mermaidTarget] });
          } catch (mErr) {
            console.error("Failed to render Mermaid graph", mErr);
            mermaidTarget.innerHTML = `<div style="color:var(--red);font-size:12px;font-family:var(--font)">Diagram parse error: ${escHtml(mErr.message)}</div>`;
          }
        }
      } else if (mapContainer) {
        mapContainer.style.display = 'none';
        if (mermaidTarget) mermaidTarget.innerHTML = '';
      }
    } catch (err) {
      console.error("Failed to load dashboard files", err);
    }
  } else {
    if (dashboardView) dashboardView.style.display = 'none';
    if (fileViewContainer) fileViewContainer.style.display = 'flex';

    let content = '';
    try {
      if (['design','plan','consensus','tests','questions'].includes(key)) {
        const res = await fetch(`/workspace/file/${key}`).then(r=>r.json());
        content = res.content;
      } else {
        const encoded = key.split('/').map(encodeURIComponent).join('/');
        const res = await fetch(`/workspace/src/${encoded}`).then(r=>r.json());
        content = res.content;
      }
      renderFileContent(key, content);
    } catch (err) {
      console.error("Failed to load file contents", err);
      renderFileContent(key, null);
    }
  }
}

async function refreshWorkspace() {
  const ws = await fetch('/workspace').then(r=>r.json());
  const srcList = document.getElementById('srcFileList');
  srcList.innerHTML = (ws.src_files||[]).map(f =>
    `<button class="ws-file-btn" onclick="loadWsFile(decodeURIComponent('${encodeURIComponent(f)}'))">${getFileIcon(f)} ${escHtml(f)}</button>`
  ).join('');
  await loadWsFile(currentWsKey);
  refreshPlanProgress();
}

async function loadRunHistory() {
  const data = await fetch('/runs').then(r=>r.json());
  renderHistory(data.runs || []);
}

function renderHistory(runs) {
  const grid = document.getElementById('historyGrid');
  if (!runs.length) {
    grid.innerHTML = '<div class="empty-state">No saved runs for this project yet.</div>';
    return;
  }
  grid.innerHTML = runs.map(run => `<div class="run-card">
    <div class="run-card-title">${escHtml(run.idea)}</div>
    <div class="run-card-meta">
      <div>${escHtml(run.status)} · ${new Date(run.started_at).toLocaleString()}</div>
      <div>${Number(run.total_tokens||0).toLocaleString()} tokens · ${formatCost(run.estimated_cost_usd||0)}</div>
      <div>run ${escHtml(run.run_id)}</div>
    </div>
  </div>`).join('');
}

