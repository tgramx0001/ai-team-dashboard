    // State Store
    // ---- AUTH: attach bearer token (localStorage) to every /api call ----
    const AI_TEAM_AUTH_KEY = 'ai_team_auth_token';
    let authPromptActive = false;
    let authPromptDisabled = false;
    const _nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const isApi = url.startsWith('/api/') || url.startsWith(location.origin + '/api/');
      if (!isApi) return _nativeFetch(input, init);
      init = init || {};
      const headers = new Headers(init.headers || undefined);
      const token = localStorage.getItem(AI_TEAM_AUTH_KEY);
      if (token && !headers.has('Authorization')) headers.set('Authorization', 'Bearer ' + token);
      init.headers = headers;
      return _nativeFetch(input, init).then(res => {
        if (res.status === 401 && !authPromptActive && !authPromptDisabled) {
          authPromptActive = true;
          // non-blocking: jangan menahan response polling saat dialog terbuka
          openInputDialog({
            title: 'API Auth Token',
            message: 'Server meminta token autentikasi. Masukkan AI_TEAM_AUTH_TOKEN:',
            placeholder: 'token',
            confirmText: 'Simpan'
          }).then(t => {
            authPromptActive = false;
            if (t && t.trim()) {
              localStorage.setItem(AI_TEAM_AUTH_KEY, t.trim());
              showToast('Token tersimpan. Ulangi aksi terakhir.', '🔑');
            } else {
              authPromptDisabled = true; // jangan spam prompt di sesi halaman ini
            }
          }).catch(() => { authPromptActive = false; authPromptDisabled = true; });
        }
        return res;
      });
    };

    // ---- XSS: LLM output is untrusted; never inject raw markdown HTML ----
    function mdRender(src) {
      const html = marked.parse(src == null ? '' : String(src));
      if (window.DOMPurify) return DOMPurify.sanitize(html);
      return String(html).replace(/</g, '&lt;'); // CDN gagal → fail closed
    }

    let presets = {};
    let activePreset = 'auto';
    let customStages = [];
    let currentWorkspace = ''; // diisi dari GET /api/workspace/presets (lihat loadWorkspacePresets)
    let workspaceInfo = null;
    let currentTaskId = null;
    let pollingInterval = null;
    let activeModalStageIndex = null;
    let currentActiveTab = 'deliverable';

    // Hermes Bridge State
    let installedSkills = [];
    let selectedSkills = [];
    let hermesSessions = [];
    let currentSessionId = null;
    let availableModels = [];
    let activeModelName = 'bai';

    // --- HERMES SKILLS FUNCTIONS ---
    async function loadHermesSkills() {
      try {
        const res = await fetch('/api/hermes/skills');
        if (!res.ok) return;
        const data = await res.json();
        installedSkills = data.skills || [];
        const badge = document.getElementById('skillsCountBadge');
        if (badge) badge.innerText = `${installedSkills.length} skills`;
        renderSkillChips();
      } catch (e) {
        console.error('Failed to load skills:', e);
      }
    }

    function renderSkillChips() {
      const container = document.getElementById('skillsChipsContainer');
      if (!container) return;
      if (!installedSkills.length) {
        container.innerHTML = '<span class="text-[10px] text-[#62666d]">Belum ada skill terdeteksi</span>';
        return;
      }

      let html = '';
      installedSkills.forEach(s => {
        const isSelected = selectedSkills.includes(s.name);
        const cls = isSelected 
          ? 'bg-brand/25 border-brand text-brand-light font-medium' 
          : 'bg-white/[0.03] border-subtle text-[#8a8f98] hover:text-[#d0d6e0] hover:border-white/20';
        
        html += `
          <button type="button" onclick="toggleSkill('${s.name}')" title="${s.description} [${s.category}]" class="px-2 py-0.5 rounded text-[10px] border transition flex items-center gap-1 ${cls}">
            <span>${isSelected ? '✓' : '+'}</span>
            <span>${s.name}</span>
          </button>
        `;
      });
      container.innerHTML = html;
    }

    function toggleSkill(name) {
      if (selectedSkills.includes(name)) {
        selectedSkills = selectedSkills.filter(s => s !== name);
      } else {
        selectedSkills.push(name);
      }
      renderSkillChips();
    }

    // --- HERMES MODEL FUNCTIONS ---
    async function loadHermesModel() {
      try {
        const res = await fetch('/api/hermes/model');
        if (!res.ok) return;
        const data = await res.json();
        activeModelName = data.active_model || 'bai';
        availableModels = data.available_models || [];
        
        const navEl = document.getElementById('navModelName');
        if (navEl) navEl.innerText = activeModelName;
        const statusModelEl = document.getElementById('navStatusModel');
        if (statusModelEl) statusModelEl.innerText = `${data.provider || '9Router'} (${activeModelName})`;
        
        const modalActiveEl = document.getElementById('modalActiveModelLabel');
        if (modalActiveEl) modalActiveEl.innerText = activeModelName;
        const modalProvEl = document.getElementById('modalProviderLabel');
        if (modalProvEl) modalProvEl.innerText = `Provider: ${data.provider} (${data.base_url})`;
        const scopeEl = document.getElementById('modalModelScopeLabel');
        if (scopeEl) {
          const scopeText = { env: 'LLM_MODEL (env)', dashboard: 'override dashboard', hermes: 'config Hermes (global)', default: 'default' }[data.scope] || data.scope;
          scopeEl.innerText = `Sumber model: ${scopeText}${data.hermes_default ? ' · hermes: ' + data.hermes_default : ''}`;
        }
        
        renderModelsList(availableModels);
      } catch (e) {
        console.error('Failed to load hermes model:', e);
      }
    }

    function renderModelsList(models) {
      const container = document.getElementById('modalModelsList');
      if (!container) return;
      if (!models.length) {
        container.innerHTML = '<div class="text-xs text-[#62666d] p-2">Tidak ada model ditemukan.</div>';
        return;
      }
      let html = '';
      models.forEach(m => {
        const isActive = (m === activeModelName);
        html += `
          <div onclick="switchHermesModel('${m}')" class="p-2 rounded cursor-pointer transition flex items-center justify-between border ${isActive ? 'bg-brand/10 border-brand/50 text-white font-semibold' : 'bg-surface/40 border-subtle/50 text-[#8a8f98] hover:text-white hover:bg-white/[0.04]'}">
            <span class="truncate">${m}</span>
            ${isActive ? '<span class="text-[10px] text-brand-light font-mono">Aktif</span>' : '<span class="text-[10px] text-[#62666d]">Pilih</span>'}
          </div>
        `;
      });
      container.innerHTML = html;
    }

    function filterModelsList() {
      const q = document.getElementById('modelSearchInput').value.toLowerCase().trim();
      const filtered = availableModels.filter(m => m.toLowerCase().includes(q));
      renderModelsList(filtered);
    }

    async function switchHermesModel(modelName) {
      if (modelName === activeModelName) {
        closeModelModal();
        return;
      }
      try {
        const applyGlobally = document.getElementById('modelGlobalToggle')?.checked === true;
        const res = await fetch('/api/hermes/model', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ model: modelName, apply_globally: applyGlobally })
        });
        if (!res.ok) throw new Error('Gagal mengganti model');
        const data = await res.json().catch(() => ({}));
        activeModelName = modelName;
        document.getElementById('navModelName').innerText = activeModelName;
        document.getElementById('modalActiveModelLabel').innerText = activeModelName;
        showToast(
          applyGlobally
            ? `Model global Hermes diganti ke: ${modelName}`
            : `Model dashboard diganti ke: ${modelName} (config Hermes tidak diubah)`,
          '🤖'
        );
        renderModelsList(availableModels);
        closeModelModal();
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    function openModelModal() {
      document.getElementById('modelModal').style.display = 'flex';
      loadHermesModel();
    }

    function closeModelModal() {
      document.getElementById('modelModal').style.display = 'none';
    }

    // --- HERMES SESSIONS FUNCTIONS ---
    async function openSessionsModal() {
      document.getElementById('sessionsModal').style.display = 'flex';
      await loadHermesSessions();
    }

    function closeSessionsModal() {
      document.getElementById('sessionsModal').style.display = 'none';
    }

    async function loadHermesSessions() {
      try {
        const res = await fetch('/api/hermes/sessions');
        if (!res.ok) return;
        const data = await res.json();
        hermesSessions = data.sessions || [];
        document.getElementById('sessionsCountBadge').innerText = `${hermesSessions.length} Sessions`;
        renderSessionsList(hermesSessions);
      } catch (e) {
        console.error('Failed to load sessions:', e);
      }
    }

    function renderSessionsList(sessions) {
      const container = document.getElementById('sessionsListContainer');
      if (!container) return;
      if (!sessions.length) {
        container.innerHTML = '<div class="p-4 text-[#62666d] text-center">Belum ada sesi tercatat.</div>';
        return;
      }
      let html = '';
      sessions.forEach(s => {
        const isSelected = (s.id === currentSessionId);
        const icon = s.source === 'telegram' ? '📱' : (s.source === 'cron' ? '⏰' : '💻');
        html += `
          <div onclick="selectSessionDetail('${s.id}')" class="p-3 cursor-pointer transition ${isSelected ? 'bg-brand/10 border-l-2 border-brand text-white' : 'hover:bg-white/[0.03] text-[#8a8f98]'}">
            <div class="flex items-center justify-between mb-1">
              <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.04] border border-subtle text-[#d0d6e0] flex items-center gap-1">
                <span>${icon}</span> <span>${s.source}</span>
              </span>
              <span class="text-[10px] text-[#62666d] font-mono">${s.message_count} msgs</span>
            </div>
            <div class="text-xs font-medium text-[#d0d6e0] truncate">${s.title}</div>
            <div class="text-[10px] text-[#62666d] font-mono mt-1">${s.last_active || s.started}</div>
          </div>
        `;
      });
      container.innerHTML = html;
    }

    function filterSessionsList() {
      const q = document.getElementById('sessionSearchInput').value.toLowerCase().trim();
      const filtered = hermesSessions.filter(s => 
        (s.title && s.title.toLowerCase().includes(q)) || 
        (s.source && s.source.toLowerCase().includes(q)) ||
        (s.id && s.id.toLowerCase().includes(q))
      );
      renderSessionsList(filtered);
    }

    async function selectSessionDetail(sessionId) {
      currentSessionId = sessionId;
      const s = hermesSessions.find(item => item.id === sessionId);
      if (s) {
        document.getElementById('sessionDetailTitle').innerText = s.title;
        document.getElementById('sessionDetailMeta').innerText = `ID: ${s.id} · Platform: ${s.source} · Model: ${s.model} · Terakhir aktif: ${s.last_active}`;
      }
      renderSessionsList(hermesSessions);

      const container = document.getElementById('sessionMessagesContainer');
      container.innerHTML = '<div class="text-center p-8 text-[#8a8f98] animate-pulse">Memuat riwayat pesan...</div>';

      try {
        const res = await fetch(`/api/hermes/sessions/${sessionId}/messages`);
        if (!res.ok) throw new Error('Gagal memuat pesan');
        const data = await res.json();
        const msgs = data.messages || [];

        if (!msgs.length) {
          container.innerHTML = '<div class="text-center p-8 text-[#62666d]">Sesi ini belum memiliki rekaman pesan.</div>';
          return;
        }

        let html = '';
        msgs.forEach(m => {
          const isUser = m.role === 'user';
          const isTool = m.role === 'tool';
          const roleLabel = isUser ? 'User' : (isTool ? `Tool (${m.tool_name || 'call'})` : 'Hermes');
          const borderCls = isUser ? 'border-brand/40 bg-brand/5' : (isTool ? 'border-amber-500/20 bg-amber-500/5' : 'border-subtle bg-surface/60');
          const textCls = isUser ? 'text-white' : (isTool ? 'text-amber-200/80 text-[11px]' : 'text-[#d0d6e0]');

          html += `
            <div class="p-3 rounded-lg border ${borderCls} space-y-1">
              <div class="flex items-center justify-between text-[10px] text-[#8a8f98]">
                <span class="font-semibold uppercase tracking-wider text-brand-light">${roleLabel}</span>
                <span>${m.timestamp || ''}</span>
              </div>
              <div class="whitespace-pre-wrap leading-relaxed ${textCls}">${escapeHtml(m.content || '')}</div>
            </div>
          `;
        });
        container.innerHTML = html;
        container.scrollTop = container.scrollHeight;
      } catch (err) {
        container.innerHTML = `<div class="p-4 text-red-400">Error: ${err.message}</div>`;
      }
    }

    function showToast(msg, icon = '✓') {
      const toast = document.getElementById('toastNotification');
      const msgEl = document.getElementById('toastMessage');
      const iconEl = document.getElementById('toastIcon');
      msgEl.innerText = msg;
      iconEl.innerText = icon;
      toast.classList.remove('translate-y-16', 'opacity-0');
      toast.classList.add('translate-y-0', 'opacity-100');
      setTimeout(() => {
        toast.classList.remove('translate-y-0', 'opacity-100');
        toast.classList.add('translate-y-16', 'opacity-0');
      }, 3000);
    }

    // Custom Dialog Modal System (Clean replacement for window.alert / window.prompt / window.confirm)
    let appDialogResolve = null;

    function openInputDialog({ title = 'Input', message = '', defaultValue = '', placeholder = '', confirmText = 'Lanjut' }) {
      return new Promise((resolve) => {
        appDialogResolve = resolve;
        document.getElementById('appDialogTitle').innerText = title;
        const msgEl = document.getElementById('appDialogMessage');
        if (message) {
          msgEl.innerText = message;
          msgEl.classList.remove('hidden');
        } else {
          msgEl.classList.add('hidden');
        }
        const inputWrapper = document.getElementById('appDialogInputWrapper');
        inputWrapper.classList.remove('hidden');
        const input = document.getElementById('appDialogInput');
        input.value = defaultValue || '';
        input.placeholder = placeholder || '';
        const btnConfirm = document.getElementById('appDialogBtnConfirm');
        btnConfirm.innerText = confirmText;
        btnConfirm.className = "px-3.5 py-1.5 rounded-md bg-brand hover:bg-brand-hover text-xs font-medium text-white shadow-sm transition active:scale-95";

        const modal = document.getElementById('appDialogModal');
        modal.style.display = 'flex';
        setTimeout(() => {
          input.focus();
          input.select();
        }, 50);
      });
    }

    function openConfirmDialog({ title = 'Konfirmasi', message = 'Apakah Anda yakin?', confirmText = 'Lanjut', danger = false }) {
      return new Promise((resolve) => {
        appDialogResolve = resolve;
        document.getElementById('appDialogTitle').innerText = title;
        const msgEl = document.getElementById('appDialogMessage');
        msgEl.innerText = message;
        msgEl.classList.remove('hidden');
        const inputWrapper = document.getElementById('appDialogInputWrapper');
        inputWrapper.classList.add('hidden');
        const btnConfirm = document.getElementById('appDialogBtnConfirm');
        btnConfirm.innerText = confirmText;
        if (danger) {
          btnConfirm.className = "px-3.5 py-1.5 rounded-md bg-rose-600 hover:bg-rose-500 text-xs font-medium text-white shadow-sm transition active:scale-95";
        } else {
          btnConfirm.className = "px-3.5 py-1.5 rounded-md bg-brand hover:bg-brand-hover text-xs font-medium text-white shadow-sm transition active:scale-95";
        }

        const modal = document.getElementById('appDialogModal');
        modal.style.display = 'flex';
        btnConfirm.focus();
      });
    }

    function closeAppDialog(val = null) {
      const modal = document.getElementById('appDialogModal');
      modal.style.display = 'none';
      if (appDialogResolve) {
        appDialogResolve(val);
        appDialogResolve = null;
      }
    }

    function confirmAppDialog() {
      const inputWrapper = document.getElementById('appDialogInputWrapper');
      if (inputWrapper.classList.contains('hidden')) {
        closeAppDialog(true);
      } else {
        const val = document.getElementById('appDialogInput').value.trim();
        closeAppDialog(val);
      }
    }

    let isSidebarResizing = false;

    function initSidebarResizer() {
      const sidebar = document.getElementById('mainSidebar');
      const resizer = document.getElementById('sidebarResizer');
      if (!sidebar || !resizer) return;

      if (window.innerWidth >= 768) {
        const savedWidth = localStorage.getItem('ai_team_sidebar_width');
        if (savedWidth) {
          const w = Math.max(220, Math.min(650, parseInt(savedWidth, 10)));
          sidebar.style.width = `${w}px`;
        }
      }

      resizer.addEventListener('mousedown', (e) => {
        if (window.innerWidth < 768) return;
        isSidebarResizing = true;
        document.body.classList.add('select-none');
        document.body.style.cursor = 'col-resize';
      });

      window.addEventListener('mousemove', (e) => {
        if (!isSidebarResizing || window.innerWidth < 768) return;
        const newWidth = Math.max(200, Math.min(window.innerWidth - 280, e.clientX));
        sidebar.style.width = `${newWidth}px`;
      });

      window.addEventListener('mouseup', () => {
        if (isSidebarResizing) {
          isSidebarResizing = false;
          document.body.classList.remove('select-none');
          document.body.style.cursor = '';
          localStorage.setItem('ai_team_sidebar_width', parseInt(sidebar.style.width, 10));
        }
      });
    }

    function toggleSidebar(forceState = null) {
      const isMobile = window.innerWidth < 768;
      const sidebar = document.getElementById('mainSidebar');
      const backdrop = document.getElementById('sidebarBackdrop');
      const resizer = document.getElementById('sidebarResizer');
      if (!sidebar) return;

      if (isMobile) {
        const isClosed = sidebar.classList.contains('-translate-x-full');
        const shouldOpen = forceState !== null ? forceState : isClosed;
        if (shouldOpen) {
          sidebar.classList.remove('-translate-x-full');
          backdrop?.classList.remove('hidden');
        } else {
          sidebar.classList.add('-translate-x-full');
          backdrop?.classList.add('hidden');
        }
      } else {
        if (forceState === false || (forceState === null && sidebar.style.display !== 'none')) {
          sidebar.style.display = 'none';
          if (resizer) resizer.style.display = 'none';
        } else {
          sidebar.style.display = 'flex';
          if (resizer) resizer.style.display = 'block';
        }
      }
    }

    window.addEventListener('resize', () => {
      const isMobile = window.innerWidth < 768;
      const sidebar = document.getElementById('mainSidebar');
      const backdrop = document.getElementById('sidebarBackdrop');
      const resizer = document.getElementById('sidebarResizer');
      if (!sidebar) return;

      if (isMobile) {
        sidebar.style.display = '';
        sidebar.style.width = '';
        if (resizer) resizer.style.display = 'none';
        if (sidebar.classList.contains('-translate-x-full')) {
          backdrop?.classList.add('hidden');
        } else {
          backdrop?.classList.remove('hidden');
        }
      } else {
        backdrop?.classList.add('hidden');
        sidebar.classList.remove('-translate-x-full');
        const savedWidth = localStorage.getItem('ai_team_sidebar_width') || '340';
        sidebar.style.width = `${savedWidth}px`;
        if (sidebar.style.display !== 'none' && resizer) {
          resizer.style.display = 'block';
        }
      }
    });

    let isDrawerResizing = false;
    let isDrawerMaximized = false;
    let preMaximizeWidth = '780px';

    function initDrawerResizer() {
      const drawer = document.getElementById('workspaceDrawer');
      const resizer = document.getElementById('drawerResizer');
      if (!drawer || !resizer) return;

      const savedWidth = localStorage.getItem('ai_team_drawer_width');
      if (savedWidth && window.innerWidth >= 768) {
        const w = Math.max(380, Math.min(window.innerWidth - 30, parseInt(savedWidth, 10)));
        drawer.style.width = `${w}px`;
      }

      resizer.addEventListener('mousedown', (e) => {
        if (window.innerWidth < 768) return;
        isDrawerResizing = true;
        document.body.classList.add('select-none');
        document.body.style.cursor = 'col-resize';
      });

      window.addEventListener('mousemove', (e) => {
        if (!isDrawerResizing || window.innerWidth < 768) return;
        const newWidth = Math.max(380, Math.min(window.innerWidth - 20, window.innerWidth - e.clientX));
        drawer.style.width = `${newWidth}px`;
        isDrawerMaximized = false;
      });

      window.addEventListener('mouseup', () => {
        if (isDrawerResizing) {
          isDrawerResizing = false;
          document.body.classList.remove('select-none');
          document.body.style.cursor = '';
          localStorage.setItem('ai_team_drawer_width', parseInt(drawer.style.width, 10));
        }
      });
    }

    function toggleDrawerMaximize() {
      const drawer = document.getElementById('workspaceDrawer');
      const icon = document.getElementById('btnDrawerMaximize');
      if (!drawer) return;
      if (!isDrawerMaximized) {
        preMaximizeWidth = drawer.style.width || '780px';
        drawer.style.width = 'calc(100vw - 20px)';
        isDrawerMaximized = true;
        if (icon) icon.innerText = '❐';
        showToast('Panel dimaksimalkan', '⛶');
      } else {
        drawer.style.width = preMaximizeWidth;
        isDrawerMaximized = false;
        if (icon) icon.innerText = '⛶';
        showToast('Ukuran panel dipulihkan', '↩');
      }
    }

    let isInnerResizing = false;

    function initInnerDrawerResizer() {
      const pane = document.getElementById('drawerFilesPane');
      const resizer = document.getElementById('drawerInnerResizer');
      if (!pane || !resizer) return;

      const savedPaneWidth = localStorage.getItem('ai_team_drawer_files_width');
      if (savedPaneWidth && window.innerWidth >= 768) {
        const w = Math.max(160, Math.min(480, parseInt(savedPaneWidth, 10)));
        pane.style.width = `${w}px`;
      }

      resizer.addEventListener('mousedown', (e) => {
        if (window.innerWidth < 768) return;
        isInnerResizing = true;
        document.body.classList.add('select-none');
        document.body.style.cursor = 'col-resize';
      });

      window.addEventListener('mousemove', (e) => {
        if (!isInnerResizing || window.innerWidth < 768) return;
        const rect = pane.getBoundingClientRect();
        const newWidth = Math.max(160, Math.min(500, e.clientX - rect.left));
        pane.style.width = `${newWidth}px`;
      });

      window.addEventListener('mouseup', () => {
        if (isInnerResizing) {
          isInnerResizing = false;
          document.body.classList.remove('select-none');
          document.body.style.cursor = '';
          localStorage.setItem('ai_team_drawer_files_width', parseInt(pane.style.width, 10));
        }
      });
    }

    async function init() {
      initSidebarResizer();
      initDrawerResizer();
      initInnerDrawerResizer();

      document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          dispatchTask();
        }
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
          e.preventDefault();
          toggleSidebar();
        }
        if (e.altKey && e.key.toLowerCase() === 'e') {
          e.preventDefault();
          enhancePrompt();
        }
        const dlg = document.getElementById('appDialogModal');
        if (dlg && dlg.style.display !== 'none') {
          if (e.key === 'Escape') {
            e.preventDefault();
            closeAppDialog(null);
            return;
          }
          if (e.key === 'Enter') {
            e.preventDefault();
            confirmAppDialog();
            return;
          }
        }
        if (e.key === 'Escape') {
          closeDrawer('workspace');
          closeAgentModal();
          closeSessionsModal();
          closeModelModal();
          closeWorkstationSessionsModal();
        }
      });

      try {
        const res = await fetch('/api/presets');
        const data = await res.json();
        data.forEach(p => presets[p.id] = p);
        
        selectPreset('auto');
        await fetchWorkstationSessions();
        await loadHermesSkills();
        await loadHermesModel();
        await loadWorkspacePresets();
        if (currentWorkspace) await setWorkspace(currentWorkspace);
        await fetchTasks();
        await updateSystemStatus();
        setInterval(updateSystemStatus, 15000);
      } catch (err) {
        console.error('Init failed:', err);
      }
    }

    async function updateSystemStatus() {
      try {
        const res = await fetch('/api/system/status');
        if (!res.ok) return;
        const d = await res.json();
        const countEl = document.getElementById('navTaskCount');
        if (countEl) countEl.innerText = `${d.running_count} Running`;
        const hostEl = document.getElementById('navStatusHost');
        const host = d.public_host || '';
        if (hostEl) hostEl.innerText = host;
        const sep1 = document.getElementById('navStatusSepHost');
        const sep2 = document.getElementById('navStatusSepHost2');
        const vis = host ? '' : 'none';
        if (sep1) sep1.style.display = vis;
        if (hostEl) hostEl.style.display = vis;
        if (sep2) sep2.style.display = vis;
      } catch (e) {}
    }

    // Workspace quick-switcher: paths come from config (env/.env), never from source.
    const AI_TEAM_WORKSPACE_KEY = 'ai_team_workspace';
    async function loadWorkspacePresets() {
      const saved = localStorage.getItem(AI_TEAM_WORKSPACE_KEY);
      if (saved) currentWorkspace = saved;
      try {
        const res = await fetch('/api/workspace/presets');
        if (!res.ok) return;
        const d = await res.json();
        renderWorkspacePresets(d.presets || []);
        if (!currentWorkspace) currentWorkspace = d.default || ((d.presets || [])[0] || {}).path || '';
      } catch (e) {
        console.error('Failed to load workspace presets:', e);
      }
    }

    function renderWorkspacePresets(list) {
      const el = document.getElementById('wsPresetsList');
      if (!el) return;
      el.innerHTML = '';
      (list || []).forEach((p, i) => {
        const btn = document.createElement('button');
        btn.className = 'ws-btn p-2 rounded-md border border-subtle bg-white/[0.02] hover:bg-white/[0.05] text-left transition flex items-center gap-2';
        btn.onclick = () => setWorkspace(p.path);
        const dot = document.createElement('span');
        dot.className = `w-1.5 h-1.5 rounded-full shrink-0 ${i === 0 ? 'bg-brand' : 'bg-[#8a8f98]'}`;
        const label = document.createElement('span');
        label.className = 'text-xs text-[#d0d6e0] truncate';
        label.textContent = p.label || p.path;
        btn.append(dot, label);
        el.appendChild(btn);
      });
    }

    async function setWorkspace(path) {
      if (!path) return;
      if (window.innerWidth < 768) {
        toggleSidebar(false);
      }
      currentWorkspace = path.trim();
      try { localStorage.setItem(AI_TEAM_WORKSPACE_KEY, currentWorkspace); } catch (e) {}
      const folderName = currentWorkspace.split('/').pop() || currentWorkspace;
      
      document.getElementById('navWorkspaceName').innerText = folderName;
      document.getElementById('drawerPathLabel').innerText = currentWorkspace;

      try {
        const res = await fetch(`/api/workspace/info?path=${encodeURIComponent(currentWorkspace)}`);
        workspaceInfo = await res.json();

        const ruleBadge = document.getElementById('navRuleBadge');
        const ruleDot = document.getElementById('navRuleDot');
        const ruleText = document.getElementById('navRuleText');
        const sidebarStatus = document.getElementById('sidebarWsStatusLabel');
        const gitBadge = document.getElementById('navGitBadge');
        const gitBranch = document.getElementById('navGitBranch');

        if (workspaceInfo.exists) {
          sidebarStatus.innerText = `${workspaceInfo.files.length} items di folder`;
          if (workspaceInfo.agents_md) {
            ruleDot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-400';
            ruleText.innerText = `${workspaceInfo.agents_md_file} aktif`;
          } else {
            ruleDot.className = 'w-1.5 h-1.5 rounded-full bg-amber-400';
            ruleText.innerText = 'No AGENTS.md';
          }

          if (workspaceInfo.git?.is_git) {
            gitBadge.style.display = 'flex';
            gitBranch.innerText = workspaceInfo.git.branch || 'git';
          } else {
            gitBadge.style.display = 'none';
          }
        } else {
          sidebarStatus.innerText = 'Folder tidak ditemukan';
          ruleDot.className = 'w-1.5 h-1.5 rounded-full bg-rose-400';
          ruleText.innerText = 'Invalid Path';
          gitBadge.style.display = 'none';
        }
      } catch (e) {
        console.error('Error fetching workspace:', e);
      }
    }

    async function promptCustomWorkspace() {
      const p = await openInputDialog({
        title: 'Custom Workspace Directory',
        message: 'Masukkan absolute path direktori proyek di VPS:',
        defaultValue: currentWorkspace,
        placeholder: '/absolute/path/to/project',
        confirmText: 'Buka Folder'
      });
      if (p) setWorkspace(p);
    }

    let currentDrawerSubpath = '';
    let currentActiveDrawerFile = 'AGENTS.md';
    let currentDrawerParentSubpath = null;

    async function openDrawer(name) {
      if (name === 'workspace') {
        document.getElementById('workspaceDrawerBackdrop').style.display = 'block';
        const drawer = document.getElementById('workspaceDrawer');
        if (window.innerWidth >= 768) {
          const savedWidth = localStorage.getItem('ai_team_drawer_width') || '780';
          drawer.style.width = `${Math.min(window.innerWidth - 30, Math.max(400, parseInt(savedWidth, 10)))}px`;
        } else {
          drawer.style.width = '100vw';
        }
        drawer.style.transform = 'translateX(0)';

        // Load file tree
        refreshFileTree();

        // Load AGENTS.md if not yet loaded
        if (!document.getElementById('drawerAgentsTextarea').value && workspaceInfo) {
          loadDefaultAgentsContext();
        }
      }
    }

    function closeDrawer(name) {
      if (name === 'workspace') {
        document.getElementById('workspaceDrawerBackdrop').style.display = 'none';
        document.getElementById('workspaceDrawer').style.transform = 'translateX(100%)';
      }
    }

    async function loadDefaultAgentsContext() {
      if (workspaceInfo && workspaceInfo.agents_md) {
        document.getElementById('drawerAgentsTextarea').value = workspaceInfo.agents_md;
        setActiveDrawerFile(workspaceInfo.agents_md_file || 'AGENTS.md', true);
      } else {
        await previewDrawerFile('AGENTS.md', true);
      }
    }

    function setActiveDrawerFile(relPath, isAgentsMd = false) {
      currentActiveDrawerFile = relPath;
      const fnLabel = document.getElementById('drawerRuleFilename');
      const sublabel = document.getElementById('drawerEditorSublabel');
      const saveBtnLabel = document.getElementById('btnDrawerSaveLabel');
      const fileIcon = document.getElementById('drawerActiveFileIcon');

      fnLabel.innerText = relPath;
      const shortName = relPath.split('/').pop() || relPath;
      saveBtnLabel.innerText = `Simpan (${shortName})`;

      // elemen opsional: boleh tidak ada di DOM (drawer mode tertentu)
      if (isAgentsMd || relPath.toLowerCase() === 'agents.md') {
        sublabel.innerText = 'Instruksi arsitektur ini otomatis disuntikkan ke setiap agent.';
        if (fileIcon) fileIcon.innerText = '⭐';
      } else {
        sublabel.innerText = `Menyunting file di VPS (${relPath}).`;
        if (fileIcon) fileIcon.innerText = '📄';
      }
      highlightActiveDrawerFile();
    }

    function highlightActiveDrawerFile() {
      document.querySelectorAll('#drawerFilesList .drawer-file-item').forEach(el => {
        if (el.getAttribute('data-rel-path') === currentActiveDrawerFile) {
          el.classList.add('bg-white/[0.08]', 'border-brand/40');
        } else {
          el.classList.remove('bg-white/[0.08]', 'border-brand/40');
        }
      });
    }

    async function loadDrawerFiles(subpath = '') {
      currentDrawerSubpath = subpath;
      const filesEl = document.getElementById('drawerFilesList');
      const countEl = document.getElementById('drawerFolderItemCount');
      const bcrumbEl = document.getElementById('drawerBreadcrumbBar');

      filesEl.innerHTML = '<div class="text-[#8a8f98] py-2 italic text-[11px]">Memuat folder...</div>';

      try {
        const res = await fetch(`/api/workspace/files?path=${encodeURIComponent(currentWorkspace)}&subpath=${encodeURIComponent(subpath)}`);
        if (!res.ok) {
          filesEl.innerHTML = '<div class="text-rose-400 py-2 text-[11px]">Gagal memuat direktori.</div>';
          return;
        }
        const data = await res.json();
        currentDrawerParentSubpath = data.parent_subpath;
        countEl.innerText = `(${data.entries.length})`;

        // Render breadcrumbs
        const parts = subpath ? subpath.split('/').filter(Boolean) : [];
        let bcrumbHtml = `<span onclick="loadDrawerFiles('')" class="cursor-pointer hover:text-white text-brand-light font-bold">root</span>`;
        let accum = '';
        for (let i = 0; i < parts.length; i++) {
          accum += (accum ? '/' : '') + parts[i];
          const isLast = (i === parts.length - 1);
          bcrumbHtml += `<span class="text-[#62666d]">/</span><span onclick="loadDrawerFiles('${accum}')" class="cursor-pointer hover:text-white ${isLast ? 'text-white font-semibold' : 'text-[#8a8f98]'}">${parts[i]}</span>`;
        }
        if (bcrumbEl) bcrumbEl.innerHTML = bcrumbHtml;

        let html = '';

        // Up one folder row if in subfolder
        if (subpath) {
          html += `
            <div onclick="loadDrawerFiles('${currentDrawerParentSubpath || ''}')" class="p-1.5 rounded hover:bg-white/[0.06] cursor-pointer transition flex items-center gap-1.5 text-brand-light text-[11px] border border-transparent">
              <span class="text-xs font-mono">‹</span>
              <span class="font-medium">.. (Kembali)</span>
            </div>
          `;
        }

        if (!data.entries.length) {
          html += '<div class="text-[#8a8f98] py-2 italic text-[11px]">Folder kosong.</div>';
        } else {
          html += data.entries.map(f => {
            const isFolder = f.is_dir;
            const shortName = f.name;
            const fullRel = f.rel_path;
            const sizeStr = isFolder ? '' : `${Math.round(f.size/1024)}k`;
            const iconSvg = isFolder
              ? `<svg class="w-3.5 h-3.5 text-brand-light/80 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>`
              : `<svg class="w-3.5 h-3.5 text-[#8a8f98] shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>`;

            return `
              <div data-rel-path="${fullRel}" class="drawer-file-item p-1.5 rounded hover:bg-white/[0.04] transition flex items-center justify-between group border border-transparent">
                <div onclick="${isFolder ? `loadDrawerFiles('${fullRel}')` : `previewDrawerFile('${fullRel}')`}" class="flex items-center gap-1.5 truncate flex-1 cursor-pointer">
                  ${iconSvg}
                  <span class="text-[#d0d6e0] group-hover:text-brand-light truncate text-[11px]">${shortName}</span>
                  ${isFolder ? '<span class="text-[10px] text-[#62666d]">›</span>' : ''}
                </div>
                <div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition shrink-0 ml-1">
                  ${!isFolder ? `<span class="text-[10px] text-[#62666d] mr-1 font-mono">${sizeStr}</span>` : ''}
                  <button onclick="promptRenameDrawerItem('${fullRel}', '${shortName}')" title="Ganti Nama" class="px-1 py-0.5 hover:text-white text-[#8a8f98] text-[9px] font-mono rounded hover:bg-white/[0.08]">ren</button>
                  <button onclick="deleteDrawerItem('${fullRel}', ${isFolder})" title="Hapus" class="px-1 py-0.5 hover:text-rose-400 text-[#8a8f98] text-[9px] font-mono rounded hover:bg-rose-500/10">del</button>
                </div>
              </div>
            `;
          }).join('');
        }

        filesEl.innerHTML = html;
        highlightActiveDrawerFile();
      } catch (e) {
        filesEl.innerHTML = `<div class="text-rose-400 py-2 text-[11px]">${e.message}</div>`;
      }
    }

    async function previewDrawerFile(relPath, silent = false) {
      try {
        const res = await fetch(`/api/workspace/file-content?path=${encodeURIComponent(currentWorkspace)}&filename=${encodeURIComponent(relPath)}`);
        if (!res.ok) {
          if (!silent) showToast(`File tidak dapat dibuka.`, '✕');
          return;
        }
        const d = await res.json();
        document.getElementById('drawerAgentsTextarea').value = d.content;
        setActiveDrawerFile(relPath, relPath.toLowerCase() === 'agents.md');
        updateGutterLines();
        setDrawerEditorMode('code');
        const badge = document.getElementById('drawerSyntaxBadge');
        if (badge) badge.style.display = 'none';
        appendMiniTerminal(`> Opened file: ${relPath} (${d.content.split('\n').length} lines)`);
        if (!silent) showToast(`Dibuka: ${relPath.split('/').pop()}`, '✓');
      } catch (e) {
        if (!silent) showToast('Gagal memuat file', '✕');
      }
    }

    async function saveActiveDrawerFile() {
      const content = document.getElementById('drawerAgentsTextarea').value;
      const relPath = currentActiveDrawerFile || 'AGENTS.md';

      try {
        let res;
        if (relPath.toLowerCase() === 'agents.md') {
          res = await fetch('/api/workspace/save-context', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: currentWorkspace, content: content })
          });
        } else {
          res = await fetch('/api/workspace/save-file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: currentWorkspace, rel_path: relPath, content: content })
          });
        }

        if (res.ok) {
          appendMiniTerminal(`✓ Saved file: ${relPath}`);
          showToast(`Berhasil menyimpan ${relPath.split('/').pop()}!`, '✓');
          if (relPath.toLowerCase() === 'agents.md') {
            await setWorkspace(currentWorkspace);
          }
        } else {
          const err = await res.json();
          appendMiniTerminal(`✕ Error saving: ${err.detail || 'Failed'}`);
          showToast(err.detail || 'Gagal menyimpan.', '✕');
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function promptCreateDrawerItem(isDir) {
      const typeName = isDir ? 'Folder' : 'File';
      const name = await openInputDialog({
        title: `Buat ${typeName} Baru`,
        message: `Masukkan nama ${typeName.toLowerCase()} yang ingin dibuat:`,
        placeholder: isDir ? 'src, components, models' : 'app.py, main.dart, config.json',
        confirmText: `Buat ${typeName}`
      });
      if (!name || !name.trim()) return;

      const cleanName = name.trim();
      const relPath = currentDrawerSubpath ? `${currentDrawerSubpath}/${cleanName}` : cleanName;

      try {
        const res = await fetch('/api/workspace/create-item', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            path: currentWorkspace,
            rel_path: relPath,
            is_dir: isDir
          })
        });

        if (res.ok) {
          showToast(`${typeName} ${cleanName} berhasil dibuat!`, '✓');
          await refreshFileTree();
          if (!isDir) { previewDrawerFile(relPath); }
        } else {
          const err = await res.json();
          showToast(err.detail || `Gagal membuat ${typeName}`, '✕');
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function promptRenameDrawerItem(oldRelPath, oldName) {
      const newName = await openInputDialog({
        title: 'Ganti Nama',
        message: `Ganti nama "${oldName}" menjadi:`,
        defaultValue: oldName,
        confirmText: 'Simpan Nama'
      });
      if (!newName || !newName.trim() || newName.trim() === oldName) return;

      const parent = oldRelPath.includes('/') ? oldRelPath.substring(0, oldRelPath.lastIndexOf('/')) : '';
      const newRelPath = parent ? `${parent}/${newName.trim()}` : newName.trim();

      try {
        const res = await fetch('/api/workspace/rename-item', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            path: currentWorkspace,
            old_rel_path: oldRelPath,
            new_rel_path: newRelPath
          })
        });

        if (res.ok) {
          showToast(`Berhasil mengubah nama jadi ${newName.trim()}`, '✓');
          if (currentActiveDrawerFile === oldRelPath) {
            currentActiveDrawerFile = newRelPath;
            setActiveDrawerFile(newRelPath);
          }
          await refreshFileTree();
        } else {
          const err = await res.json();
          showToast(err.detail || 'Gagal mengubah nama', '✕');
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function deleteDrawerItem(relPath, isFolder) {
      const typeName = isFolder ? 'folder beserta seluruh isinya' : 'file';
      const ok = await openConfirmDialog({
        title: `Hapus ${isFolder ? 'Folder' : 'File'}`,
        message: `Yakin ingin menghapus ${typeName} "${relPath}"? Tindakan ini tidak dapat dibatalkan.`,
        confirmText: 'Hapus Permanen',
        danger: true
      });
      if (!ok) return;

      try {
        const res = await fetch('/api/workspace/delete-item', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            path: currentWorkspace,
            rel_path: relPath
          })
        });

        if (res.ok) {
          showToast(`Berhasil menghapus ${relPath.split('/').pop()}`, '✓');
          if (currentActiveDrawerFile === relPath) {
            document.getElementById('drawerAgentsTextarea').value = '';
            setActiveDrawerFile('Pilih File');
          }
          await refreshFileTree();
        } else {
          const err = await res.json();
          showToast(err.detail || 'Gagal menghapus', '✕');
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    function selectPreset(id) {
      activePreset = id;
      const tabs = {
        'auto': document.getElementById('tabPresetAuto'),
        'dev': document.getElementById('tabPresetDev'),
        'security': document.getElementById('tabPresetSecurity'),
        'kuliah': document.getElementById('tabPresetKuliah')
      };

      const activeClass = 'py-1.5 px-1 rounded-md text-white bg-surfaceElevated border border-subtle font-medium transition flex items-center justify-center gap-1 shadow-sm';
      const inactiveClass = 'py-1.5 px-1 rounded-md text-[#8a8f98] hover:text-[#d0d6e0] transition flex items-center justify-center gap-1';

      Object.keys(tabs).forEach(k => {
        if (tabs[k]) {
          tabs[k].className = (k === id) ? activeClass : inactiveClass;
        }
      });

      resetCurrentPreset();
    }

    function resetCurrentPreset() {
      const p = presets[activePreset];
      if (!p) return;
      customStages = JSON.parse(JSON.stringify(p.stages)).map(s => ({
        ...s,
        enabled: true,
        temperature: s.temperature ?? 0.2
      }));
      renderStagesSidebar();
    }

    function renderStagesSidebar() {
      const container = document.getElementById('stagesListSidebar');
      const badge = document.getElementById('sidebarStageCountBadge');

      if (activePreset === 'auto') {
        badge.innerText = 'AUTO-ROUTED';
        badge.className = 'text-[10px] font-mono text-emerald-400 bg-emerald-500/10 px-1.5 py-0.5 rounded border border-emerald-500/20';
        container.innerHTML = `
          <div class="p-2.5 rounded-lg border border-brand/30 bg-brand/5 space-y-2">
            <div class="flex items-center gap-1.5 text-brand-light font-medium text-xs">
              <span class="w-1.5 h-1.5 rounded-full bg-brand-light"></span> Auto-Pilot Orchestrator
            </div>
            <p class="text-[11px] text-[#8a8f98] leading-relaxed">
              AI Team Lead menganalisis tugas dan otomatis memilih tim yang tepat (mem-bypass peran tidak relevan seperti UI/UX pada security audit).
            </p>
            <div class="pt-1 flex flex-wrap gap-1">
              <span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-white/[0.04] text-[#d0d6e0] border border-subtle">Architect</span>
              <span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-white/[0.04] text-[#d0d6e0] border border-subtle">UI/UX</span>
              <span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-white/[0.04] text-[#d0d6e0] border border-subtle">Coder</span>
              <span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-white/[0.04] text-[#d0d6e0] border border-subtle">Security</span>
              <span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-white/[0.04] text-[#d0d6e0] border border-subtle">QA</span>
            </div>
          </div>
        `;
        return;
      }

      badge.className = 'text-[10px] font-mono text-brand-light';
      const activeCount = customStages.filter(s => s.enabled).length;
      badge.innerText = `${activeCount} stages active`;

      container.innerHTML = customStages.map((s, idx) => `
        <div class="p-2 rounded-md border ${s.enabled ? 'border-subtle bg-white/[0.02]' : 'border-subtle/40 bg-black/20 opacity-50'} transition flex items-center justify-between">
          <div class="flex items-center gap-2 min-w-0">
            <input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggleStage(${idx})" class="w-3.5 h-3.5 rounded text-brand bg-canvas border-subtle focus:ring-0 cursor-pointer">
            <span class="text-xs cursor-pointer select-none" onclick="toggleStage(${idx})">${s.icon || '🤖'}</span>
            <div class="min-w-0">
              <div class="text-xs text-[#f7f8f8] truncate font-medium cursor-pointer" onclick="openAgentModal(${idx})">${s.name}</div>
              <div class="text-[10px] text-[#8a8f98] truncate font-mono">${s.role}</div>
            </div>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-[10px] font-mono text-[#8a8f98] px-1 py-0.5 rounded bg-white/[0.03]">t:${s.temperature ?? 0.2}</span>
            <button onclick="openAgentModal(${idx})" class="text-[#8a8f98] hover:text-white p-1 text-xs" title="Edit Prompt">⚙️</button>
            <button onclick="removeStage(${idx})" class="text-[#62666d] hover:text-rose-400 p-1 text-xs" title="Hapus">✕</button>
          </div>
        </div>
      `).join('');
    }

    function toggleStage(idx) {
      customStages[idx].enabled = !customStages[idx].enabled;
      renderStagesSidebar();
    }

    function removeStage(idx) {
      if (customStages.length <= 1) {
        showToast('Minimal 1 agent harus aktif', '⚠️');
        return;
      }
      customStages.splice(idx, 1);
      renderStagesSidebar();
    }

    async function openAddStageModal() {
      const name = await openInputDialog({
        title: 'Tambah Peran Agent',
        message: 'Nama peran atau spesialisasi agent:',
        placeholder: 'Misal: Security Auditor, Database Architect, Tech Lead',
        confirmText: 'Tambah Agent'
      });
      if (!name || !name.trim()) return;
      const cleanName = name.trim();
      customStages.push({
        role: cleanName,
        name: cleanName,
        icon: 'STEP',
        temperature: 0.2,
        system: `Kamu adalah ${cleanName}. Berikan analisis dan kontribusi terstruktur sesuai peranmu.`,
        enabled: true
      });
      renderStagesSidebar();
      showToast(`Agent ${cleanName} ditambahkan`, '✓');
    }

    function openAgentModal(idx) {
      activeModalStageIndex = idx;
      const s = customStages[idx];
      document.getElementById('modalAgentIcon').innerText = s.icon || '🤖';
      document.getElementById('modalAgentTitle').innerText = `${s.name} (${s.role})`;
      document.getElementById('modalAgentTempSlider').value = s.temperature ?? 0.2;
      document.getElementById('modalAgentTempLabel').innerText = (s.temperature ?? 0.2).toFixed(2);
      document.getElementById('modalAgentSystemPrompt').value = s.system || '';
      document.getElementById('agentModalBackdrop').style.display = 'flex';
    }

    function closeAgentModal() {
      document.getElementById('agentModalBackdrop').style.display = 'none';
      activeModalStageIndex = null;
    }

    function saveAgentModalChanges() {
      if (activeModalStageIndex === null) return;
      const s = customStages[activeModalStageIndex];
      s.temperature = parseFloat(document.getElementById('modalAgentTempSlider').value);
      s.system = document.getElementById('modalAgentSystemPrompt').value;
      closeAgentModal();
      renderStagesSidebar();
      showToast('Konfigurasi agent diperbarui', '✓');
    }

    function fillPromptTemplate(type) {
      const input = document.getElementById('taskInputPrompt');
      if (type === 'dev_feature') {
        input.value = "Rancang dan buatkan modul backend API absensi QR code berbasis token sementara (expiry 30 detik) dan validasi geolocation radius 50 meter. Tuliskan setiap file terpisah (Model, Controller, Migration, Route).";
      } else if (type === 'kuliah_paper') {
        input.value = "Buat kajian komprehensif mengenai penerapan arsitektur data engineering modern (Lakehouse & Delta Table). Sertakan tinjauan teori, studi kasus implementasi, dan kesimpulan akademik.";
      } else if (type === 'audit_qa') {
        selectPreset('security');
        document.getElementById('taskInputTitle').value = 'Security Audit & Vulnerability Assessment';
        input.value = "Lakukan security audit dan analisis potensi celah injection, broken authentication, race condition, dan token leakage pada alur transaksi dan validasi token API. Tuliskan kode perbaikan (security patch) untuk setiap celah yang ditemukan.";
      }
      input.focus();
    }

    async function enhancePrompt() {
      const input = document.getElementById('taskInputPrompt');
      const raw = input.value.trim();
      if (!raw) {
        showToast('Ketik instruksi tugas terlebih dahulu', '✕');
        input.focus();
        return;
      }

      const btn = document.getElementById('btnEnhancePrompt');
      const label = document.getElementById('btnEnhanceText');
      const originalText = label ? label.innerText : 'Enhance';
      if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-60', 'cursor-not-allowed');
      }
      if (label) label.innerText = 'Enhancing...';

      try {
        const res = await fetch('/api/prompt/enhance', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: raw,
            working_directory: currentWorkspace,
            preset_id: activePreset
          })
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || 'Gagal memoles prompt.');
        }

        const data = await res.json();
        if (data.enhanced) {
          input.value = data.enhanced;
          showToast('Prompt berhasil dioptimasi', '✓');
          input.classList.add('ring-1', 'ring-brand');
          setTimeout(() => input.classList.remove('ring-1', 'ring-brand'), 1500);
        }
      } catch (e) {
        showToast(e.message || 'Gagal meningkatkan prompt', '✕');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.classList.remove('opacity-60', 'cursor-not-allowed');
        }
        if (label) label.innerText = originalText;
        input.focus();
      }
    }

    function newDraftTask() {
      document.getElementById('taskInputPrompt').value = '';
      document.getElementById('taskInputTitle').value = '';
      document.getElementById('taskInputPrompt').focus();
    }

    async function dispatchTask() {
      const title = document.getElementById('taskInputTitle').value.trim();
      const prompt = document.getElementById('taskInputPrompt').value.trim();
      const autoSave = document.getElementById('prefAutoSave').checked;
      const autoApplyFiles = document.getElementById('prefAutoApplyFiles').checked;
      const requireApproval = document.getElementById('prefRequireApproval').checked;
      const autoFixLoops = parseInt(document.getElementById('prefAutoFixLoops').value);

      if (!prompt) {
        showToast('Tuliskan detail tugas terlebih dahulu', '⚠️');
        return;
      }

      const activeStages = customStages.filter(s => s.enabled);
      if (activePreset !== 'auto' && activeStages.length === 0) {
        showToast('Minimal 1 stage harus aktif', '⚠️');
        return;
      }

      const btn = document.getElementById('btnDispatch');
      btn.disabled = true;
      document.getElementById('btnDispatchText').innerText = 'Starting...';

      try {
        const res = await fetch('/api/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: title || prompt.substring(0, 35) + '...',
            prompt: prompt,
            preset_id: activePreset,
            working_directory: currentWorkspace,
            project_context: workspaceInfo?.agents_md || null,
            skills: selectedSkills,
            session_id: activeWorkstationSessionId || 'default',
            auto_save_artifact: autoSave,
            auto_apply_files: autoApplyFiles,
            require_approval: requireApproval,
            auto_fix_loops: autoFixLoops,
            stages: (activePreset === 'auto') ? [] : activeStages
          })
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || 'Gagal membuat tugas.');
        }

        const task = await res.json();
        btn.disabled = false;
        document.getElementById('btnDispatchText').innerText = 'Dispatch Team';
        
        showToast(`Tugas [${task.id}] dijalankan`, '🚀');
        loadTask(task.id);
        fetchTasks();
      } catch (e) {
        btn.disabled = false;
        document.getElementById('btnDispatchText').innerText = 'Dispatch Team';
        showToast(e.message, '✕');
      }
    }

    function loadTask(taskId) {
      if (window.innerWidth < 768) {
        toggleSidebar(false);
      }
      currentTaskId = taskId;
      if (pollingInterval) clearInterval(pollingInterval);
      pollTask();
      pollingInterval = setInterval(pollTask, 2000);
    }

    async function pollTask() {
      if (!currentTaskId) return;
      try {
        const res = await fetch(`/api/tasks/${currentTaskId}`);
        if (!res.ok) return;
        const task = await res.json();
        renderActiveTask(task);

        if (['completed', 'failed', 'cancelled'].includes(task.status)) {
          clearInterval(pollingInterval);
          pollingInterval = null;
          fetchTasks();
          updateSystemStatus();
        }
      } catch (e) {
        console.error('Polling error:', e);
      }
    }

    function renderActiveTask(task) {
      document.getElementById('viewTaskTitle').innerText = task.title;
      document.getElementById('viewTaskPrompt').innerText = task.prompt;
      document.getElementById('badgeTaskPreset').innerText = `[${task.preset_id?.toUpperCase() || 'CUSTOM'}]`;
      document.getElementById('badgeTaskWorkspace').innerText = task.working_directory ? `📁 ${task.working_directory.split('/').pop()}` : '';
      
      const fixLoopBadge = document.getElementById('badgeFixLoop');
      if (task.current_fix_loop && task.current_fix_loop > 0) {
        fixLoopBadge.style.display = 'inline';
        fixLoopBadge.innerText = `Fix Cycle #${task.current_fix_loop}`;
      } else {
        fixLoopBadge.style.display = 'none';
      }

      const statusBadge = document.getElementById('badgeTaskStatus');
      statusBadge.innerText = task.status.toUpperCase();
      if (task.status === 'running') {
        statusBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono font-medium bg-amber-500/10 text-amber-300 border border-amber-500/20 animate-pulse';
      } else if (task.status === 'waiting_approval') {
        statusBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono font-medium bg-amber-500/20 text-amber-200 border border-amber-500/40 animate-bounce';
      } else if (task.status === 'completed') {
        statusBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
      } else if (task.status === 'failed') {
        statusBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20';
      } else if (task.status === 'cancelled') {
        statusBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono font-medium bg-neutral-500/10 text-neutral-400 border border-neutral-500/20';
      }

      const cancelBtn = document.getElementById('btnCancelRunningTask');
      if (cancelBtn) {
        cancelBtn.style.display = task.status === 'running' ? 'inline-block' : 'none';
      }

      // Actions toolbar visibility
      document.getElementById('taskActionToolbar').style.display = task.status === 'completed' ? 'flex' : 'none';

      // Human Approval Gate Banner
      const approvalBanner = document.getElementById('approvalGateBanner');
      if (task.status === 'waiting_approval') {
        approvalBanner.style.display = 'block';
        document.getElementById('approvalStageNameLabel').innerText = task.waiting_stage_name || 'Architect';
      } else {
        approvalBanner.style.display = 'none';
      }

      // Auto-save banner
      const savedPill = document.getElementById('savedArtifactPill');
      if (task.saved_artifact_path) {
        savedPill.style.display = 'flex';
        let bannerText = `Dokumen tersimpan di: ${task.saved_artifact_path}`;
        if (task.applied_files && task.applied_files.length) {
          bannerText += ` (dan ${task.applied_files.length} file kode langsung ditulis!)`;
        }
        document.getElementById('savedArtifactPathText').innerText = bannerText;
      } else {
        savedPill.style.display = 'none';
      }

      // Task Error Banner
      const errorBanner = document.getElementById('taskErrorBanner');
      if (task.status === 'failed') {
        errorBanner.style.display = 'block';
        document.getElementById('taskErrorBannerDetail').innerText = task.error || 'Terjadi kesalahan sistem saat memproses tugas ini.';
      } else {
        errorBanner.style.display = 'none';
      }

      // Extracted files count
      const extractedCount = task.extracted_files?.length || 0;
      document.getElementById('filesCountBadge').innerText = extractedCount;
      document.getElementById('tabFilesCountBadge').innerText = extractedCount;

      // Timeline Steps Cards
      const timelineCards = document.getElementById('timelineStepCards');
      timelineCards.innerHTML = task.stages.map((s, idx) => {
        let borderClass = 'border-subtle bg-white/[0.015] text-[#8a8f98]';
        let statusDot = '<span class="w-1.5 h-1.5 rounded-full bg-[#62666d]"></span>';
        let statusLabel = 'Waiting';

        if (s.status === 'running') {
          borderClass = 'active-stage-card bg-brand/5 text-brand-light';
          statusDot = '<span class="w-1.5 h-1.5 rounded-full bg-brand-light animate-ping"></span>';
          statusLabel = 'Working...';
        } else if (s.status === 'completed') {
          borderClass = 'border-emerald-500/20 bg-emerald-500/5 text-emerald-400';
          statusDot = '<span class="text-[10px] text-emerald-400">✓</span>';
          statusLabel = 'Done';
        } else if (s.status === 'bypassed') {
          borderClass = 'border-slate-700/50 bg-slate-900/30 text-slate-400 opacity-60 hover:opacity-100';
          statusDot = '<span class="text-[9px] text-slate-400 font-mono">⊘</span>';
          statusLabel = 'Bypassed';
        } else if (s.status === 'error') {
          borderClass = 'border-rose-500/30 bg-rose-500/10 text-rose-300';
          statusDot = '<span class="text-[10px] text-rose-400">✕</span>';
          statusLabel = 'Failed';
        }

        const roleOrErr = (s.status === 'error' && s.error) 
          ? `<span class="text-rose-400 font-medium truncate block" title="${escapeHtml(s.error)}">✕ ${escapeHtml(s.error.split(':')[0])}</span>`
          : (s.status === 'bypassed')
          ? `<span class="text-slate-400 font-mono text-[9px] truncate block" title="${escapeHtml(s.output || 'Di luar scope')}">Out of Scope</span>`
          : `<span class="text-[#8a8f98] font-mono truncate block">${escapeHtml(s.role)}</span>`;

        return `
          <div onclick="switchViewTab('stages'); focusStageCard(${idx});" class="p-2.5 rounded-lg border ${borderClass} transition cursor-pointer flex flex-col justify-between">
            <div class="flex items-center justify-between text-[11px] mb-1">
              <span>${s.icon || '🤖'}</span>
              <div class="flex items-center gap-1 font-mono text-[10px]">
                ${statusDot}
                <span>${statusLabel}</span>
              </div>
            </div>
            <div>
              <div class="text-xs font-semibold text-[#f7f8f8] truncate">${s.name}</div>
              <div class="text-[10px]">${roleOrErr}</div>
            </div>
          </div>
        `;
      }).join('');

      // Deliverable Rendered View
      const renderEl = document.getElementById('deliverableRenderedBody');
      if (task.final_output) {
        renderEl.innerHTML = mdRender(task.final_output);
      } else if (task.status === 'failed') {
        renderEl.innerHTML = `
          <div class="py-10 px-4 text-center space-y-3 max-w-md mx-auto">
            <div class="w-10 h-10 rounded-full bg-rose-500/20 border border-rose-500/30 text-rose-400 flex items-center justify-center mx-auto text-lg font-bold">✕</div>
            <div class="text-sm font-semibold text-rose-200">Eksekusi Terhenti Karena Error</div>
            <div class="text-[11px] font-mono text-rose-300 bg-rose-500/10 p-3 rounded-lg border border-rose-500/20 text-left whitespace-pre-wrap">${escapeHtml(task.error || 'Terjadi kesalahan sistem.')}</div>
            <p class="text-[11px] text-[#8a8f98]">Silakan buka tab <strong>Agent Logs</strong> untuk melihat rincian kegagalan pada stage bersangkutan.</p>
          </div>
        `;
      } else if (task.status === 'running' || task.status === 'waiting_approval') {
        const runningStage = task.stages.find(s => s.status === 'running');
        renderEl.innerHTML = `
          <div class="py-12 flex flex-col items-center justify-center text-center space-y-3">
            <div class="w-8 h-8 rounded-full border-2 border-brand border-t-transparent animate-spin"></div>
            <div class="text-xs font-mono text-brand-light">
              ${task.status === 'waiting_approval' ? 'Menunggu persetujuan Anda di atas...' : (runningStage ? `${runningStage.name} (${runningStage.role}) sedang bekerja...` : 'Sedang memproses...')}
            </div>
            <p class="text-[11px] text-[#8a8f98] max-w-sm">Agent secara otonom memproses arsitektur, kode, dan verifikasi.</p>
          </div>
        `;
      } else {
        renderEl.innerHTML = `<div class="text-center py-12 text-[#8a8f98] text-xs">Deliverable belum dihasilkan.</div>`;
      }

      // Extracted Files View
      renderExtractedFiles(task);

      // Stages Breakdown
      const stagesLogEl = document.getElementById('stagesLogContainer');
      stagesLogEl.innerHTML = task.stages.map((s, idx) => {
        let contentHtml = '';
        if (s.status === 'bypassed') {
          contentHtml = `
            <div class="p-3.5 rounded-lg border border-slate-700/50 bg-slate-900/30 text-slate-400 font-mono text-xs flex items-center justify-between">
              <div>
                <span class="font-bold text-slate-300">STAGE BYPASSED (OUT OF SCOPE)</span>
                <p class="text-[11px] text-slate-400 mt-1 whitespace-pre-wrap">${escapeHtml(s.output || 'Peran ini dilewati secara otomatis oleh sistem karena berada di luar batasan Scope Matrix.')}</p>
              </div>
              <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400 border border-slate-700/60 shrink-0">0s Latency</span>
            </div>
          `;
        } else if (s.status === 'error') {
          contentHtml = `
            <div class="p-3.5 rounded-lg border border-rose-500/30 bg-rose-500/10 text-rose-300 font-mono text-xs space-y-2">
              <div class="font-bold flex items-center gap-1.5 text-rose-200 text-xs">
                <span>⚠️</span> Stage Error: Gagal Menyelesaikan Permintaan
              </div>
              <div class="text-[11px] leading-relaxed whitespace-pre-wrap text-rose-300 bg-black/40 p-2.5 rounded border border-rose-500/20">${escapeHtml(s.error || 'Terjadi kesalahan tidak diketahui')}</div>
              <div class="text-[10px] text-[#8a8f98] font-sans pt-1">
                💡 <strong>Tips Penanganan:</strong> Periksa koneksi ke LLM Gateway (9Router / Ollama / LM Studio), pastikan model aktif dan token prompt tidak melebihi kuota.
              </div>
            </div>
          `;
        } else if (s.output) {
          contentHtml = mdRender(s.output);
        } else {
          contentHtml = `<span class="italic text-[#8a8f98] font-mono">${s.status === 'running' ? 'Mengetik jawaban...' : 'Menunggu antrean...'}</span>`;
        }

        return `
          <div id="stageLogCard_${idx}" class="rounded-xl border ${s.status === 'running' ? 'border-brand/40 bg-panel' : (s.status === 'error' ? 'border-rose-500/40 bg-panel' : (s.status === 'bypassed' ? 'border-slate-800 bg-panel/40 opacity-70' : 'border-subtle bg-panel/70'))} overflow-hidden transition">
            <div class="px-4 py-2.5 border-b border-subtle flex items-center justify-between bg-surface/50">
              <div class="flex items-center gap-2">
                <span class="text-sm">${s.icon || '🤖'}</span>
                <span class="text-xs font-semibold text-white">${s.name}</span>
                <span class="text-[10px] font-mono text-[#8a8f98]">(${s.role})</span>
              </div>
              <span class="text-[10px] font-mono px-2 py-0.5 rounded-full ${s.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400' : (s.status === 'error' ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : (s.status === 'bypassed' ? 'bg-slate-800 text-slate-400 border border-slate-700/60' : 'bg-white/[0.03] text-[#8a8f98]'))}">
                ${s.status.toUpperCase()}
              </span>
            </div>
            <div class="p-4 prose-dark text-xs">
              ${contentHtml}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderExtractedFiles(task) {
      const container = document.getElementById('extractedFilesListContainer');
      const files = task.extracted_files || [];
      if (!files.length) {
        container.innerHTML = '<div class="text-center py-12 text-[#8a8f98] text-xs">Belum ada blok file kode yang terdeteksi.</div>';
        return;
      }

      let headerHtml = '';
      if (task.applied_files && task.applied_files.length) {
        headerHtml = `
          <div class="p-3 rounded-lg border border-emerald-500/20 bg-emerald-500/5 mb-3 flex items-center justify-between">
            <div class="flex items-center gap-2 text-xs text-emerald-400">
              <span>✓</span>
              <span><strong>${task.applied_files.length} file</strong> telah diterapkan ke workspace disk.</span>
            </div>
            <button onclick="rollbackActiveDrawerFile()" class="px-2.5 py-1 rounded bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/30 text-xs font-medium transition flex items-center gap-1">
              <span>↺</span> Undo / Rollback
            </button>
          </div>
        `;
      }

      container.innerHTML = headerHtml + files.map((f, idx) => {
        const isBlocked = f.blocked;
        const blockedBadge = isBlocked 
          ? `<span class="text-[10px] text-rose-400 bg-rose-500/10 border border-rose-500/30 px-1.5 py-0.5 rounded font-mono font-medium" title="${escapeHtml(f.blocked_reason || '')}">BLOCKED BY SCOPE MATRIX</span>`
          : '';

        return `
          <div class="rounded-lg border ${isBlocked ? 'border-rose-500/30 bg-rose-500/5 opacity-85' : 'border-subtle bg-surface/60'} overflow-hidden text-xs">
            <div class="px-3 py-2 bg-surface border-b border-subtle flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span>📄</span>
                <span class="font-mono text-white font-medium ${isBlocked ? 'line-through text-rose-300' : ''}">${f.path}</span>
                <span class="text-[10px] text-[#8a8f98] px-1.5 py-0.5 rounded bg-black/40 font-mono">${f.language} · ${f.lines} baris</span>
                ${blockedBadge}
              </div>
              <div class="flex items-center gap-2">
                <button onclick="previewDiffForExtractedFile(${idx})" class="text-brand-light hover:text-white text-[11px] transition">Diff (+/-)</button>
                <button onclick="copyFileContent(${idx})" class="text-[#8a8f98] hover:text-white text-[11px] transition">Salin Kode</button>
              </div>
            </div>
            <pre class="p-3 text-[11px] font-mono text-[#d0d6e0] overflow-x-auto bg-[#0a0b0c] max-h-64"><code class="language-${escapeHtml(f.language || 'text')}">${escapeHtml(f.content)}</code></pre>
          </div>
        `;
      }).join('');

      if (window.Prism) {
        setTimeout(() => {
          try { Prism.highlightAllUnder(container); } catch (e) {}
        }, 10);
      }
    }

    function escapeHtml(string) {
      const entityMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
      return String(string).replace(/[&<>"']/g, s => entityMap[s]);
    }

    function copyTextToClipboard(text, label = 'Teks') {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => {
          showToast(`${label} disalin!`, '📋');
        }).catch(() => fallbackCopy(text, label));
      } else {
        fallbackCopy(text, label);
      }
    }

    function fallbackCopy(text, label) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast(`${label} disalin!`, '📋');
      } catch (e) {
        showToast('Gagal menyalin', '✕');
      }
    }

    async function copyFileContent(idx) {
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${currentTaskId}`);
      const task = await res.json();
      const file = task.extracted_files?.[idx];
      if (file) {
        copyTextToClipboard(file.content, `Kode ${file.path}`);
      }
    }

    async function submitApproval(action) {
      if (!currentTaskId) return;
      const feedback = document.getElementById('approvalFeedbackInput').value.trim();
      try {
        const res = await fetch(`/api/tasks/${currentTaskId}/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: action, feedback: feedback || null })
        });
        if (res.ok) {
          showToast(action === 'approve' ? 'Tahap diapprove! Melanjutkan ke Coder...' : 'Tugas dibatalkan.', '✓');
          document.getElementById('approvalFeedbackInput').value = '';
          pollTask();
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function applyExtractedFilesToWorkspace() {
      if (!currentTaskId) return;
      try {
        const res = await fetch(`/api/tasks/${currentTaskId}/apply-files`, { method: 'POST' });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || 'Gagal menerapkan file');
        }
        const data = await res.json();
        showToast(`${data.count} file berhasil ditulis ke proyek! (.bak dibuat)`, '⚡');
        await setWorkspace(currentWorkspace);
        pollTask();
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    function switchViewTab(tab) {
      currentActiveTab = tab;
      const tabDeliverable = document.getElementById('tabContentDeliverable');
      const tabFiles = document.getElementById('tabContentFiles');
      const tabStages = document.getElementById('tabContentStages');
      const btnDeliverable = document.getElementById('tabBtnDeliverable');
      const btnFiles = document.getElementById('tabBtnFiles');
      const btnStages = document.getElementById('tabBtnStages');

      tabDeliverable.style.display = tab === 'deliverable' ? 'block' : 'none';
      tabFiles.style.display = tab === 'files' ? 'block' : 'none';
      tabStages.style.display = tab === 'stages' ? 'block' : 'none';

      btnDeliverable.className = tab === 'deliverable' ? 'pb-2 text-white border-b-2 border-brand transition' : 'pb-2 text-[#8a8f98] hover:text-[#d0d6e0] border-b-2 border-transparent transition';
      btnFiles.className = tab === 'files' ? 'pb-2 text-white border-b-2 border-brand transition' : 'pb-2 text-[#8a8f98] hover:text-[#d0d6e0] border-b-2 border-transparent transition';
      btnStages.className = tab === 'stages' ? 'pb-2 text-white border-b-2 border-brand transition' : 'pb-2 text-[#8a8f98] hover:text-[#d0d6e0] border-b-2 border-transparent transition';
    }

    function focusStageCard(idx) {
      const card = document.getElementById(`stageLogCard_${idx}`);
      if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }

    async function copyDeliverable() {
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${currentTaskId}`);
      const task = await res.json();
      if (task.final_output) {
        copyTextToClipboard(task.final_output, 'Deliverable');
      }
    }

    async function downloadDeliverable() {
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${currentTaskId}`);
      const task = await res.json();
      if (task.final_output) {
        const blob = new Blob([task.final_output], { type: 'text/markdown' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${task.title.replace(/[^a-zA-Z0-9]/g, '_')}.md`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 1500);
        showToast('Download dimulai', '⬇️');
      }
    }

    async function saveDeliverableToFile() {
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${currentTaskId}`);
      const task = await res.json();
      if (!task.final_output) return;

      const fname = await openInputDialog({
        title: 'Simpan Dokumen Deliverable',
        message: 'Tentukan nama berkas markdown untuk menyimpan deliverable:',
        defaultValue: `DELIVERABLE_${task.id}.md`,
        confirmText: 'Simpan Berkas'
      });
      if (!fname) return;

      try {
        const postRes = await fetch('/api/workspace/save-artifact', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            path: currentWorkspace,
            filename: fname,
            content: task.final_output
          })
        });
        if (postRes.ok) {
          const d = await postRes.json();
          showToast(`File tersimpan di ${fname}`, '✓');
          await setWorkspace(currentWorkspace);
        } else {
          showToast('Gagal menyimpan file', '✕');
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function cancelCurrentTask() {
      if (!currentTaskId) return;
      const ok = await openConfirmDialog({
        title: 'Batalkan Tugas',
        message: 'Yakin ingin membatalkan tugas yang sedang berjalan?',
        confirmText: 'Ya, Batalkan',
        danger: true
      });
      if (!ok) return;
      try {
        const res = await fetch(`/api/tasks/${currentTaskId}/cancel`, { method: 'POST' });
        if (res.ok) {
          showToast('Tugas dibatalkan.', '✓');
          pollTask();
          fetchTasks();
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function deleteTask(taskId) {
      const ok = await openConfirmDialog({
        title: 'Hapus Tugas',
        message: `Hapus tugas [${taskId}] dari riwayat? Data output tidak dapat dipulihkan.`,
        confirmText: 'Hapus Riwayat',
        danger: true
      });
      if (!ok) return;
      try {
        const res = await fetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
        if (res.ok) {
          showToast(`Tugas ${taskId} dihapus.`, '✓');
          if (currentTaskId === taskId) {
            currentTaskId = null;
            document.getElementById('viewTaskTitle').innerText = 'Belum ada tugas yang aktif';
            document.getElementById('viewTaskPrompt').innerText = '';
            document.getElementById('badgeTaskStatus').innerText = 'IDLE';
            document.getElementById('badgeTaskStatus').className = 'px-2 py-0.5 rounded text-[10px] font-mono font-medium bg-white/[0.05] text-[#8a8f98]';
            document.getElementById('taskActionToolbar').style.display = 'none';
            document.getElementById('timelineStepCards').innerHTML = '';
            document.getElementById('deliverableRenderedBody').innerHTML = '<div class="text-center py-12 text-[#8a8f98] text-xs">Pilih tugas dari riwayat atau buat tugas baru.</div>';
          }
          fetchTasks();
        }
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    let taskSessionFilter = 'active';

    function toggleTaskSessionFilter() {
      taskSessionFilter = taskSessionFilter === 'active' ? 'all' : 'active';
      const btn = document.getElementById('btnToggleSessionFilter');
      if (btn) btn.innerText = taskSessionFilter === 'active' ? 'Sesi Ini' : 'Semua';
      fetchTasks();
    }

    async function fetchTasks() {
      try {
        const res = await fetch('/api/tasks');
        const tasks = await res.json();
        const container = document.getElementById('historyTaskSidebarList');
        if (!tasks.length) {
          container.innerHTML = '<div class="text-[11px] text-[#8a8f98] py-2 text-center">Belum ada tugas.</div>';
          return;
        }

        let displayedTasks = tasks;
        if (taskSessionFilter === 'active' && activeWorkstationSessionId) {
          displayedTasks = tasks.filter(t => (t.session_id || 'default') === activeWorkstationSessionId);
        }

        if (!displayedTasks.length) {
          container.innerHTML = `
            <div class="text-[11px] text-[#8a8f98] py-3 text-center space-y-1">
              <div>Tidak ada tugas di sesi ini.</div>
              <button onclick="toggleTaskSessionFilter()" class="text-brand-light hover:underline text-[10px]">Tampilkan Semua</button>
            </div>
          `;
          return;
        }

        container.innerHTML = displayedTasks.map(t => {
          let badgeColor = 'text-[#8a8f98]';
          if (t.status === 'running') badgeColor = 'text-amber-400 font-bold';
          if (t.status === 'waiting_approval') badgeColor = 'text-amber-300 font-bold animate-pulse';
          if (t.status === 'completed') badgeColor = 'text-emerald-400';
          if (t.status === 'failed') badgeColor = 'text-rose-400';
          if (t.status === 'cancelled') badgeColor = 'text-[#62666d]';

          return `
            <div onclick="loadTask('${t.id}')" class="p-1.5 rounded hover:bg-white/[0.04] cursor-pointer transition flex items-center justify-between text-xs group">
              <div class="min-w-0 pr-1">
                <div class="text-[#d0d6e0] group-hover:text-white truncate font-medium text-[11px]">${t.title}</div>
                <div class="text-[9px] text-[#62666d] truncate font-mono">${new Date(t.created_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})} · ${t.preset_id || 'custom'}</div>
              </div>
              <div class="flex items-center gap-1.5 shrink-0">
                <span class="text-[9px] font-mono ${badgeColor}">${t.status.substring(0, 4)}</span>
                <button onclick="event.stopPropagation(); deleteTask('${t.id}')" class="opacity-0 group-hover:opacity-100 p-0.5 hover:text-rose-400 text-[#62666d] text-[10px] transition" title="Hapus Tugas">✕</button>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        console.error('Fetch tasks error:', e);
      }
    }

    /* ================= FILE TREE EXPLORER ================= */
    let fileTreeData = [];
    let fileTreeCollapsed = new Set();
    let fileTreeSearchQuery = '';
    let activeTreeFile = null;

    let lastTreeWorkspace = null;

    async function refreshFileTree() {
      const container = document.getElementById('drawerFilesList');
      if (!container) return;
      if (lastTreeWorkspace !== currentWorkspace) {
        fileTreeCollapsed.clear();
        activeTreeFile = null;
        lastTreeWorkspace = currentWorkspace;
      }
      container.innerHTML = '<div class="text-[11px] text-[#8a8f98] px-2 py-3 italic">Memuat tree...</div>';
      try {
        const res = await fetch(`/api/workspace/tree?path=${encodeURIComponent(currentWorkspace)}&depth=5`);
        if (!res.ok) { container.innerHTML = '<div class="text-rose-400 px-2 py-2 text-[11px]">Gagal memuat.</div>'; return; }
        const data = await res.json();
        fileTreeData = data.tree || [];
        const total = countTreeNodes(fileTreeData);
        const countEl = document.getElementById('drawerFolderItemCount');
        if (countEl) countEl.innerText = `(${total})`;
        renderFileTree();
      } catch (e) {
        container.innerHTML = `<div class="text-rose-400 px-2 py-2 text-[11px]">${escapeHtml(e.message)}</div>`;
      }
    }

    function countTreeNodes(nodes) {
      let count = 0;
      for (const n of nodes) { count++; if (n.children) count += countTreeNodes(n.children); }
      return count;
    }

    function filterFileTree(query) {
      fileTreeSearchQuery = query.trim().toLowerCase();
      renderFileTree();
    }

    function renderFileTree() {
      const container = document.getElementById('drawerFilesList');
      if (!container) return;
      if (!fileTreeData.length) { container.innerHTML = '<div class="text-[11px] text-[#8a8f98] px-2 py-3 italic">Folder kosong.</div>'; return; }
      const rootName = currentWorkspace.split('/').filter(Boolean).pop() || 'root';
      container.innerHTML = `
        <div class="mb-0.5">
          <div class="flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-mono font-semibold text-brand-light select-none">
            <span>⊞</span><span class="truncate">${escapeHtml(rootName)}</span>
          </div>
          <div id="treeRoot" class="ml-2">${renderTreeNodes(fileTreeData, 0)}</div>
        </div>`;
    }

    function matchesSearch(node, query) {
      if (!query) return true;
      if (node.name.toLowerCase().includes(query)) return true;
      if (node.children) return node.children.some(c => matchesSearch(c, query));
      return false;
    }

    function renderTreeNodes(nodes, depth) {
      let html = '';
      for (const node of nodes) {
        if (!matchesSearch(node, fileTreeSearchQuery)) continue;
        const isDir = node.is_dir;
        const key = node.rel_path;
        const isCollapsed = fileTreeCollapsed.has(key) && !fileTreeSearchQuery;
        const isActive = activeTreeFile === key;
        const indent = depth * 10;

        if (isDir) {
          const chevron = isCollapsed ? '▶' : '▼';
          const folderIcon = isCollapsed ? '📁' : '📂';
          html += `
            <div class="tree-node-dir">
              <div onclick="toggleTreeDir('${escapeHtml(key)}')"
                class="flex items-center gap-1.5 px-1.5 py-[3px] rounded cursor-pointer hover:bg-white/[0.05] transition group select-none"
                style="padding-left: ${indent + 6}px">
                <span class="text-[9px] text-[#62666d] w-3 shrink-0">${chevron}</span>
                <span class="text-[12px] shrink-0">${folderIcon}</span>
                <span class="text-[11px] font-mono text-[#c8cdd6] group-hover:text-white truncate font-medium">${escapeHtml(node.name)}</span>
                <div class="ml-auto flex gap-1 opacity-0 group-hover:opacity-100 shrink-0">
                  <button onclick="event.stopPropagation(); promptRenameDrawerItem('${escapeHtml(key)}', '${escapeHtml(node.name)}')" class="text-[9px] text-[#8a8f98] hover:text-white font-mono px-1 rounded hover:bg-white/10">ren</button>
                  <button onclick="event.stopPropagation(); deleteDrawerItem('${escapeHtml(key)}', true)" class="text-[9px] text-[#8a8f98] hover:text-rose-400 font-mono px-1 rounded hover:bg-rose-500/10">del</button>
                </div>
              </div>
              <div class="tree-children" ${isCollapsed ? 'style="display:none"' : ''}>
                ${node.children && node.children.length ? renderTreeNodes(node.children, depth + 1) : (node.truncated ? `<div style="padding-left:${indent + 22}px" class="text-[10px] text-[#62666d] py-0.5 font-mono">⋯ (kedalaman maks)</div>` : `<div style="padding-left:${indent + 22}px" class="text-[10px] text-[#62666d] py-0.5 font-mono italic">kosong</div>`)}
              </div>
            </div>`;
        } else {
          const ext = node.name.split('.').pop().toLowerCase();
          const fileIcon = {
            'py': '🐍', 'js': '🟨', 'ts': '🔷', 'dart': '🎯', 'md': '📝',
            'json': '{}', 'yaml': '⚙', 'yml': '⚙', 'html': '🌐', 'css': '🎨',
            'sh': '⚡', 'txt': '📄', 'env': '🔑', 'sql': '🗄'
          }[ext] || '📄';
          const sizeStr = node.size > 1024 ? `${(node.size/1024).toFixed(1)}k` : `${node.size}b`;
          html += `
            <div onclick="openTreeFile('${escapeHtml(key)}')"
              data-tree-path="${escapeHtml(key)}"
              class="tree-node-file flex items-center gap-1.5 px-1.5 py-[3px] rounded cursor-pointer ${isActive ? 'bg-brand/15 text-brand-light' : 'hover:bg-white/[0.04]'} transition group select-none"
              style="padding-left: ${indent + 16}px">
              <span class="text-[12px] shrink-0">${fileIcon}</span>
              <span class="text-[11px] font-mono ${isActive ? 'text-brand-light font-medium' : 'text-[#c8cdd6] group-hover:text-white'} truncate">${escapeHtml(node.name)}</span>
              <div class="ml-auto flex items-center gap-1 opacity-0 group-hover:opacity-100 shrink-0">
                <span class="text-[9px] text-[#62666d] font-mono">${sizeStr}</span>
                <button onclick="event.stopPropagation(); promptRenameDrawerItem('${escapeHtml(key)}', '${escapeHtml(node.name)}')" class="text-[9px] text-[#8a8f98] hover:text-white font-mono px-1 rounded hover:bg-white/10">ren</button>
                <button onclick="event.stopPropagation(); deleteDrawerItem('${escapeHtml(key)}', false)" class="text-[9px] text-[#8a8f98] hover:text-rose-400 font-mono px-1 rounded hover:bg-rose-500/10">del</button>
              </div>
            </div>`;
        }
      }
      return html;
    }

    function toggleTreeDir(relPath) {
      if (fileTreeCollapsed.has(relPath)) {
        fileTreeCollapsed.delete(relPath);
      } else {
        fileTreeCollapsed.add(relPath);
      }
      renderFileTree();
    }

    async function openTreeFile(relPath) {
      activeTreeFile = relPath;
      renderFileTree();
      await previewDrawerFile(relPath);
      appendMiniTerminal(`> Membuka: ${relPath}`);
    }

    function updateGutterLines() {
      const ta = document.getElementById('drawerAgentsTextarea');
      const gutter = document.getElementById('drawerLineGutter');
      if (!ta || !gutter) return;
      const count = (ta.value.match(/\n/g) || []).length + 1;
      let s = '';
      for (let i = 1; i <= count; i++) {
        s += i + '\n';
      }
      gutter.innerText = s;
    }

    function syncGutterScroll() {
      const ta = document.getElementById('drawerAgentsTextarea');
      const gutter = document.getElementById('drawerLineGutter');
      if (ta && gutter) {
        gutter.scrollTop = ta.scrollTop;
      }
    }

    let currentDrawerEditorMode = 'code';

    async function setDrawerEditorMode(mode) {
      currentDrawerEditorMode = mode;
      const btnCode = document.getElementById('btnDrawerModeCode');
      const btnColor = document.getElementById('btnDrawerModeHighlight');
      const btnDiff = document.getElementById('btnDrawerModeDiff');
      const ta = document.getElementById('drawerAgentsTextarea');
      const gutter = document.getElementById('drawerLineGutter');
      const highlightBox = document.getElementById('drawerHighlightContainer');
      const highlightCode = document.getElementById('drawerHighlightCode');
      const diffBox = document.getElementById('drawerDiffContainer');

      // Reset button styles
      if (btnCode) btnCode.className = 'px-2 py-0.5 rounded text-[#8a8f98] hover:text-white transition';
      if (btnColor) btnColor.className = 'px-2 py-0.5 rounded text-[#8a8f98] hover:text-white transition';
      if (btnDiff) btnDiff.className = 'px-2 py-0.5 rounded text-[#8a8f98] hover:text-white transition';

      if (mode === 'diff') {
        if (btnDiff) btnDiff.className = 'px-2 py-0.5 rounded bg-brand text-white font-medium transition';
        if (ta) ta.style.display = 'none';
        if (gutter) gutter.style.display = 'none';
        if (highlightBox) highlightBox.style.display = 'none';
        if (diffBox) diffBox.style.display = 'block';
        await renderDrawerDiff();
      } else if (mode === 'highlight') {
        if (btnColor) btnColor.className = 'px-2 py-0.5 rounded bg-brand text-white font-medium transition';
        if (ta) ta.style.display = 'none';
        if (gutter) gutter.style.display = 'none';
        if (diffBox) diffBox.style.display = 'none';
        if (highlightBox && highlightCode && ta) {
          const fname = currentActiveDrawerFile || 'AGENTS.md';
          const ext = fname.split('.').pop().toLowerCase();
          const langMap = {
            'py': 'python',
            'dart': 'dart',
            'js': 'javascript',
            'mjs': 'javascript',
            'json': 'json',
            'html': 'markup',
            'css': 'css',
            'md': 'markdown'
          };
          const lang = langMap[ext] || 'text';
          highlightCode.className = `language-${lang}`;
          highlightCode.textContent = ta.value;
          highlightBox.style.display = 'block';
          if (window.Prism) {
            Prism.highlightElement(highlightCode);
          }
        }
      } else {
        if (btnCode) btnCode.className = 'px-2 py-0.5 rounded bg-brand text-white font-medium transition';
        if (ta) ta.style.display = 'block';
        if (gutter) gutter.style.display = 'block';
        if (highlightBox) highlightBox.style.display = 'none';
        if (diffBox) diffBox.style.display = 'none';
      }
    }

    async function renderDrawerDiff() {
      const diffBox = document.getElementById('drawerDiffContainer');
      const ta = document.getElementById('drawerAgentsTextarea');
      if (!diffBox || !ta) return;
      diffBox.innerHTML = '<div class="text-xs text-[#8a8f98] py-4 text-center">Menghitung visual diff...</div>';

      const relP = currentActiveDrawerFile || 'AGENTS.md';
      try {
        const res = await fetch('/api/workspace/diff', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            path: currentWorkspace,
            rel_path: relP,
            new_content: ta.value
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Gagal menghitung diff');

        if (!data.diff_lines || !data.diff_lines.length) {
          diffBox.innerHTML = '<div class="text-xs text-slate-400 py-6 text-center font-mono">Tidak ada perbedaan dengan file di disk (konten identik).</div>';
          return;
        }

        let html = `<div class="mb-2 flex items-center justify-between text-[11px] font-mono px-1">
          <span class="text-slate-300 font-semibold">${escapeHtml(data.rel_path)}</span>
          <div class="flex items-center gap-2">
            <span class="text-emerald-400 font-bold">+${data.additions} lines</span>
            <span class="text-rose-400 font-bold">-${data.deletions} lines</span>
          </div>
        </div>`;

        html += '<div class="divide-y divide-subtle/30 rounded border border-subtle bg-black/50 overflow-x-auto select-text">';
        data.diff_lines.forEach(l => {
          let bg = '';
          let textCol = 'text-[#d0d6e0]';
          let prefix = ' ';
          if (l.type === 'add') {
            bg = 'bg-emerald-500/15 text-emerald-300';
            prefix = '+';
          } else if (l.type === 'del') {
            bg = 'bg-rose-500/15 text-rose-300';
            prefix = '-';
          } else if (l.type === 'chunk') {
            bg = 'bg-brand/20 text-brand-light font-bold text-[10px]';
            prefix = '@';
          }
          html += `<div class="px-2 py-0.5 text-[11px] font-mono ${bg} ${textCol} whitespace-pre"><span class="select-none inline-block w-4 text-[#62666d]">${prefix}</span>${escapeHtml(l.text)}</div>`;
        });
        html += '</div>';
        diffBox.innerHTML = html;
      } catch (err) {
        diffBox.innerHTML = `<div class="text-xs text-rose-400 py-2">Error: ${escapeHtml(err.message)}</div>`;
      }
    }

    async function previewDiffForExtractedFile(idx) {
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${currentTaskId}`);
      const task = await res.json();
      const file = (task.extracted_files || [])[idx];
      if (!file) return;

      openDrawer('workspace');
      currentActiveDrawerFile = file.path;
      document.getElementById('drawerRuleFilename').innerText = file.path;
      document.getElementById('drawerAgentsTextarea').value = file.content;
      updateGutterLines();
      setDrawerEditorMode('diff');
      appendMiniTerminal(`> Previewing diff for extracted file: ${file.path}`);
    }

    function insertApprovalChip(text) {
      const el = document.getElementById('approvalFeedbackInput');
      if (el) {
        el.value = (el.value ? el.value.trim() + ' ' : '') + text;
        el.focus();
      }
    }

    async function checkActiveDrawerSyntax() {
      const ta = document.getElementById('drawerAgentsTextarea');
      const badge = document.getElementById('drawerSyntaxBadge');
      const relP = currentActiveDrawerFile || 'AGENTS.md';

      appendMiniTerminal(`> Checking syntax for: ${relP}...`);
      try {
        const res = await fetch('/api/workspace/check-syntax', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            rel_path: relP,
            content: ta.value
          })
        });
        const data = await res.json();
        if (data.valid) {
          if (badge) {
            badge.className = 'text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
            badge.innerText = 'Syntax Valid';
            badge.style.display = 'inline-block';
          }
          appendMiniTerminal(`✓ [VALID] ${data.message}`);
          showToast('Sintaks Valid (Bebas Error)', '✓');
        } else {
          if (badge) {
            badge.className = 'text-[10px] font-mono px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20';
            badge.innerText = `Line ${data.line || '?'}: Error`;
            badge.style.display = 'inline-block';
          }
          appendMiniTerminal(`✕ [ERROR] ${data.message}`);
          showToast(data.message, '✕');
        }
      } catch (e) {
        appendMiniTerminal(`✕ Syntax check failed: ${e.message}`);
      }
    }

    async function rollbackActiveDrawerFile() {
      const confirmed = await openConfirmDialog({
        title: 'Rollback Perubahan Codebase',
        message: 'Apakah Anda yakin ingin mengembalikan seluruh file yang baru saja diterapkan ke kondisi snapshot sebelumnya? File yang ada sebelum apply akan dipulihkan dari cadangan (.bak), dan file baru akan dihapus.',
        confirmText: 'Rollback Sekarang',
        danger: true
      });
      if (!confirmed) return;

      appendMiniTerminal('> Rolling back last applied changes...');
      try {
        const res = await fetch('/api/workspace/rollback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: currentWorkspace })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Gagal rollback');

        appendMiniTerminal(`✓ [ROLLBACK SUCCESS] ${data.restored_count} file berhasil dipulihkan.`);
        (data.restored || []).forEach(r => {
          appendMiniTerminal(`   ↺ ${r.path} (${r.action})`);
        });
        showToast(`Rollback sukses: ${data.restored_count} file dipulihkan`, '↺');
        if (currentActiveDrawerFile) {
          previewDrawerFile(currentActiveDrawerFile, true);
        }
        fetchDrawerGitStatus();
        await setWorkspace(currentWorkspace);
      } catch (e) {
        appendMiniTerminal(`✕ [ROLLBACK FAILED] ${e.message}`);
        showToast(e.message, '✕');
      }
    }

    function appendMiniTerminal(text) {
      const box = document.getElementById('drawerTerminalLogs');
      if (!box) return;
      const line = document.createElement('div');
      line.className = text.startsWith('✓') ? 'text-emerald-400' : (text.startsWith('✕') ? 'text-rose-400' : (text.startsWith('>') ? 'text-[#8a8f98]' : 'text-[#d0d6e0]'));
      line.innerText = text;
      box.appendChild(line);
      box.scrollTop = box.scrollHeight;
    }

    function clearMiniTerminal() {
      const box = document.getElementById('drawerTerminalLogs');
      if (box) box.innerHTML = '<div class="text-slate-500">> Console cleared.</div>';
    }

    let terminalHistory = [];
    let terminalHistoryIndex = -1;

    function handleTerminalInputKey(e) {
      if (e.key === 'Enter') {
        executeDrawerTerminal();
      } else if (e.key === 'ArrowUp') {
        if (terminalHistory.length > 0) {
          if (terminalHistoryIndex === -1) {
            terminalHistoryIndex = terminalHistory.length - 1;
          } else if (terminalHistoryIndex > 0) {
            terminalHistoryIndex--;
          }
          e.target.value = terminalHistory[terminalHistoryIndex] || '';
        }
      } else if (e.key === 'ArrowDown') {
        if (terminalHistoryIndex !== -1) {
          if (terminalHistoryIndex < terminalHistory.length - 1) {
            terminalHistoryIndex++;
            e.target.value = terminalHistory[terminalHistoryIndex] || '';
          } else {
            terminalHistoryIndex = -1;
            e.target.value = '';
          }
        }
      }
    }

    async function executeDrawerTerminal() {
      const input = document.getElementById('drawerTerminalInput');
      if (!input) return;
      const cmd = (input.value || '').trim();
      if (!cmd) return;

      terminalHistory.push(cmd);
      terminalHistoryIndex = -1;
      input.value = '';

      appendMiniTerminal(`$ ${cmd}`);
      try {
        const res = await fetch('/api/workspace/terminal', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: cmd, path: currentWorkspace })
        });
        const data = await res.json();
        if (!res.ok) {
          appendMiniTerminal(`✕ [EXIT ${res.status}] ${data.detail || 'Command failed'}`);
          return;
        }
        if (data.stdout) {
          const lines = data.stdout.split('\n');
          lines.forEach(l => {
            if (l.trim() || lines.length === 1) appendMiniTerminal(`  ${l}`);
          });
        }
        if (data.stderr) {
          const errLines = data.stderr.split('\n');
          errLines.forEach(l => {
            if (l.trim() || errLines.length === 1) appendMiniTerminal(`! ${l}`);
          });
        }
        const statusPrefix = data.exit_code === 0 ? '✓' : '✕';
        appendMiniTerminal(`${statusPrefix} [Exit: ${data.exit_code} | ${data.duration_ms}ms | cwd: ${data.cwd}]`);
        if (cmd.startsWith('git ')) {
          fetchDrawerGitStatus();
        }
      } catch (e) {
        appendMiniTerminal(`✕ Error executing command: ${e.message}`);
      }
    }

    async function fetchDrawerGitStatus() {
      appendMiniTerminal(`> git status (${currentWorkspace})...`);
      try {
        const res = await fetch(`/api/workspace/git/status?path=${encodeURIComponent(currentWorkspace)}`);
        const data = await res.json();
        const git = data.git || data;
        const badge = document.getElementById('drawerGitBranchBadge');
        if (git.is_git) {
          if (badge) badge.innerText = `[${git.branch || 'HEAD'}]`;
          if (git.status_lines && git.status_lines.length) {
            git.status_lines.forEach(l => appendMiniTerminal(`  ${l}`));
          } else {
            appendMiniTerminal('  Working tree clean (tidak ada perubahan uncommitted).');
          }
        } else {
          if (badge) badge.innerText = '';
          appendMiniTerminal('  (Bukan repositori git)');
        }
      } catch (e) {
        appendMiniTerminal(`  Error: ${e.message}`);
      }
    }

    async function fetchDrawerGitDiff() {
      appendMiniTerminal(`> git diff (${currentWorkspace})...`);
      try {
        const res = await fetch(`/api/workspace/git/diff?path=${encodeURIComponent(currentWorkspace)}`);
        const data = await res.json();
        if (!res.ok) {
          appendMiniTerminal(`✕ ${data.detail || 'Git diff failed'}`);
          return;
        }
        if (!data.is_git) {
          appendMiniTerminal('  (Bukan repositori git)');
          return;
        }
        if (data.is_empty) {
          appendMiniTerminal('  Tidak ada perubahan diff uncommitted (working tree bersih).');
          return;
        }
        appendMiniTerminal(`✓ Diff (${data.files_changed} file berubah):`);
        const lines = data.raw_diff.split('\n');
        lines.slice(0, 100).forEach(l => appendMiniTerminal(`  ${l}`));
        if (lines.length > 100) {
          appendMiniTerminal(`  ... (${lines.length - 100} baris diff lainnya dipotong)`);
        }
      } catch (e) {
        appendMiniTerminal(`  Error: ${e.message}`);
      }
    }

    /* ================= WORKSTATION PROJECT SESSIONS ================= */
    let workstationSessions = [];
    let activeWorkstationSessionId = 'default';

    async function fetchWorkstationSessions() {
      try {
        const res = await fetch('/api/workstation/sessions');
        const data = await res.json();
        workstationSessions = data.sessions || [];
        renderWorkstationSessionsUI();
      } catch (e) {
        console.error('Fetch workstation sessions error:', e);
      }
    }

    function renderWorkstationSessionsUI() {
      const active = workstationSessions.find(s => s.id === activeWorkstationSessionId) || workstationSessions[0];
      const label = document.getElementById('topbarSessionLabel');
      if (label && active) {
        label.innerText = active.title;
      }
      const badge = document.getElementById('wsSessionCountBadge');
      if (badge) {
        badge.innerText = `${workstationSessions.length} Sessions`;
      }

      const container = document.getElementById('wsSessionsListContainer');
      if (!container) return;

      if (!workstationSessions.length) {
        container.innerHTML = '<div class="text-center py-6 text-xs text-[#8a8f98]">Belum ada sesi proyek.</div>';
        return;
      }

      container.innerHTML = workstationSessions.map(s => {
        const isActive = s.id === activeWorkstationSessionId;
        const taskCount = (s.task_ids || []).length;
        const dateStr = new Date(s.updated_at * 1000).toLocaleDateString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});

        return `
          <div onclick="switchWorkstationSession('${s.id}')" class="p-2.5 rounded-lg border ${isActive ? 'border-brand bg-brand/10' : 'border-subtle bg-white/[0.02] hover:bg-white/[0.04]'} cursor-pointer transition flex items-center justify-between group">
            <div class="min-w-0 pr-2">
              <div class="flex items-center gap-1.5">
                <span class="text-xs font-semibold ${isActive ? 'text-brand-light' : 'text-white'} truncate">${escapeHtml(s.title)}</span>
                ${s.pinned ? '<span class="text-[9px] text-amber-400 font-mono">PINNED</span>' : ''}
                ${isActive ? '<span class="text-[9px] px-1.5 py-0.2 rounded bg-brand/30 text-white font-mono">ACTIVE</span>' : ''}
              </div>
              <div class="text-[10px] text-[#8a8f98] font-mono mt-0.5 truncate">
                ${taskCount} tasks · diperbarui ${dateStr}
              </div>
            </div>
            <div class="flex items-center gap-1.5 opacity-0 group-hover:opacity-100 transition shrink-0" onclick="event.stopPropagation()">
              <button onclick="renameWorkstationSessionPrompt('${s.id}')" class="p-1 rounded text-[#8a8f98] hover:text-white text-xs" title="Ganti Nama">✎</button>
              <button onclick="togglePinWorkstationSession('${s.id}')" class="p-1 rounded ${s.pinned ? 'text-amber-400' : 'text-[#8a8f98] hover:text-white'} text-xs" title="${s.pinned ? 'Lepas Pin' : 'Pin Sesi'}">★</button>
              ${s.id !== 'default' ? `<button onclick="deleteWorkstationSessionPrompt('${s.id}')" class="p-1 rounded text-[#8a8f98] hover:text-rose-400 text-xs" title="Hapus Sesi">✕</button>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }

    function openWorkstationSessionsModal() {
      fetchWorkstationSessions();
      const modal = document.getElementById('workstationSessionsModal');
      if (modal) modal.style.display = 'flex';
    }

    function closeWorkstationSessionsModal() {
      const modal = document.getElementById('workstationSessionsModal');
      if (modal) modal.style.display = 'none';
    }

    function switchWorkstationSession(id) {
      activeWorkstationSessionId = id;
      renderWorkstationSessionsUI();
      closeWorkstationSessionsModal();
      const s = workstationSessions.find(item => item.id === id);
      if (s) {
        showToast(`Beralih ke sesi: ${s.title}`, '✓');
        newDraftTask();
        fetchTasks();
      }
    }

    async function createNewSessionPrompt() {
      const title = await openInputDialog({
        title: 'Buat Sesi Proyek Baru',
        message: 'Nama sesi proyek (multi-turn workstream):',
        placeholder: 'Refactor Auth & JWT, Integrasi Payment, dll.',
        confirmText: 'Buat Sesi'
      });
      if (!title || !title.trim()) return;

      try {
        const res = await fetch('/api/workstation/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: title.trim(),
            working_directory: currentWorkspace
          })
        });
        const s = await res.json();
        activeWorkstationSessionId = s.id;
        await fetchWorkstationSessions();
        closeWorkstationSessionsModal();
        newDraftTask();
        showToast(`Sesi "${s.title}" berhasil dibuat`, '✓');
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function renameWorkstationSessionPrompt(id) {
      const s = workstationSessions.find(item => item.id === id);
      if (!s) return;
      const newTitle = await openInputDialog({
        title: 'Ubah Nama Sesi',
        message: 'Masukkan nama baru untuk sesi ini:',
        defaultValue: s.title,
        confirmText: 'Simpan Nama'
      });
      if (!newTitle || !newTitle.trim()) return;

      try {
        await fetch(`/api/workstation/sessions/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle.trim() })
        });
        await fetchWorkstationSessions();
        showToast('Nama sesi diperbarui', '✓');
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function togglePinWorkstationSession(id) {
      const s = workstationSessions.find(item => item.id === id);
      if (!s) return;
      try {
        await fetch(`/api/workstation/sessions/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pinned: !s.pinned })
        });
        await fetchWorkstationSessions();
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    async function deleteWorkstationSessionPrompt(id) {
      if (id === 'default') {
        showToast('Sesi default tidak dapat dihapus', '⚠️');
        return;
      }
      const confirmed = await openConfirmDialog({
        title: 'Hapus Sesi Proyek',
        message: 'Apakah Anda yakin ingin menghapus sesi proyek ini dari daftar?',
        confirmText: 'Hapus Sesi',
        danger: true
      });
      if (!confirmed) return;

      try {
        await fetch(`/api/workstation/sessions/${id}`, { method: 'DELETE' });
        if (activeWorkstationSessionId === id) {
          activeWorkstationSessionId = 'default';
        }
        await fetchWorkstationSessions();
        showToast('Sesi berhasil dihapus', '✓');
      } catch (e) {
        showToast(e.message, '✕');
      }
    }

    init();

