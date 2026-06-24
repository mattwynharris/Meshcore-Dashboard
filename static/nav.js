/* nav.js — always renders the same 7-item nav on every page */
(function () {
  var NAV_ITEMS = [
    { id: 'home',     label: 'Dashboard', url: '/',         badge: false, cls: '' },
    { id: 'map',      label: 'Map',       url: '/map',      badge: false, cls: '' },
    { id: 'messages', label: 'Messages',  url: '/messages', badge: true,  cls: '' },
    { id: 'packets',  label: 'Packets',   url: '/packets',  badge: false, cls: '' },
    { id: 'contacts', label: 'Contacts',  url: '/contacts', badge: false, cls: '' },
    { id: 'logs',     label: 'Logs',      url: '/logs',     badge: false, cls: '' },
    { id: 'settings', label: '⚙',        url: '/settings', badge: false, cls: 'nav-settings' },
  ];

  function renderNav() {
    var group = document.querySelector('nav.nav-group');
    if (!group) return;
    var path = window.location.pathname;
    group.innerHTML = NAV_ITEMS.map(function (item) {
      var active = item.url === '/' ? path === '/' : path.startsWith(item.url);
      var cls = 'nav-btn' + (item.cls ? ' ' + item.cls : '') + (active ? ' nav-btn-active' : '');
      var inner = item.label;
      if (item.badge) inner += '<span id="msgsBadge" class="msgs-unread-dot" style="display:none"></span>';
      return '<a href="' + item.url + '" class="' + cls + '" title="' + item.label + '">' + inner + '</a>';
    }).join('');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderNav);
  } else {
    renderNav();
  }
}());
