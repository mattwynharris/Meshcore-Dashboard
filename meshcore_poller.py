import asyncio
import logging
import time

from meshcore import MeshCore, EventType

import config as cfg
from data_store import DataStore

logger = logging.getLogger("meshcore_poller")


class MeshcorePoller:
    """
    Connects to a MeshCore companion device over TCP and polls
    configured repeaters on a staggered schedule.

    Reads config dynamically each cycle so web UI changes
    (companion IP, repeater list, timing) take effect without restart.
    """

    def __init__(self, store: DataStore):
        self.store = store
        self.mc: MeshCore = None
        self._running = False
        self._contacts = {}
        self._current_host = None
        self._current_port = None
        self._needs_reconnect = False

    async def start(self):
        """Main entry point. Runs forever, reconnecting on errors."""
        self._running = True

        # Register any initially configured repeaters
        for r in cfg.get_repeaters():
            self.store.init_repeater(r["pubkey"], r["name"])

        while self._running:
            try:
                await self._connect_and_poll()
            except Exception as e:
                logger.error(f"Poller error: {e}", exc_info=True)
                if self.mc:
                    try:
                        await self.mc.disconnect()
                    except Exception:
                        pass
                    self.mc = None
                logger.info("Reconnecting in 10 seconds...")
                await asyncio.sleep(10)

    async def stop(self):
        self._running = False
        if self.mc:
            try:
                await self.mc.disconnect()
            except Exception:
                pass

    def request_reconnect(self):
        """Called by the web API when settings change."""
        self._needs_reconnect = True

    async def _connect_and_poll(self):
        host = cfg.get_companion_host()
        port = cfg.get_companion_port()
        self._current_host = host
        self._current_port = port
        self._needs_reconnect = False

        logger.info(f"Connecting to companion at {host}:{port}")
        self.mc = await MeshCore.create_tcp(
            host,
            port,
            auto_reconnect=True,
            max_reconnect_attempts=5,
        )
        logger.info("Connected to companion device")

        await self._refresh_contacts()

        while self._running and not self._needs_reconnect:
            # Re-read config each cycle for dynamic updates
            repeaters = cfg.get_repeaters()
            poll_interval = cfg.get_poll_interval()

            # Sync the store with current repeater list
            for r in repeaters:
                self.store.init_repeater(r["pubkey"], r["name"])

            # Check if companion IP changed
            new_host = cfg.get_companion_host()
            new_port = cfg.get_companion_port()
            if new_host != self._current_host or new_port != self._current_port:
                logger.info(f"Companion address changed to {new_host}:{new_port}, reconnecting...")
                break

            cycle_start = time.monotonic()
            await self._poll_all_repeaters(repeaters)
            elapsed = time.monotonic() - cycle_start
            remaining = max(0, poll_interval - elapsed)
            if remaining > 0:
                logger.info(f"Cycle complete. Next poll in {remaining:.0f}s")
                # Sleep in small chunks so we can respond to reconnect requests
                while remaining > 0 and self._running and not self._needs_reconnect:
                    await asyncio.sleep(min(remaining, 2))
                    remaining -= 2

        # Disconnect before reconnecting with new settings
        if self.mc:
            try:
                await self.mc.disconnect()
            except Exception:
                pass
            self.mc = None

    async def _refresh_contacts(self):
        """Fetch contacts from companion to populate routing table."""
        try:
            result = await self.mc.commands.get_contacts()
            if result.type == EventType.ERROR:
                logger.error(f"get_contacts failed: {result.payload}")
                return

            contacts = result.payload
            if isinstance(contacts, dict):
                self._contacts = contacts
            elif isinstance(contacts, list):
                self._contacts = {}
                for c in contacts:
                    pk = c.get("public_key", "")
                    if pk:
                        self._contacts[pk] = c

            logger.info(f"Loaded {len(self._contacts)} contacts from companion")
        except Exception as e:
            logger.error(f"Contact refresh failed: {e}")

    def _find_contact(self, pubkey: str):
        """Find a contact matching the configured pubkey (full or prefix)."""
        if pubkey in self._contacts:
            return self._contacts[pubkey]

        for key, contact in self._contacts.items():
            pk = key if isinstance(key, str) else key.hex() if isinstance(key, bytes) else str(key)
            if pk.startswith(pubkey) or pubkey.startswith(pk):
                return contact

            inner_pk = contact.get("public_key", "")
            if isinstance(inner_pk, bytes):
                inner_pk = inner_pk.hex()
            if inner_pk.startswith(pubkey) or pubkey.startswith(inner_pk):
                return contact

        return None

    async def _poll_all_repeaters(self, repeaters: list):
        """Poll each configured repeater with staggered delays."""
        await self._refresh_contacts()
        stagger = cfg.get_stagger_delay()

        for i, repeater_cfg in enumerate(repeaters):
            if not self._running or self._needs_reconnect:
                break

            pubkey = repeater_cfg["pubkey"]
            name = repeater_cfg["name"]

            contact = self._find_contact(pubkey)
            if contact is None:
                logger.warning(
                    f"[{name}] No contact found for pubkey {pubkey[:16]}... "
                    f"(is the repeater in range?)"
                )
                if i < len(repeaters) - 1:
                    await asyncio.sleep(stagger)
                continue

            # Extract hop count and route path from contact
            hops = 0
            route_path = ""
            if isinstance(contact, dict):
                hops = contact.get("hops", contact.get("path_len", 0))
                raw_path = contact.get("path", contact.get("route", None))
                if raw_path:
                    if isinstance(raw_path, bytes):
                        route_path = " > ".join(f"{b:02x}" for b in raw_path)
                    elif isinstance(raw_path, list):
                        route_path = " > ".join(f"{b:02x}" if isinstance(b, int) else str(b) for b in raw_path)
                    elif isinstance(raw_path, str) and raw_path:
                        route_path = raw_path
            elif hasattr(contact, "hops"):
                hops = contact.hops
            elif hasattr(contact, "path_len"):
                hops = contact.path_len
            self.store.update_route(pubkey, hops, route_path)

            logger.info(f"[{name}] Polling repeater ({i+1}/{len(repeaters)}), hops={hops}, route={route_path or 'flood'}...")

            # Apply custom path if configured, otherwise use flood
            custom_path = repeater_cfg.get("path", "").strip()
            await self._apply_path(contact, pubkey, name, custom_path)

            # Login to repeater before requesting data
            admin_pass = repeater_cfg.get("admin_pass", "password")
            await self._login_to_repeater(contact, name, admin_pass)
            await asyncio.sleep(3)

            await self._request_status(pubkey, name, contact)
            await asyncio.sleep(2)
            await self._request_telemetry(pubkey, name, contact)

            if i < len(repeaters) - 1:
                logger.debug(f"Waiting {stagger}s before next repeater")
                await asyncio.sleep(stagger)

    async def _apply_path(self, contact, pubkey: str, name: str, custom_path: str):
        """Apply a custom route path or reset to flood if empty."""
        try:
            if custom_path:
                # Parse comma-separated hex bytes like "4d,3c,ee"
                hex_parts = [p.strip() for p in custom_path.replace(" ", "").split(",") if p.strip()]
                path_bytes = bytes(int(h, 16) for h in hex_parts)
                await self.mc.commands.change_contact_path(contact, path_bytes)
                logger.info(f"[{name}] Set custom path: {' > '.join(hex_parts)}")
            else:
                await self.mc.commands.reset_path(pubkey)
                logger.debug(f"[{name}] Using flood routing")
        except Exception as e:
            logger.error(f"[{name}] Path update error: {e}")

    async def _login_to_repeater(self, contact, name: str, password: str):
        """Login to a repeater so it responds to status/telemetry requests."""
        try:
            result = await self.mc.commands.send_login(contact, password)
            if result.type == EventType.ERROR:
                logger.warning(f"[{name}] Login failed: {result.payload}")
            else:
                logger.info(f"[{name}] Login sent (pwd={'default' if password == 'password' else 'custom'})")
        except Exception as e:
            logger.error(f"[{name}] Login error: {e}")

    async def _request_status(self, pubkey: str, name: str, contact):
        """Request status from a repeater and update the store."""
        try:
            status = await self.mc.commands.req_status_sync(contact, timeout=30)
            if status is None:
                logger.warning(f"[{name}] Status request timed out")
                return

            updates = {}

            if "bat" in status:
                updates["battery_mv"] = status["bat"]
                updates["battery_voltage"] = status["bat"] / 1000.0

            if "last_rssi" in status:
                updates["rssi"] = status["last_rssi"]

            if "last_snr" in status:
                snr_raw = status["last_snr"]
                if isinstance(snr_raw, int) and abs(snr_raw) > 50:
                    updates["snr"] = snr_raw / 4.0
                else:
                    updates["snr"] = float(snr_raw)

            if "noise_floor" in status:
                updates["noise_floor"] = status["noise_floor"]

            if "uptime" in status:
                updates["uptime_seconds"] = status["uptime"]

            if "nb_recv" in status:
                updates["packets_recv"] = status["nb_recv"]
            if "nb_sent" in status:
                updates["packets_sent"] = status["nb_sent"]

            if updates:
                self.store.update_repeater(pubkey, **updates)
                logger.info(
                    f"[{name}] Status: {updates.get('battery_mv', '?')}mV, "
                    f"RSSI={updates.get('rssi', '?')}dBm, "
                    f"SNR={updates.get('snr', '?')}dB"
                )

        except Exception as e:
            logger.error(f"[{name}] Status request error: {e}")

    async def _request_telemetry(self, pubkey: str, name: str, contact):
        """Request LPP telemetry and update the store with any extra data."""
        try:
            telemetry = await self.mc.commands.req_telemetry_sync(contact, timeout=30)
            if telemetry is None:
                logger.debug(f"[{name}] Telemetry request returned no data")
                return

            updates = {}
            sensors = telemetry if isinstance(telemetry, list) else []
            for sensor in sensors:
                sensor_type = sensor.get("type", "")
                value = sensor.get("value")

                if sensor_type == "voltage" and value is not None:
                    updates["battery_voltage"] = float(value)
                    updates["battery_mv"] = int(float(value) * 1000)
                elif sensor_type == "analog" and value is not None:
                    if sensor.get("channel") == 0:
                        updates["battery_voltage"] = float(value)
                        updates["battery_mv"] = int(float(value) * 1000)

            if updates:
                self.store.update_repeater(pubkey, **updates)
                logger.info(f"[{name}] Telemetry: {updates}")

        except Exception as e:
            logger.error(f"[{name}] Telemetry request error: {e}")

    async def ping_repeater(self, pubkey: str) -> dict:
        """Request fresh status and telemetry from a repeater, updating the store."""
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}

        contact = self._find_contact(pubkey)
        if contact is None:
            await self._refresh_contacts()
            contact = self._find_contact(pubkey)
        if contact is None:
            return {"ok": False, "error": "Repeater not found in contacts — may be out of range"}

        # Find name and admin password from config
        name = pubkey[:8]
        admin_pass = "password"
        for r in cfg.get_repeaters():
            pk = r["pubkey"]
            if pk == pubkey or pk.startswith(pubkey) or pubkey.startswith(pk):
                admin_pass = r.get("admin_pass", "password")
                name = r.get("name", name)
                break

        start = time.monotonic()
        try:
            await self._login_to_repeater(contact, name, admin_pass)
            await asyncio.sleep(1)
            await self._request_status(pubkey, name, contact)
            await asyncio.sleep(1)
            await self._request_telemetry(pubkey, name, contact)
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.info(f"[{name}] Manual refresh completed in {latency_ms}ms")
            return {"ok": True, "latency_ms": latency_ms}
        except Exception as e:
            logger.error(f"[{name}] Manual refresh error: {e}")
            return {"ok": False, "error": str(e)}
