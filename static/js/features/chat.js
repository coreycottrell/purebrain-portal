(function(){
'use strict';
  // ===== AUTH & STATE =====
  var token = localStorage.getItem('portal_token') || '';
  var chatWs = null;
  var chatWsReconnectTimeout = null;
  var _wsReconnectDelay = 2000;
  var knownMsgIds = new Set();
  var lastSentOptimisticText = null;
  var lastSentRawText = null;
  var civName = 'AI';  // Default; updated dynamically from /api/status endpoint

  var sendBtn = document.querySelector('.c-send-circle');
  var chatInput = document.getElementById('composerInput');
  var chatMsgs = document.querySelector('.chat-messages');

  if (!sendBtn || !chatInput || !chatMsgs) return;
  if (chatMsgs) chatMsgs.innerHTML = '<div class="chat-loading">Connecting...</div>';

  // ===== HELPER FUNCTIONS =====
  // safeJson: use global _safeJson from auth.js (identical implementation)
  var safeJson = _safeJson;

  function formatTime(ts) {
    if (!ts) return '';
    var d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}).toLowerCase();
  }

  function renderMarkdown(text) {
    var s = escHtml(text);
    // Code blocks
    s = s.replace(/```([a-z]*)\n?([\s\S]*?)```/g, function(_, lang, code) {
      return '<pre><code>' + code.trim() + '</code></pre>';
    });
    // Inline code
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    // Headings
    s = s.replace(/^###### (.+)$/gm, '<h6>$1</h6>');
    s = s.replace(/^##### (.+)$/gm,  '<h5>$1</h5>');
    s = s.replace(/^#### (.+)$/gm,   '<h4>$1</h4>');
    s = s.replace(/^### (.+)$/gm,    '<h3>$1</h3>');
    s = s.replace(/^## (.+)$/gm,     '<h2>$1</h2>');
    s = s.replace(/^# (.+)$/gm,      '<h1>$1</h1>');
    // Bold
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_\n]+)__/g,     '<strong>$1</strong>');
    // Italic
    s = s.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    // Links
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(match, text, url) {
      var u = url.trim().toLowerCase();
      if (u.startsWith('javascript:') || u.startsWith('data:') || u.startsWith('vbscript:')) {
        return match; // strip dangerous schemes, return text as-is
      }
      if (u.startsWith('http://') || u.startsWith('https://') || u.startsWith('/') || u.startsWith('mailto:')) {
        return '<a href="' + url + '" target="_blank" rel="noopener">' + text + '</a>';
      }
      return text + ' (' + url + ')';
    });
    // Auto-link URLs
    s = (function(html) {
      var URL_RE = /(https?:\/\/[^\s<>"')\]]+)/g;
      var parts = html.split(/(<a[\s\S]*?<\/a>)/gi);
      return parts.map(function(part, i) {
        if (i % 2 === 1) return part;
        return part.replace(URL_RE, function(url) {
          return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
        });
      }).join('');
    })(s);
    // Unordered lists
    s = s.replace(/^[ \t]*[-*] (.+)$/gm, '<li>$1</li>');
    s = s.replace(/(<li>[^]*?<\/li>(\n|$))+/g, function(m) {
      return '<ul>' + m + '</ul>';
    });
    // Paragraphs
    var parts = s.split(/\n\n+/);
    s = parts.map(function(p) {
      p = p.trim();
      if (!p) return '';
      if (/^<(h[1-6]|ul|ol|pre|li)/.test(p)) return p;
      return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
    }).filter(Boolean).join('\n');
    return s;
  }

  // Add copy buttons to code blocks
  function addCodeCopyButtons(containerEl) {
    var pres = containerEl.querySelectorAll('pre');
    pres.forEach(function(pre) {
      if (pre.querySelector('.code-copy-btn')) return;
      var btn = document.createElement('button');
      btn.className = 'code-copy-btn';
      btn.textContent = 'Copy';
      // Style handled by .code-copy-btn CSS class
      pre.style.position = 'relative';
      btn.addEventListener('click', function() {
        var code = pre.querySelector('code');
        var text = code ? code.textContent : pre.textContent;
        navigator.clipboard.writeText(text).then(function() {
          btn.textContent = 'Copied!';
          setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
        });
      });
      pre.appendChild(btn);
    });
  }

  // ===== AGENT IDENTITY DETECTION =====
  // Known agent name suffixes for classification
  var _leadSuffixes = ['-lead'];
  var _specSuffixes = ['-specialist','-auditor','-architect','-researcher','-tester','-optimizer','-curator','-designer','-synthesizer','-resolver','-detector','-archaeologist','-strategist','-liaison','-expert'];
  var _knownAgents = ['coder','reviewer','tester','planner','writer','editor','deployer','monitor'];

  function _classifyAgent(name) {
    var n = (name || '').toLowerCase();
    for (var i = 0; i < _leadSuffixes.length; i++) {
      if (n.indexOf(_leadSuffixes[i]) !== -1) return { type: 'team-lead', badge: 'Team Lead' };
    }
    for (var i = 0; i < _specSuffixes.length; i++) {
      if (n.indexOf(_specSuffixes[i]) !== -1) return { type: 'specialist', badge: 'Specialist' };
    }
    if (n === 'system') return { type: 'system', badge: 'System' };
    return { type: 'specialist', badge: 'Agent' };
  }

  function detectAgentIdentity(text, agentContext) {
    if (!text && !agentContext) return null;

    // 1. Server-provided agent_context (from JSONL tool_use parsing)
    if (agentContext) {
      var ctxName = agentContext.agent || agentContext.target;
      if (ctxName) {
        var cls = _classifyAgent(ctxName);
        return { type: cls.type, name: ctxName, badge: cls.badge, strip: '', fromContext: true };
      }
    }

    if (!text) return null;
    var first300 = text.substring(0, 300);

    // 2. Explicit bracket prefix: [web-lead], [coder], [system]
    var bracketMatch = first300.match(/^\[([a-z0-9][a-z0-9-]{1,28}[a-z0-9])\]/i);
    if (bracketMatch) {
      var cls = _classifyAgent(bracketMatch[1]);
      return { type: cls.type, name: bracketMatch[1], badge: cls.badge, strip: bracketMatch[0] };
    }

    // 3. Bold agent name at message start: **web-lead**: or **web-lead** reports
    var boldMatch = first300.match(/^\*\*([a-z0-9][a-z0-9-]{1,28}[a-z0-9])\*\*\s*[:—\-]/i);
    if (boldMatch) {
      var n = boldMatch[1].toLowerCase();
      // Only match if it looks like an agent name (has a suffix or is a known agent)
      var looksLikeAgent = _knownAgents.indexOf(n) !== -1;
      if (!looksLikeAgent) {
        for (var i = 0; i < _leadSuffixes.concat(_specSuffixes).length; i++) {
          if (n.indexOf(_leadSuffixes.concat(_specSuffixes)[i]) !== -1) { looksLikeAgent = true; break; }
        }
      }
      if (looksLikeAgent) {
        var cls = _classifyAgent(n);
        return { type: cls.type, name: boldMatch[1], badge: cls.badge, strip: '' };
      }
    }

    // 4. @mention at start: @web-lead ...
    var atMatch = first300.match(/^@([a-z0-9][a-z0-9-]{1,28}[a-z0-9])\b/i);
    if (atMatch) {
      var n = atMatch[1].toLowerCase();
      var looksLikeAgent = _knownAgents.indexOf(n) !== -1;
      if (!looksLikeAgent) {
        for (var i = 0; i < _leadSuffixes.concat(_specSuffixes).length; i++) {
          if (n.indexOf(_leadSuffixes.concat(_specSuffixes)[i]) !== -1) { looksLikeAgent = true; break; }
        }
      }
      if (looksLikeAgent) {
        var cls = _classifyAgent(n);
        return { type: cls.type, name: atMatch[1], badge: cls.badge, strip: atMatch[0] };
      }
    }

    // 5. Teammate message blocks: "From web-lead:" or "web-lead says:"
    var fromMatch = first300.match(/^(?:From|Via|Reply from)\s+([a-z0-9][a-z0-9-]{1,28}[a-z0-9])\s*:/i);
    if (fromMatch) {
      var cls = _classifyAgent(fromMatch[1]);
      return { type: cls.type, name: fromMatch[1], badge: cls.badge, strip: '' };
    }

    // 6. System messages
    if (/^\[system\]/i.test(first300)) return { type: 'system', name: 'System', badge: 'System', strip: first300.match(/^\[system\]/i)[0] };

    return null;
  }

  // ===== MESSAGE RENDERING =====
  // Build a message DOM element matching Vortex CSS structure
  // ===== PORTAL_FILE card support =====
  // Backend emits [PORTAL_FILE:stored_name:display_name] (portal_server.py) for
  // delivered files and serves them token-authed at /api/chat/uploads/{stored}.
  // stored_name is colon-free ([A-Za-z0-9._-]); display_name MAY contain spaces
  // and colons. So we split on the FIRST colon AFTER the "PORTAL_FILE:" prefix.
  // Pure function (no DOM / no globals) so it is unit-testable in isolation.
  function parsePortalFile(text) {
    if (!text || typeof text !== 'string') return null;
    var m = text.match(/\[PORTAL_FILE:([^\]]+)\]/);
    if (!m) return null;
    var inner = m[1]; // "stored_name:display_name(may contain colons/spaces)"
    var sep = inner.indexOf(':'); // FIRST colon = stored/display boundary
    if (sep === -1) return null; // malformed: no display_name separator
    var storedName = inner.slice(0, sep);
    var displayName = inner.slice(sep + 1);
    if (!storedName || !displayName) return null;
    var ext = (storedName.split('.').pop() || '').toLowerCase();
    var kind;
    if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].indexOf(ext) !== -1) {
      kind = 'image';
    } else if (ext === 'pdf') {
      kind = 'pdf';
    } else if (['md', 'markdown', 'txt', 'text', 'log', 'csv', 'json'].indexOf(ext) !== -1) {
      kind = 'text';
    } else {
      kind = 'file';
    }
    return {
      raw: m[0],
      storedName: storedName,
      displayName: displayName,
      ext: ext,
      kind: kind,
      // Index of the tag within the source text (for caption extraction).
      index: m.index
    };
  }

  // Build the token-authed serve URL. Token comes from localStorage (CIV-agnostic).
  function _portalFileUrl(storedName) {
    var tok = localStorage.getItem('portal_token') || token || '';
    return '/api/chat/uploads/' + encodeURIComponent(storedName) +
           '?token=' + encodeURIComponent(tok);
  }

  // Render an on-brand file card element (preview + download), matching the
  // existing inline-image styling used elsewhere in chat.
  function renderPortalFileCard(info) {
    var url = _portalFileUrl(info.storedName);
    var card = document.createElement('div');
    card.className = 'portal-file-card';

    // Inline preview by kind.
    if (info.kind === 'image') {
      var img = document.createElement('img');
      img.className = 'msg-inline-img portal-file-preview';
      img.src = url;
      img.alt = info.displayName;
      img.addEventListener('click', function() { window.open(url, '_blank'); });
      img.addEventListener('error', function() { img.style.display = 'none'; });
      card.appendChild(img);
    } else if (info.kind === 'pdf') {
      var frame = document.createElement('iframe');
      frame.className = 'portal-file-preview portal-file-pdf';
      frame.src = url;
      frame.setAttribute('title', info.displayName);
      frame.setAttribute('loading', 'lazy');
      card.appendChild(frame);
    } else if (info.kind === 'text') {
      var pre = document.createElement('pre');
      pre.className = 'portal-file-preview portal-file-text';
      pre.textContent = 'Loading preview…';
      fetch(url).then(function(r) {
        if (!r.ok) throw new Error('preview fetch failed');
        return r.text();
      }).then(function(body) {
        // Cap preview length to keep the card light.
        var capped = body.length > 4000 ? body.slice(0, 4000) + '\n…' : body;
        pre.textContent = capped;
      }).catch(function() {
        pre.textContent = '(Preview unavailable — use Download below.)';
      });
      card.appendChild(pre);
    }

    // File meta row: name + kind label.
    var meta = document.createElement('div');
    meta.className = 'portal-file-meta';
    var nameEl = document.createElement('span');
    nameEl.className = 'portal-file-name';
    nameEl.textContent = info.displayName;
    meta.appendChild(nameEl);
    card.appendChild(meta);

    // Download affordance (token-authed). `download` hints the displayName.
    var dl = document.createElement('a');
    dl.className = 'portal-file-download';
    dl.href = url;
    dl.setAttribute('download', info.displayName);
    dl.setAttribute('rel', 'noopener');
    dl.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
      '<path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '<span>Download</span>';
    card.appendChild(dl);

    return card;
  }

  function buildMessageEl(text, role, ts, id, agentContext, replyMeta) {
    var msgId = id || ('local-' + Date.now() + '-' + Math.random());
    var timeStr = ts ? formatTime(ts) : formatTime(Math.floor(Date.now() / 1000));
    var senderName = role === 'user' ? 'You' : civName;
    var cssClass = role === 'user' ? 'msg msg-user' : 'msg msg-ai';

    // Detect agent identity for AI messages
    var agentInfo = null;
    var displayText = text || '';
    if (role !== 'user' && role !== 'thinking') {
      agentInfo = detectAgentIdentity(displayText, agentContext);
      if (agentInfo) {
        senderName = agentInfo.name;
        if (agentInfo.strip) {
          displayText = displayText.substring(agentInfo.strip.length).replace(/^\s*/, '');
        }
      }
    }

    var div = document.createElement('div');
    div.className = cssClass;
    div.setAttribute('data-id', msgId);
    div.setAttribute('data-ts', ts || Math.floor(Date.now() / 1000));

    var avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    if (role === 'user') {
      avatar.textContent = 'You';
    } else if (agentInfo) {
      // Color-coded avatar based on agent type — styles via CSS classes
      var _avatarType = agentInfo.type === 'team-lead' ? 'team-lead' : agentInfo.type === 'specialist' ? 'specialist' : agentInfo.type === 'system' ? 'system' : 'primary';
      avatar.className = 'msg-avatar msg-avatar-agent type-' + _avatarType;
      avatar.textContent = senderName.charAt(0).toUpperCase();
    } else {
      var avatarImg = document.createElement('img');
      avatarImg.src = 'https://purebrain.ai/wp-content/uploads/2026/02/MA1.BI-1.2.4-002-211107-Icon-PT.png';
      avatarImg.alt = civName;
      avatarImg.style.cssText = 'width:100%;height:100%;object-fit:contain;border-radius:50%';
      avatar.appendChild(avatarImg);
    }

    var content = document.createElement('div');
    content.className = 'msg-content';

    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    if (role === 'thinking') {
      div.className = 'msg msg-ai thinking';
      // Full text stored for expand/collapse; CSS handles opacity/italic/truncation
      bubble.textContent = displayText;
      // Click to expand/collapse thinking messages
      div.addEventListener('click', function() {
        this.classList.toggle('expanded');
      });
    } else {
      // Check for [Image: ...] pattern — render inline image
      // Format from portal: [Image: display_name — USE Read tool on /path/stored_name TO VIEW]
      // Format from addFileImage: [Image: display_name]
      var imgMatch = displayText.match(/\[Image:\s*([^\]]+)\]/);
      if (imgMatch && role === 'user') {
        var imgRaw = imgMatch[1].trim();
        var imgFilename;
        // Extract stored filename from portal upload notification
        var pathMatch = imgRaw.match(/\/([^\/]+)\s+TO VIEW$/);
        if (pathMatch) {
          imgFilename = pathMatch[1]; // stored filename from path
        } else {
          imgFilename = imgRaw; // simple [Image: filename] format
        }
        var captionText = displayText.replace(/\[Image:\s*[^\]]+\]\n?/, '').trim();
        // Strip portal upload notification wrapper to get just the user's caption
        var instrMatch = captionText.match(/INSTRUCTIONS from [^:]+:\s*(.*)/s);
        if (instrMatch) captionText = instrMatch[1].trim();
        // Also strip the "[Portal Upload...]" and "File saved to: ..." prefix
        captionText = captionText.replace(/^\[Portal Upload[^\]]*\]\s*/i, '').replace(/^File saved to:\s*\S+\s*/i, '').trim();
        if (captionText) {
          bubble.innerHTML = renderMarkdown(captionText);
        }
        var inlineImg = document.createElement('img');
        inlineImg.className = 'msg-inline-img';
        inlineImg.src = '/api/chat/uploads/' + encodeURIComponent(imgFilename) + '?token=' + encodeURIComponent(token);
        inlineImg.alt = imgFilename;
        inlineImg.addEventListener('click', function() { window.open(inlineImg.src, '_blank'); });
        inlineImg.addEventListener('error', function() { inlineImg.style.display = 'none'; });
        bubble.appendChild(inlineImg);
      } else {
        // Check for [PORTAL_FILE:stored_name:display_name] — delivered file card.
        var pf = parsePortalFile(displayText);
        if (pf) {
          // Render any caption text surrounding the tag as markdown, then the card.
          var captionPF = displayText.replace(pf.raw, '').trim();
          if (captionPF) {
            bubble.innerHTML = renderMarkdown(captionPF);
            if (role === 'assistant' || role === 'ai') addCodeCopyButtons(bubble);
          }
          bubble.appendChild(renderPortalFileCard(pf));
        } else {
          bubble.innerHTML = renderMarkdown(displayText);
          if (role === 'assistant' || role === 'ai') {
            addCodeCopyButtons(bubble);
          }
        }
      }
    }

    // Render clickable reply-to header if this message is a reply
    if (replyMeta && replyMeta.id) {
      var replyLink = document.createElement('button');
      replyLink.className = 'msg-reply-link';
      var parentInDom = chatMsgs.querySelector('[data-id="' + replyMeta.id.replace(/"/g, '') + '"]');
      if (parentInDom) {
        replyLink.setAttribute('aria-label', 'Jump to replied message by ' + (replyMeta.author || 'unknown'));
        replyLink.setAttribute('role', 'link');
        replyLink.innerHTML = '<span class="msg-reply-author">' + escHtml(replyMeta.author || 'Unknown') + '</span> ' + escHtml(replyMeta.text || '');
        (function(rid) { replyLink.onclick = function() { scrollToReply(rid); }; })(replyMeta.id);
        replyLink.onkeydown = function(e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.click(); } };
      } else {
        replyLink.className = 'msg-reply-link unavailable';
        replyLink.textContent = 'Original message unavailable';
        replyLink.disabled = true;
      }
      content.appendChild(replyLink);
    }

    content.appendChild(bubble);

    var meta = document.createElement('div');
    meta.className = 'msg-meta';

    var nameSpan = document.createElement('span');
    // Add agent badge before name
    if (agentInfo) {
      var badgeClass = agentInfo.type === 'team-lead' ? 'team-lead' : agentInfo.type === 'specialist' ? 'specialist' : agentInfo.type === 'system' ? 'system' : 'primary';
      nameSpan.innerHTML = '<span class="agent-badge ' + badgeClass + '">' + escHtml(agentInfo.badge) + '</span>' + escHtml(senderName) + ' \u00B7 ' + escHtml(timeStr);
    } else {
      nameSpan.textContent = senderName + ' \u00B7 ' + timeStr;
    }
    meta.appendChild(nameSpan);

    var actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.innerHTML = '<button class="msg-action-btn" onclick="copyMsgText(this)" title="Copy"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><button class="msg-action-btn" onclick="pinMsg(this)" title="Pin"><svg viewBox="0 0 24 24"><path d="M12 17v5m-4-9l1-5 3-3 4 0 3 3 1 5-4 2v2H8v-2l-4-2z"/></svg></button><button class="msg-action-btn" onclick="replyToMsg(this)" title="Reply"><svg viewBox="0 0 24 24"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg></button>';
    meta.appendChild(actions);

    content.appendChild(meta);
    div.appendChild(avatar);
    div.appendChild(content);
    return { el: div, bubble: bubble, msgId: msgId };
  }

  function addMessage(text, role, ts, id, agentContext, topic, replyMeta) {
    // Skip empty/noise messages
    if (!text || !text.trim()) return;
    var trimmed = text.trim();
    if (role !== 'thinking' && trimmed.length <= 2 && /^[^a-zA-Z0-9]+$/.test(trimmed)) return;
    if (role === 'thinking' && trimmed.length < 3) return;

    var msgId = id || ('local-' + Date.now() + '-' + Math.random());
    if (knownMsgIds.has(msgId)) return;

    // Content-based dedup for image/file uploads: if a user message with the same
    // [Image: X] or [File: X] filename already exists in the DOM, skip it.
    // This catches duplicates arriving with different IDs (e.g. session UUID vs portal ID).
    if (role === 'user') {
      var imgDup = trimmed.match(/\[Image:\s*([^\]]+)\]/);
      var fileDup = !imgDup && trimmed.match(/\[File:\s*([^\]]+)\]/);
      var dupKey = imgDup ? imgDup[1].trim() : (fileDup ? fileDup[1].trim() : null);
      if (dupKey) {
        var existing = chatMsgs.querySelectorAll('.msg-user');
        for (var di = 0; di < existing.length; di++) {
          var bubble = existing[di].querySelector('.msg-bubble');
          if (bubble && bubble.querySelector('img.msg-inline-img')) {
            // Image already rendered for this upload — skip duplicate
            var eSrc = bubble.querySelector('img.msg-inline-img').alt || '';
            if (eSrc && dupKey.indexOf(eSrc) !== -1) return;
          }
        }
      }
    }

    knownMsgIds.add(msgId);
    // Prune to prevent unbounded growth
    if (knownMsgIds.size > 500) {
      var arr = Array.from(knownMsgIds);
      knownMsgIds.clear();
      arr.slice(-250).forEach(function(k) { knownMsgIds.add(k); });
    }

    var built = buildMessageEl(text, role, ts, msgId, agentContext, replyMeta);
    // Store topic on the DOM element and render pill
    if (topic) {
      // Normalize topic to key format (lowercase, hyphens) for consistent filtering
      var topicKey = topic.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      built.el.setAttribute('data-topic', topicKey);
      // Prettify key for pill display (e.g. "project-status" -> "Project Status")
      var topicDisplay = topicKey.replace(/-/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
      var metaSpan = built.el.querySelector('.msg-meta > span');
      if (metaSpan) metaSpan.insertAdjacentHTML('beforeend', _renderTopicPill(topicDisplay));
    }
    // Apply current topic filter
    _applyTopicFilter(built.el);
    chatMsgs.appendChild(built.el);
    // Check nearBottom AFTER append so the new element's height is included
    // Use generous 300px threshold for AI messages (longer content pushes further)
    var _scrollThreshold = (role === 'user') ? 150 : 300;
    var _nearBot = chatMsgs.scrollHeight - chatMsgs.scrollTop - chatMsgs.clientHeight < _scrollThreshold;
    if (_nearBot) chatMsgs.scrollTop = chatMsgs.scrollHeight;
  }

  function addThinkingIndicator(thinkingId) {
    var div = document.createElement('div');
    div.className = 'msg msg-ai';
    div.id = thinkingId;
    div.innerHTML = '<div class="msg-avatar"><img src="https://purebrain.ai/wp-content/uploads/2026/02/MA1.BI-1.2.4-002-211107-Icon-PT.png" alt="AI" style="width:100%;height:100%;object-fit:contain;border-radius:50%"></div><div class="msg-content"><div class="msg-bubble" style="opacity:0.7"><span class="thinking-dots"><span class="thinking-label">Thinking</span><span class="td">.</span><span class="td">.</span><span class="td">.</span></span></div></div>';
    var _nearBot = chatMsgs.scrollHeight - chatMsgs.scrollTop - chatMsgs.clientHeight < 150;
    chatMsgs.appendChild(div);
    if (_nearBot) chatMsgs.scrollTop = chatMsgs.scrollHeight;
  }

  function removeThinkingIndicator(id) {
    var el = document.getElementById(id);
    if (el) el.remove();
  }

  // ===== AUTH FLOW =====
  // getToken: delegates to shared _tok() from auth.js
  function getToken() { return _tok(); }

  function doAuth(t) {
    return fetch('/api/status', { headers: { 'Authorization': 'Bearer ' + t } })
      .then(function(r) {
        if (r.status === 401) throw new Error('Invalid token');
        if (!r.ok) throw new Error('Server error ' + r.status);
        return safeJson(r);
      })
      .then(function() {
        token = t;
        localStorage.setItem('portal_token', t);
        window.dispatchEvent(new CustomEvent('portal-auth', { detail: { token: t } }));
        bootChat();
      });
  }

  // ===== BOOT CHAT =====
  function bootChat() {
    // Fetch civ name from /health (public endpoint, no token needed)
    fetch('/health').then(function(r){ return safeJson(r); }).then(function(d){
      var name = d.civ || 'AI';
      name = name.charAt(0).toUpperCase() + name.slice(1);
      civName = name;
      window._portalCivName = name;
      // Update sidebar version badge from server
      if (d.version) {
        var vBadge = document.getElementById('sidebarVersion');
        if (vBadge) vBadge.textContent = 'v' + d.version;
      }
      // Update composer placeholder
      chatInput.placeholder = 'Message ' + name + '...';
      // Update poke button text
      var pokeBtn = document.querySelector('.poke-btn');
      if (pokeBtn) {
        var pokeSvg = pokeBtn.querySelector('svg');
        var pokeSvgHtml = pokeSvg ? pokeSvg.outerHTML + ' ' : '';
        pokeBtn.innerHTML = pokeSvgHtml + 'Poke ' + name;
      }
      // Update all hardcoded civ name references in UI
      var bannerEl = document.querySelector('.brain-banner');
      if (bannerEl) bannerEl.textContent = name + "'s Brain Stream";
      var profileName = document.querySelector('.profile-hero-name');
      if (profileName) profileName.textContent = name;
      // Profile card name value
      document.querySelectorAll('.profile-value').forEach(function(el) {
        if (el.textContent.trim() === 'AI') el.textContent = name;
      });
      var hubCivName = document.querySelector('.hub-civ-name');
      if (hubCivName) hubCivName.textContent = name;
      // Teams tab bar — update "Primary" label to civ name
      var primaryTab = document.querySelector('#teamsTabBar .teams-tab:first-child');
      if (primaryTab && primaryTab.textContent.trim() === 'Primary') {
        primaryTab.innerHTML = '<span class="pane-dot live"></span>' + escHtml(name);
      }
      // Composer placeholder (also handled above via chatInput)
      var composerEl = document.getElementById('composerInput');
      if (composerEl) composerEl.placeholder = 'Message ' + name + '...';
      // Hub avatar initial
      var hubAvatar = document.querySelector('.hub-avatar');
      if (hubAvatar && hubAvatar.textContent.trim().length <= 2) hubAvatar.textContent = name.charAt(0);
    }).catch(function(){});
    // Connect WebSocket (which triggers loadChatHistory on open)
    connectChatWS();
    // Load discovered topics into the dropdown
    _loadTopics();
  }

  // ===== LOAD CHAT HISTORY =====
  function loadChatHistory() {
    chatMsgs.innerHTML = '<div class="chat-loading" style="text-align:center;padding:40px;color:var(--pb-text-dim);font-size:13px">Loading chat history...</div>';

    var histCount = (window._portalPrefs && window._portalPrefs.chat_history_count) || '200';
    fetch('/api/chat/history?last=' + histCount, {
      headers: { 'Authorization': 'Bearer ' + token }
    })
    .then(function(r) { return safeJson(r); })
    .then(function(data) {
      // CRITICAL: Clear knownMsgIds atomically with innerHTML.
      // Root cause fix (2026-06-03): If innerHTML is cleared but knownMsgIds is
      // not, any message rendered by WS push between fetch-request and
      // fetch-response becomes an orphan (ID in knownMsgIds but DOM element
      // destroyed). addMessage() then skips it forever. Clearing both together
      // ensures they stay in sync. Duplicates are prevented because addMessage()
      // re-adds each history message to knownMsgIds during the forEach below.
      chatMsgs.innerHTML = '';
      knownMsgIds.clear();
      if (data.messages && data.messages.length > 0) {
        data.messages.forEach(function(m) {
          // Every message has a topic from backend (defaults to "general")
          var msgTopic = m.topic || 'general';
          var mReply = m.reply_to_id ? {id:m.reply_to_id, text:m.reply_to_text||'', author:m.reply_to_author||''} : null;
          addMessage(m.text, m.role, m.timestamp, m.id, m.agent_context, msgTopic, mReply);
        });
        requestAnimationFrame(function() { chatMsgs.scrollTop = chatMsgs.scrollHeight; });
      } else {
        chatMsgs.innerHTML = '<div class="chat-loading" style="text-align:center;padding:40px;color:var(--pb-text-dim);font-size:13px">No messages yet. Say hello!</div>';
      }
    })
    .catch(function(e) {
      chatMsgs.innerHTML = '<div class="chat-loading" style="text-align:center;padding:40px;color:var(--pb-text-dim);font-size:13px">Error loading history: ' + escHtml(e.message) + '</div>';
    });
  }

  // ===== WEBSOCKET CONNECTION =====
  function connectChatWS() {
    if (chatWsReconnectTimeout) { clearTimeout(chatWsReconnectTimeout); chatWsReconnectTimeout = null; }
    if (chatWs) { try { chatWs.close(); } catch(e) {} }
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    chatWs = new WebSocket(proto + '//' + location.host + '/ws/chat?token=' + encodeURIComponent(token));

    chatWs.onopen = function() {
      if (chatWsReconnectTimeout) { clearTimeout(chatWsReconnectTimeout); chatWsReconnectTimeout = null; }
      _wsReconnectDelay = 2000;
      loadChatHistory();
      console.log('[WS] Chat connected');
      // Heartbeat: force reconnect if no data in 120s
      if (window._wsHeartbeat) clearInterval(window._wsHeartbeat);
      window._wsLastMsg = Date.now();
      window._wsHeartbeat = setInterval(function() {
        if (!chatWs || chatWs.readyState !== 1) return;
        if (Date.now() - window._wsLastMsg > 120000) {
          console.warn('[WS] no data in 120s, forcing reconnect');
          chatWs.close();
        }
      }, 30000);
    };

    chatWs.onmessage = function(e) {
      window._wsLastMsg = Date.now();
      try {
        var msg = JSON.parse(e.data);
        // Ignore keepalive pings
        if (msg.type === 'ping') return;
        // Remove loading placeholder if present
        var placeholder = chatMsgs.querySelector('.chat-loading');
        if (placeholder) placeholder.remove();
        // Skip WS echo of user messages shown optimistically
        if (msg.role === 'user' && lastSentOptimisticText && (msg.text === lastSentOptimisticText || msg.text === lastSentRawText)) {
          lastSentOptimisticText = null;
          lastSentRawText = null;
          return;
        }
        // Handle thinking blocks
        if (msg.role === 'thinking') {
          var LIVE_THINKING_ID = 'thinking-live-indicator';
          if (!document.getElementById(LIVE_THINKING_ID)) {
            addThinkingIndicator(LIVE_THINKING_ID);
          }
          if (window._liveThinkingTimer) clearTimeout(window._liveThinkingTimer);
          window._liveThinkingTimer = setTimeout(function() {
            removeThinkingIndicator(LIVE_THINKING_ID);
            window._liveThinkingTimer = null;
          }, 8000);
          addMessage(msg.text, 'thinking', msg.timestamp, msg.id);
          return;
        }
        // Handle assistant messages (streaming updates)
        if (msg.role === 'assistant') {
          // Clear ALL thinking indicators
          document.querySelectorAll('[id^="thinking-"]').forEach(function(el) { el.remove(); });
          window._pendingThinkingId = null;
          if (window._liveThinkingTimer) { clearTimeout(window._liveThinkingTimer); window._liveThinkingTimer = null; }
          if (window._uploadThinkingTimeout) { clearTimeout(window._uploadThinkingTimeout); window._uploadThinkingTimeout = null; }
          // If this message ID already exists, update its content in-place
          if (msg.id && knownMsgIds.has(msg.id)) {
            var existingDiv = chatMsgs.querySelector('[data-id="' + msg.id + '"]');
            if (existingDiv) {
              var bubble = existingDiv.querySelector('.msg-bubble');
              var updText = msg.text && msg.text.trim();
              if (bubble && updText) {
                // Re-detect agent identity for updated text (may have context now)
                var updAgent = detectAgentIdentity(updText, msg.agent_context);
                if (updAgent && updAgent.strip) {
                  updText = updText.substring(updAgent.strip.length).replace(/^\s*/, '');
                }
                bubble.innerHTML = renderMarkdown(updText);
                addCodeCopyButtons(bubble);
                // Update sender name/badge if agent detected
                if (updAgent) {
                  var metaSpan = existingDiv.querySelector('.msg-meta span');
                  if (metaSpan) {
                    var badgeClass = updAgent.type === 'team-lead' ? 'team-lead' : updAgent.type === 'specialist' ? 'specialist' : updAgent.type === 'system' ? 'system' : 'primary';
                    var ts = existingDiv.getAttribute('data-ts');
                    var timeStr = ts ? formatTime(parseInt(ts)) : '';
                    metaSpan.innerHTML = '<span class="agent-badge ' + badgeClass + '">' + escHtml(updAgent.badge) + '</span>' + escHtml(updAgent.name) + ' \u00B7 ' + escHtml(timeStr);
                  }
                  // Update avatar with CSS classes
                  var avatar = existingDiv.querySelector('.msg-avatar');
                  if (avatar) {
                    var _updType = updAgent.type === 'team-lead' ? 'team-lead' : updAgent.type === 'specialist' ? 'specialist' : updAgent.type === 'system' ? 'system' : 'primary';
                    avatar.className = 'msg-avatar msg-avatar-agent type-' + _updType;
                    avatar.textContent = updAgent.name.charAt(0).toUpperCase();
                    var img = avatar.querySelector('img');
                    if (img) img.remove();
                  }
                }
              // Update topic if the stable re-send now has one
              if (msg.topic && !existingDiv.getAttribute('data-topic')) {
                var updTopicKey = msg.topic.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
                existingDiv.setAttribute('data-topic', updTopicKey);
                var updTopicDisplay = updTopicKey.replace(/-/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
                var updMeta = existingDiv.querySelector('.msg-meta > span');
                if (updMeta) updMeta.insertAdjacentHTML('beforeend', _renderTopicPill(updTopicDisplay));
                _applyTopicFilter(existingDiv);
              }
              }
            }
            return;
          }
          // Topic from backend (every message has a topic — defaults to "general")
          var wsTopic = msg.topic || 'general';
          var wsReply = msg.reply_to_id ? {id:msg.reply_to_id, text:msg.reply_to_text||'', author:msg.reply_to_author||''} : null;
          addMessage(msg.text, 'assistant', msg.timestamp, msg.id, msg.agent_context, wsTopic, wsReply);
          // Refresh topic dropdown when a new topic appears
          if (wsTopic) _debouncedLoadTopics();
          // HMI Voice: speak AI response aloud if voice overlay is open or TTS enabled
          if (typeof window._hmiSpeakResponse === 'function' && msg.text) {
            // Speak if: chat TTS is toggled on, OR voice-sent within 90s, OR HMI overlay open
            if (window._chatTtsEnabled
                || (window._voiceSendTimestamp && (Date.now() - window._voiceSendTimestamp) < 90000)
                || window._hmiVoiceOverlayOpen) {
              window._hmiSpeakResponse(msg.text);
            }
            // Also add to live transcript if overlay is open
            if (window._hmiVoiceOverlayOpen && typeof window.hmiTranscriptAdd === 'function') {
              window.hmiTranscriptAdd(msg.text, 'ai');
            }
          }
          return;
        }
        // Default: add as-is
        var dfReply = msg.reply_to_id ? {id:msg.reply_to_id, text:msg.reply_to_text||'', author:msg.reply_to_author||''} : null;
        addMessage(msg.text, msg.role, msg.timestamp, msg.id, msg.agent_context, msg.topic, dfReply);
      } catch(err) {
        console.error('[WS] message handling error:', err);
      }
    };

    chatWs.onclose = function(ev) {
      if (window._wsHeartbeat) { clearInterval(window._wsHeartbeat); window._wsHeartbeat = null; }
      console.warn('[WS] closed, code=' + ev.code + ', reconnecting in ' + (_wsReconnectDelay/1000) + 's...');
      chatWs = null;
      if (ev.code === 1008 || ev.code === 4001) {
        console.error('[WS] Auth rejected (code ' + ev.code + '), not reconnecting');
        return;
      }
      chatWsReconnectTimeout = setTimeout(function() {
        if (token) connectChatWS();
      }, _wsReconnectDelay);
      _wsReconnectDelay = Math.min(_wsReconnectDelay * 2, 30000);
    };

    chatWs.onerror = function() {
      // Error triggers onclose which handles reconnection
    };
  }

  // Reconnect WS on tab visibility change (iOS app resume, etc.)
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState !== 'visible' || !token) return;
    var wsAlive = chatWs && chatWs.readyState === WebSocket.OPEN;
    if (!wsAlive) {
      console.log('[Portal] Visible again, WS dead -- reconnecting');
      if (chatWsReconnectTimeout) clearTimeout(chatWsReconnectTimeout);
      _wsReconnectDelay = 2000;
      connectChatWS();
    }
  });

  // ===== SEND MESSAGE (REAL) =====
  function sendMessage() {
    var text = chatInput.value.trim();
    var hasFiles = pendingFiles.length > 0;
    if (!text && !hasFiles) return;
    if (!token) {
      showToast('Not authenticated. Please log in first.');
      return;
    }
    // When files are pending, force-enable (previous 1500ms cooldown must not block file sends)
    if (hasFiles) sendBtn.disabled = false;
    if (sendBtn.disabled) return;
    sendBtn.disabled = true;

    // Check for reply context from the reply bar
    var replyBar = document.querySelector('.composer-reply-bar');
    var replyPrefix = '';
    var replyMeta = null;
    if (replyBar) {
      var replyEm = replyBar.querySelector('em');
      if (replyEm) {
        replyPrefix = '> ' + replyEm.textContent + '\n\n';
      }
      var rId = replyBar.getAttribute('data-reply-to-id');
      var rText = replyBar.getAttribute('data-reply-to-text');
      var rAuthor = replyBar.getAttribute('data-reply-to-author');
      if (rId) replyMeta = { id: rId, text: rText || '', author: rAuthor || '' };
      replyBar.remove();
    }
    var fullMsg = replyPrefix + text;
    chatInput.value = '';
    chatInput.style.height = 'auto';

    // If files are pending, upload them (with optional caption) and return
    if (hasFiles) {
      uploadPendingFiles(fullMsg || null);
      setTimeout(function() { sendBtn.disabled = false; }, 1500);
      chatInput.focus();
      return;
    }
    // No popup — topic is visible via the active topic pill.
    // User controls topic by selecting/clearing the pill before sending.

    // Remove loading placeholder if present
    var placeholder = chatMsgs.querySelector('.chat-loading');
    if (placeholder) placeholder.remove();

    // Add user message optimistically
    var optimisticId = 'opt-' + Date.now() + '-' + Math.floor(Math.random() * 100000);
    var sendTopic = (currentActiveTopic && currentActiveTopic !== 'general') ? currentActiveTopic : null;
    addMessage(fullMsg, 'user', null, optimisticId, null, sendTopic, replyMeta);
    // HMI Voice: add user message to live transcript
    if (window._hmiVoiceOverlayOpen && typeof window.hmiTranscriptAdd === 'function') {
      window.hmiTranscriptAdd(fullMsg, 'user');
    }
    lastSentOptimisticText = fullMsg;
    lastSentRawText = text;

    // Add thinking indicator
    var thinkingId = 'thinking-' + Date.now();
    addThinkingIndicator(thinkingId);
    window._pendingThinkingId = thinkingId;

    // Force scroll to bottom
    requestAnimationFrame(function() { chatMsgs.scrollTop = chatMsgs.scrollHeight; });

    // Always send topic — including "general" so backend knows user's intent
    var sendBody = { message: fullMsg, topic: currentActiveTopic || 'general' };
    if (replyMeta) {
      sendBody.reply_to_id = replyMeta.id;
      sendBody.reply_to_text = replyMeta.text;
      sendBody.reply_to_author = replyMeta.author;
    }
    fetch('/api/chat/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify(sendBody)
    })
    .then(function(r) { return safeJson(r); })
    .then(function(data) {
      if (data.error) {
        window._pendingThinkingId = null;
        addMessage('Error: ' + data.error, 'assistant', null, 'err-' + Date.now());
        removeThinkingIndicator(thinkingId);
      } else if (data.msg_id) {
        // Pre-register server-assigned ID to suppress WS echo duplicate
        knownMsgIds.add(data.msg_id);
      }
    })
    .catch(function(e) {
      window._pendingThinkingId = null;
      lastSentOptimisticText = null;
      lastSentRawText = null;
      addMessage('Network error: ' + e.message, 'assistant', null, 'err-' + Date.now());
      removeThinkingIndicator(thinkingId);
    })
    .finally(function() {
      setTimeout(function() { sendBtn.disabled = false; }, 1500);
      chatInput.focus();
    });
  }

  // ===== FILE COMPRESSION =====
  function _isImageFile(file) {
    return /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name) ||
           (file.type && file.type.startsWith('image/'));
  }
  function _isTextFile(file) {
    return /\.(txt|md|csv|js|ts|py|java|c|cpp|h|css|html|htm|json|xml|yaml|yml|sh|rb|go|rs|sql)$/i.test(file.name) ||
           (file.type && (file.type.startsWith('text/') || file.type === 'application/json'));
  }
  function _estimateCompressedSize(file) {
    if (_isImageFile(file)) return Math.round(file.size * 0.55);
    if (_isTextFile(file)) return Math.round(file.size * 0.97);
    return file.size;
  }
  function _canCompress(file) {
    return _isImageFile(file) || _isTextFile(file);
  }
  function _compressImage(file) {
    return new Promise(function(resolve) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function() {
        URL.revokeObjectURL(url);
        var maxW = 1920;
        var w = img.width, h = img.height;
        if (w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
        var canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        var ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, w, h);
        canvas.toBlob(function(blob) {
          if (!blob) { resolve(file); return; }
          var name = file.name.replace(/\.[^.]+$/, '') + '.jpg';
          resolve(new File([blob], name, { type: 'image/jpeg' }));
        }, 'image/jpeg', 0.7);
      };
      img.onerror = function() { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }
  function _compressText(file) {
    return new Promise(function(resolve) {
      var reader = new FileReader();
      reader.onload = function(e) {
        var text = e.target.result;
        var trimmed = text.split('\n').map(function(line) {
          return line.replace(/[\t ]+$/, '');
        }).join('\n');
        var blob = new Blob([trimmed], { type: file.type || 'text/plain' });
        resolve(new File([blob], file.name, { type: file.type || 'text/plain' }));
      };
      reader.onerror = function() { resolve(file); };
      reader.readAsText(file);
    });
  }
  function _compressFile(file) {
    if (_isImageFile(file)) return _compressImage(file);
    if (_isTextFile(file)) return _compressText(file);
    return Promise.resolve(file);
  }
  function _fmtBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }
  function _showUploadModeModal(files) {
    return new Promise(function(resolve) {
      var overlay = document.getElementById('upload-mode-overlay');
      var totalOriginal = files.reduce(function(s, f) { return s + f.size; }, 0);
      var totalEstComp = files.reduce(function(s, f) { return s + _estimateCompressedSize(f); }, 0);
      var hasCompressable = files.some(_canCompress);

      var subtitle = document.getElementById('upm-subtitle');
      if (files.length === 1) {
        subtitle.innerHTML = '<strong>' + escHtml(files[0].name) + '</strong>';
      } else {
        subtitle.innerHTML = '<strong>' + files.length + ' files</strong> selected';
      }

      document.getElementById('upm-size-original').textContent = _fmtBytes(totalOriginal);
      document.getElementById('upm-size-compressed').textContent = hasCompressable
        ? _fmtBytes(totalEstComp) : 'Same';

      var btnComp = document.getElementById('upm-btn-compressed');
      btnComp.disabled = !hasCompressable;
      btnComp.style.opacity = hasCompressable ? '' : '0.45';
      btnComp.title = hasCompressable ? '' : 'No compression available for this file type';

      function _cleanup() {
        overlay.classList.remove('visible');
        document.getElementById('upm-btn-original').removeEventListener('click', _onOriginal);
        document.getElementById('upm-btn-compressed').removeEventListener('click', _onCompressed);
        overlay.removeEventListener('click', _onOverlayClick);
      }
      function _onOriginal() {
        _cleanup();
        resolve(files);
      }
      function _onCompressed() {
        _cleanup();
        if (!hasCompressable) { resolve(files); return; }
        Promise.all(files.map(_compressFile)).then(resolve);
      }
      function _onOverlayClick(e) {
        if (e.target === overlay) _onOriginal();
      }

      document.getElementById('upm-btn-original').addEventListener('click', _onOriginal);
      document.getElementById('upm-btn-compressed').addEventListener('click', _onCompressed);
      overlay.addEventListener('click', _onOverlayClick);
      overlay.classList.add('visible');
    });
  }

  // ===== FILE UPLOAD (Preview-before-send) =====
  var attachBtn = document.querySelector('.c-btn[title="Attach"]');
  var fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.style.display = 'none';
  fileInput.multiple = true;
  document.body.appendChild(fileInput);
  var pendingFiles = [];
  var attachPreviewBar = document.getElementById('attachPreviewBar');

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  function getFileIcon(name) {
    var ext = (name.split('.').pop() || '').toLowerCase();
    var icons = { pdf: '\uD83D\uDCC4', doc: '\uD83D\uDCC3', docx: '\uD83D\uDCC3', txt: '\uD83D\uDCC4', md: '\uD83D\uDCC4', csv: '\uD83D\uDCCA', jpg: '\uD83D\uDDBC\uFE0F', jpeg: '\uD83D\uDDBC\uFE0F', png: '\uD83D\uDDBC\uFE0F', gif: '\uD83D\uDDBC\uFE0F', webp: '\uD83D\uDDBC\uFE0F' };
    return icons[ext] || '\uD83D\uDCCE';
  }

  function renderAttachPreview() {
    attachPreviewBar.innerHTML = '';
    if (pendingFiles.length === 0) {
      attachPreviewBar.classList.remove('active');
      sendBtn.classList.remove('has-files');
      sendBtn.title = 'Send';
      return;
    }
    attachPreviewBar.classList.add('active');
    sendBtn.classList.add('has-files');
    sendBtn.title = 'Send ' + pendingFiles.length + ' file' + (pendingFiles.length > 1 ? 's' : '');
    pendingFiles.forEach(function(file, idx) {
      var item = document.createElement('div');
      item.className = 'attach-preview-item';
      var isImage = /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(file.name);
      if (isImage) {
        var thumb = document.createElement('img');
        thumb.className = 'attach-preview-thumb';
        thumb.src = URL.createObjectURL(file);
        thumb.onload = function() { URL.revokeObjectURL(thumb.src); };
        item.appendChild(thumb);
      } else {
        var icon = document.createElement('span');
        icon.className = 'attach-preview-icon';
        icon.textContent = getFileIcon(file.name);
        item.appendChild(icon);
      }
      var name = document.createElement('span');
      name.className = 'attach-preview-name';
      name.title = file.name;
      name.textContent = file.name;
      var size = document.createElement('span');
      size.className = 'attach-preview-size';
      size.textContent = formatFileSize(file.size);
      var removeBtn = document.createElement('button');
      removeBtn.className = 'attach-preview-remove';
      removeBtn.textContent = '\u00D7';
      removeBtn.title = 'Remove';
      (function(i) {
        removeBtn.addEventListener('click', function() {
          pendingFiles.splice(i, 1);
          renderAttachPreview();
        });
      })(idx);
      item.appendChild(name);
      item.appendChild(size);
      item.appendChild(removeBtn);
      attachPreviewBar.appendChild(item);
    });
  }

  if (attachBtn) {
    attachBtn.addEventListener('click', function() {
      if (!token) { showToast('Not authenticated.'); return; }
      fileInput.click();
    });
  }

  fileInput.addEventListener('change', function() {
    if (!fileInput.files || fileInput.files.length === 0) return;
    var MAX_FILE_SIZE = 50 * 1024 * 1024;
    var validFiles = [];
    Array.from(fileInput.files).forEach(function(file) {
      if (file.size > MAX_FILE_SIZE) {
        showToast('File too large. Maximum size is 50 MB.');
        return;
      }
      validFiles.push(file);
    });
    fileInput.value = '';
    if (validFiles.length === 0) return;

    var hasCompressable = validFiles.some(_canCompress);
    if (hasCompressable) {
      _showUploadModeModal(validFiles).then(function(finalFiles) {
        finalFiles.forEach(function(f) { pendingFiles.push(f); });
        renderAttachPreview();
        chatInput.focus();
      });
    } else {
      validFiles.forEach(function(f) { pendingFiles.push(f); });
      renderAttachPreview();
      chatInput.focus();
    }
  });

  function uploadPendingFiles(caption) {
    var filesToSend = pendingFiles.slice();
    pendingFiles = [];
    renderAttachPreview();
    if (filesToSend.length === 0) return;

    var uploadCount = 0;
    filesToSend.forEach(function(file) {
      var isImage = /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(file.name);
      var progressId = 'upload-' + Date.now() + '-' + Math.floor(Math.random() * 1000);
      var progressEl = document.createElement('div');
      progressEl.id = progressId;
      progressEl.className = 'msg msg-user';
      progressEl.innerHTML = '<div class="msg-avatar">You</div><div class="msg-content"><div class="msg-bubble" style="opacity:0.6">Uploading ' + escHtml(file.name) + '...</div></div>';
      chatMsgs.appendChild(progressEl);
      chatMsgs.scrollTop = chatMsgs.scrollHeight;

      var fd = new FormData();
      fd.append('file', file);
      if (caption) fd.append('caption', caption);

      fetch('/api/chat/upload', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: fd
      })
      .then(function(r) { return safeJson(r); })
      .then(function(data) {
        var prog = document.getElementById(progressId);
        if (prog) prog.remove();
        console.log('[Portal] Upload response for ' + file.name + ':', JSON.stringify(data));
        if (data.ok) {
          // Pre-register IDs for dedup — server saves to portal log AND tmux echoes back
          if (data.user_msg_id) knownMsgIds.add(data.user_msg_id);
          if (data.ack_msg_id) knownMsgIds.add(data.ack_msg_id);
          // Add the message once from upload response, skip WS/history echo
          if (isImage) {
            addFileImageMessage(data.filename, data.original || data.filename, caption, data.user_msg_id);
          } else {
            addMessage('[File: ' + escHtml(data.original || data.filename) + ']' + (caption ? '\n' + caption : ''), 'user', null, data.user_msg_id);
          }
          uploadCount++;
          if (uploadCount === filesToSend.length) {
            var uploadThinkingId = 'thinking-upload-' + Date.now();
            addThinkingIndicator(uploadThinkingId);
            window._pendingThinkingId = uploadThinkingId;
            window._uploadThinkingTimeout = setTimeout(function() {
              window._uploadThinkingTimeout = null;
              var stale = document.querySelectorAll('[id^="thinking-"]');
              stale.forEach(function(el) { el.remove(); });
            }, 30000);
          }
        } else {
          addMessage('Upload error: ' + (data.error || 'unknown'), 'assistant', null, null);
        }
      })
      .catch(function(e) {
        var prog = document.getElementById(progressId);
        if (prog) prog.remove();
        addMessage('Upload failed: ' + e.message, 'assistant', null, null);
      });
    });
  }

  // Render uploaded image inline in chat
  function addFileImageMessage(storedFilename, displayFilename, caption, msgId) {
    msgId = msgId || ('file-' + Date.now() + '-' + Math.floor(Math.random() * 10000));
    if (knownMsgIds.has(msgId)) return;
    knownMsgIds.add(msgId);
    var ts = Math.floor(Date.now() / 1000);
    var timeStr = formatTime(ts);

    var div = document.createElement('div');
    div.className = 'msg msg-user';
    div.setAttribute('data-id', msgId);
    div.setAttribute('data-ts', ts);

    var avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.textContent = 'You';

    var content = document.createElement('div');
    content.className = 'msg-content';

    var bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    if (caption) {
      bubble.innerHTML = renderMarkdown(caption);
    }

    var img = document.createElement('img');
    img.className = 'msg-inline-img';
    img.src = '/api/chat/uploads/' + encodeURIComponent(storedFilename) + '?token=' + encodeURIComponent(token);
    img.alt = displayFilename;
    img.addEventListener('click', function() { window.open(img.src, '_blank'); });
    img.addEventListener('load', function() { chatMsgs.scrollTop = chatMsgs.scrollHeight; });
    img.addEventListener('error', function() { img.style.display = 'none'; });
    bubble.appendChild(img);

    var meta = document.createElement('div');
    meta.className = 'msg-meta';
    var nameSpan = document.createElement('span');
    nameSpan.textContent = 'You \u00B7 ' + timeStr;
    meta.appendChild(nameSpan);

    content.appendChild(bubble);
    content.appendChild(meta);
    div.appendChild(avatar);
    div.appendChild(content);
    chatMsgs.appendChild(div);
    chatMsgs.scrollTop = chatMsgs.scrollHeight;
  }

  // ===== BIND SEND BUTTON & ENTER KEY =====
  sendBtn.addEventListener('click', sendMessage);
  chatInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      var sendOnEnter = !window._portalPrefs || window._portalPrefs.send_on_enter !== false;
      var slashDropdown = document.getElementById('slashDropdown');
      var slashOpen = slashDropdown && slashDropdown.classList.contains('visible');
      if (sendOnEnter && !e.shiftKey && !slashOpen) {
        e.preventDefault();
        sendMessage();
      } else if (!sendOnEnter && (e.ctrlKey || e.metaKey) && !slashOpen) {
        e.preventDefault();
        sendMessage();
      }
    }
  });

  // ===== DRAG & DROP FILE SUPPORT =====
  var chatPanel = document.getElementById('chatArea') || chatMsgs.closest('.chat-area') || chatMsgs.parentElement;
  if (chatPanel) {
    chatPanel.addEventListener('dragover', function(e) {
      e.preventDefault();
      e.stopPropagation();
      chatPanel.classList.add('drag-over');
    });
    chatPanel.addEventListener('dragleave', function(e) {
      e.preventDefault();
      e.stopPropagation();
      // Only remove if leaving the panel itself (not entering a child)
      if (!chatPanel.contains(e.relatedTarget)) {
        chatPanel.classList.remove('drag-over');
      }
    });
    chatPanel.addEventListener('drop', function(e) {
      e.preventDefault();
      e.stopPropagation();
      chatPanel.classList.remove('drag-over');
      if (!token) { showToast('Not authenticated.'); return; }
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || files.length === 0) return;
      var MAX_FILE_SIZE = 50 * 1024 * 1024;
      var validFiles = [];
      Array.from(files).forEach(function(file) {
        if (file.size > MAX_FILE_SIZE) {
          showToast('File too large. Maximum size is 50 MB.');
          return;
        }
        validFiles.push(file);
      });
      if (validFiles.length === 0) return;
      var hasCompressable = validFiles.some(_canCompress);
      if (hasCompressable) {
        _showUploadModeModal(validFiles).then(function(finalFiles) {
          finalFiles.forEach(function(f) { pendingFiles.push(f); });
          renderAttachPreview();
          chatInput.focus();
        });
      } else {
        validFiles.forEach(function(f) { pendingFiles.push(f); });
        renderAttachPreview();
        chatInput.focus();
      }
    });
  }

  // ===== CLIPBOARD PASTE IMAGE SUPPORT =====
  chatInput.addEventListener('paste', function(e) {
    if (!token) return;
    var items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    var imageFiles = [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.startsWith('image/')) {
        var file = items[i].getAsFile();
        if (file) {
          // Give pasted images a descriptive filename
          var ext = items[i].type.split('/')[1] || 'png';
          if (ext === 'jpeg') ext = 'jpg';
          var pastedFile = new File([file], 'pasted-image-' + Date.now() + '.' + ext, { type: items[i].type });
          imageFiles.push(pastedFile);
        }
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault();
      var hasCompressable = imageFiles.some(_canCompress);
      if (hasCompressable) {
        _showUploadModeModal(imageFiles).then(function(finalFiles) {
          finalFiles.forEach(function(f) { pendingFiles.push(f); });
          renderAttachPreview();
        });
      } else {
        imageFiles.forEach(function(f) { pendingFiles.push(f); });
        renderAttachPreview();
      }
    }
  });

  // ===== INIT: Auto-auth and boot =====
  var savedToken = localStorage.getItem('portal_token');
  var urlParams = new URLSearchParams(window.location.search);
  var urlToken = urlParams.get('token');
  if (urlToken) {
    history.replaceState(null, '', window.location.pathname);
    savedToken = urlToken;
  }
  if (savedToken) {
    doAuth(savedToken).catch(function(e) {
      console.warn('[Chat] Auto-auth failed:', e.message);
      token = '';
      // Clear demo messages and show auth prompt
      chatMsgs.innerHTML = '<div class="chat-loading" style="text-align:center;padding:40px;color:var(--pb-text-dim);font-size:13px">Authentication required. Please enter your bearer token in Settings.</div>';
    });
  } else {
    // No token — clear demo messages and show prompt
    chatMsgs.innerHTML = '<div class="chat-loading" style="text-align:center;padding:40px;color:var(--pb-text-dim);font-size:13px">Enter your bearer token to start chatting. Use the login in Settings or add ?token=YOUR_TOKEN to the URL.</div>';
  }

  // ===== EXPOSE for other modules =====
  window._portalChat = {
    getToken: getToken,
    setToken: function(t) { token = t; localStorage.setItem('portal_token', t); window.dispatchEvent(new CustomEvent('portal-auth', { detail: { token: t } })); },
    sendMessage: sendMessage,
    loadHistory: loadChatHistory,
    reconnectWS: connectChatWS,
    getCivName: function() { return civName; },
    renderMarkdown: renderMarkdown
  };
})();
