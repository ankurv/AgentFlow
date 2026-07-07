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

let monacoEditorInstance = null;

function getMonacoLanguage(filename) {
  const ext = filename.split('.').pop().toLowerCase();
  if (['py'].includes(ext)) return 'python';
  if (['js', 'ts'].includes(ext)) return 'javascript';
  if (['html'].includes(ext)) return 'html';
  if (['css'].includes(ext)) return 'css';
  if (['json'].includes(ext)) return 'json';
  if (['md'].includes(ext)) return 'markdown';
  return 'plaintext';
}

function renderFileContent(filename, content) {
  currentFileContent = content;
  const container = document.getElementById('fileViewContainer');
  if (!container) return;

  if (content === undefined || content === null) {
    container.innerHTML = '<div style="color:var(--muted);font-size:12.5px;font-style:italic">Select a file to view its contents.</div>';
    return;
  }

  container.innerHTML = `
    <div class="ws-content-header">
      <span class="ws-content-path">${escHtml(filename)}</span>
      <div style="display:flex; gap:8px;">
        <button class="btn btn-primary" id="wsEditBtn" onclick="startFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Edit</button>
        <button class="btn btn-secondary" onclick="navigator.clipboard.writeText(monacoEditorInstance ? monacoEditorInstance.getValue() : currentFileContent);notify('Copied file contents to clipboard!')" style="padding: 4px 10px; font-size: 11px">Copy</button>
      </div>
    </div>
    <div id="monacoContainer" style="width:100%; height:calc(100% - 40px);"></div>
  `;

  if (window.monacoReady && window.monaco) {
    monacoEditorInstance = monaco.editor.create(document.getElementById('monacoContainer'), {
      value: content,
      language: getMonacoLanguage(filename),
      theme: 'vs-dark',
      readOnly: true,
      minimap: { enabled: false },
      automaticLayout: true,
      scrollBeyondLastLine: false,
      fontSize: 13
    });
  } else {
    document.getElementById('monacoContainer').innerHTML = '<div style="padding:20px; color:var(--muted)">Loading editor...</div>';
    window.onMonacoReady = () => {
      renderFileContent(filename, currentFileContent);
    };
  }
}

function startFileEdit(filename) {
  const header = document.querySelector('.ws-content-header');
  if (header) {
    header.innerHTML = `
      <span class="ws-content-path">Editing: ${escHtml(filename)}</span>
      <div style="display:flex; gap:8px">
        <button class="btn btn-primary" onclick="saveFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Save</button>
        <button class="btn btn-secondary" onclick="cancelFileEdit('${escHtml(filename)}')" style="padding: 4px 10px; font-size: 11px">Cancel</button>
      </div>
    `;
  }
  if (monacoEditorInstance) {
    monacoEditorInstance.updateOptions({ readOnly: false });
  }
}

function cancelFileEdit(filename) {
  renderFileContent(filename, currentFileContent);
}

async function saveFileEdit(filename) {
  const newContent = monacoEditorInstance ? monacoEditorInstance.getValue() : currentFileContent;
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

