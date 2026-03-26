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
  var chain = [r.name];
  if (r.route_path) {
    var segs = r.route_path.replace(/\s/g, '').split('>');
    segs.forEach(function(seg) {
      chain.push(prefixToName[seg] || seg);
    });
  } else if (r.hops > 0) {
    // We know there are intermediates but the firmware didn't provide path data
    for (var i = 0; i < r.hops; i++) chain.push('?');
  }
  chain.push('GW');
  return chain.join(' \u2192 ');
}


// --- Render Dashboard ---

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

  // Build pubkey prefix (first byte = 2 hex chars) -> name map for route resolution
  var prefixToName = {};
  data.forEach(function(rep) {
    if (rep.pubkey) prefixToName[rep.pubkey.substring(0, 2)] = rep.name;
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
    var hopsLabel = !hopsSeen ? '--' : (r.hops === 0 ? 'Direct' : r.hops + ' hop' + (r.hops !== 1 ? 's' : ''));
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
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">SNR</div>' +
          '<div class="metric-value">' +
            (r.snr !== 0 ? r.snr.toFixed(1) : '--') +
            '<span class="metric-unit"> dB</span>' +
          '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">Noise Floor</div>' +
          '<div class="metric-value">' +
            (r.noise_floor !== 0 ? r.noise_floor : '--') +
            '<span class="metric-unit"> dBm</span>' +
          '</div>' +
        '</div>' +
        '<div class="metric">' +
          '<div class="metric-label">Uptime</div>' +
          '<div class="metric-value">' + formatUptime(r.uptime_seconds) + '</div>' +
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
        '</div>' +
        '<button class="' + advertClass + '" data-action="advert"' + (advertRemaining > 0 ? ' disabled' : '') + '>' + advertLabel + '</button>' +
        '<button class="' + pingClass + '" data-action="ping"' + (pingDisabled ? ' disabled' : '') + '>' + pingLabel + '</button>' +
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

    grid.appendChild(card);
  });
}


// --- Advert ---

function sendAdvert(pubkey, btn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.className = 'card-advert-btn';

  window._advertCooldowns[pubkey] = Date.now() / 1000 + 15;
  delete window._advertResults[pubkey];
  _startAdvertCountdown(pubkey);

  fetch('/api/advert/' + encodeURIComponent(pubkey), { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(result) {
      window._advertResults[pubkey] = result;
      var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="advert"]');
      if (b) b.className = 'card-advert-btn ' + (result.ok ? 'advert-ok' : 'advert-fail');
    })
    .catch(function() {
      window._advertResults[pubkey] = { ok: false };
      var b = document.querySelector('[data-pubkey="' + pubkey + '"] [data-action="advert"]');
      if (b) b.className = 'card-advert-btn advert-fail';
    });
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
    if (btn) btn.textContent = remaining + 's';
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


// --- History Modal ---

var historyChart = null;

function showHistory(pubkey, name) {
  var modal = document.getElementById('historyModal');
  var title = document.getElementById('historyTitle');
  if (!modal || !title) return;

  title.textContent = name + ' - 24h History';
  modal.classList.add('visible');

  fetch('/api/history/' + encodeURIComponent(pubkey) + '?hours=24')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data || data.length === 0) {
        var ctx = document.getElementById('historyChart');
        if (historyChart) { historyChart.destroy(); historyChart = null; }
        ctx.parentElement.innerHTML =
          '<p style="text-align:center;color:#64748b;padding:2rem;">No history data yet. Data will appear after a few poll cycles.</p>' +
          '<canvas id="historyChart" height="250"></canvas>';
        return;
      }

      var labels = data.map(function(d) {
        return new Date(d.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      });

      var ctx = document.getElementById('historyChart').getContext('2d');
      if (historyChart) historyChart.destroy();

      // Pre-compute battery % for each point so the tooltip can show both
      var battPctData = data.map(function(d) { return batteryPercent(d.battery_mv); });
      var battMvData  = data.map(function(d) { return d.battery_mv; });
      var battVData   = data.map(function(d) { return d.battery_v; });

      historyChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Battery (%)',
              data: battPctData,
              borderColor: '#22c55e',
              backgroundColor: '#22c55e20',
              yAxisID: 'yBatt',
              tension: 0.3,
              pointRadius: 1,
              fill: true
            },
            {
              label: 'RSSI (dBm)',
              data: data.map(function(d) { return d.rssi; }),
              borderColor: '#38bdf8',
              yAxisID: 'ySignal',
              tension: 0.3,
              pointRadius: 1,
              fill: false
            },
            {
              label: 'SNR (dB)',
              data: data.map(function(d) { return d.snr; }),
              borderColor: '#eab308',
              yAxisID: 'ySignal',
              tension: 0.3,
              pointRadius: 1,
              fill: false
            }
          ]
        },
        options: {
          responsive: true,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#94a3b8' } },
            tooltip: {
              callbacks: {
                label: function(ctx) {
                  if (ctx.datasetIndex === 0) {
                    var i   = ctx.dataIndex;
                    var pct = battPctData[i];
                    var mv  = battMvData[i];
                    var v   = battVData[i];
                    var vStr = (v && v > 0) ? v.toFixed(2) + ' V' : (mv > 0 ? (mv / 1000).toFixed(2) + ' V' : '--');
                    return 'Battery: ' + pct + '%  (' + vStr + ')';
                  }
                  return ctx.dataset.label + ': ' + ctx.parsed.y;
                }
              }
            }
          },
          scales: {
            x: {
              ticks: { color: '#64748b', maxTicksLimit: 12 },
              grid: { color: '#1e293b' }
            },
            yBatt: {
              position: 'left',
              min: 0,
              max: 100,
              title: { display: true, text: 'Battery (%)', color: '#94a3b8' },
              ticks: { color: '#22c55e', callback: function(v) { return v + '%'; } },
              grid: { color: '#1e293b' }
            },
            ySignal: {
              position: 'right',
              title: { display: true, text: 'Signal', color: '#94a3b8' },
              ticks: { color: '#38bdf8' },
              grid: { drawOnChartArea: false }
            }
          }
        }
      });
    })
    .catch(function(err) {
      console.error('History fetch error:', err);
    });
}

function closeHistory() {
  var modal = document.getElementById('historyModal');
  if (modal) modal.classList.remove('visible');
  if (historyChart) { historyChart.destroy(); historyChart = null; }
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
  // Load settings (low battery threshold + initial card order + ntfy state)
  fetch('/api/settings')
    .then(function(r) { return r.json(); })
    .then(function(s) {
      window._lowBatteryPercent = s.low_battery_percent || 20;
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

  // Start SSE for live updates
  connectSSE();

  // Node connection status — poll every 5s
  function pollNodeStatus() {
    fetch('/api/connection')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var dot  = document.getElementById('nodeDot');
        var text = document.getElementById('nodeText');
        if (!dot || !text) return;
        var btn = document.getElementById('pollToggleBtn');
        if (d.connected) {
          dot.className  = 'status-dot online';
          var batStr = '';
          if (d.battery_mv && d.battery_mv > 0) {
            var pct = batteryPercent(d.battery_mv);
            var bClass = d.battery_mv >= 3800 ? 'battery-good' : (d.battery_mv >= 3500 ? 'battery-mid' : 'battery-low');
            batStr = ' &middot; <span class="' + bClass + '" title="Node battery">&#128267; ' + pct + '% (' + (d.battery_mv / 1000).toFixed(2) + 'V)</span>';
          }
          text.innerHTML = 'Node: ' + escapeHtml(d.host || 'connected') + batStr;
          if (btn) {
            btn.style.display = '';
            btn.textContent = d.polling_enabled === false ? '▶ Resume Polling' : '⏸ Pause Polling';
            btn.style.color = d.polling_enabled === false ? '#f59e0b' : '#94a3b8';
            btn.style.borderColor = d.polling_enabled === false ? '#f59e0b' : '#334155';
          }
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
          if (btn) btn.style.display = 'none';
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

  window.togglePolling = function() {
    fetch('/api/polling/toggle', { method: 'POST' })
      .then(function(r) { return r.json(); })
      .then(function() { pollNodeStatus(); })
      .catch(function() {});
  };

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

  var msgsLink = document.getElementById('msgsNavLink');
  if (msgsLink) {
    msgsLink.addEventListener('click', function() {
      localStorage.setItem('meshcore_last_msg_seen', (Date.now() / 1000).toString());
      var badge = document.getElementById('msgsBadge');
      if (badge) badge.style.display = 'none';
    });
  }
});
