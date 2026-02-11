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


// --- Render Dashboard ---

function renderRepeaters(data) {
  var grid = document.getElementById('repeaterGrid');
  if (!grid) return;

  if (!data || data.length === 0) {
    grid.innerHTML =
      '<div class="no-data">' +
        '<h2>Waiting for data...</h2>' +
        '<p>The poller is connecting to your companion device and requesting repeater status.</p>' +
      '</div>';
    return;
  }

  grid.innerHTML = '';

  // Get low battery threshold from settings (cached from last settings load)
  var lowBatPct = window._lowBatteryPercent || 20;

  data.forEach(function(r) {
    var bPct = batteryPercent(r.battery_mv);
    var bClass = batteryClass(r.battery_mv);
    var sClass = signalClass(r.rssi);
    var dotClass = r.online ? 'online' : (r.last_seen_epoch > 0 ? 'offline' : 'unknown');
    var isLowBat = r.battery_mv > 0 && bPct <= lowBatPct;

    var card = document.createElement('div');
    card.className = 'card';
    card.setAttribute('data-pubkey', r.pubkey);

    // Battery warning banner
    var warningHtml = '';
    if (isLowBat) {
      warningHtml = '<div class="battery-warning">LOW BATTERY - ' + bPct + '%</div>';
    } else if (!r.online && r.last_seen_epoch > 0) {
      warningHtml = '<div class="offline-warning">OFFLINE - Last known values shown</div>';
    }

    card.innerHTML =
      warningHtml +
      '<div class="card-header">' +
        '<div>' +
          '<div class="card-name">' + escapeHtml(r.name) + '</div>' +
          '<div class="card-id">' + (r.pubkey_short || r.pubkey.substring(0, 12)) +
            (r.hops > 0 ? ' &middot; ' + r.hops + ' hop' + (r.hops > 1 ? 's' : '') : '') +
          '</div>' +
          '<div class="card-route">' + (r.route_path ? 'Route: ' + escapeHtml(r.route_path) : 'Flood') + '</div>' +
        '</div>' +
        '<span class="status-dot ' + dotClass + '"></span>' +
      '</div>' +
      '<div class="metrics">' +
        // Battery
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
        // RSSI
        '<div class="metric">' +
          '<div class="metric-label">RSSI</div>' +
          '<div class="metric-value ' + sClass + '">' +
            (r.rssi !== 0 ? r.rssi : '--') +
            '<span class="metric-unit"> dBm</span>' +
          '</div>' +
        '</div>' +
        // SNR
        '<div class="metric">' +
          '<div class="metric-label">SNR</div>' +
          '<div class="metric-value">' +
            (r.snr !== 0 ? r.snr.toFixed(1) : '--') +
            '<span class="metric-unit"> dB</span>' +
          '</div>' +
        '</div>' +
        // Noise Floor
        '<div class="metric">' +
          '<div class="metric-label">Noise Floor</div>' +
          '<div class="metric-value">' +
            (r.noise_floor !== 0 ? r.noise_floor : '--') +
            '<span class="metric-unit"> dBm</span>' +
          '</div>' +
        '</div>' +
        // Uptime
        '<div class="metric">' +
          '<div class="metric-label">Uptime</div>' +
          '<div class="metric-value">' +
            formatUptime(r.uptime_seconds) +
          '</div>' +
        '</div>' +
        // Hops
        '<div class="metric">' +
          '<div class="metric-label">Hops</div>' +
          '<div class="metric-value">' +
            (r.hops > 0 ? r.hops : '--') +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="card-footer">Last seen: ' + timeAgo(r.last_seen_epoch) + '</div>';

    card.addEventListener('click', function() {
      showHistory(r.pubkey, r.name);
    });

    grid.appendChild(card);
  });
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

      historyChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Battery (mV)',
              data: data.map(function(d) { return d.battery_mv; }),
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
            legend: { labels: { color: '#94a3b8' } }
          },
          scales: {
            x: {
              ticks: { color: '#64748b', maxTicksLimit: 12 },
              grid: { color: '#1e293b' }
            },
            yBatt: {
              position: 'left',
              title: { display: true, text: 'Battery (mV)', color: '#94a3b8' },
              ticks: { color: '#22c55e' },
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

// --- Settings Modal ---

function openSettings() {
  var modal = document.getElementById('settingsModal');
  if (!modal) return;
  modal.classList.add('visible');
  clearSettingsStatus();

  // Load current settings from server
  fetch('/api/settings')
    .then(function(r) { return r.json(); })
    .then(function(s) {
      document.getElementById('companionHost').value = s.companion_host || '';
      document.getElementById('companionPort').value = s.companion_port || 5000;
      document.getElementById('pollInterval').value = s.poll_interval_seconds || 120;
      document.getElementById('staggerDelay').value = s.stagger_delay_seconds || 15;
      document.getElementById('lowBatteryPct').value = s.low_battery_percent || 20;
      window._lowBatteryPercent = s.low_battery_percent || 20;

      // Render repeater rows
      var list = document.getElementById('repeaterList');
      list.innerHTML = '';
      var repeaters = s.repeaters || [];
      if (repeaters.length === 0) {
        addRepeaterRow();
      } else {
        repeaters.forEach(function(r) {
          addRepeaterRow(r.name, r.pubkey, r.admin_pass, r.path);
        });
      }
    })
    .catch(function(err) {
      showSettingsStatus('Failed to load settings', true);
    });
}

function closeSettings() {
  var modal = document.getElementById('settingsModal');
  if (modal) modal.classList.remove('visible');
}

function addRepeaterRow(name, pubkey, adminPass, path) {
  var list = document.getElementById('repeaterList');
  var row = document.createElement('div');
  row.className = 'repeater-row';
  row.innerHTML =
    '<div class="settings-field">' +
      '<label>Name</label>' +
      '<input type="text" class="rpt-name" placeholder="My Repeater" value="' + escapeAttr(name || '') + '">' +
    '</div>' +
    '<div class="settings-field">' +
      '<label>Public Key</label>' +
      '<input type="text" class="rpt-pubkey" placeholder="a1b2c3d4e5f6..." value="' + escapeAttr(pubkey || '') + '">' +
    '</div>' +
    '<div class="settings-field settings-field-small">' +
      '<label>Admin Pass</label>' +
      '<input type="text" class="rpt-pass" placeholder="password" value="' + escapeAttr(adminPass || '') + '">' +
    '</div>' +
    '<div class="settings-field settings-field-path">' +
      '<label>Path</label>' +
      '<input type="text" class="rpt-path" placeholder="4d,3c,ee" value="' + escapeAttr(path || '') + '">' +
    '</div>' +
    '<button class="btn-remove" title="Remove">&times;</button>';

  row.querySelector('.btn-remove').addEventListener('click', function() {
    row.remove();
  });

  list.appendChild(row);
}

function escapeAttr(str) {
  return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function saveSettings() {
  clearSettingsStatus();

  var host = document.getElementById('companionHost').value.trim();
  var port = document.getElementById('companionPort').value.trim();
  var pollInterval = document.getElementById('pollInterval').value.trim();
  var staggerDelay = document.getElementById('staggerDelay').value.trim();
  var lowBatPct = document.getElementById('lowBatteryPct').value.trim();

  if (!host) {
    showSettingsStatus('Companion IP is required', true);
    return;
  }

  // Gather repeater rows
  var rows = document.querySelectorAll('.repeater-row');
  var repeaters = [];
  for (var i = 0; i < rows.length; i++) {
    var n = rows[i].querySelector('.rpt-name').value.trim();
    var p = rows[i].querySelector('.rpt-pubkey').value.trim();
    var pw = rows[i].querySelector('.rpt-pass').value.trim();
    var pt = rows[i].querySelector('.rpt-path').value.trim();
    if (n && p) {
      var rpt = { name: n, pubkey: p };
      if (pw) rpt.admin_pass = pw;
      rpt.path = pt;
      repeaters.push(rpt);
    } else if (n || p) {
      showSettingsStatus('Each repeater needs both a name and public key', true);
      return;
    }
  }

  var payload = {
    companion_host: host,
    companion_port: parseInt(port) || 5000,
    repeaters: repeaters,
    poll_interval_seconds: parseInt(pollInterval) || 120,
    stagger_delay_seconds: parseInt(staggerDelay) || 15,
    stale_threshold_seconds: 900,
    low_battery_percent: parseInt(lowBatPct) || 20
  };

  fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
    .then(function(r) { return r.json(); })
    .then(function(result) {
      if (result.ok) {
        showSettingsStatus('Saved! Poller reconnecting...', false);
        setTimeout(closeSettings, 1500);
      } else {
        showSettingsStatus(result.error || 'Save failed', true);
      }
    })
    .catch(function(err) {
      showSettingsStatus('Network error: ' + err.message, true);
    });
}

function showSettingsStatus(msg, isError) {
  var el = document.getElementById('settingsStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'settings-status ' + (isError ? 'error' : 'success');
}

function clearSettingsStatus() {
  var el = document.getElementById('settingsStatus');
  if (el) { el.textContent = ''; el.className = 'settings-status'; }
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


// --- Init ---

document.addEventListener('DOMContentLoaded', function() {
  // Load low battery threshold
  fetch('/api/settings')
    .then(function(r) { return r.json(); })
    .then(function(s) { window._lowBatteryPercent = s.low_battery_percent || 20; })
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
});
