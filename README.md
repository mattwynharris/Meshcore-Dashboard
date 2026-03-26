# MeshCore Repeater Dashboard

A self-hosted web dashboard for monitoring MeshCore LoRa repeaters and contacts via a companion WiFi node.

---

## Features

- **Dashboard** — live battery, signal (RSSI/SNR), uptime, and hop count for each configured repeater; historical charts
- **Map** — plots repeaters, contacts, and advertising nodes on a live Leaflet map
  - Network path overlay showing routes through actual hops (teal = single route, amber = shared/merged segment)
  - Click a repeater to isolate and highlight only its paths; click off to restore prior state
  - 300 km sanity filter — no misleading straight lines for unknown paths
- **Messages** — channel and direct message log from the companion node
- **Packets** — raw RX log / packet feed
- **Logs** — app and poller activity log
- **Settings** — configure companion IP, repeater list (name, pubkey, admin password), poll timing, and software updates

---

## Installation

### 1. Install Docker on your device

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
> Log out and back in before continuing.

### 2. Clone the repo and run setup

```bash
git clone https://github.com/mattwyn-harris/meshcore-dashboard.git
cd meshcore-dashboard
bash install.sh
```

The script builds the Docker image and starts the dashboard. It prints the URL when done.

### 3. Open the dashboard and configure it

Go to `http://<device-ip>:8080`, click the **⚙ Settings** icon and enter:

- Companion device IP address and port *(default port: 5000)*
- Each repeater — name, public key, and admin password
- Click **Save & Apply**

The dashboard starts polling your repeaters straight away.

---

## Applying Updates

No SSH needed — updates are applied through the dashboard UI.

1. Download the latest `meshcore-dashboard-update-VX.X.zip` from the [Releases page](https://github.com/mattwyn-harris/meshcore-dashboard/releases)
2. Open **Settings** → scroll to **Software Update**
3. Click **Choose .zip…** → select the downloaded zip → click **Upload & Apply**
4. Click **Restart Now** — the page reloads automatically

---

## Useful Commands

```bash
docker compose logs -f      # live logs
docker compose restart      # restart the container
docker compose down         # stop
docker compose up -d        # start (e.g. after a reboot)
```

The container starts automatically on reboot.

---

## Data

Everything is stored in `~/meshcore-dashboard/data/` on the host — outside the container — so it survives updates and restarts:

- `data/settings.json` — companion IP, repeater list, poll timing
- `data/repeater_history.db` — telemetry history, activity logs, contact routes
