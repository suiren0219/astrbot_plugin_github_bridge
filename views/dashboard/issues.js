// 原生模块；所有 GitHub 内容均作为文本节点显示，不解析 HTML 或 Markdown。
export function createIssueWorkspace({ root, apiGet, apiPost, getSettings, getPreview, redact, announce }) {
  const el = (tag, className = '', value) => {
    const node = document.createElement(tag);
    node.className = className;
    if (value !== undefined) node.textContent = String(value ?? '');
    return node;
  };
  const button = (label, handler, className = 'button button-tonal button-small') => {
    const node = el('button', className, label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  };
  const field = (label, input, id) => {
    input.id = id;
    const wrapper = el('div', 'field');
    const caption = el('label', '', label);
    caption.htmlFor = id;
    wrapper.append(caption, input);
    return wrapper;
  };
  const badge = (label, kind = '') => el('span', `tag ${kind}`, label);
  const statusBadge = (state) => badge(state === 'open' ? '待解决' : state === 'closed' ? '已关闭' : '状态未知', state === 'open' ? 'tag-green' : state === 'closed' ? 'tag-purple' : '');
  const author = (value) => typeof value === 'string' ? value : value?.login || '未知用户';
  const date = (value) => {
    if (!value) return '时间未知';
    const parsed = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(parsed.getTime()) ? '时间未知' : parsed.toLocaleString('zh-CN', { hour12: false });
  };
  const message = (error) => redact(error?.message || '请求失败，请稍后重试。');
  const safeURL = (value) => {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
    } catch (_) { return ''; }
  };
  const s = {
    active: false, mounted: false, loaded: false, repos: [], repo: '', owner: '', repoQuery: '',
    filter: 'open', page: 1, next: false, items: [], number: null, detail: null,
    listWrite: false, detailWrite: false, repoLoading: false, listLoading: false, detailLoading: false,
    repoError: '', listError: '', detailError: '', query: '', configKey: '',
    repoRequest: 0, listRequest: 0, detailRequest: 0, dialogRequest: 0,
    draft: null, expires: 0, attempted: false, preparing: false, confirming: false, expiryTimer: null,
    action: null, opener: null, comments: new Map(), creates: new Map(),
  };
  let ui;

  function busy(node, text) {
    node.setAttribute('aria-busy', 'true');
    const row = el('div', 'list-loading');
    row.append(el('span', 'spinner'), el('span', '', text));
    node.replaceChildren(row);
  }

  function empty(node, title, text, action, label = '重新加载') {
    node.setAttribute('aria-busy', 'false');
    const box = el('div', 'empty-state');
    box.append(el('h3', '', title), el('p', '', text));
    if (action) box.append(button(label, action));
    node.replaceChildren(box);
  }

  function external(value, label = '在 GitHub 查看') {
    const url = safeURL(value);
    const box = el('div', 'issue-external');
    if (!url) return box;
    const link = el('a', 'text-link', label);
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const details = el('details', 'issue-url');
    details.append(el('summary', '', '复制链接'));
    const input = el('input', 'mono');
    input.type = 'text';
    input.readOnly = true;
    input.value = url;
    input.setAttribute('aria-label', 'GitHub 链接，可选择复制');
    input.addEventListener('focus', () => input.select());
    const hint = el('p', 'field-hint', '内嵌页面若无法打开新窗口，请选择并复制链接。');
    const copy = button('复制', async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error();
        await navigator.clipboard.writeText(url);
        hint.textContent = '链接已复制。';
      } catch (_) {
        input.focus();
        input.select();
        hint.textContent = '请长按或使用 Ctrl/Cmd+C 复制选中的链接。';
      }
    }, 'button button-quiet button-small');
    const row = el('div', 'issue-url-row');
    row.append(input, copy);
    details.append(row, hint);
    box.append(link, details);
    return box;
  }

  function currentRepo() { return s.repos.find((repo) => repo.full_name === s.repo); }
  function configured() {
    const cfg = getSettings();
    return !cfg || (cfg.token_configured !== false && Boolean(cfg.github_owner));
  }
  function writable(detail = false) {
    const repo = currentRepo();
    return configured() && repo?.can_write === true && repo.archived !== true
      && repo.has_issues !== false && s.listWrite && !s.repoLoading && !s.listLoading
      && (!detail || (s.detailWrite && !s.detailLoading && !s.detailError));
  }
  function permission() {
    const cfg = getSettings();
    const repo = currentRepo();
    if (getPreview()) return '演示模式 · 操作仅修改演示临时数据，不连接 GitHub。允许写入的仓库可体验创建、评论与关闭／重开。';
    if (!configured()) return '请先在设置中保存 GitHub 用户名与 Token，问题工作台无需开启自动监控。';
    if (repo?.archived) return '仓库已归档：只读，无法创建、评论或更改问题状态。';
    if (repo?.has_issues === false) return '此仓库未启用 Issues，请先在 GitHub 的仓库设置中开启。';
    if (cfg && !String(cfg.issue_repositories || '').trim()) return '只读模式：问题写入允许列表为空。在设置中添加本人仓库，并为 Token 授予 Issues 读写权限。';
    if (!s.repo) return '仅展示本人的仓库。写入须同时满足仓库允许列表与 Token 的 Issues 读写权限。';
    if (!writable()) return '当前仓库只读或权限尚未确认。仅允许写入允许列表内的本人仓库，Token 还需 Issues 读写权限。';
    return '允许写入 · 仅限本人仓库与问题允许列表。创建、评论和关闭／重开均须预览并确认。';
  }
  function renderPermission() {
    ui.permissionText.textContent = permission();
    ui.permissionTag.textContent = writable() ? '允许写入' : '只读';
    ui.permissionTag.className = `tag ${writable() ? 'tag-green' : 'tag-amber'}`;
    ui.create.disabled = !writable();
    ui.create.title = writable() ? '填写问题并预览' : permission();
    ui.configure.textContent = configured() ? '管理写入权限' : '前往配置连接';
  }

  function mount() {
    if (s.mounted) return;
    s.mounted = true;
    const permissionBox = el('div', 'issue-permission');
    const permissionTag = badge('只读', 'tag-amber');
    const permissionText = el('p');
    const configure = el('a', 'text-link', '管理写入权限');
    configure.href = '#settings';
    permissionBox.append(permissionTag, permissionText, configure);
    const feedback = el('div', 'notice issue-feedback');
    feedback.hidden = true;
    feedback.setAttribute('role', 'status');
    const layout = el('div', 'issue-workspace');
    const repos = el('aside', 'card issue-repositories');
    repos.setAttribute('aria-label', '选择问题仓库');
    const repoHeading = el('div', 'section-heading');
    const repoCount = el('span', 'tag', '本人仓库');
    repoHeading.append(el('h2', '', '选择仓库'), repoCount);
    const repoSearch = el('input');
    repoSearch.type = 'search';
    repoSearch.placeholder = '搜索仓库';
    repoSearch.autocomplete = 'off';
    repoSearch.addEventListener('input', () => { s.repoQuery = repoSearch.value; renderRepos(); });
    const repoList = el('div', 'issue-repo-list');
    const reload = button('刷新仓库', () => refresh(), 'button button-quiet button-small full-width');
    repos.append(repoHeading, field('仓库名称或描述', repoSearch, 'issue-repo-search'), repoList, reload);
    const list = el('section', 'card issue-list-panel');
    list.setAttribute('aria-label', '问题列表');
    const listHeading = el('div', 'section-heading');
    const listTitle = el('h2', 'issue-repo-name', '问题列表');
    const create = button('＋ 新建问题', () => openEditor('create'), 'button button-primary button-small');
    create.id = 'issue-create';
    listHeading.append(listTitle, create);
    const filters = el('div', 'filter-group issue-filters');
    filters.setAttribute('role', 'group');
    filters.setAttribute('aria-label', '问题状态筛选');
    for (const [value, label] of [['open', '待解决'], ['closed', '已关闭'], ['all', '全部']]) {
      const filter = button(label, () => {
        if (s.filter === value) return;
        s.filter = value;
        s.page = 1;
        clearDetail();
        loadList();
      }, '');
      filter.dataset.issueFilter = value;
      filters.append(filter);
    }
    const search = el('input');
    search.type = 'search';
    search.placeholder = '搜索本页标题、正文或编号';
    search.autocomplete = 'off';
    search.addEventListener('input', () => { s.query = search.value; renderList(); });
    const listMeta = el('p', 'field-hint');
    listMeta.setAttribute('role', 'status');
    const listBody = el('div', 'issue-list');
    const pager = el('div', 'issue-pagination');
    const previous = button('上一页', () => { if (s.page > 1 && !s.listLoading) { s.page--; clearDetail(); loadList(); } });
    const next = button('下一页', () => { if (s.next && !s.listLoading) { s.page++; clearDetail(); loadList(); } });
    const pageText = el('span', 'mono');
    pageText.setAttribute('aria-live', 'polite');
    pager.append(previous, pageText, next);
    list.append(listHeading, filters, field('本页搜索', search, 'issue-search'), listMeta, listBody, pager);
    const detail = el('section', 'card issue-detail-panel');
    detail.id = 'issue-detail';
    detail.setAttribute('aria-label', '问题详情');
    detail.tabIndex = -1;
    layout.append(repos, list, detail);
    root.append(permissionBox, feedback, layout);
    ui = { permissionTag, permissionText, configure, feedback, repoCount, repoList, repoSearch,
      listTitle, create, filters, search, listMeta, listBody, pager, previous, next, pageText, detail };
    mountDialog();
    renderRepos();
    renderList();
    renderDetail();
    renderPermission();
  }

  function renderRepos() {
    if (s.repoLoading) { busy(ui.repoList, '正在读取本人仓库'); return; }
    if (!configured()) {
      empty(ui.repoList, '连接尚未配置', '保存 GitHub 用户名和 Token 后即可查看本人仓库。');
      return;
    }
    if (s.repoError) { empty(ui.repoList, '仓库加载失败', s.repoError, refresh); return; }
    ui.repoList.setAttribute('aria-busy', 'false');
    ui.repoCount.textContent = `${s.repos.length} 个`;
    ui.repoList.replaceChildren();
    const query = s.repoQuery.trim().toLowerCase();
    const repos = s.repos.filter((repo) => `${repo.full_name} ${repo.description || ''}`.toLowerCase().includes(query));
    if (!repos.length) {
      empty(ui.repoList, query ? '没有匹配的仓库' : '暂无本人仓库', query ? '换一个关键词，或清除搜索。' : '确认 Token 可访问自己的仓库后刷新。', query ? () => {
        s.repoQuery = ''; ui.repoSearch.value = ''; renderRepos();
      } : refresh, query ? '清除搜索' : '重新加载');
      return;
    }
    for (const repo of repos) {
      const row = button('', () => selectRepo(repo.full_name), 'issue-repo-row');
      row.setAttribute('aria-pressed', String(s.repo === repo.full_name));
      row.append(el('strong', 'mono', repo.full_name));
      if (repo.description) row.append(el('span', 'issue-repo-description', repo.description));
      const tags = el('span', 'issue-tags');
      tags.append(badge(repo.private ? '私有' : '公开'));
      if (repo.archived) tags.append(badge('已归档', 'tag-amber'));
      if (repo.has_issues === false) tags.append(badge('未启用问题'));
      else tags.append(badge(repo.can_write === true && !repo.archived ? '允许写入' : '只读', repo.can_write === true && !repo.archived ? 'tag-green' : ''));
      row.append(tags);
      ui.repoList.append(row);
    }
  }

  function renderList() {
    ui.listTitle.textContent = s.repo || '问题列表';
    for (const item of ui.filters.children) item.setAttribute('aria-pressed', String(item.dataset.issueFilter === s.filter));
    ui.previous.disabled = s.listLoading || !!s.listError || s.page <= 1;
    ui.next.disabled = s.listLoading || !!s.listError || !s.next;
    ui.pageText.textContent = `第 ${s.page} 页`;
    ui.pager.hidden = !s.repo || !configured() || currentRepo()?.has_issues === false;
    if (s.listLoading) {
      ui.listMeta.textContent = `正在读取第 ${s.page} 页`;
      busy(ui.listBody, '正在加载问题');
      renderPermission();
      return;
    }
    ui.listBody.setAttribute('aria-busy', 'false');
    ui.listMeta.textContent = '';
    if (!s.repo || !configured()) { empty(ui.listBody, '从仓库开始', '在左侧选择一个仓库，查看它的问题。'); return; }
    if (currentRepo()?.has_issues === false) { empty(ui.listBody, '此仓库未启用问题', '在 GitHub 中启用 Issues 后，刷新仓库列表。'); return; }
    if (s.listError) { empty(ui.listBody, '问题列表加载失败', s.listError, () => loadList()); return; }
    const query = s.query.trim().toLowerCase().replace(/^#/, '');
    const items = s.items.filter((issue) => `${issue.number} ${issue.title} ${issue.body || ''}`.toLowerCase().includes(query));
    ui.listMeta.textContent = `本页 ${s.items.length} 条${s.query.trim() ? ` · 匹配 ${items.length} 条` : ''} · 搜索仅作用于本页`;
    ui.listBody.replaceChildren();
    if (!items.length) {
      empty(ui.listBody, s.query.trim() ? '本页没有匹配的问题' : '当前条件下暂无问题',
        s.query.trim() ? '清除关键词，或翻页查看其他问题。' : writable() ? '可以切换问题状态，或创建一个新问题。' : '可以切换问题状态查看；当前仓库为只读。', () => {
          const wasSearching = Boolean(s.query.trim());
          s.query = ''; ui.search.value = '';
          if (wasSearching || s.filter === 'all') renderList();
          else { s.filter = 'all'; s.page = 1; clearDetail(); loadList(); }
        }, s.query.trim() ? '清除搜索' : '查看全部状态');
      return;
    }
    for (const issue of items) {
      const row = button('', () => loadDetail(issue.number, true), 'issue-row');
      row.setAttribute('aria-pressed', String(s.number === issue.number));
      row.setAttribute('aria-controls', 'issue-detail');
      const heading = el('span', 'issue-row-heading');
      heading.append(statusBadge(issue.state), el('span', 'mono muted', `#${issue.number}`));
      row.append(heading, el('strong', 'issue-row-title', issue.title || '无标题问题'));
      const tags = labels(issue.labels);
      if (tags.childNodes.length) row.append(tags);
      row.append(el('span', 'issue-row-meta', `${author(issue.author)} · ${issue.comments ?? 0} 条评论 · 更新于 ${date(issue.updated_at)}`));
      ui.listBody.append(row);
    }
  }

  function labels(values) {
    const box = el('span', 'issue-tags');
    for (const label of Array.isArray(values) ? values : []) {
      const tag = badge(label.name || '未命名标签');
      // 只接受六位十六进制颜色；外部颜色仅用于小色点，文字保持主题对比度。
      if (/^[\da-f]{6}$/i.test(label.color || '')) {
        const dot = el('span', 'issue-label-dot');
        dot.style.backgroundColor = `#${label.color}`;
        dot.setAttribute('aria-hidden', 'true');
        tag.prepend(dot);
      }
      box.append(tag);
    }
    return box;
  }

  function clearDetail() {
    ++s.detailRequest;
    s.detailLoading = false;
    s.detailError = '';
    s.detail = null;
    s.number = null;
    s.detailWrite = false;
    if (ui) renderDetail();
  }

  function renderDetail() {
    if (s.detailLoading) { busy(ui.detail, '正在加载正文与评论'); return; }
    ui.detail.setAttribute('aria-busy', 'false');
    if (s.detailError) { empty(ui.detail, '详情加载失败', s.detailError, () => loadDetail(s.number)); return; }
    if (!s.detail) { empty(ui.detail, '选择一个问题', '正文、评论与状态操作会显示在这里。'); return; }
    const issue = s.detail.issue;
    ui.detail.replaceChildren();
    const heading = el('div', 'section-heading');
    heading.append(el('span', 'mono muted', `#${issue.number}`), button('刷新详情', () => loadDetail(s.number), 'button button-quiet button-small'));
    const title = el('h2', 'issue-detail-title', issue.title || '无标题问题');
    const meta = el('div', 'issue-tags');
    meta.append(statusBadge(issue.state), el('span', 'field-hint', `${author(issue.author)} 创建于 ${date(issue.created_at)}`));
    const body = el('div', 'issue-source', issue.body || '这个问题暂时没有正文。');
    ui.detail.append(heading, title, meta, labels(issue.labels), body, external(issue.html_url));
    const actions = el('div', 'issue-detail-actions');
    const toggle = button(issue.state === 'closed' ? '重新打开问题' : '关闭问题', () => openEditor('state'));
    toggle.id = 'issue-state';
    toggle.disabled = !writable(true) || !['open', 'closed'].includes(issue.state);
    toggle.title = toggle.disabled ? permission() : '先预览状态变更，再确认';
    actions.append(toggle, el('span', 'field-hint', '写入前须预览确认'));
    ui.detail.append(actions);
    const comments = Array.isArray(s.detail.comments) ? s.detail.comments : [];
    ui.detail.append(el('h3', 'issue-comments-heading', `讨论 · 已加载 ${comments.length} 条评论`));
    if (!comments.length) ui.detail.append(el('p', 'muted', '还没有评论，写下你的想法吧。'));
    for (const comment of comments) {
      const article = el('article', 'issue-comment');
      article.append(el('p', 'issue-comment-meta', `${author(comment.author)} · ${date(comment.created_at)}`),
        el('div', 'issue-source', comment.body || '（空评论）'), external(comment.html_url, '查看原评论'));
      ui.detail.append(article);
    }
    if (s.detail.comments_truncated) {
      const truncated = el('div', 'issue-truncated');
      truncated.append(el('p', '', '这里只显示部分评论，完整讨论请在 GitHub 查看。'), external(issue.html_url, '查看完整讨论'));
      ui.detail.append(truncated);
    }
    const form = el('form', 'issue-comment-form');
    const textarea = el('textarea');
    textarea.rows = 5;
    textarea.required = true;
    textarea.maxLength = 60000;
    textarea.placeholder = '补充背景、进展或解决思路…';
    textarea.value = s.comments.get(`${s.repo}#${s.number}`) || '';
    textarea.disabled = !writable(true);
    textarea.setAttribute('aria-describedby', 'issue-comment-hint');
    const key = `${s.repo}#${s.number}`;
    textarea.addEventListener('input', () => s.comments.set(key, textarea.value));
    const hint = el('p', 'field-hint', writable(true) ? '支持 Markdown 输入；预览按纯文本显示，不渲染格式。' : permission());
    hint.id = 'issue-comment-hint';
    const submit = el('button', 'button button-primary button-small', '预览评论');
    submit.type = 'submit';
    submit.disabled = !writable(true);
    form.append(field('添加评论', textarea, 'issue-comment-body'), hint, submit);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!textarea.value.trim()) { textarea.setCustomValidity('请填写评论内容。'); textarea.reportValidity(); textarea.setCustomValidity(''); return; }
      openEditor('comment', textarea.value);
    });
    ui.detail.append(form);
  }

  async function selectRepo(name) {
    if (s.repo === name) return;
    s.repo = name;
    s.page = 1;
    s.query = '';
    ui.search.value = '';
    clearDetail();
    ui.feedback.hidden = true;
    renderRepos();
    await loadList();
  }

  async function loadList() {
    const request = ++s.listRequest;
    s.listWrite = false;
    s.listError = '';
    s.items = [];
    s.next = false;
    s.listLoading = false;
    if (!s.repo || !configured() || currentRepo()?.has_issues === false) { renderList(); renderPermission(); return; }
    const repo = s.repo;
    const page = s.page;
    s.listLoading = true;
    renderList();
    try {
      const data = await apiGet(`issues?${new URLSearchParams({ repo, state: s.filter, page: String(page) })}`);
      if (request !== s.listRequest) return;
      if (!Array.isArray(data.items) || (data.repo && data.repo !== repo)) throw new Error('问题列表返回格式不正确，请刷新。');
      s.items = data.items;
      s.next = data.has_next === true;
      s.listWrite = data.can_write === true;
      s.page = Number.isInteger(data.page) && data.page > 0 ? data.page : page;
    } catch (error) {
      if (request !== s.listRequest) return;
      s.listError = message(error);
    } finally {
      if (request === s.listRequest) {
        s.listLoading = false;
        renderList();
        renderPermission();
        renderDetail();
      }
    }
  }

  async function loadDetail(number, focus = false) {
    if (!s.repo || !Number.isInteger(number) || number < 1) return;
    const request = ++s.detailRequest;
    const repo = s.repo;
    s.number = number;
    s.detail = null;
    s.detailWrite = false;
    s.detailError = '';
    s.detailLoading = true;
    renderList();
    renderDetail();
    if (focus) ui.detail.focus({ preventScroll: true });
    try {
      const data = await apiGet(`issue_detail?${new URLSearchParams({ repo, number: String(number) })}`);
      if (request !== s.detailRequest) return;
      if (!data.issue || data.issue.number !== number || (data.repo && data.repo !== repo)) throw new Error('问题详情返回格式不正确，请刷新。');
      s.detail = data;
      s.detailWrite = data.can_write === true;
    } catch (error) {
      if (request !== s.detailRequest) return;
      s.detailError = message(error);
    } finally {
      if (request === s.detailRequest) { s.detailLoading = false; renderDetail(); }
    }
  }

  async function refresh() {
    mount();
    const request = ++s.repoRequest;
    ++s.listRequest;
    clearDetail();
    s.listLoading = false;
    s.listWrite = false;
    s.items = [];
    s.next = false;
    s.repoError = '';
    s.listError = '';
    if (!configured()) {
      s.repoLoading = false;
      s.loaded = false;
      s.repos = [];
      s.repo = '';
      renderRepos(); renderList(); renderPermission();
      return;
    }
    s.repoLoading = true;
    renderRepos();
    busy(ui.listBody, '正在同步仓库权限');
    renderPermission();
    try {
      const data = await apiGet('issue_repositories');
      if (request !== s.repoRequest) return;
      if (!Array.isArray(data.items)) throw new Error('仓库列表返回格式不正确。');
      s.repos = data.items;
      s.owner = data.owner || '';
      s.loaded = true;
      if (!s.repos.some((repo) => repo.full_name === s.repo)) {
        s.repo = s.repos.find((repo) => repo.has_issues !== false)?.full_name || s.repos[0]?.full_name || '';
        s.page = 1;
      }
    } catch (error) {
      if (request !== s.repoRequest) return;
      s.repoError = message(error);
      s.loaded = false;
      s.repos = [];
      s.repo = '';
    } finally {
      if (request === s.repoRequest) {
        s.repoLoading = false;
        renderRepos();
        await loadList();
      }
    }
  }

  function mountDialog() {
    const dialog = el('dialog', 'issue-dialog');
    dialog.id = 'issue-write-dialog';
    dialog.setAttribute('aria-labelledby', 'issue-write-title');
    dialog.setAttribute('aria-describedby', 'issue-write-description');
    const heading = el('div', 'dialog-heading');
    const title = el('h2');
    title.id = 'issue-write-title';
    const close = button('取消', closeDialog, 'button button-quiet button-small');
    close.setAttribute('aria-label', '取消并关闭问题写入窗口');
    heading.append(title, close);
    const description = el('p', 'muted');
    description.id = 'issue-write-description';
    const form = el('form', 'issue-write-form');
    const titleInput = el('input');
    titleInput.type = 'text';
    titleInput.maxLength = 256;
    const titleField = field('问题标题', titleInput, 'issue-write-subject');
    const bodyInput = el('textarea');
    bodyInput.rows = 9;
    bodyInput.maxLength = 60000;
    const bodyField = field('问题正文', bodyInput, 'issue-write-body');
    const markdownHint = el('p', 'field-hint', '支持 Markdown 输入；预览按纯文本显示，不渲染格式。正文最多 60000 字符。');
    const prepare = el('button', 'button button-primary', '预览并核对');
    prepare.type = 'submit';
    form.append(titleField, bodyField, markdownHint, prepare);
    const preview = el('div', 'issue-preview');
    preview.hidden = true;
    const error = el('p', 'inline-error');
    error.hidden = true;
    error.setAttribute('role', 'alert');
    const footer = el('div', 'dialog-footer');
    const expiry = el('p', 'field-hint');
    const edit = button('返回修改', () => {
      invalidateDraft();
      ui.write.preview.hidden = true;
      ui.write.form.hidden = false;
      ui.write.footer.hidden = true;
      ui.write.error.hidden = true;
      ui.write.bodyInput.focus();
    }, 'button button-quiet');
    const confirm = button('确认发布', confirmWrite, 'button button-primary');
    confirm.id = 'issue-write-confirm';
    footer.append(expiry, edit, confirm);
    footer.hidden = true;
    dialog.append(heading, description, form, preview, error, footer);
    document.body.append(dialog);
    Object.assign(ui, { write: { dialog, title, close, description, form, titleInput, titleField,
      bodyInput, bodyField, prepare, preview, error, footer, expiry, edit, confirm } });
    titleInput.addEventListener('input', saveCreateDraft);
    bodyInput.addEventListener('input', saveCreateDraft);
    form.addEventListener('submit', (event) => { event.preventDefault(); prepareWrite(); });
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      if (!s.confirming) closeDialog();
    });
    dialog.addEventListener('close', () => {
      ++s.dialogRequest;
      invalidateDraft();
      s.preparing = false;
      s.attempted = false;
      s.action = null;
      if (s.opener?.isConnected) s.opener.focus({ preventScroll: true });
    });
  }

  function invalidateDraft() {
    s.draft = null;
    s.expires = 0;
    window.clearInterval(s.expiryTimer);
    s.expiryTimer = null;
    if (ui?.write) ui.write.confirm.disabled = true;
  }
  function closeDialog() {
    if (s.confirming) return;
    saveCreateDraft();
    ++s.dialogRequest;
    invalidateDraft();
    ui.write.dialog.close();
  }
  function saveCreateDraft() {
    if (s.action?.action === 'create' && !s.attempted) {
      s.creates.set(s.action.repo, { title: ui.write.titleInput.value, body: ui.write.bodyInput.value });
    }
  }
  function openEditor(action, body = '') {
    if (!writable(action !== 'create') || ui.write.dialog.open) return;
    s.opener = document.activeElement;
    s.action = { action, repo: s.repo };
    if (action !== 'create') s.action.number = s.number;
    if (action === 'state') s.action.state = s.detail.issue.state === 'closed' ? 'open' : 'closed';
    s.attempted = false;
    s.preparing = false;
    invalidateDraft();
    const w = ui.write;
    const saved = s.creates.get(s.repo);
    w.title.textContent = action === 'create' ? '新建问题' : action === 'comment' ? '预览评论' : s.action.state === 'open' ? '重新打开问题' : '关闭问题';
    w.description.textContent = `${s.repo}${action === 'create' ? '' : ` · #${s.number}`} · 关闭此窗口即可取消，预览不会发布。`;
    w.titleInput.value = action === 'create' ? saved?.title || '' : '';
    w.bodyInput.value = action === 'create' ? saved?.body || '' : body;
    w.titleInput.disabled = false;
    w.bodyInput.disabled = false;
    w.titleInput.required = action === 'create';
    w.bodyInput.required = action !== 'state';
    w.titleField.hidden = action !== 'create';
    w.bodyField.hidden = action === 'state';
    w.form.hidden = action === 'state';
    w.preview.hidden = true;
    w.preview.replaceChildren();
    w.footer.hidden = true;
    w.error.hidden = true;
    w.edit.hidden = action === 'state';
    w.close.disabled = false;
    w.prepare.disabled = false;
    w.prepare.textContent = '预览并核对';
    w.dialog.showModal();
    if (action === 'state') prepareWrite();
    else if (action === 'comment') prepareWrite();
    else w.titleInput.focus();
  }

  function showWriteError(text) {
    ui.write.error.textContent = text;
    ui.write.error.hidden = false;
  }
  function tickExpiry() {
    const left = Math.max(0, Math.ceil((s.expires - Date.now()) / 1000));
    ui.write.expiry.textContent = left > 0
      ? `草稿 ${Math.floor(left / 60)} 分 ${left % 60} 秒后失效 · 关闭即取消`
      : '草稿已过期，请关闭窗口后重新预览。';
    ui.write.confirm.disabled = left <= 0 || s.attempted || s.confirming;
    if (left <= 0) { window.clearInterval(s.expiryTimer); s.expiryTimer = null; }
  }

  async function prepareWrite() {
    if (!s.action || s.preparing || s.confirming || s.attempted) return;
    const w = ui.write;
    const payload = { ...s.action };
    if (payload.action !== 'state') {
      if (!w.form.reportValidity()) return;
      if (!w.bodyInput.value.trim() || (payload.action === 'create' && !w.titleInput.value.trim())) {
        showWriteError('标题与正文不能只有空白，请补充内容。');
        return;
      }
      payload.body = w.bodyInput.value;
      if (payload.action === 'create') payload.title = w.titleInput.value.trim();
    }
    const request = ++s.dialogRequest;
    s.preparing = true;
    w.error.hidden = true;
    w.prepare.disabled = true;
    w.prepare.textContent = '正在准备预览…';
    w.titleInput.disabled = true;
    w.bodyInput.disabled = true;
    try {
      const data = await apiPost('issue_prepare', payload);
      if (request !== s.dialogRequest || !w.dialog.open) return;
      if (!data.draft_id || !data.preview || data.preview.action !== payload.action || data.preview.repo !== payload.repo) throw new Error('草稿预览格式不正确，请关闭后重新预览。');
      const expiry = typeof data.expires_at === 'number' ? data.expires_at * (data.expires_at < 1e12 ? 1000 : 1) : Date.parse(data.expires_at);
      if (!Number.isFinite(expiry)) throw new Error('草稿有效期不可用，请重新预览。');
      s.draft = data.draft_id;
      s.expires = expiry;
      w.form.hidden = true;
      w.preview.hidden = false;
      w.preview.replaceChildren();
      const p = data.preview;
      const actionLabel = p.action === 'create' ? '创建新问题' : p.action === 'comment' ? '发布评论' : p.state === 'open' ? '重新打开问题' : '关闭问题';
      w.preview.append(badge('待确认', 'tag-amber'), el('h3', '', actionLabel),
        el('p', 'mono', `${p.repo}${p.number ? ` · #${p.number}` : ''}`));
      if (p.title) w.preview.append(el('h3', '', p.title));
      if (p.action !== 'state') w.preview.append(el('div', 'issue-source', p.body || '（空正文）'));
      else w.preview.append(statusBadge(p.state));
      w.preview.append(el('p', 'field-hint', '这是服务端草稿的纯文本预览。确认后将写入 GitHub，关闭窗口则不发布。'));
      w.footer.hidden = false;
      w.confirm.textContent = p.action === 'state' ? '确认更改状态' : '确认发布';
      tickExpiry();
      s.expiryTimer = window.setInterval(tickExpiry, 1000);
      w.confirm.focus();
    } catch (error) {
      if (request !== s.dialogRequest) return;
      invalidateDraft();
      showWriteError(`预览准备失败：${message(error)}（尚未发布）`);
      if (payload.action === 'state') {
        w.preview.hidden = false;
        w.preview.replaceChildren(button('重新准备预览', prepareWrite));
      }
    } finally {
      if (request === s.dialogRequest) {
        s.preparing = false;
        w.prepare.disabled = false;
        w.prepare.textContent = '预览并核对';
        w.titleInput.disabled = false;
        w.bodyInput.disabled = false;
      }
    }
  }

  async function confirmWrite() {
    if (!s.draft || s.confirming || s.attempted || s.expires <= Date.now()) return;
    const w = ui.write;
    const draft = s.draft;
    const action = { ...s.action };
    // 草稿一次性：发出确认即在本地消耗，网络失败也绝不自动重发。
    s.attempted = true;
    s.confirming = true;
    invalidateDraft();
    w.confirm.disabled = true;
    w.confirm.textContent = '正在提交…';
    w.edit.hidden = true;
    w.close.disabled = true;
    w.error.hidden = true;
    w.expiry.textContent = '正在等待 GitHub 响应，请勿重复提交。';
    let result;
    try {
      result = await apiPost('issue_confirm', { draft_id: draft });
      if (result.ok !== true) throw new Error(result.message || '未收到明确的成功结果');
    } catch (error) {
      showWriteError(`确认失败：${message(error)}。结果可能尚未同步，请先到 GitHub 核实，避免重复发布。本草稿不会再次提交。`);
      w.preview.append(external(`https://github.com/${action.repo}/issues${action.number ? `/${action.number}` : ''}`, '核实 GitHub 上的结果'));
      w.confirm.textContent = '本草稿已停止提交';
      w.expiry.textContent = '请先核实结果，再关闭窗口；如需重试，重新预览。';
      return;
    } finally {
      s.confirming = false;
      w.close.disabled = false;
    }
    if (action.action === 'comment') s.comments.delete(`${action.repo}#${action.number}`);
    if (action.action === 'create') s.creates.delete(action.repo);
    w.dialog.close();
    ui.feedback.replaceChildren(el('div', '', result.message || '操作成功，GitHub 已接收。'), external(result.html_url || result.issue?.html_url || result.comment?.html_url));
    ui.feedback.hidden = false;
    announce('问题操作成功，正在刷新列表与详情');
    if (s.repo !== action.repo) return;
    const number = result.issue?.number || action.number;
    if (action.action === 'create') {
      s.filter = 'open'; s.page = 1; s.query = ''; ui.search.value = '';
    }
    await loadList();
    if (number) await loadDetail(number);
  }

  function settingsChanged() {
    const cfg = getSettings();
    const key = JSON.stringify([cfg?.github_owner, cfg?.token_configured, cfg?.issue_repositories, cfg?.excluded_repositories, getPreview()]);
    if (key === s.configKey) return;
    const previous = s.configKey;
    s.configKey = key;
    s.loaded = false;
    if (s.mounted) {
      renderPermission();
      if (ui.write.dialog.open && !s.confirming) closeDialog();
      // 首次载入配置由 activate/refresh 完成；之后改权限即重新读取服务端权限。
      if (s.active && previous) refresh();
    }
  }

  return {
    activate() {
      s.active = true;
      mount();
      settingsChanged();
      if (!s.loaded && !s.repoLoading) refresh();
    },
    deactivate() {
      s.active = false;
      if (ui?.write.dialog.open && !s.confirming) closeDialog();
    },
    refresh,
    settingsChanged,
  };
}
