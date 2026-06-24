/* ============================================================
   MeshCore Repeater Dashboard - Frontend Logic
   ============================================================ */

// --- Utility Functions ---

function batteryClass(mv) {
  if (mv <= 0) return '';
  if (mv >= 3800) return 'battery-good';
  if (mv >= 3500) return 'battery-mid';
  return 'battery-low';
}

function batteryColor(mv) {
  if (mv >= 3800) return '#22c55e';
  if (mv >= 3500) return '#eab308';
  return '#ef4444';
}

function batteryPercent(mv) {
  // Approximate LiPo percentage from millivolts
  // 4200mV = 100%, 3000mV = 0%
  if (mv <= 0) return 0;
  var pct = Math.round(((mv - 3000) / (4200 - 3000)) * 100);
  return Math.max(0, Math.min(100, pct));
}

function signalClass(rssi) {
  if (rssi === 0) return '';
  if (rssi > -90) return 'signal-good';
  if (rssi > -110) return 'signal-mid';
  return 'signal-bad';
}

function timeAgo(epoch) {
  if (!epoch || epoch === 0) return 'Never';
  var diff = Math.floor(Date.now() / 1000 - epoch);
  if (diff < 0) return 'Just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 2592000) return Math.floor(diff / 86400) + 'd ago';
  return Math.floor(diff / 2592000) + 'mo ago';
}

function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '--';
  var d = Math.floor(seconds / 86400);
  var h = Math.floor((seconds % 86400) / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}


// --- Card State ---

window._pingCooldowns  = window._pingCooldowns  || {};
window._pingTimers     = window._pingTimers     || {};
window._pingResults    = window._pingResults    || {};
window._advertCooldowns = window._advertCooldowns || {};
window._advertTimers    = window._advertTimers    || {};
window._advertResults   = window._advertResults   || {};
window._currentData   = window._currentData   || [];
window._cardOrder     = window._cardOrder     || [];


// --- Route chain helper ---

function buildRouteChain(r, prefixToName) {
  if (r.last_seen_epoch === 0) return null;
  // If SDK says Direct (hops=0) but we have a known route_path, trust the path
  var pathSegs = r.route_path ? r.route_path.replace(/\s/g, '').split('>').filter(Boolean) : [];
  var rawHops = r.hops < 0 ? 0 : r.hops;
  var effectiveHops = rawHops === 0 && pathSegs.length > 0 ? pathSegs.length : rawHops;
  if (effectiveHops === 0) return null;  // Genuinely direct — no path to show
  // Discard path if segment count far exceeds hop count (stale data)
  if (effectiveHops > 0 && pathSegs.length > effectiveHops + 1) pathSegs = [];

  var chain = [r.name];
  if (pathSegs.length > 0) {
    var si = 0;
    while (si < pathSegs.length) {
      var seg = pathSegs[si].toUpperCase();
      var name = null;
      // Try direct lookup first (handles 2, 4, 6-char segments)
      name = prefixToName[seg] || null;
      // Fallback: try first 2 chars (resolves longer segs via 1-byte prefix)
      if (!name) name = prefixToName[seg.substring(0, 2)] || null;
      if (!name) name = seg;
      if (name !== chain[chain.length - 1]) chain.push(name);
      si++;
    }
  } else {
    for (var i = 0; i < effectiveHops; i++) chain.push('?');
  }
  chain.push('You');
  return chain.join(' \u2192 ');
}


// --- Render Dashboard ---

// Alerts that the user has dismissed this session (by key)
var _dismissedAlerts = {};

function updateAlerts(data) {
  var bar = document.getElementById('alertsBar');
  var list = document.getElementById('alertsList');
  if (!bar || !list) return;

  var lowBatPct   = window._lowBatteryPercent || 20;
  var highTemp    = window._highTempAlert || 60;
  var highNoise   = window._highNoiseAlert || -90;
  var alerts = [];

  data.forEach(function(r) {
    if (!r.name) return;
    // Low battery
    if (r.battery_mv > 0 && batteryPercent(r.battery_mv) <= lowBatPct) {
      var key = 'bat_' + r.pubkey;
      if (!_dismissedAlerts[key]) {
        alerts.push({
          key: key,
          type: 'warn',
          icon: '🔋',
          text: r.name + ' — low battery (' + batteryPercent(r.battery_mv) + '%  ' + (r.battery_mv / 1000).toFixed(2) + 'V)'
        });
      }
    }
    // High temperature
    if (r.temperature_c && r.temperature_c >= highTemp) {
      var tkey = 'temp_' + r.pubkey;
      if (!_dismissedAlerts[tkey]) {
        alerts.push({
          key: tkey,
          type: 'danger',
          icon: '🌡️',
          text: r.name + ' — high temperature (' + r.temperature_c.toFixed(1) + '\u00b0C, threshold ' + highTemp + '\u00b0C)'
        });
      }
    }
    // High noise floor (closer to 0 = worse)
    if (r.noise_floor && r.noise_floor !== 0 && r.noise_floor >= highNoise) {
      var nkey = 'noise_' + r.pubkey;
      if (!_dismissedAlerts[nkey]) {
        alerts.push({
          key: nkey,
          type: 'warn',
          icon: '📡',
          text: r.name + ' — high noise floor (' + r.noise_floor + ' dBm, threshold ' + highNoise + ' dBm)'
        });
      }
    }
    // Clock drift
    if (r.clock_offset_s !== null && r.clock_offset_s !== undefined && Math.abs(r.clock_offset_s) >= 30) {
      var ckey = 'clock_' + r.pubkey;
      if (!_dismissedAlerts[ckey]) {
        var driftStr = (r.clock_offset_s > 0 ? '+' : '') + r.clock_offset_s.toFixed(0) + 's';
        alerts.push({
          key: ckey,
          type: 'warn',
          icon: '🕐',
          text: r.name + ' — clock drift (' + driftStr + ' from system time)'
        });
      }
    }
  });

  window._liveAlerts = alerts; // available for the alerts tile panel

  if (alerts.length === 0) {
    bar.style.display = 'none';
    list.innerHTML = '';
    return;
  }

  bar.style.display = '';
  list.innerHTML = '';
  alerts.forEach(function(a) {
    var div = document.createElement('div');
    div.className = 'alert-item alert-' + (a.type === 'danger' ? 'danger' : 'warn');
    div.innerHTML =
      '<span class="alert-icon">' + a.icon + '</span>' +
      '<span class="alert-text">' + escapeHtml(a.text) + '</span>' +
      '<button class="alert-dismiss" title="Dismiss">&times;</button>';
    div.querySelector('.alert-dismiss').addEventListener('click', function() {
      _dismissedAlerts[a.key] = true;
      div.remove();
      if (!list.children.length) bar.style.display = 'none';
    });
    list.appendChild(div);
  });
}

function renderRepeaters(rawData) {
  var grid = document.getElementById('repeaterGrid');
  if (!grid) return;

  if (!rawData || rawData.length === 0) {
    grid.innerHTML =
      '<div class="no-data">' +
        '<h2>Waiting for data...</h2>' +
        '<p>The poller is connecting to your companion device and requesting repeater status.</p>' +
      '</div>';
    return;
  }

  // Apply user-defined card order
  var data = rawData.slice();
  if (window._cardOrder && window._cardOrder.length > 0) {
    data.sort(function(a, b) {
      var ai = window._cardOrder.indexOf(a.pubkey);
      var bi = window._cardOrder.indexOf(b.pubkey);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
  }
  window._currentData = data;

  grid.innerHTML = '';

  // Build pubkey prefix -> name map (2, 4, and 6-char keys for 1/2/3-byte hop modes)
  var prefixToName = {};
  data.forEach(function(rep) {
    if (rep.pubkey) {
      var pk = rep.pubkey.toUpperCase();
      prefixToName[pk.substring(0, 2)] = rep.name;
      if (pk.length >= 4) prefixToName[pk.substring(0, 4)] = rep.name;
      if (pk.length >= 6) prefixToName[pk.substring(0, 6)] = rep.name;
    }
  });

  var lowBatPct = window._lowBatteryPercent || 20;
  var now = Date.now() / 1000;

  data.forEach(function(r, idx) {
    var bPct = batteryPercent(r.battery_mv);
    var bClass = batteryClass(r.battery_mv);
    var sClass = signalClass(r.rssi);
    // Status dot: green=poll ok, red=poll failed, grey=never polled
    var dotClass = r.poll_ok === true ? 'online' : (r.poll_ok === false ? 'offline' : 'unknown');
    var isLowBat = r.battery_mv > 0 && bPct <= lowBatPct;

    // Hops: "--" never polled, "Direct" for 0 intermediates, "N hop(s)" otherwise
    var hopsSeen = r.last_seen_epoch > 0;
    var _pathSegs = r.route_path ? r.route_path.replace(/\s/g, '').split('>').filter(Boolean) : [];
    var _rawHops = r.hops < 0 ? 0 : r.hops;
    var _effHops = _rawHops === 0 && _pathSegs.length > 0 ? _pathSegs.length : _rawHops;
    var hopsLabel = !hopsSeen ? '--' : (_effHops === 0 ? 'Direct' : _effHops + ' hop' + (_effHops !== 1 ? 's' : ''));
    var routeChain = buildRouteChain(r, prefixToName);

    // Ping button state
    var cooldown = window._pingCooldowns[r.pubkey] || 0;
    var pingRemaining = Math.max(0, Math.ceil(cooldown - now));
    var pingDisabled = pingRemaining > 0;
    var pingResult = window._pingResults[r.pubkey];
    var pingLabel = pingRemaining > 0 ? pingRemaining + 's' : 'Ping';
    var pingClass = 'card-ping-btn';
    if (pingRemaining > 0 && pingResult) {
      pingClass += pingResult.ok ? ' ping-ok' : ' ping-fail';
    }

    // Advert button state (per-card cooldown)
    var advertCooldown = window._advertCooldowns[r.pubkey] || 0;
    var advertRemaining = Math.max(0, Math.ceil(advertCooldown - now));
    var advertResult = window._advertResults[r.pubkey];
    var advertLabel = advertRemaining > 0 ? advertRemaining + 's' : 'Advert';
    var advertClass = 'card-advert-btn';
    if (advertRemaining > 0 && advertResult) {
      advertClass += advertResult.ok ? ' advert-ok' : ' advert-fail';
    }

    var card = document.createElement('div');
    card.className = 'card';
    card.setAttribute('data-pubkey', r.pubkey);

    var warningHtml = isLowBat
      ? '<div class="battery-warning">LOW BATTERY - ' + bPct + '%</div>'
      : '';

    // Offline: only when poll explicitly failed (not just stale timeout)
    var isOffline = r.poll_ok === false;

    card.innerHTML =
      warningHtml +
      '<div class="card-header">' +
        '<div>' +
          '<div class="card-name">' + escapeHtml(r.name) + '</div>' +
          '<div class="card-id">' + (r.pubkey_short || r.pubkey.substring(0, 12)) + '</div>' +
        '</div>' +
        '<span class="status-dot ' + dotClass + '"></span>' +
      '</div>' +
      '<div class="metrics">' +
        '<div class="metric">' +
          '<div class="metric-label">Battery</div>' +
          '<div class="metric-value ' + bClass + '">' +
            (r.battery_mv > 0 ? bPct : '--') +
            '<span class="metric-unit"> %</span>' +
          '</div>' +
          '<div class="metric-sub">' +
            (r.battery_voltage > 0 ? r.battery_voltage.toFixed(2) + ' V' : '--') +
          '</div>' +
          '<div class="bar-bg">' +
            '<div class="bar-fill" style="width:' + bPct + '%;background:' + batteryColor(r.battery_mv) + '"></div>' +
          '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">RSSI</div>' +
          '<div class="metric-value ' + sClass + '">' +
            (r.rssi !== 0 ? r.rssi : '--') +
            '<span class="metric-unit"> dBm</span>' +
          '</div>' +
          '<div class="metric-sub">Noise: ' + (r.noise_floor !== 0 ? r.noise_floor + ' dBm' : '--') + '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">SNR</div>' +
          '<div class="metric-value">' +
            (r.snr !== 0 ? r.snr.toFixed(1) : '--') +
            '<span class="metric-unit"> dB</span>' +
          '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">Temp</div>' +
          '<div class="metric-value">' +
            (r.temperature_c ? r.temperature_c.toFixed(1) : '--') +
            (r.temperature_c ? '<span class="metric-unit"> °C</span>' : '') +
          '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">Uptime</div>' +
          '<div class="metric-value" style="font-size:0.95rem;">' + formatUptime(r.uptime_seconds) + '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">Hops</div>' +
          '<div class="metric-value">' + hopsLabel + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="card-footer">' +
        '<div class="card-footer-left">' +
          '<div class="card-footer-seen">Last seen: ' + timeAgo(r.last_seen_epoch) + '</div>' +
          (routeChain ? '<div class="card-footer-route">' + escapeHtml(routeChain) + '</div>' : '') +
          (isOffline ? '<div class="card-offline-text">No response to last poll</div>' : '') +
          (r.fw_version ? '<div class="card-footer-fw">FW ' + escapeHtml(r.fw_version) + '</div>' : '') +
        '</div>' +
        '<button class="' + advertClass + '" data-action="advert"' + (advertRemaining > 0 ? ' disabled' : '') + '>' + advertLabel + '</button>' +
        '<button class="' + pingClass + '" data-action="ping"' + (pingDisabled ? ' disabled' : '') + '>' + pingLabel + '</button>' +
        '<button class="card-admin-btn" data-action="admin">Admin</button>' +
      '</div>';

    // Card click → history (ignore button clicks)
    card.addEventListener('click', function(e) {
      if (e.target.tagName === 'BUTTON') return;
      showHistory(r.pubkey, r.name);
    });

    card.querySelector('[data-action="advert"]').addEventListener('click', function(e) {
      e.stopPropagation();
      sendAdvert(r.pubkey, e.currentTarget);
    });

    card.querySelector('[data-action="ping"]').addEventListener('click', function(e) {
      e.stopPropagation();
      pingRepeater(r.pubkey, e.currentTarget);
    });

    card.querySelector('[data-action="admin"]').addEventListener('click', function(e) {
      e.stopPropagation();
      openAdminPopup(r.pubkey, r.name);
    });

    grid.appendChild(card);
  });

  updateAlerts(data);
}


// --- Advert ---
window._advertWatchers = window._advertWatchers || {};

function sendAdvert(pubkey, btn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.className = 'card-advert-btn';

  var clickedAt = Date.now() / 1000;
  window._advertCooldowns[pubkey] = clickedAt + 30;
  delete window._advertResults[pubkey];
  _startAdvertCountdown(pubkey);

  fetch('/api/advert/' + encodeURIComponent(pubkey), { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(result) {
      if (!result.ok) {
        window._advertResults[pubkey] = result;
        var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="advert"]');
        if (b) b.className = 'card-advert-btn advert-fail';
      } else {
        // Command sent — now watch packets feed for the advert coming back over RF
        _watchForAdvert(pubkey, clickedAt);
      }
    })
    .catch(function() {
      window._advertResults[pubkey] = { ok: false };
      var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="advert"]');
      if (b) b.className = 'card-advert-btn advert-fail';
    });
}

function _watchForAdvert(pubkey, since) {
  if (window._advertWatchers[pubkey]) clearInterval(window._advertWatchers[pubkey]);
  var deadline = since + 30; // give up after 30s
  var pkUpper = pubkey.toUpperCase();
  window._advertWatchers[pubkey] = setInterval(function() {
    if (Date.now() / 1000 > deadline) {
      clearInterval(window._advertWatchers[pubkey]);
      delete window._advertWatchers[pubkey];
      return;
    }
    fetch('/api/packets?limit=30')
      .then(function(r) { return r.json(); })
      .then(function(packets) {
        for (var i = 0; i < packets.length; i++) {
          var p = packets[i];
          if (p.ts < since) continue;  // older than when we clicked
          if (p.pkt_type !== 'Advert') continue;
          var apk = p.advert && p.advert.pubkey ? p.advert.pubkey.toUpperCase() : '';
          if (apk && (apk === pkUpper || apk.startsWith(pkUpper) || pkUpper.startsWith(apk))) {
            // Advert received from this repeater
            clearInterval(window._advertWatchers[pubkey]);
            delete window._advertWatchers[pubkey];
            window._advertResults[pubkey] = { ok: true, seen: true };
            // Snap cooldown to 10s remaining so button clears soon
            window._advertCooldowns[pubkey] = Date.now() / 1000 + 10;
            var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="advert"]');
            if (b) { b.className = 'card-advert-btn advert-ok'; b.textContent = '✓ Seen'; }
            return;
          }
        }
      }).catch(function() {});
  }, 2000);
}

function _startAdvertCountdown(pubkey) {
  if (window._advertTimers[pubkey]) clearInterval(window._advertTimers[pubkey]);
  function tick() {
    var remaining = Math.max(0, Math.ceil(window._advertCooldowns[pubkey] - Date.now() / 1000));
    var btn = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="advert"]');
    if (remaining <= 0) {
      clearInterval(window._advertTimers[pubkey]);
      delete window._advertTimers[pubkey];
      delete window._advertCooldowns[pubkey];
      delete window._advertResults[pubkey];
      if (btn) { btn.textContent = 'Advert'; btn.className = 'card-advert-btn'; btn.disabled = false; }
      return;
    }
    // Only update text if not already showing "✓ Seen"
    if (btn && btn.textContent !== '\u2713 Seen') btn.textContent = remaining + 's';
  }
  tick();
  window._advertTimers[pubkey] = setInterval(tick, 1000);
}


// --- Ping ---

function pingRepeater(pubkey, btn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.className = 'card-ping-btn';

  // Start countdown immediately so the button counts down while waiting
  window._pingCooldowns[pubkey] = Date.now() / 1000 + 30;
  delete window._pingResults[pubkey];
  _startPingCountdown(pubkey);

  fetch('/api/ping/' + encodeURIComponent(pubkey), { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(result) {
      window._pingResults[pubkey] = result;
      // Apply result colour; countdown continues
      var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="ping"]');
      if (b) b.className = 'card-ping-btn ' + (result.ok ? 'ping-ok' : 'ping-fail');
    })
    .catch(function() {
      window._pingResults[pubkey] = { ok: false };
      var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="ping"]');
      if (b) b.className = 'card-ping-btn ping-fail';
    });
}

function _startPingCountdown(pubkey) {
  if (window._pingTimers[pubkey]) clearInterval(window._pingTimers[pubkey]);
  function tick() {
    var remaining = Math.max(0, Math.ceil(window._pingCooldowns[pubkey] - Date.now() / 1000));
    var btn = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="ping"]');
    if (remaining <= 0) {
      clearInterval(window._pingTimers[pubkey]);
      delete window._pingTimers[pubkey];
      delete window._pingCooldowns[pubkey];
      delete window._pingResults[pubkey];
      if (btn) { btn.textContent = 'Ping'; btn.className = 'card-ping-btn'; btn.disabled = false; }
      return;
    }
    if (btn) btn.textContent = remaining + 's';
  }
  tick();
  window._pingTimers[pubkey] = setInterval(tick, 1000);
}

// --- Remote Admin ---
var _adminPubkey = '';
var _adminHistory = (function() {
  try { return JSON.parse(localStorage.getItem('meshcore_admin_history')) || []; } catch(e) { return []; }
})();
var _adminHistoryIdx = -1;

var _ADMIN_CMDS = [
  { group: 'Common', cmds: [
    { label: 'advert',              cmd: 'advert',                     tip: 'Broadcast node advertisement' },
    { label: 'neighbors',           cmd: 'neighbors',                  tip: 'Show 8 most recently discovered nodes' },
    { label: 'reboot',              cmd: 'reboot',                     tip: 'Restart the node' },
    { label: 'ver',                 cmd: 'ver',                        tip: 'Get firmware version' },
    { label: 'clock',               cmd: 'clock',                      tip: 'Show current UTC time' },
    { label: 'clock sync',          cmd: 'clock sync',                 tip: 'Sync time with remote device' },
    { label: 'stats-core',          cmd: 'stats-core',                 tip: 'Battery, uptime, queue, debug flags' },
    { label: 'stats-radio',         cmd: 'stats-radio',                tip: 'Noise floor, RSSI/SNR, airtime, errors' },
    { label: 'stats-packets',       cmd: 'stats-packets',              tip: 'Show received/transmitted counts' },
    { label: 'clear stats',         cmd: 'clear stats',                tip: 'Reset all statistics counters' },
  ]},
  { group: 'Radio', cmds: [
    { label: 'get radio',           cmd: 'get radio',                  tip: 'View freq, bw, sf, cr' },
    { label: 'set radio',           cmd: 'set radio ',                 tip: 'set radio <freq>,<bw>,<sf>,<cr>' },
    { label: 'get tx',              cmd: 'get tx',                     tip: 'View TX power (dBm)' },
    { label: 'set tx',              cmd: 'set tx ',                    tip: 'Set TX power 1-22 dBm' },
    { label: 'get freq',            cmd: 'get freq',                   tip: 'View frequency (MHz)' },
    { label: 'set freq',            cmd: 'set freq ',                  tip: 'Set frequency in MHz' },
    { label: 'tempradio',           cmd: 'tempradio ',                 tip: 'Temp radio change: <f>,<bw>,<sf>,<cr>,<mins>' },
  ]},
  { group: 'Config', cmds: [
    { label: 'get name',            cmd: 'get name',                   tip: 'View node name' },
    { label: 'set name',            cmd: 'set name ',                  tip: 'Set node name' },
    { label: 'get lat',             cmd: 'get lat',                    tip: 'View latitude' },
    { label: 'set lat',             cmd: 'set lat ',                   tip: 'Set latitude in degrees' },
    { label: 'get lon',             cmd: 'get lon',                    tip: 'View longitude' },
    { label: 'set lon',             cmd: 'set lon ',                   tip: 'Set longitude in degrees' },
    { label: 'get role',            cmd: 'get role',                   tip: 'Show configured node type' },
    { label: 'get repeat',          cmd: 'get repeat',                 tip: 'View packet forwarding state' },
    { label: 'set repeat',          cmd: 'set repeat ',                tip: 'Enable/disable packet forwarding' },
    { label: 'get flood.advert.interval', cmd: 'get flood.advert.interval', tip: 'View advert broadcast frequency' },
    { label: 'set flood.advert.interval', cmd: 'set flood.advert.interval ', tip: 'Set advert interval in hours (3-168)' },
    { label: 'get path.hash.mode', cmd: 'get path.hash.mode',          tip: 'View path hash size mode (0=1byte 1=2byte 2=3byte)' },
    { label: 'set path.hash.mode', cmd: 'set path.hash.mode ',         tip: 'Set path hash mode' },
    { label: 'password',            cmd: 'password ',                  tip: 'Change admin password' },
    { label: 'get public.key',      cmd: 'get public.key',             tip: 'Display node public key' },
    { label: 'powersaving',         cmd: 'powersaving',                tip: 'View/toggle power saving mode' },
  ]},
];

function openAdminPopup(pubkey, name) {
  _adminPubkey = pubkey;
  _adminHistoryIdx = -1;
  document.getElementById('adminPopupTitle').textContent = 'Admin — ' + name;
  var out = document.getElementById('adminOutput');
  out.innerHTML = '';
  adminAppendOutput('Logging in to ' + name + '…', 'info');

  document.getElementById('adminCmdInput').value = '';
  document.getElementById('adminCmdInput').disabled = true;
  document.getElementById('adminSendBtn').disabled = true;

  // Build help chips grouped
  var panel = document.getElementById('adminHelpPanel');
  panel.style.display = 'none';
  var chips = document.getElementById('adminHelpChips');
  chips.innerHTML = '';
  _ADMIN_CMDS.forEach(function(group) {
    var grpLabel = document.createElement('div');
    grpLabel.className = 'admin-help-label';
    grpLabel.style.marginTop = '0.4rem';
    grpLabel.textContent = group.group;
    chips.appendChild(grpLabel);
    group.cmds.forEach(function(c) {
      var chip = document.createElement('span');
      chip.className = 'admin-cmd-chip';
      chip.textContent = c.label;
      chip.title = c.tip;
      chip.addEventListener('click', function() {
        document.getElementById('adminCmdInput').value = c.cmd;
        document.getElementById('adminCmdInput').focus();
        panel.style.display = 'none';
      });
      chips.appendChild(chip);
    });
  });

  document.getElementById('adminOverlay').style.display = 'block';
  document.getElementById('adminPopup').style.display = 'block';

  fetch('/api/login/' + encodeURIComponent(pubkey), { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) {
        var msg = 'Logged in to ' + name + ' ✔';
        if (d.ver) msg += '  (' + d.ver + ')';
        adminAppendOutput(msg, 'info');
        document.getElementById('adminCmdInput').disabled = false;
        document.getElementById('adminSendBtn').disabled = false;
        document.getElementById('adminCmdInput').focus();
      } else {
        adminAppendOutput('Login failed: ' + (d.error || 'Unknown error'), 'err');
        document.getElementById('adminCmdInput').disabled = false;
        document.getElementById('adminSendBtn').disabled = false;
      }
    })
    .catch(function() {
      adminAppendOutput('Login failed: network error', 'err');
      document.getElementById('adminCmdInput').disabled = false;
      document.getElementById('adminSendBtn').disabled = false;
    });
}

function adminRelogin() {
  if (!_adminPubkey) return;
  var btn = document.getElementById('adminReloginBtn');
  btn.disabled = true;
  adminAppendOutput('Re-logging in…', 'info');
  fetch('/api/login/' + encodeURIComponent(_adminPubkey), { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btn.disabled = false;
      if (d.ok) {
        var msg = 'Re-login OK ✔';
        if (d.ver) msg += '  (' + d.ver + ')';
        adminAppendOutput(msg, 'info');
      } else {
        adminAppendOutput('Re-login failed: ' + (d.error || 'Unknown error'), 'err');
      }
    })
    .catch(function() {
      btn.disabled = false;
      adminAppendOutput('Re-login failed: network error', 'err');
    });
}

function closeAdminPopup() {
  document.getElementById('adminOverlay').style.display = 'none';
  document.getElementById('adminPopup').style.display = 'none';
  _adminPubkey = '';
}

function toggleAdminHelp() {
  var panel = document.getElementById('adminHelpPanel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function adminAppendOutput(text, cls) {
  var out = document.getElementById('adminOutput');
  var line = document.createElement('div');
  line.className = 'admin-output-line ' + (cls || '');
  line.textContent = text;
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
}

function adminSendCmd() {
  var input = document.getElementById('adminCmdInput');
  var cmd = input.value.trim();
  if (!cmd || !_adminPubkey) return;
  var btn = document.getElementById('adminSendBtn');
  btn.disabled = true;
  // Add to history
  if (_adminHistory[0] !== cmd) _adminHistory.unshift(cmd);
  if (_adminHistory.length > 50) _adminHistory.pop();
  _adminHistoryIdx = -1;
  try { localStorage.setItem('meshcore_admin_history', JSON.stringify(_adminHistory)); } catch(e) {}
  adminAppendOutput('❯ ' + cmd, 'sent');
  input.value = '';

  fetch('/api/cmd/' + encodeURIComponent(_adminPubkey), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cmd: cmd })
  })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btn.disabled = false;
      if (d.ok) {
        if (d.response) {
          d.response.split('\n').forEach(function(line) {
            if (line.trim()) adminAppendOutput(line, 'recv');
          });
          // If this was a 'neighbors' command, cache the result for the map
          if (cmd === 'neighbors') {
            var neighbors = [];
            d.response.trim().split('\n').forEach(function(line) {
              var parts = line.trim().split(':');
              if (parts.length >= 3) {
                try {
                  neighbors.push({
                    pubkey: parts[0].toUpperCase(),
                    seconds_ago: parseInt(parts[1]),
                    snr_db: Math.round(parseInt(parts[2]) / 4.0 * 10) / 10
                  });
                } catch(e) {}
              }
            });
            if (neighbors.length > 0) {
              try {
                localStorage.setItem('meshcore_rf_neighbors_' + _adminPubkey, JSON.stringify({
                  neighbors: neighbors,
                  updatedAt: Date.now()
                }));
              } catch(e) {}
            }
          }
        } else {
          adminAppendOutput('Sent ✓', 'info');
        }
      } else {
        var errMsg = d.error || 'Failed';
        adminAppendOutput('Error: ' + errMsg, 'err');
        if (errMsg.toLowerCase().indexOf('timed out') !== -1) {
          adminAppendOutput('(No response — repeater may be out of range or session expired. Use Re-login to reconnect.)', 'info');
        }
      }
      input.focus();
    })
    .catch(function() {
      btn.disabled = false;
      adminAppendOutput('Network error', 'err');
      input.focus();
    });
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { closeAdminPopup(); return; }
  if (document.activeElement && document.activeElement.id !== 'adminCmdInput') return;
  if (e.key === 'Enter') { adminSendCmd(); return; }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (_adminHistory.length === 0) return;
    _adminHistoryIdx = Math.min(_adminHistoryIdx + 1, _adminHistory.length - 1);
    document.getElementById('adminCmdInput').value = _adminHistory[_adminHistoryIdx];
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _adminHistoryIdx = Math.max(_adminHistoryIdx - 1, -1);
    document.getElementById('adminCmdInput').value = _adminHistoryIdx >= 0 ? _adminHistory[_adminHistoryIdx] : '';
  }
});

function escapeHtml(str) {
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}


// --- Server-Sent Events ---

var evtSource = null;

function connectSSE() {
  if (evtSource) {
    evtSource.close();
  }

  evtSource = new EventSource('/api/stream');

  evtSource.addEventListener('update', function(e) {
    try {
      var data = JSON.parse(e.data);
      renderRepeaters(data);
      _updateLiveStats(data);
      refreshNetStats();
      document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
      document.getElementById('connDot').className = 'status-dot online';
      document.getElementById('connText').textContent = 'Connected';
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  });

  evtSource.onerror = function() {
    document.getElementById('connDot').className = 'status-dot offline';
    document.getElementById('connText').textContent = 'Disconnected - retrying...';
  };
}


// --- Network Stats Row ---

var _netNodes = [];
var _newNodes = [];
var _liveAlertCount = 0;
var _liveAvgRssi = null;

function _timeAgo(ts) {
  if (!ts) return '—';
  var s = Math.round(Date.now() / 1000 - ts);
  if (s < 60)   return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}

// Pool of all available dashboard tiles. Users pick exactly 6 to display.
var _TILE_DEFS = [
  { id: 'packets',
    label: 'RF Packets (1h)', color: 'nst-blue',
    stat: function(d) { return d.packets_1h != null ? d.packets_1h : '—'; },
    onclick: "window.location='/packets'" },
  { id: 'nodes',
    label: 'Total Nodes', color: 'nst-orange',
    stat: function(d) { return d.nodes_total != null ? d.nodes_total : '—'; },
    onclick: "showNodePanel(event)", domId: 'nsTileNodes' },
  { id: 'nodes_24h',
    label: 'Active Nodes (24h)', color: 'nst-orange',
    stat: function(d) { return d.nodes_24h != null ? d.nodes_24h : '—'; },
    onclick: "showNodePanel(event)" },
  { id: 'routes',
    label: 'Known Routes', color: 'nst-purple',
    stat: function(d) { return d.routes_known != null ? d.routes_known : '—'; },
    onclick: "window.location='/contacts'" },
  { id: 'repeaters',
    label: 'Repeaters Online', color: 'nst-green',
    stat: function(d) { return d.repeaters_total ? d.repeaters_online + '/' + d.repeaters_total : (d.repeaters_online != null ? d.repeaters_online : '—'); },
    onclick: "window.location='/map'" },
  { id: 'repeaters_total',
    label: 'Total Repeaters', color: 'nst-green',
    stat: function(d) { return d.repeaters_total != null ? d.repeaters_total : '—'; },
    onclick: "window.location='/map'" },
  { id: 'new_nodes',
    label: 'New Nodes (24h)', color: 'nst-cyan',
    special: true,
    onclick: "showNewNodePanel(event)", domId: 'nsTileNew' },
  { id: 'messages',
    label: 'Messages (24h)', color: 'nst-rose',
    stat: function(d) { return d.messages_24h != null ? d.messages_24h : '—'; },
    onclick: "window.location='/messages'" },
  { id: 'alerts',
    label: 'Active Alerts', color: 'nst-yellow',
    live: true,
    onclick: "showAlertsPanel(event)" },
  { id: 'avg_rssi',
    label: 'Avg RSSI', color: 'nst-purple',
    live: true,
    onclick: "window.location='/map'" },
];

var _TILE_DEFAULT = ['packets', 'nodes', 'routes', 'repeaters', 'new_nodes', 'messages'];

function _getTilesCfg() {
  try {
    var raw = localStorage.getItem('meshcore_tiles_v1');
    if (raw) { var p = JSON.parse(raw); if (p && p.length) return p; }
  } catch(e) {}
  return null;
}

function _saveTilesCfg(ids) {
  localStorage.setItem('meshcore_tiles_v1', JSON.stringify(ids));
}

function buildStatsTiles() {
  var row = document.getElementById('netStatsRow');
  if (!row) return;
  var cfg = _getTilesCfg() || _TILE_DEFAULT.slice();
  // Always show exactly 6 — pad with defaults if needed, trim if over
  _TILE_DEFAULT.forEach(function(id) {
    if (cfg.length < 6 && cfg.indexOf(id) === -1) cfg.push(id);
  });
  cfg = cfg.slice(0, 6);
  row.innerHTML = cfg.map(function(id) {
    var def = _TILE_DEFS.filter(function(x) { return x.id === id; })[0];
    if (!def) return '';
    var oc = def.onclick ? ' onclick="' + def.onclick + '"' : '';
    if (def.special) {
      return '<div class="net-stat-tile ' + def.color + '" id="nsTileNew"' + oc + ' title="' + def.label + '">'
        + '<div class="nst-label">' + def.label + ' <span id="nsNewCount" class="nst-count-badge"></span></div>'
        + '<div class="nst-new-list" id="nsNewList"><span class="nst-empty-hint">None yet</span></div>'
        + '</div>';
    }
    var domId = def.domId ? ' id="' + def.domId + '"' : '';
    return '<div class="net-stat-tile ' + def.color + '"' + domId + oc + ' title="' + def.label + '">'
      + '<div class="nst-label">' + def.label + '</div>'
      + '<div class="nst-value" id="nsStat_' + id + '">—</div>'
      + '</div>';
  }).join('');
}

// Called from SSE handler so live tiles (Alerts, Avg RSSI) update without a fetch.
function _updateLiveStats(data) {
  if (!Array.isArray(data)) return;
  var alerts = 0;
  var rssiSum = 0, rssiCount = 0;
  data.forEach(function(r) {
    if (r.poll_ok === false) alerts++;
    if (r.battery_mv > 0 && r.battery_mv < 3500) alerts++;
    if (r.clock_error_ms && Math.abs(r.clock_error_ms) > 30000) alerts++;
    if (r.rssi != null) { rssiSum += r.rssi; rssiCount++; }
  });
  _liveAlertCount = alerts;
  _liveAvgRssi = rssiCount > 0 ? Math.round(rssiSum / rssiCount) : null;
  var alertEl = document.getElementById('nsStat_alerts');
  if (alertEl) alertEl.textContent = alerts;
  var rssiEl = document.getElementById('nsStat_avg_rssi');
  if (rssiEl) rssiEl.textContent = _liveAvgRssi != null ? _liveAvgRssi + ' dBm' : '—';
}

function refreshNetStats() {
  fetch('/api/network-stats')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var cfg = _getTilesCfg() || _TILE_DEFAULT.slice();
      cfg.forEach(function(id) {
        var def = _TILE_DEFS.filter(function(x) { return x.id === id; })[0];
        if (!def || def.special || def.live) return;
        var el = document.getElementById('nsStat_' + id);
        if (el) el.textContent = def.stat(d);
      });

      _netNodes = d.nodes_list || [];
      _newNodes = d.new_nodes || [];

      var countBadge = document.getElementById('nsNewCount');
      if (countBadge) countBadge.textContent = _newNodes.length ? '· ' + _newNodes.length : '';
      var listEl = document.getElementById('nsNewList');
      if (listEl) {
        if (_newNodes.length === 0) {
          listEl.innerHTML = '<span class="nst-empty-hint">None yet</span>';
        } else {
          listEl.innerHTML = _newNodes.slice(0, 3).map(function(n) {
            return '<div class="nst-new-item">'
              + '<span class="nst-new-name">' + escapeHtml(n.name || n.pubkey || '?') + '</span>'
              + '<span class="nst-new-time">' + _timeAgo(n.first_seen) + '</span>'
              + '</div>';
          }).join('');
        }
      }
    })
    .catch(function() {});
}

function showNewNodePanel(e) {
  if (e) e.stopPropagation();
  var panel = document.getElementById('newNodePanel');
  var body  = document.getElementById('newNodePanelBody');
  if (!panel || !body) return;

  if (!_newNodes || _newNodes.length === 0) {
    body.innerHTML = '<div class="np-empty">No new nodes discovered in the last 24 hours.</div>';
  } else {
    body.innerHTML = _newNodes.map(function(n) {
      var name = n.name || n.pubkey || '?';
      var mapUrl = (n.lat && n.lon)
        ? '/map?lat=' + n.lat + '&lon=' + n.lon + '&name=' + encodeURIComponent(name)
        : '/map';
      var mapLink = '<a href="' + mapUrl + '" class="np-map-btn">Map</a>';
      var locStr = (n.lat && n.lon)
        ? '<span class="np-loc">' + n.lat.toFixed(4) + ', ' + n.lon.toFixed(4) + '</span>'
        : '';
      return '<div class="np-node np-node-detail">'
        + '<div class="np-node-row">'
        +   '<span class="np-name">' + escapeHtml(name) + '</span>'
        +   mapLink
        + '</div>'
        + '<div class="np-node-meta">'
        +   '<span class="np-meta-item">First seen: <b>' + _timeAgo(n.first_seen) + '</b></span>'
        +   '<span class="np-meta-item">Last seen: <b>' + _timeAgo(n.last_seen) + '</b></span>'
        +   (locStr ? '<span class="np-meta-item">' + locStr + '</span>' : '')
        + '</div>'
        + '</div>';
    }).join('');
  }

  panel.style.display = '';
  setTimeout(function() { document.addEventListener('click', _closeNewPanelOutside); }, 0);
}

function closeNewNodePanel() {
  var panel = document.getElementById('newNodePanel');
  if (panel) panel.style.display = 'none';
  document.removeEventListener('click', _closeNewPanelOutside);
}

function _closeNewPanelOutside(e) {
  var panel = document.getElementById('newNodePanel');
  var tile  = document.getElementById('nsTileNew');
  if (panel && !panel.contains(e.target) && tile && !tile.contains(e.target)) {
    closeNewNodePanel();
  }
}

function showAlertsPanel(e) {
  if (e) e.stopPropagation();
  var panel = document.getElementById('alertsDetailPanel');
  var body  = document.getElementById('alertsDetailBody');
  if (!panel || !body) return;
  var alerts = window._liveAlerts || [];
  if (alerts.length === 0) {
    body.innerHTML = '<div class="np-empty">No active alerts</div>';
  } else {
    body.innerHTML = alerts.map(function(a) {
      return '<div class="alert-item alert-' + (a.type === 'danger' ? 'danger' : 'warn') + '" style="margin-bottom:0.35rem">'
        + '<span class="alert-icon">' + a.icon + '</span>'
        + '<span class="alert-text">' + escapeHtml(a.text) + '</span>'
        + '</div>';
    }).join('');
  }
  panel.style.display = '';
  setTimeout(function() { document.addEventListener('click', _closeAlertsPanelOutside); }, 0);
}

function closeAlertsPanel() {
  var panel = document.getElementById('alertsDetailPanel');
  if (panel) panel.style.display = 'none';
  document.removeEventListener('click', _closeAlertsPanelOutside);
}

function _closeAlertsPanelOutside(e) {
  var panel = document.getElementById('alertsDetailPanel');
  if (panel && !panel.contains(e.target)) closeAlertsPanel();
}

function showNodePanel(e) {
  if (e) e.stopPropagation();
  var panel = document.getElementById('nodePanel');
  var body  = document.getElementById('nodePanelBody');
  if (!panel || !body) return;

  if (!_netNodes || _netNodes.length === 0) {
    body.innerHTML = '<div class="np-empty">No nodes discovered in last 24 hours.</div>';
  } else {
    var now = Date.now() / 1000;
    body.innerHTML = _netNodes.map(function(n) {
      var ago = Math.round((now - n.last_seen) / 60);
      var agoStr = ago < 60 ? ago + 'm ago' : Math.round(ago / 60) + 'h ago';
      var name = n.name || n.pubkey || '?';
      var mapUrl = (n.lat && n.lon)
        ? '/map?lat=' + n.lat + '&lon=' + n.lon + '&name=' + encodeURIComponent(name)
        : '/map';
      var mapLink = '<a href="' + mapUrl + '" class="np-map-btn">Map</a>';
      return '<div class="np-node">'
        + '<span class="np-name">' + escapeHtml(name) + '</span>'
        + '<span class="np-ago">' + agoStr + '</span>'
        + mapLink
        + '</div>';
    }).join('');
  }

  panel.style.display = '';
  setTimeout(function() {
    document.addEventListener('click', _closePanelOutside);
  }, 0);
}

function closeNodePanel() {
  var panel = document.getElementById('nodePanel');
  if (panel) panel.style.display = 'none';
  document.removeEventListener('click', _closePanelOutside);
}

function _closePanelOutside(e) {
  var panel = document.getElementById('nodePanel');
  var tile  = document.getElementById('nsTileNodes');
  if (panel && !panel.contains(e.target) && tile && !tile.contains(e.target)) {
    closeNodePanel();
  }
}

// --- History Modal ---

var historyChart = null;

function buildTimelineHtml(pollData, graphHours) {
  if (!pollData || pollData.length === 0) return '';
  var now = Date.now() / 1000;
  var spanStart = now - (graphHours * 3600);
  var spanEnd = now;
  var total = spanEnd - spanStart;

  // Build segments: [{start, end, online}]
  var segments = [];
  for (var i = 0; i < pollData.length; i++) {
    var p = pollData[i];
    var segStart = i === 0 ? spanStart : pollData[i - 1].ts;
    var segEnd = p.ts;
    // Gap between polls > 2x the poll interval treated as unknown/offline
    segments.push({ start: segStart, end: segEnd, online: p.online });
  }
  // Tail: from last poll to now
  if (pollData.length > 0) {
    var lastTs = pollData[pollData.length - 1].ts;
    var lastOnline = pollData[pollData.length - 1].online;
    if (now - lastTs < 600) {
      segments.push({ start: lastTs, end: now, online: lastOnline });
    } else {
      segments.push({ start: lastTs, end: now, online: false });
    }
  }

  var bars = '';
  segments.forEach(function(seg) {
    var pct = Math.max(0, Math.min(100, ((seg.end - seg.start) / total) * 100));
    var color = seg.online ? '#22c55e' : '#ef4444';
    var ts1 = new Date(seg.start * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    var ts2 = new Date(seg.end * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    bars += '<div style="display:inline-block;width:' + pct + '%;height:100%;background:' + color +
            ';opacity:0.85;" title="' + (seg.online ? 'Online' : 'Offline') + ' ' + ts1 + '\u2013' + ts2 + '"></div>';
  });

  var onlineCount = pollData.filter(function(p) { return p.online; }).length;
  var uptimePct = pollData.length > 0 ? Math.round((onlineCount / pollData.length) * 100) : 0;
  var uptimeColor = uptimePct >= 90 ? '#22c55e' : uptimePct >= 70 ? '#eab308' : '#ef4444';

  return '<div style="padding:0 0 0.75rem;">' +
    '<div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.3rem;">' +
    '<span style="font-size:0.75rem;color:#64748b;">Uptime</span>' +
    '<span style="font-size:0.75rem;font-weight:600;color:' + uptimeColor + '">' + uptimePct + '%</span>' +
    '</div>' +
    '<div style="width:100%;height:12px;background:#1e293b;border-radius:6px;overflow:hidden;position:relative;">' +
    bars +
    '</div>' +
    '<div style="display:flex;justify-content:space-between;font-size:0.65rem;color:#475569;margin-top:0.2rem;">' +
    '<span>' + new Date(spanStart * 1000).toLocaleString([], { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }) + '</span>' +
    '<span>Now</span>' +
    '</div>' +
    '</div>';
}

function fmtChartLabel(ts, spanHours) {
  var d = new Date(ts * 1000);
  var hh = d.getHours().toString().padStart(2, '0');
  var mm = d.getMinutes().toString().padStart(2, '0');
  var time = hh + ':' + mm;
  if (spanHours > 20) {
    var day = d.getDate().toString().padStart(2, '0');
    var mon = (d.getMonth() + 1).toString().padStart(2, '0');
    return day + '/' + mon + ' ' + time;
  }
  return time;
}

function showHistory(pubkey, name) {
  window._historyPubkey = pubkey;
  window._historyName   = name;

  var modal = document.getElementById('historyModal');
  var title = document.getElementById('historyTitle');
  if (!modal || !title) return;

  // Sync hours dropdown to settings value on first open
  var hoursEl = document.getElementById('historyHoursSelect');
  var graphHours = hoursEl ? parseInt(hoursEl.value) : 24;

  var hoursLabel = graphHours >= 168 ? '7 days' : graphHours >= 72 ? '3 days' : graphHours >= 48 ? '48h' : '24h';
  title.textContent = name + ' \u2014 ' + hoursLabel + ' history';
  modal.classList.add('visible');

  // Look up current repeater status from dashboard data
  var repeater = null;
  if (window._currentData) {
    window._currentData.forEach(function(r) { if (r.pubkey === pubkey) repeater = r; });
  }

  // Build info panel from current repeater data
  var infoHtml = '';
  if (repeater) {
    infoHtml = '<div style="display:flex;gap:1.2rem;flex-wrap:wrap;padding:0 0 0.75rem;font-size:0.82rem;color:#94a3b8;">';
    if (repeater.battery_mv && repeater.battery_mv > 0) {
      var pct = batteryPercent(repeater.battery_mv);
      var bv = repeater.battery_v && repeater.battery_v > 0 ? repeater.battery_v.toFixed(2) : (repeater.battery_mv / 1000).toFixed(2);
      var bColor = repeater.battery_mv >= 3800 ? '#22c55e' : repeater.battery_mv >= 3500 ? '#eab308' : '#ef4444';
      infoHtml += '<span>Battery: <strong style="color:' + bColor + '">' + pct + '% (' + bv + 'V)</strong></span>';
    }
    if (repeater.hops !== undefined) {
      var hopsVal = repeater.hops < 0 ? 0 : repeater.hops;
      var hopStr = hopsVal === 0 ? 'Direct' : hopsVal + (hopsVal === 1 ? ' hop' : ' hops');
      infoHtml += '<span>Hops: <strong style="color:#e2e8f0">' + hopStr + '</strong></span>';
    }
    if (repeater.rssi) {
      infoHtml += '<span>RSSI: <strong style="color:#38bdf8">' + repeater.rssi + ' dBm</strong></span>';
    }
    if (repeater.last_seen_epoch && repeater.last_seen_epoch > 0) {
      infoHtml += '<span>Last seen: <strong style="color:#e2e8f0">' + timeAgo(repeater.last_seen_epoch) + '</strong></span>';
    }
    infoHtml += '</div>';
  }

  Promise.all([
    fetch('/api/history/' + encodeURIComponent(pubkey) + '?hours=' + graphHours).then(function(r) { return r.json(); }),
    fetch('/api/poll-history/' + encodeURIComponent(pubkey) + '?hours=' + graphHours).then(function(r) { return r.json(); }).catch(function() { return []; })
  ]).then(function(results) {
    var data = results[0];
    var pollData = results[1] || [];
    var canvasWrap = document.getElementById('historyChartBody');
    if (!data || data.length === 0) {
      if (historyChart) { historyChart.destroy(); historyChart = null; }
      canvasWrap.innerHTML =
        infoHtml +
        buildTimelineHtml(pollData, graphHours) +
        '<p style="text-align:center;color:#64748b;padding:2rem;">No history data yet. Data will appear after a few poll cycles.</p>' +
        '<canvas id="historyChart" height="250"></canvas>';
      return;
    }

    var spanHours = data.length > 1 ? (data[data.length - 1].ts - data[0].ts) / 3600 : graphHours;
    var labels = data.map(function(d) { return fmtChartLabel(d.ts, spanHours); });

    if (historyChart) { historyChart.destroy(); historyChart = null; }
    canvasWrap.innerHTML = infoHtml + buildTimelineHtml(pollData, graphHours) + '<canvas id="historyChart" height="250"></canvas>';
    var ctx = document.getElementById('historyChart').getContext('2d');

    // Pre-compute all series
    var battPctData  = data.map(function(d) { return batteryPercent(d.battery_mv); });
    var battMvData   = data.map(function(d) { return d.battery_mv; });
    var battVData    = data.map(function(d) { return d.battery_v && d.battery_v > 0 ? d.battery_v : null; });
    var rssiData     = data.map(function(d) { return d.rssi || null; });
    var snrData      = data.map(function(d) { return d.snr != null ? d.snr : null; });
    var noiseData    = data.map(function(d) { return d.noise_floor || null; });
    var tempData     = data.map(function(d) { return d.temperature_c || null; });
    var uptimeData   = data.map(function(d) { return d.uptime_seconds ? d.uptime_seconds / 3600 : null; });
    var hasTempData  = tempData.some(function(v) { return v !== null; });
    var hasNoiseData = noiseData.some(function(v) { return v !== null; });
    var hasVoltData  = battVData.some(function(v) { return v !== null; });
    var hasUptime    = uptimeData.some(function(v) { return v !== null; });

    var battGradient = ctx.createLinearGradient(0, 0, 0, 250);
    battGradient.addColorStop(0, 'rgba(34,197,94,0.25)');
    battGradient.addColorStop(1, 'rgba(34,197,94,0.02)');

    var datasets = [
      {
        label: 'Battery (%)',
        data: battPctData,
        borderColor: '#22c55e',
        backgroundColor: battGradient,
        yAxisID: 'yBatt',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        fill: true
      },
      {
        label: 'RSSI (dBm)',
        data: rssiData,
        borderColor: '#38bdf8',
        backgroundColor: 'transparent',
        yAxisID: 'ySignal',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        fill: false,
        spanGaps: true
      },
      {
        label: 'SNR (dB)',
        data: snrData,
        borderColor: '#a78bfa',
        backgroundColor: 'transparent',
        yAxisID: 'ySignal',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        borderDash: [4, 3],
        fill: false,
        spanGaps: true
      }
    ];

    if (hasNoiseData) {
      datasets.push({
        label: 'Noise Floor (dBm)',
        data: noiseData,
        borderColor: '#94a3b8',
        backgroundColor: 'transparent',
        yAxisID: 'ySignal',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        borderDash: [2, 4],
        fill: false,
        spanGaps: true
      });
    }

    if (hasVoltData) {
      datasets.push({
        label: 'Voltage (V)',
        data: battVData,
        borderColor: '#4ade80',
        backgroundColor: 'transparent',
        yAxisID: 'yVolt',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        borderDash: [3, 2],
        fill: false,
        spanGaps: true
      });
    }

    if (hasTempData) {
      var tempGradient = ctx.createLinearGradient(0, 0, 0, 250);
      tempGradient.addColorStop(0, 'rgba(249,115,22,0.18)');
      tempGradient.addColorStop(1, 'rgba(249,115,22,0.02)');
      datasets.push({
        label: 'Temp (\u00b0C)',
        data: tempData,
        borderColor: '#f97316',
        backgroundColor: tempGradient,
        yAxisID: 'yTemp',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        fill: true,
        spanGaps: true
      });
    }

    if (hasUptime) {
      datasets.push({
        label: 'Uptime (h)',
        data: uptimeData,
        borderColor: '#fb923c',
        backgroundColor: 'transparent',
        yAxisID: 'yUptime',
        tension: 0.4,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 5,
        pointHitRadius: 10,
        fill: false,
        spanGaps: true
      });
    }

    // Add latency dataset if we have poll data with latency
    var latencyData = null;
    if (pollData && pollData.length > 0) {
      latencyData = data.map(function(d) {
        var best = null, bestDiff = 120;
        pollData.forEach(function(p) {
          if (p.latency_ms === null || p.latency_ms === undefined) return;
          var diff = Math.abs(p.ts - d.ts);
          if (diff < bestDiff) { bestDiff = diff; best = p.latency_ms; }
        });
        return best;
      });
      var hasLatency = latencyData.some(function(v) { return v !== null; });
      if (hasLatency) {
        datasets.push({
          label: 'Latency (ms)',
          data: latencyData,
          borderColor: '#f59e0b',
          backgroundColor: 'transparent',
          yAxisID: 'yLatency',
          tension: 0.3,
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 5,
          pointHitRadius: 10,
          borderDash: [2, 2],
          fill: false,
          spanGaps: true
        });
      } else {
        latencyData = null;
      }
    }

    var gridColor = '#1e293b';
    var scales = {
      x: {
        ticks: {
          color: '#64748b',
          maxTicksLimit: 10,
          font: { size: 11 },
          maxRotation: 45,
          minRotation: 30
        },
        grid: { color: gridColor }
      },
      yBatt: {
        display: 'auto',
        position: 'left',
        min: 0,
        max: 100,
        ticks: {
          color: '#22c55e',
          font: { size: 11 },
          callback: function(v) { return v + '%'; }
        },
        grid: { color: gridColor }
      },
      ySignal: {
        display: 'auto',
        position: 'right',
        ticks: { color: '#38bdf8', font: { size: 11 } },
        grid: { drawOnChartArea: false }
      }
    };

    if (hasVoltData) {
      scales.yVolt = {
        display: 'auto',
        position: 'right',
        ticks: {
          color: '#4ade80',
          font: { size: 11 },
          callback: function(v) { return v.toFixed(2) + 'V'; }
        },
        grid: { drawOnChartArea: false }
      };
    }

    if (hasTempData) {
      scales.yTemp = {
        display: 'auto',
        position: 'right',
        ticks: {
          color: '#f97316',
          font: { size: 11 },
          callback: function(v) { return v + '\u00b0'; }
        },
        grid: { drawOnChartArea: false }
      };
    }

    if (hasUptime) {
      scales.yUptime = {
        display: 'auto',
        position: 'right',
        min: 0,
        ticks: {
          color: '#fb923c',
          font: { size: 11 },
          callback: function(v) { return v.toFixed(0) + 'h'; }
        },
        grid: { drawOnChartArea: false }
      };
    }

    if (latencyData) {
      scales.yLatency = {
        display: 'auto',
        position: 'right',
        min: 0,
        ticks: {
          color: '#f59e0b',
          font: { size: 11 },
          callback: function(v) { return v + 'ms'; }
        },
        grid: { drawOnChartArea: false }
      };
    }

    historyChart = new Chart(ctx, {
      type: 'line',
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        animation: { duration: 400 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#0f172a',
            borderColor: '#334155',
            borderWidth: 1,
            titleColor: '#e2e8f0',
            bodyColor: '#94a3b8',
            padding: 10,
            callbacks: {
              label: function(ctx) {
                if (ctx.dataset.label === 'Battery (%)') {
                  var i   = ctx.dataIndex;
                  var pct = battPctData[i];
                  var mv  = battMvData[i];
                  var v   = battVData[i];
                  var vStr = (v && v > 0) ? v.toFixed(2) + 'V' : (mv > 0 ? (mv / 1000).toFixed(2) + 'V' : '--');
                  return ' Battery: ' + pct + '%  (' + vStr + ')';
                }
                if (ctx.dataset.label === 'Latency (ms)') {
                  var ms = ctx.parsed.y;
                  return ms !== null ? ' Latency: ' + ms + ' ms' : null;
                }
                if (ctx.dataset.label === 'Uptime (h)') {
                  var hrs = ctx.parsed.y;
                  if (hrs === null) return null;
                  var ud = Math.floor(hrs / 24);
                  var uh = Math.floor(hrs % 24);
                  return ' Uptime: ' + (ud > 0 ? ud + 'd ' : '') + uh + 'h';
                }
                var val = ctx.parsed.y !== null ? ctx.parsed.y : '--';
                return ' ' + ctx.dataset.label + ': ' + val;
              }
            }
          }
        },
        scales: scales
      }
    });

    // Apply current dropdown filter after chart builds
    var metricSel = document.getElementById('historyMetricSelect');
    if (metricSel && metricSel.value !== 'all') {
      updateHistoryFilter(metricSel.value);
    }

  }).catch(function(err) {
    console.error('History fetch error:', err);
  });
}

function updateHistoryFilter(metric) {
  if (!historyChart) return;
  var metricMap = {
    'battery': ['Battery (%)'],
    'voltage': ['Voltage (V)'],
    'rssi':    ['RSSI (dBm)'],
    'snr':     ['SNR (dB)'],
    'noise':   ['Noise Floor (dBm)'],
    'temp':    ['Temp (\u00b0C)'],
    'uptime':  ['Uptime (h)'],
    'latency': ['Latency (ms)']
  };
  historyChart.data.datasets.forEach(function(ds, i) {
    if (metric === 'all') {
      historyChart.getDatasetMeta(i).hidden = false;
    } else {
      var allowed = metricMap[metric] || [];
      historyChart.getDatasetMeta(i).hidden = allowed.indexOf(ds.label) === -1;
    }
  });
  historyChart.update();
}

function refreshHistoryChart() {
  if (window._historyPubkey && window._historyName) {
    showHistory(window._historyPubkey, window._historyName);
  }
}

function showCompanionInfo() {
  var modal = document.getElementById('historyModal');
  var title = document.getElementById('historyTitle');
  if (!modal || !title) return;

  var graphHours = window._graphHours || 24;
  var hoursLabel = graphHours >= 168 ? '7 days' : graphHours >= 72 ? '3 days' : graphHours >= 48 ? '48h' : '24h';
  title.textContent = 'Companion Node \u2014 ' + hoursLabel + ' battery history';
  modal.classList.add('visible');

  // Make title reset to show all
  var titleEl = document.getElementById('historyTitle');
  titleEl.style.cursor = 'default';
  titleEl.title = '';
  titleEl.onclick = null;

  // Fetch current status + history in parallel
  Promise.all([
    fetch('/api/connection').then(function(r) { return r.json(); }),
    fetch('/api/companion/history?hours=' + graphHours).then(function(r) { return r.json(); })
  ]).then(function(results) {
    var conn = results[0];
    var data = results[1];

    var canvasWrap = document.getElementById('historyChartBody');

    // Build uptime / status line above the chart
    var infoHtml = '<div style="display:flex;gap:1.5rem;flex-wrap:wrap;padding:0 0 1rem;font-size:0.82rem;color:#94a3b8;">';
    var host = conn.host || '--';
    infoHtml += '<span>Host: <strong style="color:#e2e8f0">' + escapeHtml(host) + '</strong></span>';
    if (conn.battery_mv && conn.battery_mv > 0) {
      var pct = batteryPercent(conn.battery_mv);
      var v = (conn.battery_mv / 1000).toFixed(2);
      var bClass = conn.battery_mv >= 3800 ? '#22c55e' : conn.battery_mv >= 3500 ? '#eab308' : '#ef4444';
      infoHtml += '<span>Battery: <strong style="color:' + bClass + '">' + pct + '% (' + v + 'V)</strong></span>';
    }
    if (conn.connected_since && conn.connected_since > 0) {
      var diff = Math.floor(Date.now() / 1000 - conn.connected_since);
      var upStr = '';
      var d = Math.floor(diff / 86400);
      var h = Math.floor((diff % 86400) / 3600);
      var m = Math.floor((diff % 3600) / 60);
      if (d > 0) upStr = d + 'd ' + h + 'h ' + m + 'm';
      else if (h > 0) upStr = h + 'h ' + m + 'm';
      else upStr = m + 'm ' + (diff % 60) + 's';
      infoHtml += '<span>Connected for: <strong style="color:#e2e8f0">' + upStr + '</strong></span>';
    }
    infoHtml += '</div>';

    if (!data || data.length === 0) {
      if (historyChart) { historyChart.destroy(); historyChart = null; }
      canvasWrap.innerHTML =
        infoHtml +
        '<p style="text-align:center;color:#64748b;padding:1.5rem 0;">No battery history yet. Data is logged each poll cycle.</p>' +
        '<canvas id="historyChart" height="250"></canvas>';
      return;
    }

    var spanHours = data.length > 1 ? (data[data.length - 1].ts - data[0].ts) / 3600 : graphHours;
    var labels = data.map(function(d) { return fmtChartLabel(d.ts, spanHours); });
    var battPctData = data.map(function(d) { return batteryPercent(d.battery_mv); });
    var battMvData  = data.map(function(d) { return d.battery_mv; });

    if (historyChart) { historyChart.destroy(); historyChart = null; }
    canvasWrap.innerHTML = infoHtml + '<canvas id="historyChart" height="250"></canvas>';
    var ctx = document.getElementById('historyChart').getContext('2d');

    var battGradient = ctx.createLinearGradient(0, 0, 0, 250);
    battGradient.addColorStop(0, 'rgba(34,197,94,0.25)');
    battGradient.addColorStop(1, 'rgba(34,197,94,0.02)');

    historyChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: 'Battery (%)',
          data: battPctData,
          borderColor: '#22c55e',
          backgroundColor: battGradient,
          tension: 0.4,
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 5,
          fill: true
        }]
      },
      options: {
        responsive: true,
        animation: { duration: 400 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: '#94a3b8', boxWidth: 12, boxHeight: 2, font: { size: 12 } } },
          tooltip: {
            backgroundColor: '#0f172a',
            borderColor: '#334155',
            borderWidth: 1,
            titleColor: '#e2e8f0',
            bodyColor: '#94a3b8',
            padding: 10,
            callbacks: {
              label: function(ctx) {
                var i = ctx.dataIndex;
                var mv = battMvData[i];
                return ' Battery: ' + ctx.parsed.y + '%  (' + (mv / 1000).toFixed(2) + 'V)';
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: '#64748b', maxTicksLimit: 10, font: { size: 11 }, maxRotation: 45, minRotation: 30 }, grid: { color: '#1e293b' } },
          y: {
            min: 0, max: 100,
            ticks: { color: '#22c55e', font: { size: 11 }, callback: function(v) { return v + '%'; } },
            grid: { color: '#1e293b' }
          }
        }
      }
    });
  }).catch(function(err) {
    console.error('Companion info error:', err);
  });
}

function closeHistory() {
  var modal = document.getElementById('historyModal');
  if (modal) modal.classList.remove('visible');
  if (historyChart) { historyChart.destroy(); historyChart = null; }
  var titleEl = document.getElementById('historyTitle');
  if (titleEl) { titleEl.onclick = null; titleEl.style.cursor = ''; titleEl.title = ''; }
}

// --- Logs Modal ---

function openLogs() {
  var modal = document.getElementById('logsModal');
  if (!modal) return;
  modal.classList.add('visible');

  // Load current retention setting
  fetch('/api/settings')
    .then(function(r) { return r.json(); })
    .then(function(s) {
      var retInput = document.getElementById('logRetention');
      if (retInput) retInput.value = s.log_retention_hours || 24;
    })
    .catch(function() {});

  refreshLogs();
}

function closeLogs() {
  var modal = document.getElementById('logsModal');
  if (modal) modal.classList.remove('visible');
}

function refreshLogs() {
  var hours = document.getElementById('logHours').value || 24;
  var level = document.getElementById('logLevel').value || '';
  var url = '/api/logs?hours=' + hours + '&limit=500';
  if (level) url += '&level=' + encodeURIComponent(level);

  var tbody = document.getElementById('logsBody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="4" class="logs-empty">Loading...</td></tr>';

  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(logs) {
      if (!logs || logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="logs-empty">No log entries found for this time range.</td></tr>';
        return;
      }

      tbody.innerHTML = '';
      logs.forEach(function(entry) {
        var tr = document.createElement('tr');
        tr.className = 'log-row log-' + entry.level.toLowerCase();

        var tdTime = document.createElement('td');
        tdTime.className = 'log-time';
        tdTime.textContent = new Date(entry.ts * 1000).toLocaleString([], {
          month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit', second: '2-digit'
        });

        var tdLevel = document.createElement('td');
        tdLevel.className = 'log-level';
        tdLevel.textContent = entry.level;

        var tdSource = document.createElement('td');
        tdSource.className = 'log-source';
        tdSource.textContent = entry.logger;

        var tdMsg = document.createElement('td');
        tdMsg.className = 'log-message';
        tdMsg.textContent = entry.message;

        tr.appendChild(tdTime);
        tr.appendChild(tdLevel);
        tr.appendChild(tdSource);
        tr.appendChild(tdMsg);
        tbody.appendChild(tr);
      });
    })
    .catch(function(err) {
      tbody.innerHTML = '<tr><td colspan="4" class="logs-empty">Failed to load logs: ' + escapeHtml(err.message) + '</td></tr>';
    });
}

function saveLogRetention() {
  var retVal = parseInt(document.getElementById('logRetention').value) || 24;
  if (retVal < 1) retVal = 1;
  if (retVal > 720) retVal = 720;

  fetch('/api/settings')
    .then(function(r) { return r.json(); })
    .then(function(s) {
      s.log_retention_hours = retVal;
      return fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(s)
      });
    })
    .then(function(r) { return r.json(); })
    .then(function(result) {
      if (result.ok) {
        var btn = document.querySelector('.logs-retention-setting .btn');
        if (btn) {
          btn.textContent = 'Saved!';
          setTimeout(function() { btn.textContent = 'Save'; }, 1500);
        }
      }
    })
    .catch(function(err) {
      console.error('Failed to save retention:', err);
    });
}


// --- Modal close handlers ---

document.addEventListener('click', function(e) {
  if (e.target && e.target.id === 'historyModal') {
    closeHistory();
  }
  if (e.target && e.target.id === 'settingsModal') {
    closeSettings();
  }
  if (e.target && e.target.id === 'logsModal') {
    closeLogs();
  }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeHistory();
    closeSettings();
    closeLogs();
  }
});


// --- Notification Toggle ---

var _ntfyEnabled = true;

function _updateNtfyBtn() {
  var btn = document.getElementById('ntfyToggleBtn');
  if (!btn) return;
  if (_ntfyEnabled) {
    btn.classList.remove('ntfy-disabled');
    btn.title = 'Notifications on — click to mute';
  } else {
    btn.classList.add('ntfy-disabled');
    btn.title = 'Notifications muted — click to unmute';
  }
}

function toggleNtfy() {
  fetch('/api/ntfy/toggle', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(res) {
      if (res.ok) {
        _ntfyEnabled = res.enabled;
        _updateNtfyBtn();
      }
    })
    .catch(function() {});
}


// --- Init ---

document.addEventListener('DOMContentLoaded', function() {
  // Load settings (low battery threshold + initial card order + ntfy state + graph hours)
  fetch('/api/settings')
    .then(function(r) { return r.json(); })
    .then(function(s) {
      window._lowBatteryPercent = s.low_battery_percent || 20;
      window._highTempAlert = s.high_temp_alert_c || 60;
      window._highNoiseAlert = s.high_noise_alert_dbm || -90;
      if (!window._cardOrder || window._cardOrder.length === 0) {
        window._cardOrder = (s.repeaters || []).map(function(r) { return r.pubkey; });
      }
      // Show notification toggle button only if a topic is configured
      var btn = document.getElementById('ntfyToggleBtn');
      if (btn && s.ntfy_topic) {
        _ntfyEnabled = s.ntfy_enabled !== false;
        btn.style.display = '';
        _updateNtfyBtn();
      }
    })
    .catch(function() {});

  // Initial data fetch
  fetch('/api/repeaters')
    .then(function(r) { return r.json(); })
    .then(renderRepeaters)
    .catch(function() {
      document.getElementById('repeaterGrid').innerHTML =
        '<div class="no-data"><h2>Connecting...</h2><p>Waiting for the server to respond.</p></div>';
    });

  // Build stats tiles (reads localStorage config) then start data feeds
  buildStatsTiles();

  // Start SSE for live updates
  connectSSE();

  // Network stats row — initial load + refresh every 60s
  refreshNetStats();
  setInterval(refreshNetStats, 60000);

  // Node connection status — poll every 5s
  function pollNodeStatus() {
    fetch('/api/connection')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var dot  = document.getElementById('nodeDot');
        var text = document.getElementById('nodeText');
        if (!dot || !text) return;
        if (d.connected) {
          dot.className  = 'status-dot online';
          var batStr = '';
          if (d.battery_mv && d.battery_mv > 0) {
            var pct = batteryPercent(d.battery_mv);
            var bClass = d.battery_mv >= 3800 ? 'battery-good' : (d.battery_mv >= 3500 ? 'battery-mid' : 'battery-low');
            batStr = ' &middot; <span class="' + bClass + '" title="Node battery">&#128267; ' + pct + '% (' + (d.battery_mv / 1000).toFixed(2) + 'V)</span>';
          }
          text.innerHTML = 'Node: ' + escapeHtml(d.host || 'connected') + batStr;
        } else {
          dot.className  = 'status-dot offline';
          var lastStr = '';
          if (d.last_connected && d.last_connected > 0) {
            var ago = Math.floor(Date.now() / 1000 - d.last_connected);
            if (ago < 60) lastStr = ' &middot; last seen ' + ago + 's ago';
            else if (ago < 3600) lastStr = ' &middot; last seen ' + Math.floor(ago / 60) + 'm ago';
            else if (ago < 86400) lastStr = ' &middot; last seen ' + Math.floor(ago / 3600) + 'h ago';
            else lastStr = ' &middot; last seen ' + new Date(d.last_connected * 1000).toLocaleString();
          }
          text.innerHTML = 'Node: disconnected' + lastStr;
        }
      })
      .catch(function() {
        var dot  = document.getElementById('nodeDot');
        var text = document.getElementById('nodeText');
        if (dot)  dot.className  = 'status-dot unknown';
        if (text) text.innerHTML = 'Node: --';
      });
  }
  pollNodeStatus();
  setInterval(pollNodeStatus, 5000);

  // New message badge — check every 30s
  function checkUnreadMessages() {
    var lastSeen = parseFloat(localStorage.getItem('meshcore_last_msg_seen') || '0');
    fetch('/api/messages?hours=48&limit=1')
      .then(function(r) { return r.json(); })
      .then(function(msgs) {
        var badge = document.getElementById('msgsBadge');
        if (!badge) return;
        badge.style.display = (msgs.length > 0 && msgs[0].ts > lastSeen) ? '' : 'none';
      })
      .catch(function() {});
  }
  checkUnreadMessages();
  setInterval(checkUnreadMessages, 30000);
  window._checkUnreadMessages = checkUnreadMessages;

  // Clicking Messages link marks as seen
  document.addEventListener('click', function(e) {
    var a = e.target.closest('a[href="/messages"]');
    if (!a) return;
    localStorage.setItem('meshcore_last_msg_seen', (Date.now() / 1000).toString());
    var badge = document.getElementById('msgsBadge');
    if (badge) badge.style.display = 'none';
  });
});

// Cross-tab sync via localStorage storage events
window.addEventListener('storage', function(e) {
  if (e.key === 'meshcore_tiles_v1') {
    buildStatsTiles();
    refreshNetStats();
  }
  if (e.key === 'meshcore_last_msg_seen' || (e.key && e.key.startsWith('meshcore_ch_seen_'))) {
    if (window._checkUnreadMessages) window._checkUnreadMessages();
  }
});
