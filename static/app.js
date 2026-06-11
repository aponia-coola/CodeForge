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
  // 每个图标用 data-target 指向它要切换的面板(通用方案,支持左右两侧)
  const icons = document.querySelectorAll('.sidebar-icons [data-target]');
  icons.forEach(icon => {
    icon.addEventListener('click', () => {
      const panel = document.querySelector(icon.dataset.target);
      if (!panel) return;

      const isCollapsed = panel.classList.toggle('collapsed');
      icon.classList.toggle('active', !isCollapsed);
    });
  });

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
    return fetch(`/api/folder?path=${encodeURIComponent(path)}&_t=${Date.now()}`)
      .then(r => r.json())
      .then(d => {
        if (!d || !d.ok || !d.tree) return false;
        const sig = computeSignature(d.tree);
        const old = treeSignature.get(path);
        if (old === sig) return false;
        treeSignature.set(path, sig);
        replaceListInDom(path, d.tree);
        return true;
      })
      .catch(() => false);
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
        <div class="shortcut-row"><span class="kbd">Ctrl+P</span> 快速打开</div>
        <div class="shortcut-row"><span class="kbd">Ctrl+Shift+P</span> 命令面板</div>
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
    let args = {};
    try { args = JSON.parse(tc.function.arguments || '{}'); } catch (e) { args = { _raw: tc.function.arguments }; }
    const div = document.createElement('div');
    div.className = 'msg msg-tool';
    div.innerHTML = `
      <div class="msg-label">🔧 ${escapeHtml(tc.function.name || 'tool')}</div>
      <div class="msg-bubble tool-bubble">${escapeHtml(JSON.stringify(args, null, 2))}</div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
  }

  function appendToolResult(m) {
    if (!aiMessages) return;
    const div = document.createElement('div');
    div.className = 'msg msg-tool-result';
    const txt = (m.content || '').toString();
    const trimmed = txt.length > 600 ? txt.slice(0, 600) + '\n…(已截断)' : txt;
    div.innerHTML = `
      <div class="msg-label">↳ 结果</div>
      <div class="msg-bubble tool-result-bubble">${escapeHtml(trimmed)}</div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
  }

  function appendLoading() {
    if (!aiMessages) return null;
    const div = document.createElement('div');
    div.className = 'msg msg-assistant msg-loading';
    div.innerHTML = `
      <div class="msg-label">Assistant</div>
      <div class="msg-bubble"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>`;
    aiMessages.appendChild(div);
    scrollToBottom();
    return div;
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

  // ──────── 发送消息 ────────
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

    // 2. 同步推入 history
    history.push({ role: 'user', content: text });

    // 3. loading 占位
    const loading = appendLoading();

    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: history,
          cwd: currentRoot || null,
          plan_model: togglePlan ? togglePlan.checked : null,
        }),
      });
      const data = await r.json();

      // 4. 拆掉 loading
      if (loading && loading.parentNode) loading.parentNode.removeChild(loading);

      if (!data || !data.ok) {
        appendStatus('✗ 失败: ' + ((data && data.error) || r.status));
        return;
      }

      // 5. 用后端返回的 history 同步本地(它含 assistant + tool_calls + tool 结果)
      if (Array.isArray(data.history)) history = data.history;
      // 把最后一条 assistant 的新内容也单独展示(冗余但更醒目)
      const lastAssistant = [...history].reverse().find(m => m.role === 'assistant');
      if (lastAssistant && !Array.isArray(lastAssistant.tool_calls) && lastAssistant.content) {
        // 已经从 history 渲染过,不再追加
      } else {
        // 有工具调用但最后一条不是 assistant,补一行
        if (data.answer) appendMessage('assistant', data.answer);
      }

      // 6. 把这一轮新增的 assistant/tool 行追加进视图(history 可能很长,这里只渲染尾部新增)
      //    为简单:重新渲染尾部,从 lastUserIndex 之后开始
      reRenderFromLastUser();

      // 7. 处理 pending
      renderPending(data.pending);
      if (data.pending) {
        appendStatus('⏸ 等待用户确认(见上方卡片)');
      } else if (data.stopped === 'max_rounds') {
        appendStatus('⚠ 达到最大轮次,未收敛');
      } else if (data.stopped === 'error') {
        appendStatus('✗ 错误,已停止');
      }
    } catch (err) {
      if (loading && loading.parentNode) loading.parentNode.removeChild(loading);
      appendStatus('✗ 请求失败: ' + err);
    } finally {
      sending = false;
      sendBtn && (sendBtn.disabled = false);
      if (aiStatus) aiStatus.textContent = 'Enter 发送 · Shift+Enter 换行';
      aiInput && aiInput.focus();
    }
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
})();
