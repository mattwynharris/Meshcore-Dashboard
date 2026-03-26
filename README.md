# MeshCore Repeater Dashboard

A self-hosted web dashboard for monitoring MeshCore LoRa repeaters and contacts via a companion WiFi node.

---
<img width="1440" height="669" alt="Screenshot 2026-03-26 at 9 15 12 PM" src="https://github.com/user-attachments/assets/a0aacd82-5f93-4eec-a61f-1dbf62895e44" />
<img width="1430" height="761" alt="Screenshot 2026-03-26 at 9 15 55 PM" src="https://github.com/user-attachments/assets/5c57ad33-fe68-4ddb-b409-cd4b2faed2c9" />
<img width="1431" height="743" alt="Screenshot 2026-03-26 at 9 22 50 PM" src="https://github.com/user-attachments/assets/2b611ffe-5f00-4e1b-9a40-574e7a72d9df" />

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
## VM Requirement 
- Guest OS Ubuntu Linux (64-bit)
- Compatibility ESXi 7.0 U2 virtual machine
- VMware Tools Yes
- CPUs 2
- Memory 2 GB

---
## Installation

### 1. Set Up VM and Install Docker on your device
Set up your VM OS Ubuntu Linux 
then SSH in to your VM 

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
> Log out and back in before continuing.

### 2. Clone the repo and run setup

```bash
git clone https://github.com/mattwynharris/Meshcore-Dashboard.git
cd Meshcore-Dashboard
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

1. Download the latest `meshcore-dashboard-update-VX.X.zip` from the [Releases page](https://github.com/mattwynharris/Meshcore-Dashboard/releases)
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
