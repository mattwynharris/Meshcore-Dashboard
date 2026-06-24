# MeshCore Dashboard

A self-hosted web dashboard for monitoring MeshCore LoRa repeaters, contacts, and messages via a WiFi companion node. this is a vibe coded Project

> **Requirement:** A MeshCore WiFi companion node on your network with TCP enabled (default port **5000**).

---

<img width="1440" alt="Dashboard" src="https://github.com/user-attachments/assets/a0aacd82-5f93-4eec-a61f-1dbf62895e44" />
<img width="1430" alt="Map" src="https://github.com/user-attachments/assets/5c57ad33-fe68-4ddb-b409-cd4b2faed2c9" />

---

## What's New in v2.0

### Messages — Channel List
Messages now opens as a list of all your channels, each showing the last message preview and a red unread count badge. Tap a channel to open the conversation. Back button returns to the list.

### Configurable Dashboard Tiles
The 6 stat tiles on the dashboard are now fully customisable. Go to **Settings → Appearance** and tap any tile slot to pick what it shows. 10 stats to choose from: Packets, Nodes, Active 24h, Routes, Active Repeaters, Total Repeaters, New Nodes, Messages, Alerts, and Avg RSSI.

### Alerts Detail
Clicking the Active Alerts tile now opens a panel showing exactly what each alert is — battery level, temperature, noise floor, or clock drift — with icons and repeater names.

### Settings Accordion
Settings is now organised into 4 collapsible sections: **Companion Device**, **Repeaters**, **Appearance**, and **Update**. Tap a section header to expand it; the rest collapse automatically.

### Mobile Layout
The stats row is now a **3×2 grid on phones** instead of a cramped single row of 6. Nav buttons are evenly spaced and consistent across all pages.

### Contacts Page
New dedicated Contacts page with sortable columns showing all nodes seen by your companion.

### Navigation Consistency
All 7 pages share the same navigation bar in the same order. The active page is highlighted automatically.

### Cross-Tab Sync
Change settings or read messages in one browser tab — other open tabs update their tiles and unread badges automatically.

---

## Features

- **Dashboard** — live battery, RSSI/SNR, uptime, and hop count per repeater; 6 configurable stat tiles; historical charts
- **Map** — live Leaflet map with repeaters, contacts, and advertising nodes; network path overlay showing routes through hops
- **Messages** — channel list with unread badges; tap a channel to open the conversation
- **Packets** — raw RX packet feed with filters
- **Contacts** — full contact list with sortable columns
- **Logs** — app and poller activity log
- **Settings** — companion IP, repeater list, poll timing, alert thresholds, dashboard appearance, and software updates

---

## Installation

### Option 1 — Docker (recommended)

Works on any Linux server, VM, NAS, or Raspberry Pi with Docker installed.

**1. Install Docker**

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
> Log out and back in after running this.

**2. Create a folder and download the compose file**

```bash
mkdir meshcore-dashboard && cd meshcore-dashboard
mkdir -p data
curl -O https://raw.githubusercontent.com/mattwynharris/Meshcore-Dashboard/main/docker-compose.yml
```

**3. Start the dashboard**

```bash
docker compose up -d
```

Docker pulls the image automatically — no building required. Open `http://<your-device-ip>:8080` in a browser.

---

### Option 2 — VM (ESXi / Proxmox / Hyper-V)

Recommended specs:
- **OS:** Ubuntu Server 24.04 LTS (64-bit)
- **CPU:** 2 cores
- **RAM:** 2 GB
- **Disk:** 10 GB

SSH into the VM, then follow the Docker steps above.

---

### Option 3 — Raspberry Pi

Any Pi model 3 or newer running Raspberry Pi OS (64-bit recommended).

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Log out and back in, then follow the Docker steps above.

---

### Option 4 — Manual (Python, no Docker)

```bash
git clone https://github.com/mattwynharris/Meshcore-Dashboard.git
cd Meshcore-Dashboard
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

---

## First-time Setup

1. Open `http://<device-ip>:8080`
2. Click **⚙ Settings**
3. Under **Companion Device** — enter your companion node IP and port (default `5000`)
4. Under **Repeaters** — add each repeater with its name, public key, and admin password
5. Click **Save & Apply**

The dashboard starts polling your repeaters immediately.

---

## Updating

### Docker users

```bash
docker compose pull
docker compose up -d
```

This pulls the latest image and restarts the container. Your data is kept.

### Web update (zip)

1. Download the latest `meshcore-dashboard-update-VX.X.zip` from the [Releases page](https://github.com/mattwynharris/Meshcore-Dashboard/releases)
2. Open **Settings → Update**
3. Click **Choose .zip…** → select the zip → **Upload & Apply**
4. The dashboard restarts automatically

---

## Useful Commands

```bash
docker compose logs -f        # live logs
docker compose restart        # restart
docker compose down           # stop
docker compose up -d          # start
docker compose pull           # pull latest image
```

The container starts automatically on reboot.

---

## Data

Stored in `./data/` on the host — survives updates and restarts:

| File | Contents |
|------|----------|
| `data/settings.json` | Companion IP, repeater list, poll timing, thresholds |
| `data/repeater_history.db` | Telemetry history, logs, messages, contact routes |

---

## Docker Image

`docker pull mattwh/meshcore-dashboard:latest`

Available on [Docker Hub](https://hub.docker.com/r/mattwh/meshcore-dashboard) — built for **linux/amd64** and **linux/arm64**.

---

*This is an independent community-built tool and is not affiliated with or endorsed by MeshCore or its developers.*
