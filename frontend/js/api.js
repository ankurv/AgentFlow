// ── SSE connection ───────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource('/events');
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    handleEvent(ev);
  };
  // EventSource reconnects itself. Creating another instance here multiplies
  // streams and replays history once per connection.
  es.onerror = () => {};
}

function handleEvent(ev) {
  eventCount++;
  document.getElementById('eventCount').textContent = eventCount;

  // Update status
  if (ev.kind === 'phase') {
    if (ev.data.status === 'waiting_for_continuation' || ev.data.status === 'waiting_for_approval') {
      updateStatus('paused');
    } else {
      updateStatus('running');
    }
  }
  if (ev.kind === 'done')  { updateStatus('done'); loadRunHistory(); }
  if (ev.kind === 'error') updateStatus(ev.data.recoverable ? 'needs_attention' : 'error');

  // Render feed item
  appendFeed(ev);

  // Update agent sidebar on turn events
  if (ev.kind === 'turn_start' || ev.kind === 'turn_end' || ev.kind === 'retry' || ev.kind === 'error') {
    fetchAgentStatus();
  }
  if (ev.kind === 'file_write' && ev.data.file === 'PLAN.md') {
    refreshPlanProgress();
  }
}

function appendFeed(ev) {
  const feed = document.getElementById('feed');
  const div = document.createElement('div');
  div.className = `feed-item ${ev.kind}`;

  const agentColor = agentColors[ev.agent] || '#64748b';
  const ts = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';

  let summary = '';
  let detail = '';
  let metricsHtml = '';

  switch(ev.kind) {
    case 'phase':
      summary = `Phase: ${ev.data.phase?.toUpperCase() || ''} ${ev.data.status ? '— ' + ev.data.status : ''} ${ev.data.iteration ? 'iter ' + ev.data.iteration : ''} ${ev.data.round ? 'round ' + ev.data.round : ''}`;
      if (ev.data.roles) summary += ' | ' + Object.entries(ev.data.roles).map(([r,a])=>`${a}=${r}`).join(' ');
      break;
    case 'turn_start': {
      const verb = getAgentVerb(ev.agent);
      summary = `${verb}... (${ev.data.turn_id || 'turn'} · attempt ${ev.data.attempt || 1})`;
      break;
    }
    case 'turn_end': {
      const u = ev.data.usage || {};
      const verb = getAgentVerb(ev.agent);
      summary = `${verb} completed (${ev.data.turn_id || 'turn'} · attempt ${ev.data.attempt || 1}) · ${(u.input_tokens||0).toLocaleString()} in, ${(u.output_tokens||0).toLocaleString()} out`;
      detail = ev.data.response || '';
      const totalTok = (u.input_tokens || 0) + (u.output_tokens || 0);
      const turnCost = ev.data.pricing_known ? formatCost(ev.data.cost_usd || 0) : 'unpriced';
      metricsHtml = `<span class="feed-metrics-badge">${totalTok.toLocaleString()} tok · ${turnCost}</span>`;
      break;
    }
    case 'vote':
      summary = `Vote: ${ev.data.vote} (round ${ev.data.round})`;
      break;
    case 'verdict':
      summary = `${ev.data.role?.toUpperCase()} verdict: ${ev.data.verdict}`;
      break;
    case 'consensus':
      summary = ev.data.forced ? '⚠ Forced consensus (max rounds)' : `✓ Consensus reached in round ${ev.data.round}`;
      break;
    case 'file_write':
      summary = `Wrote ${ev.data.file}`;
      detail = ev.data.preview || '';
      break;
    case 'steer':
      summary = `Steering injected: "${ev.data.message}"`;
      break;
    case 'retry':
      summary = `${ev.data.turn_id || 'Turn'} waiting · attempt ${ev.data.attempt} · retry in ${formatDuration(ev.data.retry_in_seconds)}`;
      detail = ev.data.reason || '';
      break;
    case 'done':
      summary = '✅ Run complete';
      updateStatus('done');
      break;
    case 'error':
      summary = ev.data.recoverable
        ? `${ev.data.turn_id} failed on attempt ${ev.data.attempt} · fix ${ev.agent} and retry this turn`
        : `Error: ${ev.data.error}`;
      detail = ev.data.error || ev.data.message || '';
      break;
    default:
      summary = JSON.stringify(ev.data).slice(0, 100);
  }

  const avatarChar = (ev.agent || 'SYS').slice(0, 1).toUpperCase();

  div.innerHTML = `
    <div class="feed-row">
      <div class="feed-avatar" style="background:${agentColor}; text-shadow: 0 1px 4px rgba(0,0,0,0.3)">
        ${avatarChar}
      </div>
      <div class="feed-meta">
        <div class="feed-header-line">
          <div class="feed-agent-details">
            <span class="feed-agent">${ev.agent || 'System'}</span>
            <span class="feed-kind">${ev.kind}</span>
            ${metricsHtml}
          </div>
          <span class="feed-ts">${ts}</span>
        </div>
        <div class="feed-text" style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
          <span style="flex: 1">${escHtml(summary)}</span>
          ${detail ? `<button class="btn btn-secondary" style="padding: 2px 8px; font-size: 12px; font-weight: bold; line-height: 1; border-radius: 4px;" onclick="const d = this.parentElement.nextElementSibling; if(d.style.display === 'none'){d.style.display='block';this.innerText='-';}else{d.style.display='none';this.innerText='+';}">${ev.data.verdict === 'PAUSE_FOR_INPUT' ? '-' : '+'}</button>` : ''}
        </div>
        ${detail ? `<div class="feed-detail" style="display: ${ev.data.verdict === 'PAUSE_FOR_INPUT' ? 'block' : 'none'}; margin-top: 8px;">${escHtml(detail)}</div>` : ''}
      </div>
    </div>
  `;

  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function getAgentVerb(agent) {
  const map = {
    'architect': 'Designing',
    'developer': 'Coding',
    'reviewer': 'Reviewing',
    'tester': 'Testing',
    'coordinator': 'Coordinating'
  };
  return map[(agent||'').toLowerCase()] || 'Thinking';
}

function escAttr(s) {
  return escHtml(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Run controls ─────────────────────────────────────────────────────────────
// ── Run controls ─────────────────────────────────────────────────────────────
async function startRun(prompt) {
  let idea = prompt;
  if (idea === undefined) {
    idea = document.getElementById('steerInput').value.trim();
    document.getElementById('steerInput').value = '';
  }

  if (!idea) {
    notify('Please type a prompt/task in the bottom chat input to start the run.', true);
    return;
  }

  if (!projectOpen) {
    const opened = await openProject();
    if (!opened) return;
  }

  const agents = await fetch('/agents').then(r=>r.json());
  const mergedAgents = agents.merged || [];
  if (!mergedAgents.length) { notify('Add at least one agent in the Agents tab', true); return; }

  // Assign colors
  mergedAgents.forEach(a => {
    if (!agentColors[a.name]) agentColors[a.name] = COLORS[colorIdx++ % COLORS.length];
  });

  // Extract execution mode from prompt instructions
  let mode = "all";
  const ideaLower = idea.toLowerCase();
  if (ideaLower.includes("debate only") || ideaLower.includes("design only") || (ideaLower.includes("debate") && !ideaLower.includes("build") && !ideaLower.includes("implement"))) {
    mode = "debate";
  } else if (ideaLower.includes("build only") || ideaLower.includes("implement only") || (ideaLower.includes("build") && !ideaLower.includes("debate") && !ideaLower.includes("design"))) {
    mode = "build";
  }

  totalTokens = 0;
  totalCost = 0;
  eventCount = 0;
  document.getElementById('totalTokens').textContent = '0';
  document.getElementById('totalCachedTokens').textContent = '0';
  document.getElementById('totalCost').textContent = '$0.000000';
  document.getElementById('progressTaskList').innerHTML = '<div style="color:var(--muted);font-size:12px">Planning task list...</div>';
  document.getElementById('eventCount').textContent = '0';
  document.getElementById('feed').innerHTML = '';

  const res = await fetch('/run/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      idea,
      project_path: currentProjectPath,
      max_debate_rounds: 20,
      max_build_iterations: 10,
      mode: mode,
    })
  });
  const data = await res.json();
  if (res.ok && data.ok) {
    document.getElementById('runId').textContent = data.run_id;
    updateStatus('running');
  } else {
    notify(data.detail || 'Failed to start', true);
  }
}

function formatDuration(seconds) {
  seconds = Number(seconds || 0);
  if (seconds >= 3600) return `${Math.ceil(seconds/3600)}h`;
  if (seconds >= 60) return `${Math.ceil(seconds/60)}m`;
  return `${Math.ceil(seconds)}s`;
}

async function pauseResume() {
  if (paused) {
    await fetch('/run/resume', {method:'POST'});
    paused = false;
    document.getElementById('pauseBtn').textContent = 'Pause';
    updateStatus('running');
  } else {
    await fetch('/run/pause', {method:'POST'});
    paused = true;
    document.getElementById('pauseBtn').textContent = 'Resume';
    updateStatus('paused');
  }
}

async function retryFailedTurn() {
  const response = await fetch('/run/retry', {method:'POST'});
  const data = await response.json();
  if (!response.ok) { notify(data.detail || 'Could not retry the failed turn', true); return; }
  paused = false;
  updateStatus('running');
  notify('Retrying the same failed turn');
}

async function resetRun() {
  if (runStatus === 'running') return;
  if (!confirm("Are you sure you want to reset? This will clear the conversational history (but keep your project files intact).")) return;
  try {
    await fetch('/run/reset', { method: 'POST' });
    document.getElementById('feed').innerHTML = '';
    notify('Run state reset.');
  } catch (err) {
    console.error(err);
    alert('Failed to reset run.');
  }
}

async function stopRun() {
  await fetch('/run/stop', {method:'POST'});
  paused = false;
  updateStatus('idle');
}

async function steer() {
  const msg = document.getElementById('steerInput').value.trim();
  if (!msg) return;

  if (appStatus === 'idle' || appStatus === 'done' || appStatus === 'error') {
    document.getElementById('steerInput').value = '';
    await startRun(msg);
  } else {
    await fetch('/run/steer', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: msg})
    });
    document.getElementById('steerInput').value = '';
    if (appStatus === 'paused') {
      await fetch('/run/resume', {method:'POST'});
      paused = false;
      updateStatus('running');
    }
  }
}

function updateStatus(s) {
  appStatus = s;
  paused = (s === 'paused');
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  const pauseBtn = document.getElementById('pauseBtn');
  const stopBtn = document.getElementById('stopBtn');
  const retryBtn = document.getElementById('retryBtn');
  dot.className = `status-dot ${s}`;
  txt.textContent = s;

  // Sync to progress pane status banner
  const pDot = document.getElementById('progressStatusDot');
  const pTxt = document.getElementById('progressStatusText');
  if (pDot && pTxt) {
    const color = {
      idle: 'var(--muted, #8e9cae)',
      running: 'var(--yellow, #eab308)',
      paused: 'var(--yellow, #eab308)',
      needs_attention: 'var(--red, #ef4444)',
      done: 'var(--green, #22c55e)',
      error: 'var(--red, #ef4444)'
    }[s] || 'var(--muted, #8e9cae)';
    pDot.style.background = color;
    pTxt.textContent = s.charAt(0).toUpperCase() + s.slice(1).replace('_', ' ');
  }

  const steerInput = document.getElementById('steerInput');
  if (steerInput) {
    if (s === 'idle' || s === 'done' || s === 'error') {
      steerInput.placeholder = 'Type a prompt/task here and press Enter to start the run…';
    } else {
      steerInput.placeholder = 'Steer agents — inject a message into the active run…';
    }
  }

  const chips = document.getElementById('debateActionChips');
  if (chips) {
    chips.style.display = (s === 'idle' || s === 'done' || s === 'error') ? 'flex' : 'none';
  }

  const running = s === 'running' || s === 'paused' || s === 'needs_attention';
  retryBtn.style.display = s === 'needs_attention' ? '' : 'none';
  if (pauseBtn) {
    pauseBtn.style.display = (s === 'running' || s === 'paused') ? '' : 'none';
    pauseBtn.textContent = s === 'paused' ? 'Resume' : 'Pause';
  }
  if (stopBtn) {
    stopBtn.style.display = running ? '' : 'none';
  }
  const resetBtn = document.getElementById('resetBtn');
  if (resetBtn) {
    resetBtn.style.display = (s === 'idle' || s === 'done' || s === 'error') ? '' : 'none';
  }
}

async function runChipPrompt(prompt) {
  document.getElementById('steerInput').value = prompt;
  await steer();
}

async function fetchAgentStatus() {
  const res = await fetch('/run/status').then(r=>r.json());
  if (res.status === 'needs_attention' && appStatus !== 'needs_attention') updateStatus('needs_attention');
  let visibleAgents = res.agents || [];
  if (!visibleAgents.length) {
    const configured = await fetch('/agents').then(r=>r.json());
    visibleAgents = (configured.merged || []).map(a => ({...a, status:'idle', total_tokens:0,
      input_tokens:0, cached_input_tokens:0, output_tokens:0, cost_usd:0, pricing_known:false}));
  }
  const list = document.getElementById('agentList');
  const maxTokens = Math.max(1, ...visibleAgents.map(a => a.total_tokens || 0));
  list.innerHTML = visibleAgents.map(a => {
    const color = agentColors[a.name] || '#64748b';
    const statusColor = {thinking:'var(--yellow)',waiting:'var(--yellow)',done:'var(--green)',error:'var(--red)',idle:'var(--muted)'}[a.status] || 'var(--muted)';
    const cost = a.pricing_known ? formatCost(a.cost_usd || 0) : 'cost n/a';
    const cacheUsage = a.cache_reporting === 'unavailable'
      ? `${a.context_reused ? 'session resumed' : 'session new'} · cache usage unreported`
      : `cached ${(a.cached_input_tokens||0).toLocaleString()}`;
    const pct = Math.max(0, Math.min(100, ((a.total_tokens||0) / maxTokens) * 100));
    const retry = a.status === 'waiting' && a.retry_at
      ? `<div class="usage-detail" style="color:var(--yellow)">retry scheduled ${new Date(a.retry_at).toLocaleTimeString()}</div>`
      : a.status === 'error'
        ? `<div class="usage-detail" style="color:var(--red)">${escHtml(a.error_message || 'Turn failed')}</div>`
        : '';
    return `<div class="agent-chip" style="display:block">
      <div style="display:flex;align-items:center;gap:8px">
      <div class="agent-dot" style="background:${color}"></div>
      <div style="flex:1;min-width:0">
        <div class="aname">${escHtml(a.name)}</div>
        <div class="akind">${escHtml(a.role || 'Generalist')} · ${escHtml(a.kind)} ${a.model ? '· '+escHtml(a.model) : ''}</div>
      </div>
      <div style="text-align:right">
        <div class="astatus" style="color:${statusColor}">${a.status}</div>
        <div class="token-count">${(a.total_tokens||0).toLocaleString()} tok · ${cost}</div>
      </div>
      </div>
      <div class="usage-detail">in ${(a.input_tokens||0).toLocaleString()} · ${cacheUsage} · out ${(a.output_tokens||0).toLocaleString()}</div>
      ${retry}
      <div class="usage-track"><div class="usage-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`;
  }).join('');

  totalTokens = (res.agents || []).reduce((sum, a) => sum + (a.total_tokens || 0), 0);
  const cached = (res.agents || []).reduce((sum, a) => sum + (a.cached_input_tokens || 0), 0);
  const hasUnreportedCache = (res.agents || []).some(a => a.cache_reporting === 'unavailable');
  totalCost = (res.agents || []).reduce((sum, a) => sum + (a.cost_usd || 0), 0);
  const allKnown = (res.agents || []).every(a => a.pricing_known);
  const costText = allKnown ? formatCost(totalCost) : `${formatCost(totalCost)} + unpriced`;
  document.getElementById('totalTokens').textContent = totalTokens.toLocaleString();
  document.getElementById('totalCachedTokens').textContent = cached.toLocaleString() + (hasUnreportedCache ? ' + unreported' : '');
  document.getElementById('totalCost').textContent = costText;
}

function formatCost(value) {
  return '$' + Number(value || 0).toFixed(value >= 0.01 ? 4 : 6);
}

function clearFeed() {
  document.getElementById('feed').innerHTML = '';
  totalTokens = 0; eventCount = 0;
  document.getElementById('totalTokens').textContent = '0';
  document.getElementById('totalCachedTokens').textContent = '0';
  document.getElementById('totalCost').textContent = '$0.000000';
  document.getElementById('progressTaskList').innerHTML = '<div style="color:var(--muted);font-size:12px">No tasks defined in PLAN.md yet.</div>';
  document.getElementById('eventCount').textContent = '0';
}

async function refreshPlanProgress() {
  try {
    const res = await fetch('/workspace/file/plan').then(r=>r.json());
    if (res && res.content) {
      renderPlanProgress(res.content);
    }
  } catch (err) {
    console.error("Failed to load plan progress", err);
  }
}

function renderPlanProgress(markdown) {
  const container = document.getElementById('progressTaskList');
  if (!container) return;
  const lines = markdown.split('\n');
  let html = '';
  let totalTasks = 0;
  let completedTasks = 0;

  lines.forEach(line => {
    const match = line.match(/^(\s*)-\s*\[([ xX/])\]\s*(.*)$/);
    if (match) {
      totalTasks++;
      const indent = match[1].length;
      const statusChar = match[2].toLowerCase();
      const checked = statusChar === 'x';
      const inProgress = statusChar === '/';
      
      if (checked) completedTasks++;
      
      const text = match[3].trim();
      const marginLeft = indent * 12;
      
      let checkIcon = '<span class="task-bullet todo"></span>';
      let style = '';
      if (checked) {
        checkIcon = '<span class="task-bullet done">✓</span>';
        style = 'text-decoration: line-through; opacity: 0.55;';
      } else if (inProgress) {
        checkIcon = '<span class="task-bullet running">⋯</span>';
        style = 'color: var(--accent2); font-weight: 500;';
      }
      
      html += `
        <div class="task-item" style="display:flex;align-items:center;gap:8px;margin-left:${marginLeft}px;${style}">
          ${checkIcon}
          <span class="task-text">${escHtml(text)}</span>
        </div>
      `;
    }
  });

  const percent = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
  
  let headerHtml = '';
  if (totalTasks > 0) {
    headerHtml = `
      <div class="progress-bar-container">
        <div class="progress-bar-label">
          <span>Task Progress</span>
          <span>${completedTasks}/${totalTasks} (${percent}%)</span>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" style="width: ${percent}%"></div>
        </div>
      </div>
    `;
  }

  if (!html) {
    container.innerHTML = '<div style="color:var(--muted);font-size:12px">No tasks defined in PLAN.md yet.</div>';
  } else {
    container.innerHTML = headerHtml + `<div class="task-items-list" style="display:flex;flex-direction:column;gap:6px;margin-top:10px">${html}</div>`;
  }
}

