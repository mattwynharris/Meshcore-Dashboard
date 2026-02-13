# MeshCore Repeater Dashboard

Monitor your MeshCore LoRa repeaters from a web browser. Shows live battery, signal (RSSI/SNR), uptime, hops, and historical charts for each repeater.

working along side a WIFI connected Node on port 5000

<img width="1440" height="787" alt="Meshcore Dashboard" src="https://github.com/user-attachments/assets/c199bc15-1b07-4145-9d10-d8d11f932af8" />

---

## Requirements 

- Guest OS :  Ubuntu Linux (64-bit)
- Compatibility :  ESXi 7.0 U2 virtual machine
- CPUs :  1
- Memory : 1 GB

## Installation

### 1. Install Docker on the VM
*(Skip if Docker is already installed)*

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
> Log out and back in before continuing.

### 2. Clone the repo and run setup

```bash
# Run these on the VM
git clone https://github.com/YOUR_USERNAME/meshcore-dashboard.git
cd meshcore-dashboard
bash install.sh
```

The script builds the Docker image and starts the dashboard. It will print the URL when done.

### 3. Open the dashboard and configure it

Go to `http://<vm-ip>:8080` in your browser, then click the **⚙ gear icon** and enter:

- Companion device IP address and port *(default port: 5000)*
- Each repeater — name, public key, and admin password
- Click **Save & Apply**

The dashboard will start polling your repeaters straight away.

---

## Applying Updates

No SSH needed — updates go through the dashboard.

**Download the latest update zip** from the [Releases page](https://github.com/mattwynharris/Meshcore-Dashboard/releases) — look for `meshcore-dashboard-update.zip` attached to the latest release.

**In the browser:**
1. Open **Settings** → scroll to **Software Update**
2. Click **Choose .zip…** → select the downloaded zip → click **Upload & Apply**
3. Click **Restart Now** — the page reloads automatically

---

## Useful Commands *(run on the VM)*

```bash
docker compose logs -f      # live logs
docker compose restart      # restart the container
docker compose down         # stop
docker compose up -d        # start (e.g. after a reboot)
```

The container is configured to start automatically on VM reboot.

---

## Your Data

Everything is stored in `~/meshcore-dashboard/data/` on the VM — outside the container — so it survives updates and restarts:

- `data/settings.json` — companion IP, repeater list, poll timing
- `data/repeater_history.db` — telemetry history and activity logs
