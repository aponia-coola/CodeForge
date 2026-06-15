/* ============================================================
   AI Agent Web Compiler — app.js
   前端交互:模型切换下拉菜单
   ============================================================ */

(function () {
  'use strict';

  // ============ 模型切换下拉 ============
  const select = document.querySelector('.model-select');
  if (!select) return;

  // 1. 启动时拉取模型列表,刷新按钮显示
  // 初始化:把直接文本节点包进 .model-label 里(让省略号只裁文字、不裁下拉)
  wrapLabel(select);

  fetch('/api/models')
    .then(r => r.json())
    .then(data => {
      select.dataset.models = JSON.stringify(data.models);
      const current = data.models.find(m => m.id === data.current);
      if (current) getLabel(select).textContent = current.name;
    })
    .catch(err => {
      console.error('拉取模型列表失败:', err);
    });

  // 2. 点击切换下拉显示
  select.addEventListener('click', e => {
    e.stopPropagation();
    closeAllDropdowns();
    if (select.classList.contains('open')) {
      select.classList.remove('open');
      return;
    }
    select.classList.add('open');

    const models = JSON.parse(select.dataset.models || '[]');
    const current = getLabel(select).textContent;

    const menu = document.createElement('div');
    menu.className = 'model-menu';
    models.forEach(m => {
      const item = document.createElement('div');
      item.className = 'model-item';
      if (m.name === current) item.classList.add('active');
      item.textContent = m.name;
      item.addEventListener('click', ev => {
        ev.stopPropagation();
        switchModel(m);
      });
      menu.appendChild(item);
    });
    select.appendChild(menu);
  });

  // 3. 点击外部关闭
  document.addEventListener('click', () => closeAllDropdowns());

  // ============ 侧边栏图标切换/折叠 ============
  // 三个 sidebar 内部视图(资源管理器/搜索/源代码管理)互斥显示;二次点击自身则全部折叠
  // 右边 Agent 栏(`.right`)是另一条线,继续走 Ctrl+J / 折叠
  const INTERNAL_VIEWS = ['.sidebar', '.search-view', '.scm-view'];  // 对应三个图标
  const INTERNAL_KEYS = ['explorer', 'search', 'scm'];
  const INTERNAL_TITLES = { explorer: '资源管理器', search: '搜索', scm: '源代码管理' };
  const sidebarTitleEl = document.getElementById('sidebar-header-title');
  function setInternalView(name) {
    // name: 'explorer' | 'search' | 'scm' | ''  (空 = 全部折叠)
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) {
      sidebar.classList.remove('collapsed');
      sidebar.style.width = '';
    }
    // 取消所有内部视图图标的 active
    document.querySelectorAll('.sidebar-icons [data-target]').forEach(i => {
      if (INTERNAL_VIEWS.includes(i.dataset.target)) i.classList.remove('active');
    });
    // 显示目标
    switchSidebarView(name);
    // 标记对应图标 active + 更新 header 标题
    if (name) {
      const sel = INTERNAL_VIEWS[INTERNAL_KEYS.indexOf(name)];
      document.querySelector(`.sidebar-icons [data-target="${sel}"]`)?.classList.add('active');
      if (sidebarTitleEl) sidebarTitleEl.textContent = INTERNAL_TITLES[name] || '资源管理器';
    } else {
      if (sidebarTitleEl) sidebarTitleEl.textContent = '资源管理器';
    }
    // 侧边栏手柄
    const handle = document.querySelector('.resize-handle[data-target=".sidebar"]');
    if (handle) handle.style.display = name ? '' : 'none';
  }

  const icons = document.querySelectorAll('.sidebar-icons [data-target]');
  icons.forEach(icon => {
    icon.addEventListener('click', () => {
      const target = icon.dataset.target;

      // ── 内部视图分支 ──
      const idx = INTERNAL_VIEWS.indexOf(target);
      if (idx !== -1) {
        const name = INTERNAL_KEYS[idx];
        if (icon.classList.contains('active')) {
          // 二次点击当前激活的 → 全部折叠
          setInternalView('');
        } else {
          // 切换到该视图
          setInternalView(name);
          if (name === 'scm') loadGitStatus();
          if (name === 'search') {
            setTimeout(() => document.getElementById('search-input')?.focus(), 50);
          }
        }
        return;
      }

      // ── 外部面板(Agent 栏):继续走折叠逻辑 ──
      const panel = document.querySelector(target);
      if (!panel) return;
      const isCollapsed = panel.classList.toggle('collapsed');
      icon.classList.toggle('active', !isCollapsed);
      if (isCollapsed) panel.style.width = '';
      const handle = document.querySelector(`.resize-handle[data-target="${target}"]`);
      if (handle) handle.style.display = isCollapsed ? 'none' : '';
    });
  });

  // sidebar 内部:explorer / scm / search 三选一互斥
  function switchSidebarView(which) {
    const explorerEl = document.querySelector('.sidebar .explorer');
    const scmEl      = document.getElementById('scm-view');
    const searchEl   = document.getElementById('search-view');
    const show = (el, on) => { if (el) el.style.display = on ? '' : 'none'; };
    show(explorerEl, which === 'explorer');
    show(scmEl,      which === 'scm');
    show(searchEl,   which === 'search');
  }

  // ============ 快捷键:Ctrl+B 折叠资源管理器,Ctrl+J 折叠 Agent 栏 ============
  document.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const key = e.key.toLowerCase();
    if (key === 'b' && !e.shiftKey) {
      e.preventDefault();
      const explorerIcon = document.querySelector('.sidebar-icons [data-target=".sidebar"]');
      if (explorerIcon) explorerIcon.click();
    } else if (key === 'j' && !e.shiftKey) {
      e.preventDefault();
      const agentIcon = document.querySelector('.sidebar-icons [data-target=".right"]');
      if (agentIcon) agentIcon.click();
    }
  });

  // 4. ESC 关闭
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeAllDropdowns();
  });

  // ============ 辅助函数 ============
  function wrapLabel(el) {
    if (el.querySelector(':scope > .model-label')) return;
    const label = document.createElement('span');
    label.className = 'model-label';
    // 把所有非 .model-menu 的子节点移进 .model-label
    Array.from(el.childNodes).forEach(node => {
      if (node.nodeType === Node.ELEMENT_NODE && node.classList.contains('model-menu')) return;
      label.appendChild(node);
    });
    el.insertBefore(label, el.firstChild);
  }

  function getLabel(el) {
    return el.querySelector(':scope > .model-label');
  }

  function closeAllDropdowns() {
    document.querySelectorAll('.model-select.open').forEach(el => {
      el.classList.remove('open');
      el.querySelectorAll('.model-menu').forEach(m => m.remove());
    });
  }

  // ============ 顶栏状态灯 ============
  // running → wc-max(绿) 高亮,带脉冲
  // plan    → wc-min(橙) 高亮,带脉冲
  // error   → wc-close(红) 高亮,带脉冲
  // idle    → 全部熄灭
  const _wcClose = document.querySelector('.wc-close');
  const _wcMin   = document.querySelector('.wc-min');
  const _wcMax   = document.querySelector('.wc-max');
  function setAgentLight(state) {
    [_wcClose, _wcMin, _wcMax].forEach(el => el && el.classList.remove('wc-active'));
    if (state === 'running' && _wcMax)   _wcMax.classList.add('wc-active');
    if (state === 'plan'     && _wcMin)   _wcMin.classList.add('wc-active');
    if (state === 'error'    && _wcClose) _wcClose.classList.add('wc-active');
  }

  function switchModel(model) {
    // 乐观更新 UI(立即反映)
    getLabel(select).textContent = model.name;
    select.classList.add('switching');
    closeAllDropdowns();

    fetch('/api/model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model.id }),
    })
      .then(r => r.json())
      .then(data => {
        select.classList.remove('switching');
        if (!data.ok) {
          console.error('切换失败:', data.error);
          // 回滚显示
          fetch('/api/models').then(r => r.json()).then(d => {
            const cur = d.models.find(m => m.id === d.current);
            if (cur) getLabel(select).textContent = cur.name;
          });
          return;
        }
        // 成功 → 以服务端为准,重新拉一遍最新状态
        return fetch('/api/models').then(r => r.json()).then(d => {
          select.dataset.models = JSON.stringify(d.models);
          const cur = d.models.find(m => m.id === d.current);
          if (cur) getLabel(select).textContent = cur.name;
        });
      })
      .catch(err => {
        select.classList.remove('switching');
        console.error('切换请求失败:', err);
      });
  }

  // ============ 资源管理器:打开文件夹 ============
  const explorer = document.querySelector('.explorer');
  const sidebar = document.querySelector('.sidebar');
  const openFolderBtn = document.querySelector('.empty-btn[data-action="open-folder"]');
  if (explorer && openFolderBtn) {
    // 空状态紫色按钮:
    //   1) 打开默认家目录 → 文件树
    //   2) 树渲染完成后,再进入 input 模式 —— 此时 path-bar 已存在,
    //      input 预填 = currentRoot(默认路径),path-bar 与 input 自动同步
    openFolderBtn.addEventListener('click', () => {
      openFolder(null, () => showOpenFolderInput());
    });
  }

  // ============ 资源管理器:新建文件 / 新建文件夹 ============
  // 空状态的两个按钮 + 顶栏图标都共用同一处理函数(基于 currentRoot 决定目录)
  async function ensureRoot() {
    if (currentRoot) return currentRoot;
    // 没开过文件夹 → 用服务端默认路径(home 或 /sdcard)
    const r = await fetch('/api/folder');
    const d = await r.json();
    if (d.ok) { currentRoot = d.tree.path; return currentRoot; }
    throw new Error(d.error || '无法获取默认路径');
  }

  // 内联 input 行(与"打开文件夹"input 行同款,prompt() 在嵌入式浏览器被禁)
  function showCreateInput(type) {
    if (!explorer) return;
    // 已存在就先聚焦
    const existing = explorer.querySelector('.explorer-input-row');
    if (existing) { existing.querySelector('input').focus(); return; }

    const placeholder = type === 'file' ? '例如: newfile.txt' : '例如: newfolder';
    const endpoint    = type === 'file' ? '/api/file/create' : '/api/folder/create';

    const inputRow = document.createElement('div');
    inputRow.className = 'explorer-input-row';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'explorer-path-input';
    input.placeholder = placeholder;
    input.spellcheck = false;
    input.autocomplete = 'off';

    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'sidebar-confirm-btn';
    confirmBtn.textContent = '确定';

    inputRow.appendChild(input);
    inputRow.appendChild(confirmBtn);
    explorer.insertBefore(inputRow, explorer.firstChild);
    input.focus();

    const cleanup = () => inputRow.remove();

    const submit = async () => {
      const name = input.value.trim();
      if (!name) { cleanup(); return; }
      confirmBtn.disabled = true;
      try {
        const path = await ensureRoot();
        const r = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, name, content: '' }),
        });
        const d = await r.json();
        if (!d.ok) {
          confirmBtn.disabled = false;
          input.value = '';
          input.placeholder = '失败: ' + d.error;
          input.focus();
          return;
        }
        cleanup();
        openFolder(currentRoot);  // 刷新树
      } catch (e) {
        confirmBtn.disabled = false;
        input.value = '';
        input.placeholder = '请求失败: ' + e;
        input.focus();
      }
    };

    input.addEventListener('keydown', e => {
      if (e.key === 'Enter')      { e.preventDefault(); submit(); }
      else if (e.key === 'Escape'){ e.preventDefault(); cleanup(); }
    });
    input.addEventListener('blur', e => {
      const next = e.relatedTarget;
      if (next && (next === input || next === confirmBtn ||
                   next.closest('.explorer-input-row'))) return;
      cleanup();
    });
    confirmBtn.addEventListener('click', e => { e.preventDefault(); submit(); });
  }

  document.querySelectorAll('[data-action="create-file"]').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); showCreateInput('file'); });
  });
  document.querySelectorAll('[data-action="create-folder"]').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); showCreateInput('folder'); });
  });
  // 顶栏的"打开文件夹"按钮:始终可用,允许随时切换根目录
  const openFolderHeaderBtn = document.querySelector('.sidebar-header [data-action="open-folder-header"]');
  if (openFolderHeaderBtn) {
    openFolderHeaderBtn.addEventListener('click', () => showOpenFolderInput());
  }
  // 路径条(.explorer-path)也是入口:点一下重新进 input 模式
  // 用事件代理,path-bar 在 renderTree 时会被整体替换
  if (explorer) {
    explorer.addEventListener('click', e => {
      if (e.target.closest('.explorer-path')) {
        showOpenFolderInput();
      }
    });
  }

  // 已知已展开的目录 path → 渲染其子树
  const expandedFolders = new Set();
  // 当前已打开的根路径(用于 ↑ 返回上级 & 输入框默认值)
  let currentRoot = null;

  // ── 自动刷新:每 ~2.5s 轮询根目录 + 所有已展开子目录 ──
  // 简单 name+size 签名做 diff,变了就替换对应 .explorer-list 的 DOM。
  const WATCH_INTERVAL_MS = 2500;
  const treeSignature = new Map();   // path -> "D:foo|F:bar.txt:42|..." 字符串签名
  const inflightPoll  = new Map();   // path -> AbortController(同一路径新一轮会中止上一轮)
  let watcherTimer = null;

  function computeSignature(tree) {
    if (!tree) return '';
    const parts = [];
    for (const f of tree.folders) parts.push('D:' + f.name);
    for (const f of tree.files)   parts.push('F:' + f.name + ':' + (f.size || 0));
    parts.sort();
    return parts.join('|');
  }

  function pollPath(path) {
    if (!path) return Promise.resolve(false);
    // 同一路径已有请求在飞,先中止(避免旧响应覆盖新响应,也避免 ERR_ABORTED 噪声)
    const prev = inflightPoll.get(path);
    if (prev) prev.abort();
    const ctrl = new AbortController();
    inflightPoll.set(path, ctrl);
    return fetch(`/api/folder?path=${encodeURIComponent(path)}&_t=${Date.now()}`, { signal: ctrl.signal })
      .then(r => r.json())
      .then(d => {
        if (ctrl.signal.aborted) return false;
        if (!d || !d.ok || !d.tree) return false;
        const sig = computeSignature(d.tree);
        const old = treeSignature.get(path);
        if (old === sig) return false;
        treeSignature.set(path, sig);
        replaceListInDom(path, d.tree);
        return true;
      })
      .catch(err => {
        // 中止是被我们自己触发的,静默忽略;其他错误也保持静默(轮询不应阻塞 UI)
        if (err && err.name === 'AbortError') return false;
        return false;
      })
      .finally(() => {
        if (inflightPoll.get(path) === ctrl) inflightPoll.delete(path);
      });
  }

  function pollAll() {
    if (!currentRoot) return;
    pollPath(currentRoot);
    for (const p of expandedFolders) pollPath(p);
  }

  function startWatcher() {
    stopWatcher();
    if (!currentRoot) return;
    // 立即跑一次(不等 interval)
    pollAll();
    watcherTimer = setInterval(pollAll, WATCH_INTERVAL_MS);
  }

  function stopWatcher() {
    if (watcherTimer) { clearInterval(watcherTimer); watcherTimer = null; }
  }

  // 用新的 tree 替换 DOM 里对应的 .explorer-list 节点
  // 保留原本的 indent 类(展开子层会有),保留 list 节点位置
  function replaceListInDom(path, tree) {
    if (!explorer) return;
    // CSS.escape 防止路径里含特殊字符
    const sel = `.explorer-list[data-path="${CSS.escape(path)}"]`;
    const old = explorer.querySelector(sel);
    if (!old) return;
    const fresh = buildListEl(tree);
    if (old.classList.contains('explorer-list-indent')) {
      fresh.classList.add('explorer-list-indent');
    }
    old.parentNode.replaceChild(fresh, old);
  }

  // ── 手动刷新按钮:强制立刻轮询(忽略签名比较) ──
  const explorerRefresh = document.getElementById('explorer-refresh');
  if (explorerRefresh) {
    explorerRefresh.addEventListener('click', e => {
      e.stopPropagation();
      if (!currentRoot) return;            // 没开过文件夹 → 无目标
      explorerRefresh.classList.add('spinning');
      // 强制清掉所有签名,确保下次 pollPath 一定走 DOM 替换
      treeSignature.clear();
      const tasks = [pollPath(currentRoot)];
      for (const p of expandedFolders) tasks.push(pollPath(p));
      Promise.all(tasks).finally(() => {
        setTimeout(() => explorerRefresh.classList.remove('spinning'), 400);
      });
    });
  }

  // ════════════════════════════════════════════════════════════
  //   工作目录缓存:刷新后回到上次的目录(localStorage 持久化)
  // ════════════════════════════════════════════════════════════
  //   缓存格式 { root: "C:/path", ts: 1234 }
  //   只存 root;expanded 列表每次切换根都会清,暂不持久化
  const EXPLORER_CACHE_KEY = 'codeforge:explorer:state';

  function saveExplorerState() {
    if (!currentRoot) return;
    try {
      localStorage.setItem(EXPLORER_CACHE_KEY, JSON.stringify({
        root: currentRoot,
        ts:   Date.now(),
      }));
    } catch (e) { /* localStorage 不可用(隐私模式/禁用)→ 静默 */ }
  }

  function clearExplorerState() {
    try { localStorage.removeItem(EXPLORER_CACHE_KEY); } catch (e) {}
  }

  function loadExplorerState() {
    try {
      const raw = localStorage.getItem(EXPLORER_CACHE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!data || typeof data.root !== 'string' || !data.root) return null;
      return data;
    } catch (e) { return null; }
  }

  function openFolder(path, onSuccess, options) {
    // 切换根 → 之前打开/展开的所有子层全部关闭,只保留新根
    const isRestore = !!(options && options.isRestore);
    expandedFolders.clear();
    treeSignature.clear();        // 旧签名作废
    const url = path ? `/api/folder?path=${encodeURIComponent(path)}` : '/api/folder';
    fetch(url)
      .then(r => r.json().then(data => ({ status: r.status, data })))
      .then(({ status, data }) => {
        if (status === 200 && data.ok) {
          currentRoot = data.tree.path;
          renderTree(data.tree, /* asRoot */ true);
          // 写入根的签名,启动 watcher
          treeSignature.set(currentRoot, computeSignature(data.tree));
          startWatcher();
          // 持久化:刷新后能回到这里
          saveExplorerState();
          if (onSuccess) onSuccess(data.tree);
        } else {
          showExplorerError((data && data.error) || `请求失败 (${status})`);
          // 恢复失败:目录被删/权限没了 → 清掉过期缓存,免得每次刷新都进死循环
          if (isRestore) clearExplorerState();
        }
      })
      .catch(err => {
        showExplorerError(String(err));
        if (isRestore) clearExplorerState();
      });
  }

  // 顶栏"打开文件夹"按钮触发的内联路径输入框
  function showOpenFolderInput() {
    if (!explorer || !sidebar) return;
    // 已经有输入框:只聚焦
    const existing = explorer.querySelector('.explorer-path-input');
    if (existing) { existing.focus(); existing.select(); return; }

    // 路径条:在 input 模式下与 input 双向同步
    const pathText = explorer.querySelector('.explorer-path-text');
    const basePath = currentRoot || (pathText ? pathText.textContent : '') || '';

    // 把"输入框 + 确定按钮"合并到一行,作为 explorer 的固定头部
    // (position: sticky 浮在文件列表上方,列表滚动时不会跟着滚)
    const inputRow = document.createElement('div');
    inputRow.className = 'explorer-input-row';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'explorer-path-input';
    input.placeholder = '输入文件夹绝对路径 · Enter 打开 · Esc 取消';
    input.spellcheck = false;
    input.autocomplete = 'off';
    if (basePath) input.value = basePath;

    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'sidebar-confirm-btn';
    confirmBtn.textContent = '确定';

    inputRow.appendChild(input);
    inputRow.appendChild(confirmBtn);

    // 同步 path 条文本(初次 / 手动输入)
    const syncPathBar = (p) => {
      if (pathText) pathText.textContent = p || basePath || '/';
    };
    syncPathBar(basePath);

    const cleanup = () => {
      // 取消时把 path 条还原回当前根(可能已被点击 folder 改写过)
      if (pathText) pathText.textContent = currentRoot || basePath || '/';
      inputRow.remove();
    };
    const submit = () => {
      const path = input.value.trim();
      if (path) {
        cleanup();              // 先把输入行拆掉,再切根
        openFolder(path);
      } else {
        cleanup();
      }
    };

    input.addEventListener('keydown', e => {
      if (e.key === 'Enter')      { e.preventDefault(); submit(); }
      else if (e.key === 'Escape'){ e.preventDefault(); cleanup(); }
    });
    // 手动输入时也实时同步
    input.addEventListener('input', () => syncPathBar(input.value.trim()));
    // 点击外部自动关闭 —— 用 relatedTarget 精确判定焦点去向:
    //   - 焦点去了 输入行内任意元素(输入框/确定按钮) / 文件夹行 / 路径条 / 内部文件 → 保留 input 模式
    //   - 焦点跑到 sidebar / explorer 之外 → 关闭
    //   - relatedTarget 为 null(点了 body/不可聚焦元素)→ 关闭
    input.addEventListener('blur', e => {
      const next = e.relatedTarget;
      if (next && (
          next === input ||
          next === confirmBtn ||
          next.closest('.explorer-input-row') ||
          next.closest('.folder.folder-row') ||
          next.closest('.explorer-path') ||
          next.closest('.file')
      )) {
        return;     // 焦点在"input 模式关联元素"内,不关闭
      }
      cleanup();
    });
    confirmBtn.addEventListener('click', e => {
      e.preventDefault();
      submit();
    });

    // 整行插到 explorer 顶部 → 浮在文件列表之上,不会被列表滚走
    explorer.insertBefore(inputRow, explorer.firstChild);
    input.focus();
    input.select();
  }

  function showExplorerError(msg) {
    if (!explorer) return;
    explorer.innerHTML = `<div class="explorer-empty">
      <div class="empty-title" style="color:var(--c-red)">无法打开文件夹</div>
      <div class="empty-title">${escapeHtml(msg)}</div>
      <div class="empty-actions" style="max-width:180px">
        <button class="empty-btn" data-action="open-folder">
          <span>重试</span>
        </button>
      </div>
    </div>`;
    const retry = explorer.querySelector('[data-action="open-folder"]');
    if (retry) retry.addEventListener('click', () => openFolder());
  }

  // ──────── Diff Viewer ────────
  const diffViewerEl = document.getElementById('diff-viewer');
  const diffTabsEl   = document.getElementById('diff-tabs');
  const diffBodyEl   = document.getElementById('diff-body');
  const welcomeEl    = document.getElementById('center-welcome');
  const centerEl     = document.querySelector('.center');
  let diffFiles   = [];           // [{path, name, op}]
  let diffActive  = null;         // 当前选中的 path

  const _EXT_ICON = {
    py: '🐍', pyw: '🐍',
    js: 'JS', mjs: 'JS', jsx: 'JS', ts: 'TS', tsx: 'TS',
    json: '{}', jsonc: '{}',
    html: '<>', htm: '<>',
    css: '#', scss: '#', less: '#',
    md: 'M↓', markdown: 'M↓',
    txt: '📄', log: '📄',
    yml: 'Y↓', yaml: 'Y↓',
    sh: '$_', bash: '$_', zsh: '$_',
  };
  function _fileIcon(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    return _EXT_ICON[ext] || '📄';
  }

  async function loadDiffList() {
    try {
      const r = await fetch('/api/diffs');
      const d = await r.json();
      if (d.ok) setDiffFiles(d.files || []);
    } catch (e) { /* 静默 */ }
  }

  function setDiffFiles(files) {
    diffFiles = files;
    if (diffActive && !files.find(f => f.path === diffActive)) {
      diffActive = files.length ? files[0].path : null;
    } else if (!diffActive && files.length) {
      diffActive = files[0].path;
    } else if (files.length === 0) {
      diffActive = null;
    }
    renderDiffTabs();
    if (diffActive) loadDiff(diffActive);
    else renderDiffBodyEmpty();
  }

  function renderDiffTabs() {
    if (!diffTabsEl) return;
    diffTabsEl.innerHTML = '';
    const hasFiles = diffFiles.length > 0;
    // 切显示:有改动 → 显示 diff viewer;没有 → 显示主页
    if (diffViewerEl) diffViewerEl.style.display = hasFiles ? '' : 'none';
    if (welcomeEl)    welcomeEl.style.display    = hasFiles ? 'none' : '';
    if (centerEl)     centerEl.classList.toggle('diff-mode', hasFiles);
    for (const f of diffFiles) {
      const tab = document.createElement('div');
      tab.className = 'diff-tab op-' + f.op + (f.path === diffActive ? ' active' : '');
      tab.dataset.path = f.path;

      const name = document.createElement('span');
      name.className = 'diff-tab-name';
      name.innerHTML =
        `<span class="diff-tab-icon">${escapeHtml(_fileIcon(f.name))}</span>` +
        escapeHtml(f.name) +
        `<span class="diff-tab-op">${f.op === 'create' ? 'NEW' : f.op === 'remove' ? 'DEL' : 'EDIT'}</span>`;

      const close = document.createElement('span');
      close.className = 'diff-tab-close';
      close.title = '从视图中移除(后端仍保留)';
      close.textContent = '×';
      close.addEventListener('click', e => {
        e.stopPropagation();
        hideDiff(f.path);
      });

      tab.appendChild(name);
      tab.appendChild(close);
      tab.addEventListener('click', () => {
        diffActive = f.path;
        renderDiffTabs();
        loadDiff(f.path);
      });
      diffTabsEl.appendChild(tab);
    }
  }

  // 关闭单个 diff 的视图(后端数据不动,下次"查看更改"会重新拉回来)
  function hideDiff(path) {
    const next = diffFiles.filter(f => f.path !== path);
    setDiffFiles(next);
  }

  async function loadDiff(path) {
    if (!diffBodyEl) return;
    diffBodyEl.innerHTML = '';
    try {
      const r = await fetch('/api/diff?path=' + encodeURIComponent(path));
      const d = await r.json();
      if (!d.ok) {
        diffBodyEl.innerHTML = `<div class="diff-line meta">⚠ ${escapeHtml(d.error || '无 diff')}</div>`;
        return;
      }
      for (const ln of d.lines || []) {
        const row = document.createElement('div');
        row.className = 'diff-line ' + ln.type;
        let gutter = ' ';
        if (ln.type === 'add') gutter = '+';
        else if (ln.type === 'del') gutter = '−';
        else if (ln.type === 'hunk') gutter = '@';
        row.innerHTML =
          `<span class="diff-gutter">${gutter}</span>` +
          `<span class="diff-text">${escapeHtml(ln.text)}</span>`;
        diffBodyEl.appendChild(row);
      }
      // 没行时给个提示
      if (!(d.lines || []).length) {
        diffBodyEl.innerHTML = `<div class="diff-line meta">(无变化)</div>`;
      }
    } catch (e) {
      diffBodyEl.innerHTML = `<div class="diff-line meta">⚠ 加载失败:${escapeHtml(String(e))}</div>`;
    }
  }

  function renderDiffBodyEmpty() {
    if (!diffBodyEl) return;
    diffBodyEl.innerHTML = '';
  }

  // ──────── SCM(源代码管理) ────────
  const scmBranchEl  = document.getElementById('scm-branch');
  const scmAheadEl   = document.getElementById('scm-ahead');
  const scmChangesEl = document.getElementById('scm-changes');
  const scmRefreshEl = document.getElementById('scm-refresh');
  const scmMsgEl     = document.getElementById('scm-commit-msg');
  const scmCommitBtn = document.getElementById('scm-commit-btn');
  const scmPushBtn   = document.getElementById('scm-push-btn');

  let scmStatus = null;   // {branch, ahead, behind, changes: [...]}
  let scmBusy   = false;  // 防止并发

  async function loadGitStatus() {
    if (!scmChangesEl) return;
    let cwd = currentRoot;
    if (!cwd) {
      try {
        const r = await fetch('/api/folder');
        const d = await r.json();
        if (d.ok) cwd = d.tree.path;
      } catch (e) { /* 静默 */ }
    }
    if (!cwd) {
      renderScmNotGit('未打开任何目录');
      return;
    }
    try {
      const r = await fetch('/api/git/status?path=' + encodeURIComponent(cwd));
      const d = await r.json();
      if (!d.ok) {
        renderScmError(d.error || '加载失败');
        return;
      }
      if (!d.is_git) {
        renderScmNotGit('当前目录不是 git 仓库');
        return;
      }
      scmStatus = d;
      renderScm();
    } catch (e) {
      renderScmError(String(e));
    }
  }
  function renderScmNotGit(msg) {
    if (scmBranchEl) scmBranchEl.textContent = '—';
    if (scmAheadEl)  scmAheadEl.textContent  = '';
    if (!scmChangesEl) return;
    scmChangesEl.innerHTML = `<div class="scm-not-git">${escapeHtml(msg)}</div>`;
  }
  function renderScmError(msg) {
    if (!scmChangesEl) return;
    scmChangesEl.innerHTML = `<div class="scm-error">⚠ ${escapeHtml(msg)}</div>`;
  }
  function renderScm() {
    if (!scmStatus || !scmChangesEl) return;
    if (scmBranchEl) scmBranchEl.textContent = scmStatus.branch || '—';
    if (scmAheadEl) {
      const a = scmStatus.ahead  || 0;
      const b = scmStatus.behind || 0;
      scmAheadEl.textContent = (a || b) ? `↑${a} ↓${b}` : '';
    }
    const changes = scmStatus.changes || [];
    const staged   = changes.filter(c => c.staged);
    const unstaged = changes.filter(c => !c.staged);

    let html = '';
    if (staged.length) {
      html += `<div class="scm-section-title">暂存的更改 <span class="scm-count">${staged.length}</span></div>`;
      html += staged.map(renderScmFile).join('');
    }
    if (unstaged.length) {
      const title = staged.length ? '更改' : '更改';
      html += `<div class="scm-section-title">${title} <span class="scm-count">${unstaged.length}</span></div>`;
      html += unstaged.map(renderScmFile).join('');
    }
    if (!staged.length && !unstaged.length) {
      html = `<div class="scm-empty">✓ 工作区干净</div>`;
    }
    scmChangesEl.innerHTML = html;

    scmChangesEl.querySelectorAll('.scm-file').forEach(row => {
      const path = row.dataset.path;
      const staged = row.classList.contains('staged');
      row.addEventListener('click', e => {
        if (e.target.closest('.scm-file-actions')) return;
        showGitDiff(path, staged);
      });
      row.querySelector('.scm-act-discard')?.addEventListener('click', e => {
        e.stopPropagation(); gitDiscard(path);
      });
      row.querySelector('.scm-act-stage')?.addEventListener('click', e => {
        e.stopPropagation(); if (staged) gitUnstage(path); else gitStage(path);
      });
    });
  }

  function renderScmFile(c) {
    const x = c.x || '?';
    const cls = (c.staged ? 'staged' : '');
    const actStage = c.staged ? '−' : '+';
    const stageTitle = c.staged ? '取消暂存' : '暂存';
    return `<div class="scm-file ${cls}" data-path="${escapeHtml(c.path)}" title="${escapeHtml(c.path)}">
      <span class="scm-status ${escapeHtml(x)}">${escapeHtml(x === '?' ? '?' : x)}</span>
      <span class="scm-name">${escapeHtml(c.path)}</span>
      <span class="scm-file-actions">
        <span class="scm-act-stage" title="${stageTitle}">${actStage}</span>
        <span class="scm-act-discard" title="放弃改动">⟲</span>
      </span>
    </div>`;
  }

  async function gitStage(path) {
    if (scmBusy) return;
    scmBusy = true;
    try {
      await fetch('/api/git/stage', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: currentRoot, path }) });
      await loadGitStatus();
    } finally { scmBusy = false; }
  }
  async function gitUnstage(path) {
    if (scmBusy) return;
    scmBusy = true;
    try {
      await fetch('/api/git/unstage', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: currentRoot, path }) });
      await loadGitStatus();
    } finally { scmBusy = false; }
  }
  async function gitDiscard(path) {
    if (!confirm(`放弃 ${path} 的本地改动?`)) return;
    if (scmBusy) return;
    scmBusy = true;
    try {
      await fetch('/api/git/discard', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: currentRoot, path }) });
      await loadGitStatus();
    } finally { scmBusy = false; }
  }
  async function gitCommit() {
    const msg = (scmMsgEl?.value || '').trim();
    if (!msg) { scmMsgEl?.focus(); return; }
    if (scmBusy) return;
    scmBusy = true;
    scmCommitBtn.disabled = true;
    try {
      const r = await fetch('/api/git/commit', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: currentRoot, message: msg }) });
      const d = await r.json();
      if (d.ok) {
        scmMsgEl.value = '';
        appendStatus(`✓ commit ${d.hash || ''}`);
        await loadGitStatus();
      } else {
        appendStatus('✗ commit 失败: ' + (d.output || d.error || ''));
      }
    } finally {
      scmBusy = false;
      scmCommitBtn.disabled = false;
    }
  }

  async function gitPush() {
    if (scmBusy) return;
    if (!confirm('推送到当前 upstream?\n\n认证:确保系统 git 已配好 SSH key 或 credential manager。')) return;
    scmBusy = true;
    scmPushBtn.disabled = true;
    try {
      const r = await fetch('/api/git/push', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: currentRoot }) });
      const d = await r.json();
      if (d.ok) {
        appendStatus('✓ 推送成功');
        await loadGitStatus();
      } else {
        const err = d.output || d.error || '';
        appendStatus('✗ 推送失败: ' + err);
        if (/could not read username|password|authentication|permission denied/i.test(err)) {
          appendStatus('💡 提示:系统 git 没配好认证,先在终端跑一次 `git push` 让它记住凭据。');
        } else if (/non-fast-forward|rejected|fetch first/i.test(err)) {
          appendStatus('💡 提示:远程有新的提交,先 `git pull` 再 push。');
        }
      }
    } finally {
      scmBusy = false;
      scmPushBtn.disabled = false;
    }
  }

  async function showGitDiff(path, staged) {
    try {
      const r = await fetch('/api/git/diff?path=' + encodeURIComponent(path)
        + '&cwd=' + encodeURIComponent(currentRoot || '')
        + (staged ? '&staged=1' : ''));
      const d = await r.json();
      if (!d.ok || !d.diff) {
        appendStatus('该文件无 diff');
        return;
      }
      renderGitDiffInline(path, d.diff);
    } catch (e) {
      appendStatus('diff 加载失败: ' + e);
    }
  }
  function renderGitDiffInline(path, diffText) {
    if (!diffViewerEl) return;
    centerEl?.classList.add('diff-mode');
    if (welcomeEl) welcomeEl.style.display = 'none';
    if (diffViewerEl) diffViewerEl.style.display = '';
    const name = path.split(/[\\/]/).pop();
    diffFiles = [{ path: '[git] ' + path, name, op: 'edit' }];
    diffActive = diffFiles[0].path;
    diffTabsEl.innerHTML = '';
    const tab = document.createElement('div');
    tab.className = 'diff-tab op-edit active';
    tab.innerHTML =
      `<span class="diff-tab-name">🔧 ${escapeHtml(name)} <span class="diff-tab-op">GIT</span></span>`;
    diffTabsEl.appendChild(tab);
    diffBodyEl.innerHTML = '';
    for (const ln of (diffText || '').split('\n')) {
      let type = 'ctx', gutter = ' ';
      if (ln.startsWith('+++') || ln.startsWith('---')) type = 'meta';
      else if (ln.startsWith('@@')) { type = 'hunk'; gutter = '@'; }
      else if (ln.startsWith('+')) { type = 'add'; gutter = '+'; }
      else if (ln.startsWith('-')) { type = 'del'; gutter = '−'; }
      const row = document.createElement('div');
      row.className = 'diff-line ' + type;
      row.innerHTML =
        `<span class="diff-gutter">${gutter}</span>` +
        `<span class="diff-text">${escapeHtml(ln)}</span>`;
      diffBodyEl.appendChild(row);
    }
    diffBodyEl.scrollTop = 0;
    diffTabsEl.classList.remove('flash');
    void diffTabsEl.offsetWidth;
    diffTabsEl.classList.add('flash');
  }

  // SCM 事件绑定
  if (scmRefreshEl) scmRefreshEl.addEventListener('click', () => loadGitStatus());
  if (scmCommitBtn) scmCommitBtn.addEventListener('click', () => gitCommit());
  if (scmPushBtn)   scmPushBtn.addEventListener('click', () => gitPush());
  if (scmMsgEl) {
    scmMsgEl.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        gitCommit();
      }
    });
  }

  function renderTree(tree, asRoot) {
    if (!explorer) return;
    if (asRoot) {
      // 顶层:包含路径头 + 列表
      explorer.innerHTML = '';
      explorer.appendChild(buildPathBar(tree.path, /* canGoUp */ tree.path !== '/'));
      explorer.appendChild(buildListEl(tree));
    } else {
      // 子层:仅追加列表
      explorer.appendChild(buildListEl(tree));
    }
  }

  function buildPathBar(path, canGoUp) {
    const bar = document.createElement('div');
    bar.className = 'explorer-path';
    bar.title = path;

    const up = document.createElement('span');
    up.className = 'explorer-path-up';
    up.title = canGoUp ? '返回上级' : '已是根目录';
    up.textContent = '↑';
    if (canGoUp) {
      up.addEventListener('click', () => openFolder(parentDir(path)));
    } else {
      up.style.opacity = '0.35';
      up.style.cursor = 'default';
    }
    bar.appendChild(up);

    const text = document.createElement('span');
    text.className = 'explorer-path-text';
    text.textContent = shortenPath(path);
    bar.appendChild(text);

    return bar;
  }

  function buildListEl(tree) {
    const wrap = document.createElement('div');
    wrap.className = 'explorer-list';
    wrap.dataset.path = tree.path;

    if (tree.folders.length === 0 && tree.files.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'explorer-list-empty';
      empty.textContent = '空目录';
      wrap.appendChild(empty);
      return wrap;
    }

    for (const f of tree.folders) {
      wrap.appendChild(buildFolderRow(f));
    }
    for (const f of tree.files) {
      wrap.appendChild(buildFileRow(f));
    }
    return wrap;
  }

  function buildFolderRow(folder) {
    const row = document.createElement('div');
    row.className = 'folder folder-row';
    row.dataset.path = folder.path;
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');
    row.title = folder.path;

    const icon = document.createElement('span');
    icon.className = 'folder-row-icon';
    icon.innerHTML = `
      <svg viewBox="0 0 16 16" width="13" height="13" fill="none"
           stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M2 5a2 2 0 012-2h3l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V5z"/>
      </svg>`;
    row.appendChild(icon);

    const name = document.createElement('span');
    name.className = 'folder-name';
    name.textContent = folder.name;
    row.appendChild(name);

    const chevron = document.createElement('span');
    chevron.className = 'folder-row-chevron';
    chevron.innerHTML = `
      <svg viewBox="0 0 16 16" width="9" height="9" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 6l4 4 4-4"/>
      </svg>`;
    row.appendChild(chevron);

    const open = () => toggleFolder(row, folder);
    row.addEventListener('click', open);
    // 键盘可达:Enter / Space 也能展开
    row.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open();
      }
    });
    return row;
  }

  function buildFileRow(file) {
    const row = document.createElement('div');
    row.className = 'file';
    row.dataset.path = file.path;
    const icon = document.createElement('span');
    icon.className = `file-icon ${fileIconClass(file.name)}`;
    icon.textContent = fileExt(file.name);
    row.appendChild(icon);
    const name = document.createElement('span');
    name.textContent = file.name;
    row.appendChild(name);
    row.title = file.path;
    return row;
  }

  // ============ 中间区:文件编辑器(CodeMirror + 语法高亮) ============
  // 点击 .file 行 → fetch /api/file/read → 替换 .center 内容
  if (explorer) {
    explorer.addEventListener('click', e => {
      const row = e.target.closest('.file');
      if (!row) return;
      const filePath = row.dataset.path || row.title;
      if (filePath) openFileInEditor(filePath);
    });
  }

  const center = document.querySelector('.center');
  let currentEditor = null;  // {cm, path, name, saved}

  // 文件扩展名 → CodeMirror mode 映射(未列出的走纯文本)
  const MODE_MAP = {
    py: 'python',
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    jsx: 'jsx',
    ts: 'text/typescript', tsx: 'text/typescript-jsx',
    json: 'application/json', jsonc: 'application/json',
    html: 'htmlmixed', htm: 'htmlmixed', xhtml: 'htmlmixed', vue: 'htmlmixed',
    xml: 'xml', svg: 'xml',
    css: 'css', scss: 'text/x-scss', less: 'text/x-less',
    md: 'markdown', markdown: 'markdown',
    yml: 'yaml', yaml: 'yaml',
    sh: 'shell', bash: 'shell', zsh: 'shell',
    sql: 'sql',
    c: 'text/x-csrc', h: 'text/x-csrc',
    cpp: 'text/x-c++src', cc: 'text/x-c++src', cxx: 'text/x-c++src',
    hpp: 'text/x-c++src', hxx: 'text/x-c++src',
    java: 'text/x-java',
    cs: 'text/x-csharp',
    go: 'text/x-go',
    rs: 'text/x-rust',
    kt: 'text/x-kotlin',
    swift: 'text/x-swift',
    php: 'application/x-httpd-php',
    rb: 'text/x-ruby',
    lua: 'text/x-lua',
    toml: 'text/x-toml',
    ini: 'text/x-ini',
    log: 'text/x-log',
  };
  function modeFor(name) { return MODE_MAP[name.split('.').pop().toLowerCase()] || null; }

  function openFileInEditor(filePath) {
    if (!center) return;
    center.classList.add('editor-mode');
    center.innerHTML = '<div class="editor-loading">加载中...</div>';
    fetch(`/api/file/read?path=${encodeURIComponent(filePath)}`)
      .then(r => r.json().then(data => ({ status: r.status, data })))
      .then(({ status, data }) => {
        if (status === 200 && data.ok) {
          renderEditor(data);
        } else {
          renderCenterError((data && data.error) || `请求失败 (${status})`);
        }
      })
      .catch(err => renderCenterError(String(err)));
  }

  function renderEditor(file) {
    if (!center) return;
    center.classList.add('editor-mode');
    center.innerHTML = '';

    const header = document.createElement('div');
    header.className = 'editor-header';

    const name = document.createElement('span');
    name.className = 'editor-name';
    name.textContent = file.name;
    name.title = file.path;
    header.appendChild(name);

    const status = document.createElement('span');
    status.className = 'editor-status';
    status.textContent = `${formatSize(file.size)} · ${file.content.split('\n').length} 行`;
    header.appendChild(status);

    const saveBtn = document.createElement('button');
    saveBtn.className = 'editor-save-btn';
    saveBtn.textContent = '保存';
    saveBtn.title = '保存 (Ctrl+S)';
    header.appendChild(saveBtn);

    const close = document.createElement('span');
    close.className = 'editor-close';
    close.textContent = '×';
    close.title = '关闭';
    header.appendChild(close);

    // CodeMirror 容器
    const cmHost = document.createElement('div');
    cmHost.className = 'editor-cm-host';

    center.appendChild(header);
    center.appendChild(cmHost);

    const mode = modeFor(file.name);
    const cm = CodeMirror(cmHost, {
      value: file.content,
      mode: mode,
      theme: 'dracula',
      lineNumbers: true,
      indentUnit: 4,
      tabSize: 4,
      indentWithTabs: false,
      lineWrapping: false,
      autofocus: false,
      matchBrackets: true,
      autoCloseBrackets: true,
      autoCloseTags: true,
      foldGutter: true,
      gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'],
      scrollbarStyle: 'native',   // 用浏览器原生滚动条,样式由全局 ::-webkit-scrollbar 统一控制
      extraKeys: {
        'Ctrl-S': () => saveCurrentFile(),
        'Cmd-S':  () => saveCurrentFile(),
      },
    });
    // 等容器有尺寸再刷新(否则首屏空)
    requestAnimationFrame(() => cm.refresh());

    let saved = file.content;
    let dirty = false;
    setStatus(saved, dirty, file.size);

    cm.on('change', () => {
      const v = cm.getValue();
      if (v === saved) {
        if (dirty) { dirty = false; setStatus(saved, dirty, file.size); }
      } else {
        if (!dirty) { dirty = true; setStatus(saved, dirty, file.size); }
      }
    });

    function setStatus(s, d, sz) {
      if (d) {
        status.classList.add('editor-dirty');
        status.textContent = '● 未保存';
        saveBtn.disabled = false;
      } else {
        status.classList.remove('editor-dirty');
        status.textContent = `${formatSize(sz || new Blob([s]).size)} · ${s.split('\n').length} 行`;
        saveBtn.disabled = true;
      }
    }

    function flashSaved() {
      status.classList.add('editor-saved');
      status.textContent = '✓ 已保存';
      setTimeout(() => {
        status.classList.remove('editor-saved');
        status.textContent = `${formatSize(file.size)} · ${file.content.split('\n').length} 行`;
      }, 1500);
    }

    function save() {
      if (!dirty) return;
      saveBtn.disabled = true;
      status.classList.remove('editor-saved', 'editor-dirty');
      status.textContent = '保存中…';
      fetch('/api/file/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: file.path, content: cm.getValue() }),
      })
        .then(r => r.json().then(data => ({ status: r.status, data })))
        .then(({ status, data }) => {
          if (status === 200 && data.ok) {
            saved = cm.getValue();
            dirty = false;
            file.size = data.size;
            flashSaved();
          } else {
            status.textContent = '✗ 保存失败: ' + ((data && data.error) || status);
            saveBtn.disabled = false;
          }
        })
        .catch(err => {
          status.textContent = '✗ 保存失败: ' + err;
          saveBtn.disabled = false;
        });
    }

    saveBtn.addEventListener('click', save);
    close.addEventListener('click', () => {
      if (dirty && !confirm('有未保存的修改,确定关闭吗?')) return;
      showCenterEmpty();
    });
    window.addEventListener('beforeunload', e => {
      if (dirty) { e.preventDefault(); e.returnValue = ''; }
    });

    currentEditor = { cm, file, save, getDirty: () => dirty };
  }

  function saveCurrentFile() {
    if (currentEditor && !currentEditor.cm.getOption('readOnly')) {
      currentEditor.save();
    }
  }

  function renderCenterError(msg) {
    if (!center) return;
    center.classList.add('editor-mode');
    center.innerHTML = `<div class="editor-error">读取失败: ${escapeHtml(msg)}</div>`;
  }

  function showCenterEmpty() {
    if (!center) return;
    center.classList.remove('editor-mode');
    // 恢复空状态(完全复制 index.html 里的结构)
    center.innerHTML = `
      <div class="center-icon">&lt;/&gt;</div>
      <div class="center-title">CodeForge</div>
      <div class="center-sub">AI 驱动的网页编码工作台</div>
      <div class="shortcuts">
        <div class="shortcut-row"><span class="kbd">Ctrl+J</span> 切换 Agent 栏</div>
        <div class="shortcut-row"><span class="kbd">Ctrl+B</span> 切换资源管理器</div>
      </div>`;
  }

  function formatSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function toggleFolder(rowEl, folder) {
    const isExpanded = expandedFolders.has(folder.path);

    // input 模式下,点击 folder = "选中":把该 folder 的路径写入 input 与 path 条,
    // 这样点确定就能打开它,不必手敲路径。展开/折叠照旧,方便浏览。
    const liveInput = explorer.querySelector('.explorer-path-input');
    if (liveInput) {
      liveInput.value = folder.path;
      const livePathText = explorer.querySelector('.explorer-path-text');
      if (livePathText) livePathText.textContent = folder.path;
    }

    if (isExpanded) {
      // 收起:移除紧随其后的子层
      expandedFolders.delete(folder.path);
      rowEl.classList.remove('expanded');
      let next = rowEl.nextElementSibling;
      while (next && next.classList.contains('explorer-list') && isChildOf(next, folder.path)) {
        const toRemove = next;
        next = next.nextElementSibling;
        toRemove.remove();
      }
    } else {
      expandedFolders.add(folder.path);
      rowEl.classList.add('expanded');
      rowEl.classList.add('loading');
      fetch(`/api/folder?path=${encodeURIComponent(folder.path)}`)
        .then(r => r.json().then(data => ({ status: r.status, data })))
        .then(({ status, data }) => {
          rowEl.classList.remove('loading');
          if (status === 200 && data.ok) {
            const list = buildListEl(data.tree);
            // 缩进:把列表里的行再加一层 padding
            list.classList.add('explorer-list-indent');
            rowEl.parentNode.insertBefore(list, rowEl.nextSibling);
            // 写入子目录签名,后续轮询才能 diff
            treeSignature.set(folder.path, computeSignature(data.tree));
          } else {
            console.error('展开失败:', (data && data.error) || status);
            rowEl.classList.remove('expanded');
            expandedFolders.delete(folder.path);
          }
        })
        .catch(err => {
          console.error('展开失败:', err);
          rowEl.classList.remove('loading');
          rowEl.classList.remove('expanded');
          expandedFolders.delete(folder.path);
        });
    }
  }

  // 判断 listEl 是否为 folder 的直接子层:
  // buildListEl 把子层 div 的 data-path 设为该 folder 自己的路径,
  // 所以判定应当是"列表的 path === folder 的 path",而不是"列表的父路径 === folder"。
  function isChildOf(listEl, folderPath) {
    return listEl.dataset.path === folderPath;
  }

  function parentDir(path) {
    if (!path || path === '/') return '/';
    const idx = path.lastIndexOf('/');
    if (idx <= 0) return '/';
    return path.substring(0, idx);
  }

  function shortenPath(path, max = 36) {
    if (!path || path.length <= max) return path || '/';
    const head = path.slice(0, 14);
    const tail = path.slice(-(max - 14 - 1));
    return `${head}…${tail}`;
  }

  function fileExt(name) {
    const m = name.match(/\.([^.\\/]+)$/);
    return m ? m[1].slice(0, 4) : '';
  }

  function fileIconClass(name) {
    const ext = fileExt(name).toLowerCase();
    if (['tsx', 'ts', 'jsx', 'js', 'py', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'css', 'html'].includes(ext)) return ext === 'js' ? 'ts' : (ext === 'jsx' ? 'tsx' : ext);
    if (ext === 'json') return 'json';
    if (ext === 'md') return 'md';
    if (['yml', 'yaml', 'toml', 'ini', 'conf', 'cfg'].includes(ext)) return 'cfg';
    if (ext === 'git') return 'git';
    return '';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // ============================================================
  //                Agent Chat(发送键 / 历史 / 待确认)
  // ============================================================
  const aiMessages  = document.getElementById('ai-messages');
  const aiInput     = document.getElementById('ai-input');
  const sendBtn     = document.getElementById('send-btn');
  const aiStatus    = document.getElementById('ai-status');
  const togglePlan  = document.getElementById('toggle-plan');
  const toggleAuto  = document.getElementById('toggle-auto');
  const chatClear   = document.getElementById('chat-clear');
  const chatRefresh = document.getElementById('chat-refresh');
  const pendingCard     = document.getElementById('pending-card');
  const pendingBadge    = document.getElementById('pending-badge');
  const pendingTitle    = document.getElementById('pending-title');
  const pendingAction   = document.getElementById('pending-action');
  const pendingBody     = document.getElementById('pending-body');
  const pendingConfirm  = document.getElementById('pending-confirm');
  const pendingReject   = document.getElementById('pending-reject');

  // 客户端 history(后端也会存,这里再保留一份方便下次进入时直接用)
  let history = [];
  let sending = false;

  // ──────── 初始化:拉 history + agent state ────────
  function loadChat() {
    fetch('/api/chat')
      .then(r => r.json())
      .then(data => {
        if (!data || !data.ok) return;
        history = Array.isArray(data.history) ? data.history : [];
        if (data.state) applyAgentState(data.state);
        renderHistory();
      })
      .catch(err => console.error('拉取 chat 状态失败:', err));
  }

  function loadAgentState() {
    fetch('/api/agent/state')
      .then(r => r.json())
      .then(data => { if (data && data.ok) applyAgentState(data.state); })
      .catch(err => console.error('拉取 agent 状态失败:', err));
  }

  function applyAgentState(s) {
    if (!s) return;
    if (typeof s.plan_model === 'boolean' && togglePlan) togglePlan.checked = s.plan_model;
    if (typeof s.auto === 'boolean' && toggleAuto) toggleAuto.checked = s.auto;
    renderPending(s.pending);
  }

  // ──────── 渲染消息历史(从 history 还原) ────────
  function renderHistory() {
    if (!aiMessages) return;
    // 只清掉旧的"真实"消息(保留首屏欢迎语?这里清掉,统一从 history 渲染)
    aiMessages.innerHTML = '';
    if (!history || history.length === 0) {
      // 空时插回欢迎语
      aiMessages.innerHTML = `
        <div class="msg msg-assistant">
          <div class="msg-label">Assistant</div>
          <div class="msg-bubble">您好!我是 Agent。我可以帮助您编写、重构、解释和调试代码。请问您想做什么?</div>
        </div>`;
      return;
    }
    for (const m of history) appendHistoryNode(m);
    scrollToBottom();
  }

  function appendHistoryNode(m) {
    if (!aiMessages || !m) return;
    if (m.role === 'user') {
      appendMessage('user', m.content || '');
    } else if (m.role === 'assistant') {
      if (m.content) appendMessage('assistant', m.content);
      if (Array.isArray(m.tool_calls)) {
        for (const tc of m.tool_calls) {
          appendToolCall(tc);
        }
      }
    } else if (m.role === 'tool') {
      appendToolResult(m);
    }
  }

  function appendMessage(role, text) {
    if (!aiMessages) return;
    const div = document.createElement('div');
    div.className = `msg msg-${role}`;
    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = role === 'user' ? 'You' : 'Assistant';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.innerHTML = formatMarkdownLite(text || '');
    div.appendChild(label);
    div.appendChild(bubble);
    aiMessages.appendChild(div);
    scrollToBottom();
  }

  function appendToolCall(tc) {
    if (!aiMessages || !tc || !tc.function) return;
    const div = document.createElement('div');
    div.className = 'msg msg-tool';
    const toolName = tc.function.name || 'tool';
    div.innerHTML = `
      <div class="msg-label">🔧 调用工具:<span class="tool-name"> ${escapeHtml(toolName)}</span></div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
  }

  function appendToolResult(m) {
    if (!aiMessages) return;
    const div = document.createElement('div');
    div.className = 'msg msg-tool-result';
    const txt = (m.content || '').toString();
    // 判定成功/失败:尝试解析 JSON,看 ok / success / error 字段
    let parsed = null;
    try { parsed = JSON.parse(txt); } catch (e) { /* 非 JSON */ }
    let status = '成功';
    let statusClass = 'tool-status-ok';
    if (parsed && typeof parsed === 'object') {
      if (parsed.ok === false || parsed.success === false || parsed.error) {
        status = '失败';
        statusClass = 'tool-status-fail';
      } else if (parsed.ok === true || parsed.success === true) {
        status = '成功';
      }
    } else if (/^(error|err|fail|failed|exception)/i.test(txt.trim())) {
      status = '失败';
      statusClass = 'tool-status-fail';
    }
    div.innerHTML = `
      <div class="msg-label">↳ 结果:<span class="tool-status ${statusClass}"> ${status}</span></div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
  }

  function appendLoading() {
    if (!aiMessages) return null;
    const div = document.createElement('div');
    div.className = 'msg msg-assistant msg-loading';
    div.innerHTML = `
      <div class="msg-label">Assistant</div>
      <div class="msg-bubble">
        <span class="dot"></span><span class="dot"></span><span class="dot"></span>
        <span class="msg-loading-status">开始运行…</span>
      </div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
    return div;
  }
  function setLoadingStatus(loadingEl, text) {
    if (!loadingEl) return;
    const el = loadingEl.querySelector('.msg-loading-status');
    if (el) el.textContent = text;
  }

  function appendStatus(text) {
    if (!aiMessages) return null;
    const div = document.createElement('div');
    div.className = 'msg msg-status';
    div.innerHTML = `<div class="msg-status-text">${escapeHtml(text)}</div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
    return div;
  }

  // 完成状态行:一行小字 + 一个"查看更改"按钮,点击聚焦 diff viewer
  function appendCompleteStatus(fileCount) {
    if (!aiMessages) return null;
    const div = document.createElement('div');
    div.className = 'msg msg-status msg-status-done';
    const fileLabel = fileCount > 0 ? `共修改 ${fileCount} 个文件` : '没有文件改动';
    div.innerHTML =
      `<span class="msg-status-text">✓ 运行完成 · ${escapeHtml(fileLabel)}</span>` +
      (fileCount > 0
        ? `<button class="msg-status-action" id="msg-view-diff">查看更改</button>`
        : '');
    aiMessages.appendChild(div);
    scrollToBottom();
    const btn = div.querySelector('#msg-view-diff');
    if (btn) btn.addEventListener('click', () => focusDiffViewer());
    return div;
  }

  // 把视线拉到 diff viewer:重新拉后端列表(把之前隐藏的也带回来),滚动到顶 + tab 闪一下
  async function focusDiffViewer() {
    await loadDiffList();            // 后端 diff 列表(可能含被隐藏但未清的)
    if (diffBodyEl) diffBodyEl.scrollTop = 0;
    const tabs = diffTabsEl;
    if (!tabs) return;
    tabs.classList.remove('flash');
    void tabs.offsetWidth;           // 强制 reflow,重新触发动画
    tabs.classList.add('flash');
  }

  function scrollToBottom() {
    if (!aiMessages) return;
    requestAnimationFrame(() => {
      aiMessages.scrollTop = aiMessages.scrollHeight;
    });
  }

// 完整 markdown 渲染:标题/列表/引用/表格/链接/代码块 等
  // marked.parse → DOMPurify.sanitize 防 XSS
  function formatMarkdownLite(s) {
    if (!s) return '';
    if (typeof marked === 'undefined') {
      // 兜底:CDN 加载失败时退回转义 + 换行
      return escapeHtml(s).replace(/\n/g, '<br>');
    }
    marked.setOptions({ gfm: true, breaks: true });
    const raw = marked.parse(s);
    return typeof DOMPurify !== 'undefined'
      ? DOMPurify.sanitize(raw, { ADD_ATTR: ['target', 'rel'] })
      : raw;
  }

  // ──────── 待确认卡片 ────────
  function renderPending(pending) {
    if (!pending) {
      if (pendingCard)  pendingCard.classList.add('hidden');
      if (pendingBadge) pendingBadge.classList.add('hidden');
      return;
    }
    if (pendingCard)  pendingCard.classList.remove('hidden');
    if (pendingBadge) pendingBadge.classList.remove('hidden');
    if (pendingAction) pendingAction.textContent = pending.action || '';
    if (pendingTitle)  pendingTitle.textContent = '⚠ 待确认:' + (pending.action || '');
    if (pendingBody) {
      const md = pending.markdown
        || JSON.stringify(pending.args, null, 2)
        || '';
      pendingBody.innerHTML = formatMarkdownLite(md);
    }
  }

  // ──────── 发送消息(流式) ────────
  async function sendMessage() {
    if (sending) return;
    if (!aiInput) return;
    const text = aiInput.value.trim();
    if (!text) return;

    sending = true;
    sendBtn && (sendBtn.disabled = true);
    if (aiStatus) aiStatus.textContent = '思考中…';

    // 1. 立即把用户消息渲染上去
    appendMessage('user', text);
    aiInput.value = '';
    autoResize();
    history.push({ role: 'user', content: text });

    // 2. loading 占位 + 顶栏绿灯
    const loading = appendLoading();
    setAgentLight('running');

    // 跟踪当前 round/max,用来拼状态文字
    let roundNow = 0, roundMax = 0, lastTool = '';
    // 本轮新增的 diff 数量(只数本轮新改的文件,不看历史累计)
    let diffsThisTurn = 0;

    try {
      const resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: history,
          cwd: currentRoot || null,
          plan_model: togglePlan ? togglePlan.checked : null,
          max_rounds: appConfig.max_round || 20,
          flow: appConfig.flow,
        }),
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buf = '';
      let finalData = null;
      // 流式输出:思考块 + 正文块
      let thinkEl  = null, thinkTextEl = null;
      let answerEl = null, answerTextEl = null;
      let streamedAnswer = false;

      // SSE 解析:按 \n\n 分块,每块含 event/data 行
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const lines = block.split('\n');
          let evName = 'message', evData = null;
          for (const ln of lines) {
            if (ln.startsWith('event:')) evName = ln.slice(6).trim();
            else if (ln.startsWith('data:')) {
              try { evData = JSON.parse(ln.slice(5).trim()); }
              catch { evData = ln.slice(5).trim(); }
            }
          }
          if (!evData) continue;

          if (evName === 'start') {
            setLoadingStatus(loading, '开始运行…');
          } else if (evName === 'round') {
            setLoadingStatus(loading, '调用模型…');
          } else if (evName === 'reasoning_delta') {
            // 思考过程:小字、斜体、dim
            if (!thinkEl) {
              const built = makeStreamBubble(loading, 'msg-thinking');
              thinkEl = built.bubble;
              thinkTextEl = built.text;
            }
            thinkTextEl.textContent += evData.text;
            scrollToBottom();
          } else if (evName === 'content_delta') {
            // 正文:正常样式
            if (!answerEl) {
              const built = makeStreamBubble(loading, 'msg-assistant msg-stream-answer');
              answerEl = built.bubble;
              answerTextEl = built.text;
            }
            answerTextEl.textContent += evData.text;
            streamedAnswer = true;
            scrollToBottom();
          } else if (evName === 'tool_call') {
            lastTool = evData.name;
            setLoadingStatus(loading, `调用工具: ${lastTool}`);
            const tc = { function: { name: evData.name, arguments: JSON.stringify(evData.args || {}) } };
            insertBeforeLoading(loading, () => appendToolCallRaw(tc));
          } else if (evName === 'tool_result') {
            setLoadingStatus(loading,
              `${lastTool} → ${evData.ok ? '成功' : '失败'}`);
            const m = { content: evData.content || '' };
            insertBeforeLoading(loading, () => appendToolResultRaw(m));
          } else if (evName === 'pending') {
            setLoadingStatus(loading, '等待用户确认');
          } else if (evName === 'diff_updated') {
            diffsThisTurn++;
            setDiffFiles(evData.files || []);
          } else if (evName === 'done') {
            finalData = evData;
            // 收到 done:停掉流光标
            if (answerEl) answerEl.parentElement?.classList.add('msg-stream-done');
            if (thinkEl)  thinkEl.parentElement?.classList.add('msg-stream-done');
          } else if (evName === 'error') {
            throw new Error(evData.message || '流式错误');
          }
        }
      }

      // 3. 拆掉 loading
      if (loading && loading.parentNode) loading.parentNode.removeChild(loading);

      if (!finalData) {
        setAgentLight('error');
        appendStatus('✗ 流中断,未收到 done');
        return;
      }
      if (!finalData.ok) {
        setAgentLight('error');
        appendStatus('✗ 失败: ' + (finalData.answer || 'unknown'));
        return;
      }

      // 4. 同步 history
      if (Array.isArray(finalData.history)) history = finalData.history;

      // 5. 如果最后一轮没有 tool_call 且有 answer 文本,显示
      const lastAssistant = [...history].reverse().find(m => m.role === 'assistant');
      if (lastAssistant && !Array.isArray(lastAssistant.tool_calls) && lastAssistant.content
          && finalData.stopped === 'answer' && !streamedAnswer) {
        // 流式输出已在界面渲染;非流式才追加
        appendMessage('assistant', lastAssistant.content);
      }

      // 6. pending 卡 + 顶栏灯
      renderPending(finalData.pending);
      if (finalData.pending) {
        setAgentLight('plan');
        appendStatus('⏸ 等待用户确认(见上方卡片)');
      } else if (finalData.stopped === 'error') {
        setAgentLight('error');
        appendStatus('✗ 错误,已停止');
      } else if (finalData.stopped === 'max_rounds') {
        setAgentLight('idle');
        appendStatus('⚠ 达到最大轮次,未收敛');
      } else {
        setAgentLight('idle');
        // 完成行:用本轮新增的 diff 数(不累计旧值)
        appendCompleteStatus(diffsThisTurn);
      }
    } catch (err) {
      if (loading && loading.parentNode) loading.parentNode.removeChild(loading);
      setAgentLight('error');
      appendStatus('✗ 请求失败: ' + err);
    } finally {
      sending = false;
      sendBtn && (sendBtn.disabled = false);
      if (aiStatus) aiStatus.textContent = 'Enter 发送 · Shift+Enter 换行';
      aiInput && aiInput.focus();
    }
  }

  // 把节点插到 loading 之前(便于流式把"调用工具 / 工具结果"逐条插到进度条上方)
  function insertBeforeLoading(loadingEl, buildNode) {
    if (!loadingEl || !aiMessages) return;
    const node = buildNode();
    aiMessages.insertBefore(node, loadingEl);
    scrollToBottom();
  }
  // 流式输出:在 loading 上方建一个气泡(返回 {bubble, text} 用于持续 append)
  function makeStreamBubble(loadingEl, extraClass) {
    const div = document.createElement('div');
    div.className = `msg ${extraClass || ''}`;
    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = (extraClass && extraClass.includes('thinking')) ? '💭 思考中' : 'Assistant';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    const text = document.createElement('div');
    text.className = 'msg-stream-text';
    bubble.appendChild(text);
    div.appendChild(label);
    div.appendChild(bubble);
    if (loadingEl) {
      aiMessages.insertBefore(div, loadingEl);
    } else {
      aiMessages.appendChild(div);
    }
    scrollToBottom();
    return { bubble, text };
  }
  // 流式插入用的两个原始构造器,跟 appendToolCall/appendToolResult 等价但不依赖外部状态
  function appendToolCallRaw(tc) {
    const div = document.createElement('div');
    div.className = 'msg msg-tool';
    div.innerHTML = `
      <div class="msg-label">🔧 调用工具:<span class="tool-name"> ${escapeHtml(tc.function.name || 'tool')}</span></div>`;
    return div;
  }
  function appendToolResultRaw(m) {
    const txt = (m.content || '').toString();
    let parsed = null;
    try { parsed = JSON.parse(txt); } catch (e) {}
    let status = '成功', statusClass = 'tool-status-ok';
    if (parsed && typeof parsed === 'object') {
      if (parsed.ok === false || parsed.success === false || parsed.error) {
        status = '失败'; statusClass = 'tool-status-fail';
      } else if (parsed.ok === true || parsed.success === true) {
        status = '成功';
      }
    } else if (/^(error|err|fail|failed|exception)/i.test(txt.trim())) {
      status = '失败'; statusClass = 'tool-status-fail';
    }
    const div = document.createElement('div');
    div.className = 'msg msg-tool-result';
    div.innerHTML = `
      <div class="msg-label">↳ 结果:<span class="tool-status ${statusClass}"> ${status}</span></div>`;
    return div;
  }

  // 从最后一条 user 之后开始,重新渲染(包含 tool_calls/tool/assistant)
  function reRenderFromLastUser() {
    if (!aiMessages) return;
    // 找到最后一条 user 的 index
    let lastUserIdx = -1;
    for (let i = history.length - 1; i >= 0; i--) {
      if (history[i].role === 'user') { lastUserIdx = i; break; }
    }
    // 删掉 ai-messages 里最后那条 user 之后的所有节点
    const userNodes = aiMessages.querySelectorAll('.msg-user');
    const lastUserNode = userNodes[userNodes.length - 1];
    if (lastUserNode) {
      let n = lastUserNode.nextSibling;
      while (n) {
        const nx = n.nextSibling;
        n.parentNode && n.parentNode.removeChild(n);
        n = nx;
      }
    }
    // 重新追加 lastUserIdx 之后的所有消息节点
    for (let i = lastUserIdx + 1; i < history.length; i++) {
      appendHistoryNode(history[i]);
    }
    scrollToBottom();
  }

  function autoResize() {
    if (!aiInput) return;
    aiInput.style.height = 'auto';
    aiInput.style.height = Math.min(aiInput.scrollHeight, 180) + 'px';
  }

  // ──────── 事件绑定 ────────
  if (sendBtn) {
    sendBtn.addEventListener('click', e => {
      e.preventDefault();
      sendMessage();
    });
  }
  if (aiInput) {
    aiInput.addEventListener('keydown', e => {
      // Enter 发送,Shift+Enter 换行
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    aiInput.addEventListener('input', autoResize);
  }
  if (togglePlan) {
    togglePlan.addEventListener('change', () => {
      fetch('/api/agent/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_model: togglePlan.checked }),
      }).catch(err => console.error(err));
    });
  }
  if (toggleAuto) {
    toggleAuto.addEventListener('change', () => {
      fetch('/api/agent/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto: toggleAuto.checked }),
      }).catch(err => console.error(err));
    });
  }
  if (chatClear) {
    chatClear.addEventListener('click', () => {
      if (!confirm('确定清空当前对话?')) return;
      fetch('/api/chat/clear', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
          if (data && data.ok) {
            history = [];
            renderPending(null);
            renderHistory();
          }
        })
        .catch(err => console.error(err));
    });
  }
  if (chatRefresh) {
    chatRefresh.addEventListener('click', () => {
      // 重新拉模型列表(复用既有逻辑)
      fetch('/api/models')
        .then(r => r.json())
        .then(d => {
          const sel = document.querySelector('.model-select');
          if (sel && d && Array.isArray(d.models)) {
            sel.dataset.models = JSON.stringify(d.models);
            const cur = d.models.find(m => m.id === d.current);
            if (cur) {
              const lbl = sel.querySelector('.model-label');
              if (lbl) lbl.textContent = cur.name;
            }
          }
        })
        .catch(err => console.error(err));
    });
  }
  if (pendingConfirm) {
    pendingConfirm.addEventListener('click', () => {
      // 乐观关闭:用户已点确认,先把卡片收掉,避免等待后端响应
      renderPending(null);
      // 同步通知后端清 pending(双保险,即便后端先于 chat 响应到达也不冲突)
      fetch('/api/agent/pending/confirm', { method: 'POST' }).catch(() => {});
      if (aiInput) {
        aiInput.value = '确认';
        sendMessage();
      }
    });
  }
  // 收起/展开终端
  const termCollapse = document.getElementById('term-collapse');
  const terminalEl = document.querySelector('.terminal');
  if (termCollapse && terminalEl) {
    termCollapse.addEventListener('click', () => {
      const collapsed = terminalEl.classList.toggle('collapsed');
      termCollapse.title = collapsed ? '展开终端' : '收起终端';
    });
  }

  // 终端多线程会话:每个会话独立 history,cwd,tab
  const termBody     = document.getElementById('term-body');
  const termInput    = document.getElementById('term-input');
  const termClear    = document.getElementById('term-clear');
  const termNew      = document.getElementById('term-new');
  const termTabList  = document.getElementById('term-tab-list');

  const termSessions = new Map();   // id -> { id, name, cwd, history: [{text, cls}] }
  let   termActiveId = null;

  function makeSessionId() {
    return 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  }
  function sessionName(n) {
    return n === 1 ? 'bash' : 'bash ' + n;
  }
  function getActive() {
    return termSessions.get(termActiveId);
  }
  function renderTermTabs() {
    if (!termTabList) return;
    termTabList.innerHTML = '';
    for (const s of termSessions.values()) {
      const tab = document.createElement('div');
      tab.className = 'term-tab' + (s.id === termActiveId ? ' active' : '');
      tab.dataset.sid = s.id;

      const name = document.createElement('span');
      name.className = 'term-tab-name';
      name.textContent = s.name;

      const close = document.createElement('span');
      close.className = 'term-tab-close';
      close.title = '关闭此终端';
      close.textContent = '×';
      close.addEventListener('click', e => {
        e.stopPropagation();
        closeTermSession(s.id);
      });

      tab.appendChild(name);
      tab.appendChild(close);
      tab.addEventListener('click', () => selectTermSession(s.id));
      termTabList.appendChild(tab);
    }
  }
  function renderTermBody() {
    if (!termBody) return;
    // 清掉旧的输出行,保留提示符那一行
    termBody.querySelectorAll('.term-line').forEach(el => el.remove());
    const s = getActive();
    if (!s) return;
    for (const line of s.history) {
      const div = document.createElement('div');
      div.className = 'term-line' + (line.cls ? ' ' + line.cls : '');
      div.textContent = line.text;
      termBody.insertBefore(div, termBody.querySelector('.term-prompt-row'));
    }
    termBody.scrollTop = termBody.scrollHeight;
    if (termInput) termInput.value = '';
  }
  function appendTermLine(text, cls) {
    const s = getActive();
    if (s) s.history.push({ text, cls: cls || '' });
    if (!termBody) return;
    const div = document.createElement('div');
    div.className = 'term-line' + (cls ? ' ' + cls : '');
    div.textContent = text;
    termBody.insertBefore(div, termBody.querySelector('.term-prompt-row'));
    termBody.scrollTop = termBody.scrollHeight;
  }
  function createTermSession() {
    const id = makeSessionId();
    const n  = termSessions.size + 1;
    termSessions.set(id, {
      id,
      name: sessionName(n),
      cwd:  '',
      history: [
        { text: '终端', cls: '' },
        { text: '输入命令后按 Enter 执行。',    cls: 'term-dim' },
      ],
    });
    termActiveId = id;
    renderTermTabs();
    renderTermBody();
    if (termInput) termInput.focus();
  }
  function selectTermSession(id) {
    if (!termSessions.has(id)) return;
    termActiveId = id;
    renderTermTabs();
    renderTermBody();
    if (termInput) termInput.focus();
  }
  function closeTermSession(id) {
    if (!termSessions.has(id)) return;
    termSessions.delete(id);
    if (termActiveId === id) {
      // 切到下一个剩余会话;没有就新建一个
      const next = termSessions.values().next().value;
      if (next) {
        termActiveId = next.id;
      } else {
        createTermSession();
        return;   // createTermSession 已重渲
      }
    }
    renderTermTabs();
    renderTermBody();
    if (termInput) termInput.focus();
  }

  // 初始会话
  createTermSession();

  // 新建
  if (termNew) termNew.addEventListener('click', () => createTermSession());

  // 清屏:只清当前会话
  if (termClear && termBody) {
    termClear.addEventListener('click', () => {
      const s = getActive();
      if (s) s.history = [];
      termBody.querySelectorAll('.term-line').forEach(el => el.remove());
      if (termInput) termInput.focus();
    });
  }

  // 输入 → /api/terminal/run → 输出
  if (termInput && termBody) {
    termInput.addEventListener('keydown', async e => {
      if (e.key !== 'Enter' || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
      e.preventDefault();
      const cmd = termInput.value;
      if (!cmd.trim()) return;
      appendTermLine(`$ ${cmd}`, 'term-cmd');
      termInput.value = '';
      termInput.disabled = true;
      try {
        const r = await fetch('/api/terminal/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: cmd }),
        }).then(r => r.json());
        if (r.stdout) appendTermLine(r.stdout, 'term-out');
        if (r.stderr) appendTermLine(r.stderr, 'term-err');
        if (r.error)  appendTermLine(`[错误] ${r.error}`, 'term-err');
        appendTermLine(`[退出 ${r.returncode ?? '?'}]`, r.ok ? 'term-ok' : 'term-err');
      } catch (err) {
        appendTermLine(`[网络错误] ${err.message || err}`, 'term-err');
      } finally {
        termInput.disabled = false;
        termInput.focus();
      }
    });
  }
  if (pendingReject) {
    pendingReject.addEventListener('click', () => {
      fetch('/api/agent/pending/reject', { method: 'POST' })
        .then(r => r.json())
        .then(() => { renderPending(null); appendStatus('✗ 已拒绝当前操作'); })
        .catch(err => console.error(err));
    });
  }

  // ──────── 全局快捷键:聚焦输入框 ────────
  document.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.key.toLowerCase() === 'l' && !e.shiftKey) {
      // Ctrl/Cmd+L 聚焦 AI 输入框
      e.preventDefault();
      aiInput && aiInput.focus();
    }
  });

  // 启动时拉一次
  loadChat();
  loadAgentState();
  loadDiffList();        // diff viewer 初始为空列表(后端清空状态)

  // 应用配置(流式输出开关、轮数上限) — 来自 .config.json
  let appConfig = { flow: false, max_round: 20 };
  fetch('/api/config').then(r => r.json()).then(d => {
    if (d && d.ok) appConfig = { flow: !!d.flow, max_round: d.max_round || 20 };
  }).catch(() => {});

  // ──────── 全局搜索 ────────
  const searchInputEl   = document.getElementById('search-input');
  const searchResultsEl = document.getElementById('search-results');
  const searchMetaEl    = document.getElementById('search-meta');
  let searchBusy = false;
  let searchTimer = null;

  function escapeReg(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function highlight(text, q) {
    if (!q) return escapeHtml(text);
    const re = new RegExp(escapeReg(q), 'gi');
    return escapeHtml(text).replace(re, m => `<mark>${m}</mark>`);
  }

  async function runSearch(q) {
    q = (q || '').trim();
    if (!q) {
      searchResultsEl.innerHTML = `<div class="search-empty">输入关键字开始搜索</div>`;
      searchMetaEl.textContent = '';
      return;
    }
    if (searchBusy) return;
    searchBusy = true;
    searchMetaEl.textContent = '搜索中…';
    try {
      const cwd = currentRoot || '';
      const r = await fetch('/api/search?q=' + encodeURIComponent(q)
        + (cwd ? '&path=' + encodeURIComponent(cwd) : ''));
      const d = await r.json();
      if (!d.ok) {
        searchResultsEl.innerHTML = `<div class="search-empty">⚠ ${escapeHtml(d.error || '搜索失败')}</div>`;
        searchMetaEl.textContent = '';
        return;
      }
      if (!d.results.length) {
        searchResultsEl.innerHTML = `<div class="search-empty">无匹配结果</div>`;
        searchMetaEl.textContent = '0 条';
        return;
      }
      searchMetaEl.textContent = `${d.total} 条${d.truncated ? ' (已截断)' : ''}`;
      const html = d.results.map(r => {
        const fullPath = (d.root + '/' + r.path).replace(/[\\/]/g, '/');
        return `<div class="search-result" data-path="${escapeHtml(fullPath)}" data-line="${r.line}">
          <div class="sr-path"><span class="sr-line">${r.line}</span>${escapeHtml(r.path)}</div>
          <div class="sr-snippet">${highlight(r.snippet, q)}</div>
        </div>`;
      }).join('');
      searchResultsEl.innerHTML = html;
      searchResultsEl.querySelectorAll('.search-result').forEach(row => {
        row.addEventListener('click', () => {
          const p = row.dataset.path;
          const ln = parseInt(row.dataset.line, 10);
          openFile(p, ln);
        });
      });
    } catch (e) {
      searchResultsEl.innerHTML = `<div class="search-empty">⚠ ${escapeHtml(String(e))}</div>`;
      searchMetaEl.textContent = '';
    } finally {
      searchBusy = false;
    }
  }

  if (searchInputEl) {
    searchInputEl.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const q = searchInputEl.value;
      searchTimer = setTimeout(() => runSearch(q), 250);
    });
    searchInputEl.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        clearTimeout(searchTimer);
        runSearch(searchInputEl.value);
      } else if (e.key === 'Escape') {
        searchInputEl.value = '';
        runSearch('');
      }
    });
  }

  // ============ 主题切换 ============
  const THEME_KEY = 'codeforge.theme';
  const themeSwitch = document.getElementById('theme-switch');
  function applyTheme(name) {
    document.documentElement.classList.remove('theme-light', 'theme-blue');
    if (name === 'light') document.documentElement.classList.add('theme-light');
    else if (name === 'blue') document.documentElement.classList.add('theme-blue');
    // 'dark' 就是默认 :root,不加类
    try { localStorage.setItem(THEME_KEY, name); } catch (e) {}
    // 同步按钮 active 态
    if (themeSwitch) {
      themeSwitch.querySelectorAll('.theme-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.theme === name);
      });
    }
  }
  if (themeSwitch) {
    themeSwitch.addEventListener('click', e => {
      const btn = e.target.closest('.theme-btn');
      if (!btn) return;
      applyTheme(btn.dataset.theme);
    });
    // 初始化:localStorage > 默认 dark
    let saved = 'dark';
    try { saved = localStorage.getItem(THEME_KEY) || 'dark'; } catch (e) {}
    if (!['dark', 'light', 'blue'].includes(saved)) saved = 'dark';
    applyTheme(saved);
  }

  // ── 恢复上次的工作目录(localStorage) ──
  // 失败(目录被删/无权限)时 openFolder 会清掉过期缓存
  function initExplorer() {
    const cached = loadExplorerState();
    if (cached && cached.root) {
      openFolder(cached.root, null, { isRestore: true });
    }
    // 没缓存 → 保持空状态 UI(等用户主动打开)
  }
  initExplorer();

  // ============ 列宽拖拽手柄 ============
  // 拖动 .resize-handle 改变相邻列的宽度(支持 .sidebar 和 .right)
  const MIN_WIDTHS = { '.sidebar': 160, '.right': 220 };
  document.querySelectorAll('.resize-handle').forEach(handle => {
    const sel = handle.dataset.target;
    const direction = handle.dataset.direction; // "left" → 拖右增宽;"right" → 拖左增宽
    const panel = document.querySelector(sel);
    if (!panel) return;
    handle.addEventListener('mousedown', e => {
      e.preventDefault();
      if (panel.classList.contains('collapsed')) return;
      const startX = e.clientX;
      const startWidth = panel.getBoundingClientRect().width;
      const min = MIN_WIDTHS[sel] || 120;
      const max = Math.min(window.innerWidth * 0.6, 800);
      handle.classList.add('dragging');
      document.body.classList.add('resizing');
      const onMove = ev => {
        const dx = ev.clientX - startX;
        // 左侧面板:向右拖 = 增宽;右侧面板:向左拖 = 增宽
        const sign = direction === 'right' ? -1 : 1;
        const next = Math.max(min, Math.min(max, startWidth + dx * sign));
        panel.style.width = next + 'px';
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
})();
