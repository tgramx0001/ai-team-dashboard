// =====================================================================
    // PHASE 2 — HERMES CHAT (Level 1: User ↔ Hermes, POST + ReadableStream)
    // NOTE: Native EventSource is GET-only. We use fetch() + ReadableStream
    //       to consume SSE from POST /api/chat — this is the correct approach.
    // =====================================================================

    let chatOpen = false;
    let chatSessionId = null;
    let chatStreaming = false;

    function toggleChat() {
      chatOpen ? closeChat() : openChat();
    }

    function openChat() {
      chatOpen = true;
      document.getElementById('chatDrawer').style.transform = 'translateX(0)';
      if (!chatSessionId) chatNewSession();
      else loadChatMessages(chatSessionId);
      setTimeout(() => document.getElementById('chatInput')?.focus(), 200);
    }

    function closeChat() {
      chatOpen = false;
      document.getElementById('chatDrawer').style.transform = 'translateX(100%)';
    }

    const AGENT_ICONS = {
      'Hermes': '⚡', 'Researcher': '🔍', 'Coder': '⚡', 'Critic': '🧐',
      'QA': '🛡️', 'Tutor': '🎓', 'Data Analyst': '📊', 'Architect': '📐',
      'UI/UX': '🎨', 'Security': '🔍', 'Writer': '✍️', 'Planner': '📋', 'Reviewer': '🎯'
    };

    function renderActiveTeamBadges(agents) {
      const container = document.getElementById('chatActiveTeamBadges');
      if (!container) return;
      const list = agents && agents.length ? agents : ['Hermes'];
      container.innerHTML = '<span class="text-[10px] text-[#8a8f98]">Tim:</span>' + list.map(a => `
        <span class="px-1.5 py-0.5 rounded text-[10px] bg-brand/20 text-brand-light font-medium border border-brand/30 flex items-center gap-1 shrink-0">
          <span>${AGENT_ICONS[a] || '🤖'}</span>
          <span>${escapeHtml(a)}</span>
        </span>
      `).join('');
    }

    async function chatNewSession() {
      const targetAgent = document.getElementById('chatTargetAgent')?.value || 'Hermes';
      const res = await fetch('/api/chat/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: 'New Chat', active_agents: [targetAgent] })
      });
      if (!res.ok) { showToast('Gagal membuat sesi', '✕'); return; }
      const data = await res.json();
      chatSessionId = data.session.id;
      document.getElementById('chatSessionLabel').textContent = data.session.title + ' · ' + chatSessionId;
      document.getElementById('chatModelLabel').textContent = activeModelName || '—';
      document.getElementById('chatMessages').innerHTML = '';
      renderActiveTeamBadges(data.session.active_agents || ['Hermes']);
    }

    async function loadChatMessages(sessId) {
      const sessRes = await fetch(`/api/chat/sessions/${sessId}/agents`);
      if (sessRes.ok) {
        const sessData = await sessRes.json();
        renderActiveTeamBadges(sessData.active_agents || ['Hermes']);
      }

      const res = await fetch(`/api/chat/sessions/${sessId}/messages`);
      if (!res.ok) return;
      const data = await res.json();
      const container = document.getElementById('chatMessages');
      container.innerHTML = '';
      for (const m of (data.messages || [])) {
        appendChatBubble(m.role, m.content, false, m.agent);
      }
      container.scrollTop = container.scrollHeight;
    }

    function appendChatBubble(role, content, streaming, agent) {
      const container = document.getElementById('chatMessages');
      const isUser = role === 'user';
      const id = streaming ? 'chatStreamBubble' : undefined;
      const div = document.createElement('div');
      div.className = `flex ${isUser ? 'justify-end' : 'justify-start'}`;
      if (id) div.id = id;
      const bubble = document.createElement('div');
      bubble.className = isUser
        ? 'max-w-[80%] rounded-xl bg-brand/20 border border-brand/30 text-[#e8ebf0] text-xs px-3 py-2 whitespace-pre-wrap leading-relaxed'
        : 'max-w-[90%] rounded-xl bg-white/[0.04] border border-subtle text-[#e8ebf0] text-xs px-3 py-2 leading-relaxed prose-dark';
      
      if (isUser) {
        bubble.textContent = content;
      } else {
        const agentName = agent || 'Hermes';
        const icon = AGENT_ICONS[agentName] || '🤖';
        const header = `<div class="text-[10px] text-brand-light mb-1 flex items-center gap-1 font-mono font-semibold select-none"><span>${icon}</span><span>${escapeHtml(agentName)}</span></div>`;
        bubble.innerHTML = header + mdRender(content);
      }
      div.appendChild(bubble);
      container.appendChild(div);
      container.scrollTop = container.scrollHeight;
      return bubble;
    }

    async function chatSend() {
      if (chatStreaming) return;
      const input = document.getElementById('chatInput');
      const msg = (input.value || '').trim();
      if (!msg) return;

      if (!chatSessionId) await chatNewSession();

      input.value = '';
      input.disabled = true;
      document.getElementById('chatSendBtn').disabled = true;
      document.getElementById('chatStreamStatus').classList.remove('hidden');
      chatStreaming = true;

      // Show user bubble immediately
      appendChatBubble('user', msg, false);

      // Create streaming assistant bubble
      const container = document.getElementById('chatMessages');
      const streamDiv = document.createElement('div');
      streamDiv.id = 'chatStreamBubble';
      streamDiv.className = 'flex justify-start';
      const streamBubble = document.createElement('div');
      streamBubble.className = 'max-w-[90%] rounded-xl bg-white/[0.04] border border-subtle text-[#e8ebf0] text-xs px-3 py-2 leading-relaxed prose-dark';
      streamBubble.textContent = '…';
      streamDiv.appendChild(streamBubble);
      container.appendChild(streamDiv);
      container.scrollTop = container.scrollHeight;

      const useWs = document.getElementById('chatUseWorkspace')?.checked;
      const targetAgent = document.getElementById('chatTargetAgent')?.value || 'Hermes';
      const body = {
        message: msg,
        session_id: chatSessionId,
        workspace_root: useWs ? currentWorkspace : null,
        model: activeModelName || null,
        agent: targetAgent,
      };

      try {
        const token = localStorage.getItem('ai_team_auth_token');
        const headers = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = 'Bearer ' + token;

        let currentAgent = targetAgent;
        let currentIcon = AGENT_ICONS[targetAgent] || '🤖';

        // POST + ReadableStream SSE (native EventSource is GET-only, cannot do POST)
        const res = await _nativeFetch('/api/chat', { method: 'POST', headers, body: JSON.stringify(body) });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          streamBubble.textContent = '✕ ' + (err.detail || 'Gagal menghubungi server');
          streamDiv.querySelector('div').className = streamDiv.querySelector('div').className + ' text-rose-300';
        } else {
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let accumulated = '';
          let buf = '';

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split('\n');
            buf = lines.pop(); // hold partial line
            for (const line of lines) {
              if (!line.startsWith('data: ')) continue;
              const dataStr = line.slice(6).trim();
              if (!dataStr) continue;
              try {
                const evt = JSON.parse(dataStr);
                if (evt.type === 'agent.started') {
                  currentAgent = evt.agent;
                  currentIcon = evt.icon || AGENT_ICONS[evt.agent] || '🤖';
                  document.getElementById('chatStreamStatus').textContent = currentIcon + ' ' + currentAgent + ' sedang berpikir…';
                  const sel = document.getElementById('chatTargetAgent');
                  if (sel && sel.querySelector(`option[value="${evt.agent}"]`)) sel.value = evt.agent;
                } else if (evt.chunk) {
                  accumulated += evt.chunk;
                  const header = `<div class="text-[10px] text-brand-light mb-1 flex items-center gap-1 font-mono font-semibold select-none"><span>${currentIcon}</span><span>${escapeHtml(currentAgent)}</span></div>`;
                  streamBubble.innerHTML = header + mdRender(accumulated);
                  container.scrollTop = container.scrollHeight;
                } else if (evt.done) {
                  // final event — model & active agents update
                  document.getElementById('chatModelLabel').textContent = evt.model || activeModelName || '—';
                  if (evt.active_agents) renderActiveTeamBadges(evt.active_agents);
                } else if (evt.error) {
                  streamBubble.textContent = '✕ ' + evt.error;
                }
              } catch (_) {}
            }
          }
        }
      } catch (e) {
        streamBubble.textContent = '✕ ' + e.message;
      } finally {
        streamDiv.id = '';
        chatStreaming = false;
        input.disabled = false;
        document.getElementById('chatSendBtn').disabled = false;
        document.getElementById('chatStreamStatus').classList.add('hidden');
        input.focus();
      }
    }

    // Ctrl+Enter / Cmd+Enter to send
    document.getElementById('chatInput')?.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); chatSend(); }
    });

    async function openChatSessionsPicker() {
      document.getElementById('chatSessionsPicker').classList.remove('hidden');
      const res = await fetch('/api/chat/sessions');
      if (!res.ok) return;
      const data = await res.json();
      const list = document.getElementById('chatSessionsList');
      if (!data.sessions?.length) {
        list.innerHTML = '<div class="text-center py-6 text-xs text-[#8a8f98]">Belum ada riwayat sesi.</div>';
        return;
      }
      list.innerHTML = data.sessions.map(s => `
        <div class="flex items-center justify-between p-2 rounded-lg hover:bg-white/[0.04] cursor-pointer group" data-id="${escapeHtml(s.id)}" data-title="${escapeHtml(s.title)}" onclick="selectChatSession(this.dataset.id, this.dataset.title)">
          <div class="overflow-hidden">
            <div class="text-xs text-[#d0d6e0] truncate font-medium">${escapeHtml(s.title)}</div>
            <div class="text-[10px] font-mono text-[#62666d]">${s.id}</div>
          </div>
          <button data-id="${escapeHtml(s.id)}" onclick="event.stopPropagation();deleteChatSession(this.dataset.id)" class="text-[10px] text-rose-400 opacity-0 group-hover:opacity-100 px-1.5 py-0.5 rounded hover:bg-rose-500/10 ml-2 shrink-0">del</button>
        </div>`).join('');
    }

    function closeChatSessionsPicker() {
      document.getElementById('chatSessionsPicker').classList.add('hidden');
    }

    async function selectChatSession(id, title) {
      chatSessionId = id;
      document.getElementById('chatSessionLabel').textContent = title + ' · ' + id;
      closeChatSessionsPicker();
      await loadChatMessages(id);
    }

    async function deleteChatSession(id) {
      const ok = await openConfirmDialog({ title: 'Hapus Sesi', message: 'Hapus sesi chat ini beserta semua pesannya?', danger: true });
      if (!ok) return;
      const res = await fetch(`/api/chat/sessions/${id}`, { method: 'DELETE' });
      if (!res.ok) { showToast('Gagal hapus sesi', '✕'); return; }
      if (chatSessionId === id) { chatSessionId = null; document.getElementById('chatMessages').innerHTML = ''; }
      showToast('Sesi dihapus', '✓');
      await openChatSessionsPicker();
    }
