/* 共通ヘッダー描画 — 認証状態 + プラン別ナビ */
(function () {
  const NAV = [
    { href: '/app',                label: 'ダッシュボード', plan: 'free' },
    { href: '/app/research',       label: 'リサーチ',       plan: 'free' },
    { href: '/app/saved',          label: '保存',           plan: 'student' },
    { href: '/app/students',       label: '生徒',           plan: 'tutor' },
    { href: '/app/tutor/analysis', label: '過去問分析',     plan: 'tutor' },
    { href: '/app/team',           label: 'チーム',         plan: 'school' },
    { href: '/app/account',        label: 'アカウント',     plan: 'free' },
  ];
  const RANK = { free: 0, student: 1, tutor: 2, school: 3 };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  async function renderHeader(target) {
    const path = location.pathname;
    let me = null;
    try {
      const r = await fetch('/api/me');
      if (r.ok) me = await r.json();
    } catch (_) {}

    const planCode = me?.plan?.plan_code || 'free';
    const planRank = RANK[planCode] ?? 0;
    const planName = me?.plan?.name || 'Free';

    const nav = NAV.filter(n => RANK[n.plan] <= planRank).map(n => {
      const active = path === n.href || (n.href !== '/app' && path.startsWith(n.href));
      return `<a href="${esc(n.href)}" class="${active ? 'active' : ''}"${active ? ' aria-current="page"' : ''}>${esc(n.label)}</a>`;
    }).join('');

    const userArea = me ? `
      <span class="plan-badge">${esc(planName)}</span>
      <span class="app-user-name">${esc(me.name || me.email)}</span>
      <form action="/auth/logout" method="post" style="margin:0">
        <button type="submit" class="btn btn-ghost" style="padding:6px 10px">ログアウト</button>
      </form>
    ` : `
      <a href="/login" class="btn btn-ghost" style="padding:6px 14px">ログイン</a>
      <a href="/register" class="btn btn-primary" style="padding:6px 14px">新規登録</a>
    `;

    target.innerHTML = `
      <a href="${me ? '/app' : '/'}" class="app-logo">
        <span class="app-logo-mark">AO</span>
        <span>AOリサーチ</span>
      </a>
      ${me ? `<nav class="app-nav">${nav}</nav>` : `<div style="flex:1"></div>`}
      <div class="app-user">${userArea}</div>
    `;
    return me;
  }

  window.renderHeader = renderHeader;
  window._planRank = RANK;
})();
