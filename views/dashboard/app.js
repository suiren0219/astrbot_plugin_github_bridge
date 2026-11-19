import { createIssueWorkspace } from './issues.js';

(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const root = document.documentElement;
  const state = {
    authenticated: false,
    page: 'overview',
    eventStatus: 'all',
    repoFilter: 'all',
    repoSearch: '',
    status: null,
    settings: null,
    settingsDirty: false,
    settingsLoaded: false,
    repositories: [],
    events: [],
    repositoriesError: null,
    eventsError: null,
    settingsError: null,
    eventRequestId: 0,
    refreshing: false,
    importing: false,
    settingsSaving: false,
    settingPending: false,
    retrying: false,
    repositoriesLoaded: false,
    eventsLoaded: false,
    repositoriesLoading: false,
    eventsLoading: false,
    settingsLoading: false,
    importGeneration: 0,
    importFiles: { sent: null, failed: null },
    theme: 'system',
    mediaQuery: null,
  };

  const PAGES = ['overview', 'repositories', 'events', 'settings', 'issues'];
  const EVENT_STATUSES = ['all', 'pending', 'sent', 'failed'];
  const REPO_FILTERS = ['all', 'private', 'archived', 'error'];
  const THEME_CHOICES = ['system', 'light', 'dark', 'amoled'];
  const THEME_STORAGE_KEY = 'github-bridge-theme';
  const IMPORT_FILE_LIMIT = 512 * 1024;
  const IMPORT_TOTAL_LIMIT = 2 * 1024 * 1024;

  const PAGE_LABELS = { overview: '总览', repositories: '仓库', events: '推送', settings: '设置', issues: '问题' };
  const PAGE_COPY = {
    overview: {
      eyebrow: 'WORKSPACE OVERVIEW',
      title: '一切动态，尽在此处',
      description: '连接你的仓库，让值得关注的更新及时抵达。',
    },
    repositories: {
      eyebrow: 'WATCHED REPOSITORIES',
      title: '你的仓库，安静地被关注',
      description: '只读查看同步范围、状态和最近一次检查。',
    },
    events: {
      eyebrow: 'DELIVERY QUEUE',
      title: '每一条消息，都有去处',
      description: '查看推送状态，必要时把失败记录重新送入队列。',
    },
    issues: {
      eyebrow: '问题工作台',
      title: '从一个问题，推进下一步',
      description: '在自己的仓库里查看、创建与讨论问题，每次写入都先确认。',
    },
    settings: {
      eyebrow: 'BRIDGE SETTINGS',
      title: '把桥梁调成你的样子',
      description: '凭据只进服务端，页面只展示它是否已经配置。',
    },
  };

  const EVENT_KIND_META = {
    commit: { label: '提交', icon: 'i-commit', className: 'commit' },
    commits: { label: '提交', icon: 'i-commit', className: 'commit' },
    push: { label: '提交', icon: 'i-commit', className: 'commit' },
    release: { label: '发布', icon: 'i-tag', className: 'release' },
    releases: { label: '发布', icon: 'i-tag', className: 'release' },
    issue: { label: 'Issue', icon: 'i-issue', className: 'issue' },
    issues: { label: 'Issue', icon: 'i-issue', className: 'issue' },
    'issue-event': { label: 'Issue 动态', icon: 'i-issue', className: 'issue' },
    comment: { label: '评论', icon: 'i-chat', className: 'issue' },
    'issue-comment': { label: 'Issue 评论', icon: 'i-chat', className: 'issue' },
    legacy: { label: '旧记录', icon: 'i-import', className: 'metadata' },
    metadata: { label: '仓库信息', icon: 'i-repo', className: 'metadata' },
    repo: { label: '仓库信息', icon: 'i-repo', className: 'metadata' },
  };
  const EVENT_STATUS_META = {
    pending: { label: '待推送', className: 'tag-amber', dot: 'dot-amber' },
    sent: { label: '已送达', className: 'tag-green', dot: '' },
    failed: { label: '失败', className: 'tag-red', dot: 'dot-red' },
  };

  class ApiError extends Error {
    constructor(message, status = 0, data = null) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.data = data;
    }
  }

  function icon(symbolId, className = 'icon') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#${symbolId}`);
    svg.append(use);
    return svg;
  }

  function text(element, value) {
    if (element) element.textContent = value == null ? '' : String(value);
    return element;
  }

  function clear(element) {
    if (element) element.replaceChildren();
    return element;
  }

  function appendText(element, value) {
    if (element) element.append(document.createTextNode(value == null ? '' : String(value)));
    return element;
  }

  function asString(value, fallback = '') {
    if (value === null || value === undefined) return fallback;
    return typeof value === 'string' ? value : String(value);
  }

  function asBoolean(value, fallback = false) {
    return typeof value === 'boolean' ? value : fallback;
  }

  function asNumber(value, fallback = null) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return fallback;
    const number = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function parseDate(value) {
    if (value === null || value === undefined || value === '') return null;
    const numeric = typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value));
    const raw = numeric ? Number(value) : String(value);
    const date = new Date(numeric && raw < 1e12 ? raw * 1000 : raw);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function redact(message) {
    let result = asString(message).replace(/(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+/g, '[凭据已隐藏]');
    for (const id of ['github-token']) {
      const value = document.getElementById(id)?.value;
      if (value && value.length > 3) result = result.split(value).join('[凭据已隐藏]');
    }
    return result.replace(/((?:authorization|github_token|password|secret|access_token)\s*[:=]\s*)([^\s,;]+)/gi, '$1[凭据已隐藏]');
  }

  function numberText(value, fallback = '—') {
    const number = asNumber(value);
    return number === null ? fallback : number.toLocaleString('zh-CN');
  }

  function safeExternalUrl(value) {
    const candidate = asString(value).trim();
    if (!candidate) return '';
    try {
      const url = new URL(candidate, window.location.origin);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
      return url.href;
    } catch (_error) {
      return '';
    }
  }

  function formatDate(value, fallback = '—') {
    const raw = asString(value).trim();
    if (!raw) return fallback;
    const date = parseDate(value);
    if (!date) return fallback;
    return new Intl.DateTimeFormat('zh-CN', {
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date);
  }

  function formatDateTime(value, fallback = '—') {
    const raw = asString(value).trim();
    if (!raw) return fallback;
    const date = parseDate(value);
    if (!date) return fallback;
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date);
  }

  function relativeDate(value, fallback = '尚未检查') {
    const raw = asString(value).trim();
    if (!raw) return fallback;
    const date = parseDate(value);
    if (!date) return '时间不可用';
    const diff = Date.now() - date.getTime();
    const minutes = Math.floor(Math.abs(diff) / 60000);
    if (minutes < 1) return diff >= 0 ? '刚刚' : '即将';
    if (minutes < 60) return diff >= 0 ? `${minutes} 分钟前` : `${minutes} 分钟后`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return diff >= 0 ? `${hours} 小时前` : `${hours} 小时后`;
    const days = Math.floor(hours / 24);
    if (days < 7) return diff >= 0 ? `${days} 天前` : `${days} 天后`;
    return formatDate(value, fallback);
  }

  function readErrorMessage(data, fallback = '服务暂时不可用') {
    if (typeof data === 'string' && data.trim()) return data.trim();
    if (data && typeof data === 'object') {
      for (const key of ['message', 'error', 'detail']) {
        if (typeof data[key] === 'string' && data[key].trim()) return data[key].trim();
      }
    }
    return fallback;
  }

  const pluginBridge = window.AstrBotPluginView || window.AstrBotPluginPage;

  async function apiRequest(path, options = {}) {
    if (!pluginBridge) throw new ApiError('请从 AstrBot Dashboard 的插件视图打开此页面');
    const endpoint = path.replace(/^\/api\//, '');
    const [route, query = ''] = endpoint.split('?');
    const params = Object.fromEntries(new URLSearchParams(query));
    try {
      const data = options.method === 'POST'
        ? await pluginBridge.apiPost(route, options.body || {})
        : await pluginBridge.apiGet(route, params);
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('服务返回的数据格式不正确');
      if (data.ok === false || data.status === 'error' || data.error) throw new Error(readErrorMessage(data, '请求失败'));
      return data;
    } catch (error) {
      throw new ApiError(redact(error?.message || 'Dashboard 请求失败'));
    }
  }

  function apiGet(path) {
    return apiRequest(path, { method: 'GET' });
  }

  function apiPost(path, body = {}) {
    return apiRequest(path, { method: 'POST', body });
  }

  const issueWorkspace = createIssueWorkspace({
    root: $('#page-issues'),
    apiGet,
    apiPost,
    getSettings: () => state.settings,
    getPreview: () => state.status?.preview === true,
    redact,
    announce,
  });

  function announce(message) {
    text($('#announcer'), message);
  }

  function toast(message, type = 'success') {
    const region = $('#toast-region');
    if (!region) return;
    const item = document.createElement('div');
    item.className = `toast${type === 'error' ? ' error' : ''}`;
    item.setAttribute('role', type === 'error' ? 'alert' : 'status');
    item.append(icon(type === 'error' ? 'i-alert' : 'i-check'));
    const content = document.createElement('span');
    content.textContent = message;
    item.append(content);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'icon-button';
    close.setAttribute('aria-label', '关闭提示');
    close.append(icon('i-close'));
    close.addEventListener('click', () => item.remove());
    item.append(close);
    region.append(item);
    window.setTimeout(() => item.remove(), 5000);
  }

  function setConnection(kind, message) {
    const indicator = $('#connection-indicator');
    const dot = indicator ? $('.status-dot', indicator) : null;
    const label = $('#connection-text');
    if (!dot || !label) return;
    dot.classList.remove('dot-muted', 'dot-red', 'dot-amber');
    if (kind === 'online') {
      text(label, message || '已连接');
    } else if (kind === 'loading') {
      dot.classList.add('dot-amber');
      text(label, message || '同步中');
    } else {
      dot.classList.add('dot-red');
      text(label, message || '连接异常');
    }
  }

  function setBusy(button, busy, busyLabel = '处理中') {
    if (!button) return;
    const label = $('span', button);
    if (busy) {
      if (!button.dataset.originalLabel && label) button.dataset.originalLabel = label.textContent;
      button.disabled = true;
      button.classList.add('is-busy');
      if (label) text(label, busyLabel);
      return;
    }
    button.disabled = false;
    button.classList.remove('is-busy');
    if (label && button.dataset.originalLabel) text(label, button.dataset.originalLabel);
    delete button.dataset.originalLabel;
  }

  function setDot(element, kind) {
    if (!element) return;
    element.classList.remove('dot-red', 'dot-amber', 'dot-muted');
    if (kind === 'error') element.classList.add('dot-red');
    else if (kind === 'pending') element.classList.add('dot-amber');
    else if (kind === 'muted') element.classList.add('dot-muted');
  }

  function setSwitch(button, checked, disabled = false) {
    if (!button) return;
    button.setAttribute('aria-checked', checked ? 'true' : 'false');
    button.disabled = disabled;
  }

  function setFilterPressed(selector, attribute, selected) {
    $$(selector).forEach((button) => {
      button.setAttribute('aria-pressed', button.getAttribute(attribute) === selected ? 'true' : 'false');
    });
  }

  function listLoading(message) {
    const wrapper = document.createElement('div');
    wrapper.className = 'list-loading';
    wrapper.append(Object.assign(document.createElement('span'), { className: 'spinner' }));
    appendText(wrapper, message);
    return wrapper;
  }

  function emptyState(title, description, action, iconId = 'i-repo') {
    const wrapper = document.createElement('div');
    wrapper.className = 'empty-state';
    const tile = document.createElement('span');
    tile.className = 'icon-tile';
    tile.append(icon(iconId));
    wrapper.append(tile);
    const heading = document.createElement('h2');
    text(heading, title);
    wrapper.append(heading);
    const paragraph = document.createElement('p');
    text(paragraph, description);
    wrapper.append(paragraph);
    if (action && action.label && action.action) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button button-tonal';
      button.dataset.action = action.action;
      text(button, action.label);
      wrapper.append(button);
    }
    return wrapper;
  }

  function applyTheme(theme, persist = true) {
    const next = THEME_CHOICES.includes(theme) ? theme : 'system';
    state.theme = next;
    root.dataset.theme = next;
    let resolved = next;
    if (next === 'system') {
      const hostDark = pluginBridge?.getContext?.()?.isDark;
      resolved = typeof hostDark === 'boolean'
        ? (hostDark ? 'dark' : 'light')
        : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    }
    root.dataset.resolvedTheme = resolved;
    if (persist) {
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch (_error) {
        // 浏览器禁用本地存储时仍保持当前会话的主题。
      }
    }
    $$('.theme-icon use').forEach((element) => {
      element.setAttribute('href', resolved === 'dark' || resolved === 'amoled' ? '#i-moon' : '#i-sun');
    });
    $$('[data-action="theme-cycle"]').forEach((button) => {
      button.setAttribute('aria-label', `当前${themeLabel(next)}，切换外观主题`);
      button.title = `当前${themeLabel(next)}，点击切换`;
    });
    $('meta[name="theme-color"]').content = resolved === 'amoled' ? '#000000' : resolved === 'dark' ? '#131813' : '#f3f5ef';
    $$('[data-theme-choice]').forEach((button) => {
      button.setAttribute('aria-pressed', button.dataset.themeChoice === next ? 'true' : 'false');
    });
  }

  function loadStoredTheme() {
    let stored = 'system';
    try {
      const value = window.localStorage.getItem(THEME_STORAGE_KEY);
      if (THEME_CHOICES.includes(value)) stored = value;
    } catch (_error) {
      stored = 'system';
    }
    applyTheme(stored, false);
    if (window.matchMedia) {
      state.mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
      const listener = () => {
        if (state.theme === 'system') applyTheme('system', false);
      };
      if (typeof state.mediaQuery.addEventListener === 'function') state.mediaQuery.addEventListener('change', listener);
      else if (typeof state.mediaQuery.addListener === 'function') state.mediaQuery.addListener(listener);
    }
  }

  function cycleTheme() {
    const index = THEME_CHOICES.indexOf(state.theme);
    applyTheme(THEME_CHOICES[(index + 1) % THEME_CHOICES.length]);
    announce(`外观已切换为${themeLabel(state.theme)}`);
  }

  function themeLabel(theme) {
    return { system: '跟随系统', light: '浅色', dark: '深色', amoled: 'AMOLED' }[theme] || '跟随系统';
  }

  function showGlobalError(message) {
    const banner = $('#global-error');
    text($('#global-error-message'), message);
    if (banner) banner.hidden = false;
  }

  function hideGlobalError() {
    const banner = $('#global-error');
    if (banner) banner.hidden = true;
  }

  function handleUnauthorized() {
    state.authenticated = false;
    showGlobalError('Dashboard 登录会话已过期，请重新登录后刷新插件视图。');
  }

  function showApp() {
    state.authenticated = true;
    $('#boot-screen').hidden = true;
    $('#app-shell').hidden = false;
  }

  function updatePageHeading(page) {
    const copy = PAGE_COPY[page] || PAGE_COPY.overview;
    text($('#page-eyebrow'), copy.eyebrow);
    text($('#page-title'), copy.title);
    const dot = document.createElement('span');
    dot.className = 'heading-dot';
    dot.textContent = '.';
    $('#page-title').append(dot);
    text($('#page-description'), copy.description);
  }

  function parseHash() {
    let value = window.location.hash.slice(1);
    try {
      value = decodeURIComponent(value);
    } catch (_error) {
      value = 'overview';
    }
    const [pagePart, queryPart = ''] = value.split('?');
    const page = PAGES.includes(pagePart) ? pagePart : 'overview';
    const query = new URLSearchParams(queryPart);
    const status = EVENT_STATUSES.includes(query.get('status')) ? query.get('status') : 'all';
    return { page, status };
  }

  function navigate(page, status = state.eventStatus) {
    const safePage = PAGES.includes(page) ? page : 'overview';
    const suffix = safePage === 'events' && EVENT_STATUSES.includes(status) && status !== 'all' ? `?status=${encodeURIComponent(status)}` : '';
    const next = `#${safePage}${suffix}`;
    if (window.location.hash !== next) window.location.hash = next;
    else {
      activatePage(safePage);
      if (safePage === 'events') loadEvents(status);
    }
  }

  function activatePage(page) {
    const safePage = PAGES.includes(page) ? page : 'overview';
    state.page = safePage;
    updatePageHeading(safePage);
    $$('[data-page]').forEach((section) => {
      section.hidden = section.dataset.page !== safePage;
    });
    $$('.nav-item').forEach((item) => {
      const active = item.getAttribute('aria-controls') === `page-${safePage}`;
      item.setAttribute('aria-selected', active ? 'true' : 'false');
      item.tabIndex = active ? 0 : -1;
    });
    document.title = `${PAGE_LABELS[safePage]} · GitHub Bridge`;
    $('#page-check').hidden = safePage === 'issues';
    if (safePage !== 'issues') issueWorkspace.deactivate();
    if (!state.authenticated) return;
    if (safePage === 'issues') issueWorkspace.activate();
    if (safePage === 'repositories' && !state.repositoriesLoaded && !state.repositoriesError) loadRepositories();
    if (safePage === 'events' && !state.eventsLoaded && !state.eventsError) loadEvents(state.eventStatus);
    if (safePage === 'settings' && !state.settingsLoaded && !state.settingsError) loadSettings();
  }

  function handleHashChange() {
    const parsed = parseHash();
    const changed = parsed.page === 'events' && state.eventStatus !== parsed.status;
    if (parsed.page === 'events') state.eventStatus = parsed.status;
    setFilterPressed('[data-status-filter]', 'data-status-filter', state.eventStatus);
    activatePage(parsed.page);
    if (state.authenticated && changed) loadEvents(state.eventStatus);
  }

  function updateCountElements(counts = {}) {
    text($('#count-repositories'), numberText(counts.repositories));
    text($('#count-pending'), numberText(counts.pending));
    text($('#count-sent'), numberText(counts.sent));
    text($('#count-failed'), numberText(counts.failed));
    $$('[data-filter-count]').forEach((element) => {
      text(element, numberText(counts[element.dataset.filterCount], '0'));
    });
    const badge = $('#nav-failed-badge');
    const failed = asNumber(counts.failed, 0);
    if (badge) badge.hidden = failed < 1;
    const retry = $('#retry-all');
    if (retry && !state.eventsError) retry.disabled = failed < 1;
  }

  function applyStatus(status) {
    state.status = status && typeof status === 'object' ? status : {};
    const current = state.status;
    const enabled = asBoolean(current.enabled);
    const ready = asBoolean(current.ready);
    const running = asBoolean(current.running);
    const lastError = redact(current.last_error).trim();
    const preview = asBoolean(current.preview);
    const hero = $('#status-hero');
    const tag = $('#hero-status-tag');
    const tagLabel = $('#hero-status-label');
    const heroTitle = $('#hero-title');
    const heroDescription = $('#hero-description');
    const heroVersion = $('#hero-version');
    const heroDot = tag ? $('.status-dot', tag) : null;
    let statusKind = 'muted';
    let label = '已暂停';
    let title = '监控已暂停';
    let description = '自动检查已关闭，桥梁仍然保留。';
    if (lastError) {
      statusKind = 'error';
      label = '需要留意';
      title = '最近一次检查失败';
      description = lastError;
    } else if (!ready) {
      statusKind = 'pending';
      label = '待配置';
      title = '等待配置完成';
      description = '填写自己的 GitHub 用户名、Token 和私聊目标后，即可开始建立连接。';
    } else if (running) {
      statusKind = 'active';
      label = '检查中';
      title = '正在检查仓库';
      description = '变化会在本轮检查完成后进入推送队列。';
    } else if (enabled) {
      statusKind = 'active';
      label = '运行中';
      title = '桥梁正在工作';
      description = '有新的代码动态时，会安静地抵达你的私聊。';
    }
    if (hero) {
      hero.dataset.status = statusKind;
      hero.setAttribute('aria-busy', 'false');
    }
    setDot(heroDot, statusKind === 'error' ? 'error' : statusKind === 'pending' ? 'pending' : statusKind === 'muted' ? 'muted' : 'active');
    text(tagLabel, label);
    text(heroTitle, title);
    text(heroDescription, description);
    text(heroVersion, current.version ? `version ${asString(current.version)}` : 'version —');
    text($('#hero-last-check'), relativeDate(current.last_check));
    text($('#hero-next-check'), relativeDate(current.next_check, enabled ? '等待安排' : '监控未启用'));
    text($('#route-owner'), current.github_owner || (state.settings && state.settings.github_owner) || '—');
    text($('#route-target'), current.target_umo || (state.settings && state.settings.target_umo) || '—');
    setDot($('#route-source-dot'), lastError ? 'error' : ready ? 'active' : 'pending');
    if (state.settings && !state.settingPending) state.settings.enabled = enabled;
    const switchesDisabled = !state.settingsLoaded || state.settingPending || state.settingsSaving || !state.authenticated;
    setSwitch($('[data-setting-toggle="enabled"].hero-switch'), enabled, switchesDisabled);
    setSwitch($('[data-setting-toggle="enabled"]:not(.hero-switch)'), enabled, switchesDisabled);
    $('#route-source-dot').setAttribute('aria-label', lastError ? '连接异常' : ready ? '已配置连接' : '等待配置');
    const counts = current.counts && typeof current.counts === 'object' ? current.counts : {};
    updateCountElements(counts);
    text($('#runtime-interval'), current.poll_interval == null ? '—' : `${numberText(current.poll_interval)} 秒`);
    text($('#runtime-version'), current.version ? asString(current.version) : '—');
    text($('#runtime-state'), label);
    const limit = asNumber(current.rate_limit && current.rate_limit.limit);
    const remaining = asNumber(current.rate_limit && current.rate_limit.remaining);
    const quotaText = limit === null || remaining === null ? '— / —' : `${numberText(remaining)} / ${numberText(limit)}`;
    text($('#quota-value'), quotaText);
    const progress = $('#quota-progress');
    if (progress) progress.value = limit && limit > 0 && remaining !== null ? Math.max(0, Math.min(100, (remaining / limit) * 100)) : 0;
    text($('#quota-reset'), current.rate_limit && current.rate_limit.reset ? `重置时间：${formatDateTime(current.rate_limit.reset)}` : '等待接口返回额度信息');
    if (lastError) {
      text($('#status-error-message'), lastError);
      $('#status-error').hidden = false;
    } else {
      $('#status-error').hidden = true;
    }
    $('#preview-banner').hidden = !preview;
    const configurationWarning = [current.configuration_warning, current.summary_warning].filter(Boolean).join(' ');
    text($('#configuration-warning-message'), configurationWarning);
    $('#configuration-warning').hidden = !configurationWarning;
    const setupNeeded = !ready && !lastError;
    $('#setup-guide').hidden = !setupNeeded;
    const checkButton = $('#page-check');
    if (checkButton && !checkButton.classList.contains('is-busy')) {
      checkButton.disabled = !ready || running || !state.authenticated;
      checkButton.title = !ready ? '请先在设置中完成连接' : running ? '检查正在进行中' : '立即执行一次检查';
    }
    text($('#settings-version'), current.version ? `VERSION ${asString(current.version)}` : 'VERSION —');
    renderRecentEvents(Array.isArray(current.recent_events) ? current.recent_events : []);
  }

  function kindMeta(kind) {
    const normalized = asString(kind).toLowerCase().replace(/_/g, '-');
    return EVENT_KIND_META[normalized] || EVENT_KIND_META.metadata;
  }

  function statusMeta(status) {
    return EVENT_STATUS_META[asString(status).toLowerCase()] || null;
  }

  function makeTag(label, className = '') {
    const element = document.createElement('span');
    element.className = `tag${className ? ` ${className}` : ''}`;
    text(element, label);
    return element;
  }

  function createEventSymbol(event) {
    const meta = kindMeta(event && event.kind);
    const element = document.createElement('span');
    element.className = `event-symbol ${meta.className}`;
    element.append(icon(meta.icon));
    return element;
  }

  function createEventRow(event, compact = false) {
    const value = event && typeof event === 'object' ? event : {};
    const row = document.createElement('div');
    row.className = 'event-row';
    row.append(createEventSymbol(value));
    const main = document.createElement('div');
    main.className = 'event-main';
    const heading = document.createElement('div');
    heading.className = 'event-heading';
    const repo = document.createElement('span');
    repo.className = 'event-repo';
    text(repo, value.repo || '未知仓库');
    heading.append(repo);
    heading.append(makeTag(kindMeta(value.kind).label));
    main.append(heading);
    const title = document.createElement('strong');
    title.className = 'event-title';
    text(title, value.title || '未命名动态');
    main.append(title);
    const metadata = document.createElement('div');
    metadata.className = 'event-meta';
    const occurred = document.createElement('span');
    text(occurred, compact ? relativeDate(value.occurred_at, '时间未知') : formatDateTime(value.occurred_at, '时间未知'));
    metadata.append(occurred);
    if (value.attempts !== undefined && !compact) {
      const attempts = document.createElement('span');
      text(attempts, `尝试 ${numberText(value.attempts, '0')} 次`);
      metadata.append(attempts);
    }
    main.append(metadata);
    row.append(main);
    const side = document.createElement('div');
    side.className = 'event-side';
    if (!compact) {
      const time = document.createElement('time');
      time.dateTime = asString(value.occurred_at);
      text(time, relativeDate(value.occurred_at, '—'));
      side.append(time);
    }
    const status = statusMeta(value.status);
    if (status) side.append(makeTag(status.label, status.className));
    row.append(side);
    return row;
  }

  function renderRecentEvents(events) {
    const container = $('#recent-events');
    if (!container) return;
    container.setAttribute('aria-busy', 'false');
    clear(container);
    const list = Array.isArray(events) ? events.slice(0, 5) : [];
    if (!list.length) {
      container.append(emptyState('还没有最近动态', '完成首次检查后，新的提交、发布和 Issue 会在这里出现。', { label: '查看设置', action: 'go-settings' }, 'i-activity'));
      return;
    }
    list.forEach((event) => container.append(createEventRow(event, true)));
  }

  function showLoading(container, message) {
    if (!container) return;
    container.setAttribute('aria-busy', 'true');
    clear(container);
    container.append(listLoading(message));
  }

  function renderRepositories() {
    const container = $('#repositories-list');
    if (!container) return;
    container.setAttribute('aria-busy', 'false');
    clear(container);
    if (state.repositoriesError) {
      container.append(emptyState('仓库列表加载失败', state.repositoriesError, { label: '重新加载', action: 'reload-repositories' }, 'i-alert'));
      text($('#repo-results'), '无法读取仓库');
      return;
    }
    const query = state.repoSearch.trim().toLowerCase();
    const filtered = state.repositories.filter((repo) => {
      const value = repo && typeof repo === 'object' ? repo : {};
      const name = asString(value.full_name).toLowerCase();
      const description = asString(value.description).toLowerCase();
      const matchesSearch = !query || name.includes(query) || description.includes(query);
      const matchesFilter = state.repoFilter === 'all'
        || (state.repoFilter === 'private' && value.private === true)
        || (state.repoFilter === 'archived' && value.archived === true)
        || (state.repoFilter === 'error' && Boolean(value.error));
      return matchesSearch && matchesFilter;
    });
    text($('#repo-results'), `${numberText(filtered.length, '0')} 个仓库${query || state.repoFilter !== 'all' ? '符合当前筛选' : ''}`);
    if (!filtered.length) {
      const title = state.repositories.length ? '没有符合条件的仓库' : '还没有监控仓库';
      const description = state.repositories.length ? '换一个关键词或筛选条件试试。' : '保存 GitHub Token 后，第一次检查会同步可访问的仓库。';
      container.append(emptyState(title, description, state.repositories.length ? null : { label: '前往设置', action: 'go-settings' }, 'i-repo'));
      return;
    }
    filtered.forEach((repo) => container.append(createRepositoryCard(repo)));
  }

  function createRepositoryCard(repository) {
    const repo = repository && typeof repository === 'object' ? repository : {};
    const article = document.createElement('article');
    article.className = 'card repo-card';
    const top = document.createElement('div');
    top.className = 'repo-card-top';
    const tile = document.createElement('span');
    tile.className = 'icon-tile';
    tile.append(icon('i-repo'));
    top.append(tile);
    const tags = document.createElement('div');
    tags.className = 'repo-tags';
    if (repo.private === true) tags.append(makeTag('PRIVATE', 'tag-purple'));
    if (repo.archived === true) tags.append(makeTag('归档'));
    if (repo.error) tags.append(makeTag('异常', 'tag-red'));
    top.append(tags);
    article.append(top);
    const heading = document.createElement('h2');
    const url = safeExternalUrl(repo.html_url);
    if (url) {
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noreferrer noopener';
      text(link, repo.full_name || '未命名仓库');
      heading.append(link);
    } else {
      text(heading, repo.full_name || '未命名仓库');
    }
    article.append(heading);
    const owner = document.createElement('p');
    owner.className = 'repo-owner mono';
    text(owner, repo.default_branch ? `默认分支 · ${asString(repo.default_branch)}` : '默认分支 · —');
    article.append(owner);
    const description = document.createElement('p');
    description.className = 'repo-description';
    text(description, repo.description || '这个仓库没有提供描述。');
    article.append(description);
    const stats = document.createElement('div');
    stats.className = 'repo-stats';
    const statItems = [
      ['i-star', repo.stargazers_count, 'Stars'],
      ['i-fork', repo.forks_count, 'Forks'],
      ['i-issue', repo.open_issues_count, 'Issues'],
    ];
    statItems.forEach(([symbol, value, label]) => {
      const item = document.createElement('span');
      item.append(icon(symbol));
      const valueText = document.createElement('span');
      text(valueText, numberText(value));
      item.append(valueText);
      item.setAttribute('aria-label', `${label} ${numberText(value)}`);
      stats.append(item);
    });
    article.append(stats);
    if (repo.error) {
      const error = document.createElement('p');
      error.className = 'repo-error';
      text(error, asString(repo.error));
      article.append(error);
    }
    const footer = document.createElement('div');
    footer.className = 'repo-footer';
    const branch = document.createElement('span');
    branch.className = 'repo-branch';
    branch.append(icon('i-branch'));
    const branchText = document.createElement('span');
    branchText.className = 'mono';
    text(branchText, repo.default_branch || '—');
    branch.append(branchText);
    footer.append(branch);
    const checked = document.createElement('time');
    checked.dateTime = asString(repo.last_check);
    text(checked, repo.last_check ? `检查于 ${formatDate(repo.last_check)}` : '尚未检查');
    footer.append(checked);
    article.append(footer);
    return article;
  }

  function renderEvents() {
    const container = $('#events-list');
    if (!container) return;
    container.setAttribute('aria-busy', 'false');
    clear(container);
    setFilterPressed('[data-status-filter]', 'data-status-filter', state.eventStatus);
    if (state.eventsError) {
      container.append(emptyState('推送记录加载失败', state.eventsError, { label: '重新加载', action: 'reload-events' }, 'i-alert'));
      text($('#event-results'), '无法读取推送记录');
      return;
    }
    text($('#event-results'), `${numberText(state.events.length, '0')} 条动态`);
    if (!state.events.length) {
      const title = state.eventStatus === 'all' ? '还没有推送记录' : `没有${statusMeta(state.eventStatus)?.label || ''}动态`;
      const description = state.eventStatus === 'all' ? '完成首次检查后，新的变化会在这里留下记录。' : '换一个状态筛选，或等下一次检查完成。';
      container.append(emptyState(title, description, state.eventStatus === 'all' ? { label: '查看设置', action: 'go-settings' } : null, 'i-send'));
      return;
    }
    state.events.forEach((event) => container.append(createEventDetails(event)));
  }

  function createEventDetails(eventValue) {
    const event = eventValue && typeof eventValue === 'object' ? eventValue : {};
    const details = document.createElement('details');
    details.className = 'event-detail';
    const summary = document.createElement('summary');
    summary.className = 'event-summary';
    summary.append(createEventRow(event, false));
    summary.append(icon('i-chevron'));
    details.append(summary);
    const body = document.createElement('div');
    body.className = 'event-body';
    const pre = document.createElement('pre');
    text(pre, event.body || '这条动态没有附带正文。');
    body.append(pre);
    const url = safeExternalUrl(event.url);
    if (url) {
      const link = document.createElement('a');
      link.className = 'text-link';
      link.href = url;
      link.target = '_blank';
      link.rel = 'noreferrer noopener';
      text(link, '在 GitHub 中打开');
      link.append(icon('i-external'));
      body.append(link);
    }
    if (event.last_error) {
      const error = document.createElement('p');
      error.className = 'event-error';
      text(error, `错误：${redact(event.last_error)}`);
      body.append(error);
    }
    const metadata = document.createElement('div');
    metadata.className = 'event-meta';
    text(metadata, `发生于 ${formatDateTime(event.occurred_at, '时间未知')}${event.sent_at ? ` · 送达于 ${formatDateTime(event.sent_at)}` : ''}`);
    body.append(metadata);
    if (asString(event.status).toLowerCase() === 'failed' && Number.isInteger(asNumber(event.id))) {
      const actions = document.createElement('div');
      actions.className = 'event-actions';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'button button-tonal button-small';
      retry.dataset.retryId = String(asNumber(event.id));
      retry.append(icon('i-refresh'));
      appendText(retry, '重新排队');
      actions.append(retry);
      body.append(actions);
    }
    details.append(body);
    return details;
  }

  function applySettings(settings, preserveDraft = false) {
    if (!settings || typeof settings !== 'object') return;
    const dirty = preserveDraft && state.settingsDirty;
    const draft = dirty ? Object.fromEntries($$('#settings-form input').map((input) => [input.id, input.value])) : null;
    const publicSettings = { ...settings };
    delete publicSettings.github_token;
    state.settings = publicSettings;
    state.settingsLoaded = true;
    state.settingsError = null;
    $('#settings-loading').hidden = true;
    $('#settings-load-error').hidden = true;
    $('#settings-content').hidden = false;
    $('#settings-fields').disabled = false;
    const values = {
      'github-owner': settings.github_owner,
      'target-umo': settings.target_umo,
      'poll-interval': settings.poll_interval,
      'max-push': settings.max_push_per_cycle,
      'issue-repositories': settings.issue_repositories,
      'excluded-repositories': settings.excluded_repositories,
      'request-timeout': settings.request_timeout,
      'max-pages': settings.max_pages,
      'summary-provider': settings.ai_summary_provider_id,
      'summary-timeout': settings.ai_summary_timeout,
    };
    Object.entries(values).forEach(([id, value]) => {
      const element = $(`#${id}`);
      if (element && value !== undefined && value !== null) element.value = String(value);
    });
    const tokenStatus = $('#token-configured');
    if (tokenStatus) {
      tokenStatus.classList.remove('tag-green', 'tag-red');
      if (settings.token_configured === true) {
        tokenStatus.classList.add('tag-green');
        text(tokenStatus, '已配置');
      } else {
        tokenStatus.classList.add('tag-red');
        text(tokenStatus, '未配置');
      }
    }
    const token = $('#github-token');
    if (token) token.value = '';
    ['enabled', 'monitor_commits', 'monitor_releases', 'monitor_issues', 'monitor_metadata', 'ai_summary_enabled', 'ai_summary_private'].forEach((key) => {
      $$(`[data-setting-toggle="${key}"]`).forEach((button) => setSwitch(button, asBoolean(settings[key]), false));
    });
    if (draft) Object.entries(draft).forEach(([id, value]) => { document.getElementById(id).value = value; });
    state.settingsDirty = dirty;
    setSettingsActionState();
    updateEnabledDescription();
    if (state.status) {
      state.status.enabled = asBoolean(settings.enabled);
      state.status.github_owner = settings.github_owner;
      state.status.target_umo = settings.target_umo;
      state.status.poll_interval = settings.poll_interval;
      applyStatus(state.status);
    }
    text($('#settings-version'), state.status && state.status.version ? `VERSION ${asString(state.status.version)}` : 'VERSION —');
    issueWorkspace.settingsChanged();
  }

  function updateEnabledDescription() {
    const description = $('#enabled-description');
    if (!description) return;
    if (!state.settings || state.settings.token_configured !== true || !state.settings.github_owner || !state.settings.target_umo) {
      text(description, '先填写自己的 GitHub 用户名、Token 和私聊目标，再开启自动监控。');
    } else if (asBoolean(state.settings.enabled)) {
      text(description, '按设定间隔检查仓库，发现变化后推送。');
    } else {
      text(description, '已暂停自动检查；你仍可以手动执行一次检查。');
    }
  }

  function setSettingsActionState() {
    const ready = state.authenticated && state.settingsLoaded && !state.settingsError;
    const pending = state.settingsSaving || state.settingPending || state.settingsLoading;
    $('#save-settings').disabled = !ready || !state.settingsDirty || pending;
    $('#discard-settings').disabled = !ready || !state.settingsDirty || pending;
    if (!$('#test-connection').classList.contains('is-busy')) $('#test-connection').disabled = !ready || pending;
    $$('[data-setting-toggle]').forEach((button) => { button.disabled = !ready || pending; });
    $('#settings-fields').disabled = !ready || state.settingsSaving;
    const label = $('#save-state');
    clear(label);
    const dot = document.createElement('span');
    dot.className = `status-dot${state.settingsDirty ? ' dot-amber' : ''}`;
    label.append(dot, document.createTextNode(state.settingsSaving ? '正在保存配置' : state.settingsDirty ? '有未保存的修改' : '配置已同步'));
  }

  function markSettingsDirty() {
    if (!state.settingsLoaded) return;
    state.settingsDirty = true;
    setSettingsActionState();
    text($('#save-state'), '有未保存的修改');
    const dot = $('#save-state .status-dot');
    setDot(dot, 'pending');
  }

  function resetSettingsForm() {
    if (state.settings) applySettings(state.settings);
    text($('#settings-error'), '');
    $('#settings-error').hidden = true;
    text($('#save-state'), '配置已同步');
    setDot($('#save-state .status-dot'), 'active');
  }

  function collectSettingsPatch() {
    const patch = {
      enabled: Boolean(state.settings && state.settings.enabled),
      github_owner: $('#github-owner').value.trim(),
      target_umo: $('#target-umo').value.trim(),
      poll_interval: Number.parseInt($('#poll-interval').value, 10),
      max_push_per_cycle: Number.parseInt($('#max-push').value, 10),
      monitor_commits: Boolean(state.settings && state.settings.monitor_commits),
      monitor_releases: Boolean(state.settings && state.settings.monitor_releases),
      monitor_issues: Boolean(state.settings && state.settings.monitor_issues),
      monitor_metadata: Boolean(state.settings && state.settings.monitor_metadata),
      issue_repositories: $('#issue-repositories').value.trim(),
      excluded_repositories: $('#excluded-repositories').value.trim(),
      request_timeout: Number.parseInt($('#request-timeout').value, 10),
      max_pages: Number.parseInt($('#max-pages').value, 10),
      ai_summary_enabled: Boolean(state.settings && state.settings.ai_summary_enabled),
      ai_summary_private: Boolean(state.settings && state.settings.ai_summary_private),
      ai_summary_provider_id: $('#summary-provider').value.trim(),
      ai_summary_timeout: Number.parseInt($('#summary-timeout').value, 10),
    };
    const token = $('#github-token').value;
    if (token) patch.github_token = token;
    return patch;
  }

  function validatePatch(patch) {
    if (patch.github_owner && !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/.test(patch.github_owner)) return 'GitHub 用户名格式不正确。';
    if (patch.target_umo && !/^[^:\s]+:FriendMessage:[^:\s]+$/.test(patch.target_umo)) return '私聊目标格式应为 平台ID:FriendMessage:用户ID。';
    if (patch.enabled && (!patch.github_owner || !patch.target_umo)) return '启用监控前请填写自己的 GitHub 用户名和私聊目标。';
    const numericRules = [
      ['poll_interval', 60, 86400],
      ['max_push_per_cycle', 1, 20],
      ['request_timeout', 5, 120],
      ['max_pages', 1, 100],
      ['ai_summary_timeout', 3, 60],
    ];
    for (const [key, minimum, maximum] of numericRules) {
      if (!Number.isInteger(patch[key]) || patch[key] < minimum || patch[key] > maximum) return `${key} 不在允许范围内。`;
    }
    return '';
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!state.authenticated || state.settingsSaving || state.settingPending || !state.settingsDirty) return;
    const form = $('#settings-form');
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    const patch = collectSettingsPatch();
    const validationError = validatePatch(patch);
    if (validationError) {
      text($('#settings-error'), validationError);
      $('#settings-error').hidden = false;
      return;
    }
    const button = $('#save-settings');
    state.settingsSaving = true;
    setSettingsActionState();
    setBusy(button, true, '保存中');
    text($('#settings-error'), '');
    $('#settings-error').hidden = true;
    try {
      const saved = await apiPost('/api/settings', patch);
      applySettings(saved);
      text($('#save-state'), '配置已同步');
      setDot($('#save-state .status-dot'), 'active');
      toast('配置已保存，服务端已接收。');
      announce('配置已保存');
      await loadStatusOnly();
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        return;
      }
      const message = readErrorMessage(error.data, error.message || '配置保存失败');
      text($('#settings-error'), message);
      $('#settings-error').hidden = false;
      toast(message, 'error');
    } finally {
      state.settingsSaving = false;
      setBusy(button, false, '保存配置');
      setSettingsActionState();
    }
  }

  async function toggleSetting(button, key) {
    if (!state.settingsLoaded || !state.settings || button.disabled || button.getAttribute('aria-busy') === 'true') return;
    const oldValue = asBoolean(state.settings[key]);
    const nextValue = !oldValue;
    state.settingPending = true;
    button.setAttribute('aria-busy', 'true');
    setSettingsActionState();
    try {
      const saved = await apiPost('/api/settings', { [key]: nextValue });
      applySettings(saved, true);
      if (key === 'enabled' && state.status) state.status.enabled = nextValue;
      updateEnabledDescription();
      toast(nextValue ? `${settingLabel(key)}已开启` : `${settingLabel(key)}已关闭`);
      announce(`${settingLabel(key)}${nextValue ? '已开启' : '已关闭'}`);
      if (state.status) applyStatus(state.status);
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        return;
      }
      const message = readErrorMessage(error.data, error.message || '设置未能保存');
      toast(message, 'error');
      announce(message);
    } finally {
      state.settingPending = false;
      button.removeAttribute('aria-busy');
      setSettingsActionState();
    }
  }

  function settingLabel(key) {
    return {
      enabled: '自动监控',
      monitor_commits: '代码提交监控',
      monitor_releases: '版本发布监控',
      monitor_issues: 'Issue 监控',
      monitor_metadata: '仓库信息监控',
      ai_summary_enabled: 'AI 总结',
      ai_summary_private: '私有内容总结',
    }[key] || '设置';
  }

  async function loadStatusOnly() {
    try {
      const status = await apiGet('/api/status');
      applyStatus(status);
      setConnection('online');
      hideGlobalError();
      return status;
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        throw error;
      }
      setConnection('offline');
      showGlobalError(readErrorMessage(error.data, error.message || '状态同步失败'));
      throw error;
    }
  }

  async function loadRepositories() {
    if (!state.authenticated || state.repositoriesLoading) return null;
    const container = $('#repositories-list');
    showLoading(container, '正在同步仓库列表');
    state.repositoriesError = null;
    state.repositoriesLoading = true;
    try {
      const data = await apiGet('/api/repositories');
      state.repositories = Array.isArray(data.items) ? data.items : [];
      state.repositoriesLoaded = true;
      renderRepositories();
      return state.repositories;
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        throw error;
      }
      state.repositoriesError = readErrorMessage(error.data, error.message || '仓库列表加载失败');
      renderRepositories();
      return null;
    } finally {
      state.repositoriesLoading = false;
    }
  }

  async function loadEvents(status = state.eventStatus) {
    if (!state.authenticated) return null;
    const validStatus = EVENT_STATUSES.includes(status) ? status : 'all';
    state.eventStatus = validStatus;
    const requestId = ++state.eventRequestId;
    const container = $('#events-list');
    showLoading(container, '正在读取推送记录');
    state.eventsError = null;
    state.eventsLoading = true;
    try {
      const data = await apiGet(`/api/events?status=${encodeURIComponent(validStatus)}&limit=50`);
      if (requestId !== state.eventRequestId) return null;
      state.events = Array.isArray(data.items) ? data.items : [];
      state.eventsLoaded = true;
      renderEvents();
      return state.events;
    } catch (error) {
      if (requestId !== state.eventRequestId) return null;
      if (error.status === 401) {
        handleUnauthorized();
        throw error;
      }
      state.eventsError = redact(readErrorMessage(error.data, error.message || '推送记录加载失败'));
      renderEvents();
      return null;
    } finally {
      if (requestId === state.eventRequestId) state.eventsLoading = false;
    }
  }

  async function loadSettings() {
    if (!state.authenticated || state.settingsLoading) return null;
    const loading = $('#settings-loading');
    const failure = $('#settings-load-error');
    const content = $('#settings-content');
    const wasLoaded = state.settingsLoaded;
    state.settingsLoading = true;
    if (!wasLoaded) {
      loading.hidden = false;
      failure.hidden = true;
      content.hidden = true;
    }
    state.settingsError = null;
    try {
      const data = await apiGet('/api/settings');
      applySettings(data, true);
      return state.settings;
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        throw error;
      }
      const message = redact(readErrorMessage(error.data, error.message || '配置加载失败'));
      if (!wasLoaded) {
        state.settingsError = message;
        loading.hidden = true;
        content.hidden = true;
        failure.hidden = false;
        text($('#settings-load-error-message'), message);
      } else {
        toast(message, 'error');
      }
      return null;
    } finally {
      state.settingsLoading = false;
      setSettingsActionState();
    }
  }

  async function refreshAll() {
    if (state.refreshing || !state.authenticated) return;
    state.refreshing = true;
    setConnection('loading', '同步中');
    const refreshButton = $('#refresh-all');
    refreshButton.disabled = true;
    refreshButton.classList.add('is-busy');
    try {
      await loadStatusOnly();
      await Promise.allSettled([loadRepositories(), loadEvents(state.eventStatus), loadSettings()]);
      if (state.page === 'issues') await issueWorkspace.refresh();
      text($('#last-refreshed'), `刚刚同步 · ${formatDateTime(new Date().toISOString())}`);
      announce('面板数据已刷新');
    } catch (error) {
      if (error.status !== 401) announce('面板刷新失败');
    } finally {
      state.refreshing = false;
      refreshButton.disabled = false;
      refreshButton.classList.remove('is-busy');
    }
  }

  let checkTimer = null;
  let viewClosed = false;

  function waitForCheck(previousCheck, attempts = 0) {
    window.clearTimeout(checkTimer);
    if (viewClosed || !state.authenticated) return;
    checkTimer = window.setTimeout(async () => {
      try {
        const status = await loadStatusOnly();
        if (!status.running && status.last_check !== previousCheck) {
          await Promise.allSettled([loadRepositories(), loadEvents(state.eventStatus)]);
          announce('检查已结束，仓库和推送记录已更新');
          return;
        }
        if (attempts < 120) waitForCheck(previousCheck, attempts + 1);
        else announce('检查仍未完成，可稍后手动刷新查看');
      } catch (_) {
        if (attempts < 3) waitForCheck(previousCheck, attempts + 1);
      }
    }, 2000);
  }

  window.addEventListener('pagehide', () => {
    viewClosed = true;
    window.clearTimeout(checkTimer);
  });

  async function runCheck() {
    const button = $('#page-check');
    if (button.disabled || button.classList.contains('is-busy')) return;
    setBusy(button, true, '检查中');
    setConnection('loading', '检查中');
    try {
      const result = await apiPost('/api/check', {});
      toast(asString(result.message, '检查已排队'));
      announce(asString(result.message, '检查已排队'));
      await loadStatusOnly();
      waitForCheck(result.last_check);
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        return;
      }
      const message = readErrorMessage(error.data, error.message || '检查失败');
      showGlobalError(message);
      toast(message, 'error');
    } finally {
      setBusy(button, false, '立即检查');
      if (state.status) applyStatus(state.status);
    }
  }

  async function retryEvents(id) {
    const payload = id === undefined ? {} : { id: Number(id) };
    try {
      const result = await apiPost('/api/retry', payload);
      toast(`已重新排队 ${numberText(result.count, '0')} 条记录`);
      await Promise.allSettled([loadStatusOnly(), loadEvents(state.eventStatus)]);
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        return;
      }
      const message = redact(readErrorMessage(error.data, error.message || '重新排队失败'));
      toast(message, 'error');
    }
  }

  async function testConnection() {
    const button = $('#test-connection');
    if (button.disabled || button.classList.contains('is-busy')) return;
    setBusy(button, true, '测试中');
    const resultElement = $('#test-result');
    resultElement.dataset.state = '';
    text(resultElement, '正在进行只读连通测试。');
    try {
      const result = await apiPost('/api/test', {});
      resultElement.dataset.state = 'success';
      text(resultElement, `连接成功：${asString(result.login, 'GitHub 账户已响应')} · 可见仓库 ${numberText(result.repository_count, '0')} 个。`);
      toast('只读连接测试成功');
      announce('只读连接测试成功');
    } catch (error) {
      if (error.status === 401) {
        handleUnauthorized();
        return;
      }
      const message = redact(readErrorMessage(error.data, error.message || '连接测试失败'));
      resultElement.dataset.state = 'error';
      text(resultElement, message);
      toast(message, 'error');
    } finally {
      setBusy(button, false, '测试已保存的连接');
    }
  }

  function resetImportDialog() {
    const form = $('#import-form');
    if (form) form.reset();
    ['sent', 'failed'].forEach((type) => {
      const stateElement = $(`#import-${type}-file-state`);
      text(stateElement, '可粘贴或选择文件，每份最多 512 KiB，总请求小于 2 MiB。');
      const clearButton = $(`[data-clear-import="${type}"]`);
      if (clearButton) clearButton.hidden = true;
    });
    $('#import-error').hidden = true;
    text($('#import-error'), '');
    $('#import-result').hidden = true;
    clear($('#import-result'));
    updateImportSubmit();
  }

  function openImportDialog() {
    const dialog = $('#import-dialog');
    resetImportDialog();
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    $('#import-sent').focus();
  }

  function closeImportDialog() {
    const dialog = $('#import-dialog');
    if (typeof dialog.close === 'function' && dialog.open) dialog.close();
    else dialog.removeAttribute('open');
  }

  function importTextBytes(type) {
    const value = $(`#import-${type}`)?.value || '';
    return new TextEncoder().encode(value).byteLength;
  }

  function importSizeError() {
    if (importTextBytes('sent') > IMPORT_FILE_LIMIT) return '已发送记录内容不能超过 512 KiB。';
    if (importTextBytes('failed') > IMPORT_FILE_LIMIT) return '失败记录内容不能超过 512 KiB。';
    if (importTextBytes('sent') + importTextBytes('failed') >= IMPORT_TOTAL_LIMIT - 1024) return '两份记录合计过大，请保持提交请求小于 2 MiB。';
    return '';
  }

  function updateImportSubmit() {
    const sent = $('#import-sent');
    const failed = $('#import-failed');
    const button = $('#import-submit');
    const hasInput = Boolean((sent && sent.value.trim()) || (failed && failed.value.trim()));
    if (button) button.disabled = !hasInput || Boolean(importSizeError()) || state.importing;
  }

  function setImportFileState(type, message, showClear) {
    text($(`#import-${type}-file-state`), message);
    const clearButton = $(`[data-clear-import="${type}"]`);
    if (clearButton) clearButton.hidden = !showClear;
  }

  function readImportFile(type, file) {
    if (!file) return;
    state.importFiles[type] = file;
    if (file.size > IMPORT_FILE_LIMIT) {
      setImportFileState(type, '文件超过 512 KiB，未读取。', false);
      toast('单个导入文件不能超过 512 KiB', 'error');
      return;
    }
    const otherType = type === 'sent' ? 'failed' : 'sent';
    if (file.size + (state.importFiles[otherType]?.size || 0) > IMPORT_TOTAL_LIMIT) {
      setImportFileState(type, '两份文件合计超过 2 MiB，未读取。', false);
      toast('两份导入文件合计不能达到 2 MiB', 'error');
      return;
    }
    const reader = new FileReader();
    setImportFileState(type, '正在读取本地文件…', false);
    reader.addEventListener('load', () => {
      const textarea = $(`#import-${type}`);
      if (textarea) textarea.value = typeof reader.result === 'string' ? reader.result : '';
      setImportFileState(type, `已读取本地文件：${file.name}`, true);
      updateImportSubmit();
    });
    reader.addEventListener('error', () => {
      setImportFileState(type, '文件读取失败，请改用粘贴内容。', false);
      toast('本地文件读取失败', 'error');
    });
    reader.readAsText(file);
  }

  function parseImportValue(type) {
    const textarea = $(`#import-${type}`);
    const raw = textarea ? textarea.value.trim() : '';
    if (!raw) return { value: type === 'sent' ? {} : [], error: '' };
    try {
      const parsed = JSON.parse(raw);
      const valid = type === 'sent'
        ? parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        : Array.isArray(parsed);
      if (!valid) return { value: null, error: type === 'sent' ? '已发送记录必须是 JSON 对象。' : '失败记录必须是 JSON 数组。' };
      return { value: parsed, error: '' };
    } catch (_error) {
      return { value: null, error: `${type === 'sent' ? '已发送' : '失败'}记录不是有效 JSON。` };
    }
  }

  function renderImportResult(result) {
    const output = $('#import-result');
    clear(output);
    output.hidden = false;
    const summary = document.createElement('strong');
    text(summary, `导入完成：${numberText(result.imported, '0')} 条已识别，${numberText(result.queued, '0')} 条进入队列，${numberText(result.skipped, '0')} 条跳过。`);
    output.append(summary);
    if (Array.isArray(result.warnings) && result.warnings.length) {
      const list = document.createElement('ul');
      result.warnings.forEach((warning) => {
        const item = document.createElement('li');
        text(item, warning);
        list.append(item);
      });
      output.append(list);
    }
  }

  async function submitImport(event) {
    event.preventDefault();
    if (state.importing) return;
    const sent = parseImportValue('sent');
    const failed = parseImportValue('failed');
    const errorMessage = sent.error || failed.error;
    if (errorMessage) {
      text($('#import-error'), errorMessage);
      $('#import-error').hidden = false;
      return;
    }
    if (!$('#import-sent').value.trim() && !$('#import-failed').value.trim()) {
      text($('#import-error'), '请至少提供一份已发送或失败记录。');
      $('#import-error').hidden = false;
      return;
    }
    const sizeError = importSizeError();
    const requestBytes = new TextEncoder().encode(JSON.stringify({ sent: sent.value, failed: failed.value })).length;
    if (sizeError || requestBytes >= IMPORT_TOTAL_LIMIT) {
      text($('#import-error'), sizeError || '提交请求必须小于 2 MiB。');
      $('#import-error').hidden = false;
      return;
    }
    state.importing = true;
    const button = $('#import-submit');
    setBusy(button, true, '导入中');
    $('#import-error').hidden = true;
    try {
      const result = await apiPost('/api/import', { sent: sent.value, failed: failed.value });
      renderImportResult(result);
      toast('旧推送记录已导入');
      announce('旧推送记录已导入');
      await Promise.allSettled([loadStatusOnly(), loadEvents(state.eventStatus)]);
    } catch (error) {
      if (error.status === 401) {
        closeImportDialog();
        handleUnauthorized();
        return;
      }
      const message = redact(readErrorMessage(error.data, error.message || '导入失败'));
      text($('#import-error'), message);
      $('#import-error').hidden = false;
      toast(message, 'error');
    } finally {
      state.importing = false;
      setBusy(button, false, '导入记录');
      updateImportSubmit();
    }
  }

  async function checkSession() {
    if (!pluginBridge) throw new Error('请从 AstrBot Dashboard 的插件视图打开此页面');
    await pluginBridge.ready();
    const stopContext = pluginBridge.onContext?.(() => {
      if (state.theme === 'system') applyTheme('system', false);
    });
    window.addEventListener('pagehide', () => stopContext?.(), { once: true });
    applyTheme(state.theme, false);
    showApp();
    await refreshAll();
  }

  function bindNavigation() {
    window.addEventListener('hashchange', handleHashChange);
    const parsed = parseHash();
    state.eventStatus = parsed.status;
    if (!window.location.hash || !PAGES.includes(parsed.page)) {
      window.location.hash = 'overview';
    }
    activatePage(parsed.page);
    setFilterPressed('[data-status-filter]', 'data-status-filter', state.eventStatus);
    $$('.nav-item').forEach((item, index, items) => {
      item.addEventListener('click', (event) => {
        event.preventDefault();
        const page = item.getAttribute('aria-controls').replace('page-', '');
        navigate(page, page === 'events' ? state.eventStatus : 'all');
      });
      item.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let nextIndex = index;
        if (event.key === 'ArrowLeft') nextIndex = (index - 1 + items.length) % items.length;
        if (event.key === 'ArrowRight') nextIndex = (index + 1) % items.length;
        if (event.key === 'Home') nextIndex = 0;
        if (event.key === 'End') nextIndex = items.length - 1;
        const next = items[nextIndex];
        next.focus();
        const page = next.getAttribute('aria-controls').replace('page-', '');
        navigate(page, page === 'events' ? state.eventStatus : 'all');
      });
    });
  }

  function bindEvents() {
    const on = (selector, event, handler) => $(selector)?.addEventListener(event, handler);
    on('#refresh-all', 'click', refreshAll);
    on('#page-check', 'click', runCheck);
    on('#retry-all', 'click', () => retryEvents());
    on('#settings-form', 'submit', saveSettings);
    on('#discard-settings', 'click', resetSettingsForm);
    on('#test-connection', 'click', testConnection);
    on('#close-import', 'click', closeImportDialog);
    on('#import-form', 'submit', submitImport);
    on('#import-sent', 'input', updateImportSubmit);
    on('#import-failed', 'input', updateImportSubmit);
    $$('[data-import-file]').forEach((input) => {
      input.addEventListener('change', () => readImportFile(input.dataset.importFile, input.files && input.files[0]));
    });
    $$('[data-clear-import]').forEach((button) => {
      button.addEventListener('click', () => {
        const type = button.dataset.clearImport;
        const textarea = $(`#import-${type}`);
        const file = $(`#import-${type}-file`);
        if (textarea) textarea.value = '';
        if (file) file.value = '';
        state.importFiles[type] = null;
        setImportFileState(type, '可粘贴或选择文件，每份最多 512 KiB，总请求小于 2 MiB。', false);
        updateImportSubmit();
      });
    });
    $$('[data-theme-choice]').forEach((button) => {
      button.addEventListener('click', () => applyTheme(button.dataset.themeChoice));
    });
    $$('[data-action="theme-cycle"]').forEach((button) => button.addEventListener('click', cycleTheme));
    $$('[data-setting-toggle]').forEach((button) => {
      button.addEventListener('click', () => toggleSetting(button, button.dataset.settingToggle));
    });
    $$('[data-status-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        const status = button.dataset.statusFilter;
        navigate('events', EVENT_STATUSES.includes(status) ? status : 'all');
      });
    });
    $$('[data-repo-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        state.repoFilter = REPO_FILTERS.includes(button.dataset.repoFilter) ? button.dataset.repoFilter : 'all';
        setFilterPressed('[data-repo-filter]', 'data-repo-filter', state.repoFilter);
        renderRepositories();
      });
    });
    $('#repo-search').addEventListener('input', (event) => {
      state.repoSearch = event.target.value;
      renderRepositories();
    });
    document.addEventListener('input', (event) => {
      if (event.target.closest('#settings-form') && !event.target.matches('#github-token')) markSettingsDirty();
      if (event.target.matches('#github-token') && event.target.value) markSettingsDirty();
    });
    document.addEventListener('click', (event) => {
      const action = event.target.closest('[data-action]');
      if (action) {
        const name = action.dataset.action;
        if (name === 'refresh') refreshAll();
        if (name === 'reload-repositories') loadRepositories();
        if (name === 'reload-events') loadEvents(state.eventStatus);
        if (name === 'reload-settings') loadSettings();
        if (name === 'go-settings') navigate('settings');
        if (name === 'import') openImportDialog();
      }
      const retry = event.target.closest('[data-retry-id]');
      if (retry) retryEvents(retry.dataset.retryId);
      const eventLink = event.target.closest('a[data-event-filter]');
      if (eventLink) {
        event.preventDefault();
        navigate('events', eventLink.dataset.eventFilter || 'all');
      }
    });
    on('#import-dialog', 'click', (event) => {
      if (event.target === $('#import-dialog')) closeImportDialog();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
        event.preventDefault();
        if (state.page === 'repositories') $('#repo-search').focus();
      }
    });
    window.addEventListener('online', () => {
      if (state.authenticated) refreshAll();
    });
  }

  async function init() {
    loadStoredTheme();
    bindNavigation();
    bindEvents();
    await checkSession();
  }

  init().catch(error => {
    $('#boot-screen').hidden = true;
    $('#app-shell').hidden = false;
    showGlobalError(redact(error.message || '管理页初始化失败'));
  });
})();
