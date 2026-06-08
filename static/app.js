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
})();
