import asyncio
import json
import logging
import time
import urllib.request
from collections import deque

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
        self._stay_disconnected = False
        self._msg_sub_contact = None
        self._msg_sub_channel = None
        self._msg_sub_ack = None
        self._device_channels: list = []   # [{name, idx}] fetched from node
        self._msg_poll_task = None
        self._companion_battery_mv: int = 0  # battery of the companion WiFi node itself
        self._last_connected_ts: float = 0   # unix timestamp of last confirmed connection
        self._connected_since_ts: float = 0  # unix timestamp when connection was first established
        self._path_sub = None                # passive PATH_RESPONSE subscription
        self._rx_log_sub = None              # all LoRa packets heard — SNR/RSSI/type
        self._advert_sub = None              # node advertisement beacons
        self._telemetry_sub = None           # companion base telemetry (battery)
        self._recent_events: deque = deque(maxlen=200)  # live mesh activity feed
        self._node_id_name_cache: dict = self.store.load_node_names()  # pubkey first-byte (2 hex chars) → node name
        self._contact_routes: dict = {}      # pubkey_prefix (upper) → (hops, route_path) for all contacts
        self._suppress_sdk_paths: bool = False  # True after path reset — ignore SDK out_path until new paths arrive
        self._pending_rx_paths: list = []    # [(ts, hops, path)] — recent RF paths for msg correlation
        self._alert_state: dict = {}         # pubkey → {"offline": bool, "low_bat": bool} to avoid duplicate alerts
        self._dont_save_contact_msgs: bool = False  # suppress saving CLI command replies as normal messages
        self._clock_poll_counts: dict = {}   # pubkey → int, incremented each poll cycle for throttling
        # Restore polling_enabled from settings so a saved "paused" state survives restarts
        self._polling_enabled = cfg.get_settings().get("polling_enabled", True)

    @property
    def ROUTE_TTL_SECONDS(self) -> int:
        return cfg.get_route_ttl_hours() * 3600

    def _lookup_contact_route(self, pubkey_prefix: str) -> tuple:
        """Fuzzy-match pubkey_prefix against _contact_routes. Returns (hops, path) or (-1, '').
        Entries older than ROUTE_TTL_SECONDS are treated as expired."""
        if not pubkey_prefix:
            return (-1, "")
        now = time.time()
        pre = pubkey_prefix.upper()

        def _valid(v):
            """Return (hops, path) if entry is fresh, else None."""
            ts = v[2] if len(v) > 2 else 0
            return (v[0], v[1]) if (now - ts) < self.ROUTE_TTL_SECONDS else None

        # Exact match first
        if pre in self._contact_routes:
            r = _valid(self._contact_routes[pre])
            if r: return r
        # Try 2-char prefix
        if len(pre) >= 2 and pre[:2] in self._contact_routes:
            r = _valid(self._contact_routes[pre[:2]])
            if r: return r
        # Fuzzy: any stored key that starts with our prefix OR our prefix starts with stored key
        for k, v in self._contact_routes.items():
            if k.startswith(pre) or pre.startswith(k):
                r = _valid(v)
                if r: return r
        return (-1, "")

    def _cache_node_name(self, node_id: str, name: str):
        """Store a node ID → name mapping and persist it to the database."""
        node_id = node_id.upper()
        if self._node_id_name_cache.get(node_id) == name:
            return  # no change, skip write
        self._node_id_name_cache[node_id] = name
        self.store.save_node_names({node_id: name})

    def _resolve_hop_name(self, hop_id: str) -> str:
        """Resolve a hop ID (2 or 4 hex chars) to a known node name.

        Resolution order:
          1. Exact hit in node-name cache (handles both 1-byte and 2-byte IDs)
          2. Configured repeaters — compare hop_id against first N chars of their pubkey
          3. Live contacts via _resolve_contact_name
        Returns empty string when name is unknown.
        """
        hop_upper = hop_id.upper()
        # 1. Cache hit
        name = self._node_id_name_cache.get(hop_upper, "")
        if name:
            return name
        # 2. Configured repeaters — we know their full pubkeys
        for r_cfg in cfg.get_repeaters():
            pk = r_cfg.get("pubkey", "").upper()
            if pk and pk.startswith(hop_upper):
                rname = r_cfg.get("name", "")
                if rname:
                    # Cache for next time
                    self._node_id_name_cache[hop_upper] = rname
                    return rname
        # 3. Live contacts
        resolved = self._resolve_contact_name(hop_id)
        return resolved if resolved != hop_id[:8] else ""

    @property
    def is_connected(self) -> bool:
        return self.mc is not None and getattr(self.mc, "is_connected", False)

    @property
    def device_channels(self) -> list:
        return list(self._device_channels)

    async def start(self):
        """Main entry point. Runs forever, reconnecting on errors."""
        self._running = True

        # Register any initially configured repeaters
        for r in cfg.get_repeaters():
            self.store.init_repeater(r["pubkey"], r["name"])

        # Respect auto_connect setting — if disabled, start in disconnected state
        if not cfg.get_settings().get("auto_connect", True):
            self._stay_disconnected = True
            logger.info("Auto-connect disabled — staying disconnected until manually connected")

        while self._running:
            if self._stay_disconnected:
                await asyncio.sleep(1)
                continue
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
                if self._stay_disconnected:
                    continue
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
        """Called by the web API when settings change or user clicks Connect."""
        self._stay_disconnected = False
        self._needs_reconnect = True

    def manual_disconnect(self):
        """Disconnect and don't auto-reconnect until the user clicks Connect."""
        self._stay_disconnected = True
        self._needs_reconnect = True  # Break the inner poll loop

    def _log_event(self, event_type: str, **data):
        """Append an event to the recent-activity ring buffer."""
        self._recent_events.appendleft({"ts": time.time(), "type": event_type, **data})

    def get_recent_events(self, limit: int = 100) -> list:
        return list(self._recent_events)[:limit]

    def toggle_polling(self) -> bool:
        """Toggle duty-cycle polling on/off. Returns new state (True = enabled)."""
        self._polling_enabled = not self._polling_enabled
        state = "enabled" if self._polling_enabled else "disabled"
        logger.info(f"Duty-cycle polling {state}")
        try:
            settings = cfg.get_settings()
            settings["polling_enabled"] = self._polling_enabled
            cfg.save_settings(settings)
        except Exception:
            pass
        return self._polling_enabled

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
        self._connected_since_ts = time.time()
        if self.mc and hasattr(self.mc, "self_info") and self.mc.self_info:
            logger.info(f"Self info: {self.mc.self_info}")
        else:
            logger.info("Self info not available after connect")

        if cfg.get_pull_contacts_on_start():
            await self._refresh_contacts(configured_only=False)
        else:
            # Pull only configured repeaters so polling works; leave advert_nodes intact.
            # Contacts only clear when the user explicitly triggers a reset.
            await self._refresh_contacts(configured_only=True)
        await self._fetch_device_channels()
        await self._fetch_companion_telemetry()
        await self._subscribe_messages()

        while self._running and not self._needs_reconnect and not self._stay_disconnected:
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
            if self._polling_enabled:
                await self._poll_all_repeaters(repeaters)
                await self._fetch_companion_telemetry()
            else:
                logger.debug("Duty-cycle polling paused — skipping this cycle")
            elapsed = time.monotonic() - cycle_start
            remaining = max(0, poll_interval - elapsed)
            if remaining > 0:
                logger.info(f"Cycle complete. Next poll in {remaining:.0f}s")
                # Sleep in small chunks so we can respond to reconnect requests
                while remaining > 0 and self._running and not self._needs_reconnect and not self._stay_disconnected:
                    await asyncio.sleep(min(remaining, 2))
                    remaining -= 2

        # Unsubscribe before disconnecting
        self._unsubscribe_messages()

        # Disconnect before reconnecting with new settings
        if self.mc:
            try:
                await asyncio.wait_for(self.mc.disconnect(), timeout=5)
            except Exception:
                pass
            self.mc = None

    # --- Companion device telemetry (battery) ---

    async def _fetch_companion_telemetry(self):
        """Request battery level directly from the companion WiFi node."""
        if not self.mc:
            return
        self._last_connected_ts = time.time()
        try:
            event = await self.mc.commands.get_bat()
            if event and event.type == EventType.BATTERY:
                mv = event.payload.get("level", 0)
                if mv and mv > 0:
                    self._companion_battery_mv = int(mv)
                    logger.info(f"[companion] battery: {self._companion_battery_mv}mV")
                    self.store.log_companion_battery(self._companion_battery_mv)
                    return
            logger.debug(f"[companion] get_bat returned: {event!r}")
        except Exception as e:
            logger.info(f"[companion] get_bat failed: {e}")

    # --- Device channel discovery ---

    async def _fetch_device_channels(self):
        """Query the node for configured channels (indices 0-7)."""
        channels = []
        for idx in range(8):
            try:
                result = await self.mc.commands.get_channel(idx)
                if result.type == EventType.ERROR:
                    continue
                payload = result.payload if hasattr(result, "payload") else {}
                if not isinstance(payload, dict):
                    continue
                name = payload.get("channel_name", "").strip("\x00").strip()
                if name:
                    channels.append({"name": name, "idx": idx})
            except Exception:
                break  # If the command isn't supported, stop trying
        self._device_channels = channels
        if channels:
            logger.info(f"Device channels: {[c['name'] for c in channels]}")
        else:
            logger.debug("No device channels found (using settings channels)")

    # --- Message subscription ---

    async def _subscribe_messages(self):
        """Subscribe to incoming message events and start polling for buffered messages."""
        try:
            self._msg_sub_contact = self.mc.subscribe(
                EventType.CONTACT_MSG_RECV, self._on_contact_msg
            )
        except Exception as e:
            logger.warning(f"Could not subscribe to contact messages: {e}")

        try:
            self._msg_sub_channel = self.mc.subscribe(
                EventType.CHANNEL_MSG_RECV, self._on_channel_msg
            )
        except Exception as e:
            logger.warning(f"Could not subscribe to channel messages: {e}")

        try:
            self._msg_sub_ack = self.mc.subscribe(
                EventType.ACK, self._on_msg_ack
            )
            logger.debug("Subscribed to ACK events")
        except Exception as e:
            logger.debug(f"ACK events not available: {e}")

        try:
            self._path_sub = self.mc.subscribe(
                EventType.PATH_RESPONSE, self._on_path_response
            )
            logger.debug("Subscribed to passive PATH_RESPONSE events")
        except Exception as e:
            logger.debug(f"PATH_RESPONSE subscription not available: {e}")

        try:
            self._rx_log_sub = self.mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log)
            logger.debug("Subscribed to RX_LOG_DATA events")
        except Exception as e:
            logger.debug(f"RX_LOG_DATA not available: {e}")

        try:
            self._advert_sub = self.mc.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
            logger.debug("Subscribed to ADVERTISEMENT events")
        except Exception as e:
            logger.debug(f"ADVERTISEMENT not available: {e}")

        # Log all available EventType values so we can find the telemetry event
        try:
            all_event_types = [attr for attr in dir(EventType) if not attr.startswith('_')]
            logger.info(f"[companion] Available EventTypes: {all_event_types}")
        except Exception:
            pass

        # Try to subscribe to telemetry/sensor events to get companion battery
        for et_name in ("BATTERY", "TELEMETRY_RESPONSE", "TELEMETRY", "SENSOR_DATA", "BASE_TELEMETRY", "NODE_TELEMETRY", "STATUS"):
            try:
                et = getattr(EventType, et_name, None)
                if et is not None:
                    self._telemetry_sub = self.mc.subscribe(et, self._on_companion_telemetry)
                    logger.info(f"[companion] Subscribed to EventType.{et_name}")
                    break
            except Exception as e:
                logger.debug(f"[companion] EventType.{et_name} not available: {e}")

        # Try to enable auto-fetching (some firmware versions support this)
        try:
            await self.mc.commands.start_auto_message_fetching()
            logger.info("Auto message fetching started")
        except Exception:
            logger.debug("start_auto_message_fetching not available")

        # Drain any messages buffered on the node since last connect
        await self._drain_messages()

        # Also start a polling loop — this catches messages even when
        # auto-fetching/subscriptions don't fire (firmware-dependent)
        self._msg_poll_task = asyncio.create_task(self._msg_poll_loop())

    async def _drain_messages(self):
        """Pull all messages currently buffered on the node."""
        try:
            count = 0
            while True:
                result = await self.mc.commands.get_msg(timeout=3)
                if result.type in (EventType.CONTACT_MSG_RECV, EventType.CHANNEL_MSG_RECV):
                    await self._dispatch_message(result)
                    count += 1
                else:
                    break  # NO_MORE_MSGS or ERROR
            if count:
                logger.info(f"Drained {count} buffered message(s) from node")
        except Exception as e:
            logger.debug(f"Message drain: {e}")

    def _unsubscribe_messages(self):
        if self._msg_poll_task:
            self._msg_poll_task.cancel()
            self._msg_poll_task = None
        for sub in (self._msg_sub_contact, self._msg_sub_channel, self._msg_sub_ack,
                    self._path_sub, self._rx_log_sub, self._advert_sub, self._telemetry_sub):
            if sub is not None:
                try:
                    self.mc.unsubscribe(sub)
                except Exception:
                    pass
        self._msg_sub_contact = None
        self._msg_sub_channel = None
        self._msg_sub_ack = None
        self._path_sub = None
        self._rx_log_sub = None
        self._advert_sub = None
        self._telemetry_sub = None

    async def _msg_poll_loop(self):
        """Periodically drain any messages the node has buffered (backup for subscriptions)."""
        while True:
            try:
                await asyncio.sleep(30)
                if not self.mc:
                    break
                while True:
                    result = await self.mc.commands.get_msg(timeout=2)
                    if result.type in (EventType.CONTACT_MSG_RECV, EventType.CHANNEL_MSG_RECV):
                        await self._dispatch_message(result)
                    else:
                        break  # NO_MORE_MSGS or ERROR
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _dispatch_message(self, event):
        """Handle a message event from either subscription or polling."""
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            if event.type == EventType.CONTACT_MSG_RECV:
                await self._on_contact_msg(event)
            elif event.type == EventType.CHANNEL_MSG_RECV:
                await self._on_channel_msg(event)
        except Exception as e:
            logger.error(f"Error dispatching message: {e}")

    async def _on_contact_msg(self, event):
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            text = payload.get("text", "")
            sender_pubkey = str(payload.get("pubkey_prefix", ""))
            sender_name = self._resolve_contact_name(sender_pubkey)
            hops, path = self._extract_hops_path(payload)
            if hops > 0 and not path and sender_pubkey:
                stored_hops, stored_path = self.store.get_route_by_prefix(sender_pubkey)
                if stored_path:
                    path = stored_path
                if not path:
                    _, cached_path = self._lookup_contact_route(sender_pubkey)
                    if cached_path:
                        path = cached_path
                if not path:
                    import time as _time
                    now = _time.time()
                    spk = (sender_pubkey or "").lower()
                    # First pass: prefer a path whose first hop matches sender prefix
                    for entry in reversed(self._pending_rx_paths):
                        rx_ts, rx_hops, rx_path = entry[0], entry[1], entry[2]
                        first_hop = entry[3] if len(entry) > 3 else ""
                        if now - rx_ts < 5.0 and rx_path and spk and first_hop.startswith(spk[:2]):
                            path = rx_path
                            break
                    # Second pass: any recent path within 3s
                    if not path:
                        for entry in reversed(self._pending_rx_paths):
                            rx_ts, rx_hops, rx_path = entry[0], entry[1], entry[2]
                            if now - rx_ts < 3.0 and rx_path:
                                path = rx_path
                                break
                # Still no path — fire-and-forget path discovery so future messages work
                if not path:
                    contact_obj = self._find_contact(sender_pubkey)
                    if contact_obj is not None:
                        asyncio.ensure_future(
                            self._discover_path_for_contact(contact_obj, sender_pubkey, sender_name)
                        )
            if text:
                if not self._dont_save_contact_msgs:
                    is_new = self.store.store_message("in", None, sender_pubkey, sender_name, text,
                                                      hops=hops, path=path)
                    self._log_event("contact_msg", sender=sender_name, pubkey=sender_pubkey,
                                    hops=hops, path=path,
                                    path_chips=self._decode_path_chips(path),
                                    text=text[:120])
                    if is_new:
                        logger.info(f"[msg] Direct from {sender_name} ({hops} hops): {text[:60]}")
                        await self._send_ntfy("MeshCore", f"{sender_name}: {text}")
        except Exception as e:
            logger.error(f"Error handling contact message: {e}")

    async def _on_channel_msg(self, event):
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            text = payload.get("text", "")
            channel_idx = payload.get("channel_idx", 0)
            ch_name = next(
                (c["name"] for c in self._device_channels if c["idx"] == channel_idx),
                f"Ch{channel_idx}"
            )
            sender_pubkey = str(payload.get("pubkey_prefix", payload.get("sender_pubkey", payload.get("sender", ""))))
            hops, path = self._extract_hops_path(payload)
            if hops > 0 and not path and sender_pubkey:
                stored_hops, stored_path = self.store.get_route_by_prefix(sender_pubkey)
                if stored_path:
                    path = stored_path
                if not path:
                    _, cached_path = self._lookup_contact_route(sender_pubkey)
                    if cached_path:
                        path = cached_path
                if not path:
                    import time as _time
                    now = _time.time()
                    spk2 = (sender_pubkey or "").lower()
                    for entry in reversed(self._pending_rx_paths):
                        rx_ts, rx_hops, rx_path = entry[0], entry[1], entry[2]
                        first_hop = entry[3] if len(entry) > 3 else ""
                        if now - rx_ts < 5.0 and rx_path and spk2 and first_hop.startswith(spk2[:2]):
                            path = rx_path
                            break
                    if not path:
                        for entry in reversed(self._pending_rx_paths):
                            rx_ts, rx_hops, rx_path = entry[0], entry[1], entry[2]
                            if now - rx_ts < 3.0 and rx_path:
                                path = rx_path
                                break
                # Still no path — fire-and-forget path discovery so future messages work
                if not path and sender_pubkey:
                    contact_obj = self._find_contact(sender_pubkey)
                    if contact_obj is not None:
                        asyncio.ensure_future(
                            self._discover_path_for_contact(contact_obj, sender_pubkey,
                                self._resolve_contact_name(sender_pubkey))
                        )
            if text:
                is_new = self.store.store_message("in", channel_idx, sender_pubkey, ch_name, text,
                                                  hops=hops, path=path)
                if is_new:
                    logger.info(f"[msg] Channel {channel_idx} ({ch_name}, {hops} hops): {text[:60]}")
                    sender_display = self._resolve_contact_name(sender_pubkey) if sender_pubkey else "Unknown"
                    self._log_event("channel_msg", channel=ch_name, channel_idx=channel_idx,
                                    sender=sender_display, pubkey=sender_pubkey,
                                    hops=hops, path=path,
                                    path_chips=self._decode_path_chips(path),
                                    text=text[:120])
                    muted = cfg.get_settings().get("muted_channels", [])
                    if channel_idx not in muted:
                        await self._send_ntfy("MeshCore", f"[{ch_name}] {text}")
        except Exception as e:
            logger.error(f"Error handling channel message: {e}")

    async def _send_ntfy(self, title: str, message: str):
        """Fire a push notification via ntfy if a topic is configured and notifications are enabled."""
        s = cfg.get_settings()
        if not s.get("ntfy_enabled", True):
            return
        topic = s.get("ntfy_topic", "").strip()
        if not topic:
            return
        server = s.get("ntfy_server", "https://ntfy.sh").strip().rstrip("/")
        click_url = s.get("dashboard_url", "").strip()
        await self._send_ntfy_to(server, topic, title, message, click_url)

    async def _send_ntfy_to(self, server: str, topic: str, title: str, message: str, click_url: str = ""):
        """Send a ntfy notification using the headers API (plain text body, metadata in headers)."""
        url = f"{server}/{topic}"
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
        }
        if click_url:
            headers["Click"] = click_url
        data = message.encode("utf-8")
        def _post():
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                logger.warning(f"ntfy notification failed: {e}")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _post)

    async def _on_msg_ack(self, event):
        """Handle an ACK event — a node confirmed it heard one of our sent messages."""
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            # The ACK payload contains a code that matches expected_ack from the send result
            code = payload.get("code", payload.get("ack_code", payload.get("expected_ack", b"")))
            if isinstance(code, bytes):
                code = code.hex()
            elif not isinstance(code, str):
                code = str(code)
            if code:
                count = self.store.increment_message_acks(code)
                if count > 0:
                    logger.info(f"[msg] ACK received — message seen by {count} node(s)")
                else:
                    logger.debug(f"[msg] ACK received (code={code}) — no matching outgoing message")
                # Always log to Packets feed so the user can see ACKs arriving
                self._log_event("ack", ack_code=code, seen_by=count)
        except Exception as e:
            logger.debug(f"Error handling ACK event: {e}")

    async def _on_path_response(self, event):
        """Passively capture PATH_RESPONSE events to keep repeater routes fresh."""
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            pubkey_pre = str(payload.get("pubkey_pre", payload.get("pubkey_prefix", "")))
            new_hops = payload.get("out_path_len", -1)
            raw_path = payload.get("out_path", "")
            if not pubkey_pre or new_hops < 0:
                return
            # Parse path with the same auto-detect logic used elsewhere
            disc_route = ""
            if isinstance(raw_path, bytes) and raw_path:
                bpn = 1
                if new_hops > 0:
                    det = len(raw_path) // new_hops
                    if det in (1, 2):
                        bpn = det
                disc_route = " > ".join(raw_path[i:i+bpn].hex() for i in range(0, len(raw_path), bpn))
            elif isinstance(raw_path, str) and raw_path:
                cpn = 2
                if new_hops > 0:
                    det = len(raw_path) // new_hops
                    if det in (2, 4):
                        cpn = det
                segs = [raw_path[i:i+cpn] for i in range(0, len(raw_path), cpn)
                        if len(raw_path[i:i+cpn]) == cpn]
                disc_route = " > ".join(segs)

            # Cache for all contacts (used by non-configured contacts too)
            self._contact_routes[pubkey_pre.upper()] = (new_hops, disc_route, time.time())

            # Update configured repeater store if this matches one
            matched_pubkey = None
            matched_name = None
            for r in cfg.get_repeaters():
                pk = r["pubkey"]
                if pk.startswith(pubkey_pre) or pubkey_pre.startswith(pk):
                    matched_pubkey = pk
                    matched_name = r.get("name", pk[:8])
                    break
            if matched_pubkey:
                self.store.update_route(matched_pubkey, new_hops, disc_route)
            else:
                matched_name = self._resolve_contact_name(pubkey_pre)
            logger.info(f"[path] Live route update — {matched_name}: {new_hops} hop(s), path={disc_route or 'direct'}")
            self._log_event("path", name=matched_name, pubkey=matched_pubkey or pubkey_pre,
                            hops=new_hops, route=disc_route)
        except Exception as e:
            logger.debug(f"Error handling PATH_RESPONSE event: {e}")

    # Payload type codes extracted from header byte bits 2-5: (header >> 2) & 0x0F
    _PAYLOAD_TYPE_NAMES = {
        0: "Request",
        1: "Response",
        4: "Advert",
        5: "Group Text",
        7: "Anon Request",
        8: "Path Update",
        9: "Text Msg",
    }

    async def _on_rx_log(self, event):
        """Capture raw RF log events — every LoRa packet the companion hears, with SNR/RSSI."""
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            snr = payload.get("snr")
            rssi = payload.get("rssi")
            route = payload.get("route", payload.get("path", ""))
            raw = payload.get("data", payload.get("payload", ""))
            if isinstance(raw, bytes):
                raw = raw.hex()
            raw_str = str(raw) if raw else ""

            pkt_type_label = ""
            node_label = ""
            decoded_path = []   # list of {"id": "BC", "name": "Solar Pole Node"}

            if len(raw_str) >= 4:
                try:
                    header = int(raw_str[0:2], 16)
                    payload_type = (header >> 2) & 0x0F
                    route_type = header & 0x03  # 0=Direct, 1=Flood, 2=Routed, 3=Reply
                    pkt_type_label = self._PAYLOAD_TYPE_NAMES.get(
                        payload_type, f"Type {payload_type}"
                    )

                    # Direct packets (route_type=0) have no path — payload starts at byte 1.
                    # Flood/Routed/Reply: byte 1 encodes both hop size and hop count:
                    #   bits 6-7 = hash_size (00=1 byte/hop, 01=2 bytes/hop)
                    #   bits 0-5 = hop count
                    if route_type == 0:
                        path_len = 0
                        hop_hex_size = 2
                        payload_hex_start = 2
                    else:
                        path_len_byte = int(raw_str[2:4], 16) if len(raw_str) >= 4 else 0
                        hash_size_bits = (path_len_byte >> 6) & 0x03
                        path_len = path_len_byte & 0x3F
                        hop_hex_size = (hash_size_bits + 1) * 2  # 0→2, 1→4, 2→6 hex chars
                        payload_hex_start = 4 + path_len * hop_hex_size

                    # Decode each hop in the path
                    for i in range(path_len):
                        hop_hex_pos = 4 + i * hop_hex_size
                        if hop_hex_pos + hop_hex_size > len(raw_str):
                            break
                        # Show exactly what the packet says — no expansion
                        hop_id = raw_str[hop_hex_pos:hop_hex_pos + hop_hex_size].upper()
                        hop_name = self._resolve_hop_name(hop_id)
                        decoded_path.append({"id": hop_id, "name": hop_name or hop_id})

                    # Stash path for message-type packets so _on_contact/channel_msg can use it
                    _MSG_PAYLOAD_TYPES = {0, 5, 7, 9}  # Request, Group Text, Anon Req, Text Msg
                    if payload_type in _MSG_PAYLOAD_TYPES and decoded_path:
                        import time as _time
                        path_str = " > ".join(h["id"] for h in decoded_path)
                        # Store first hop ID (likely the sender or nearest relay) for matching
                        first_hop = decoded_path[0]["id"].lower() if decoded_path else ""
                        self._pending_rx_paths.append((_time.time(), path_len, path_str, first_hop))
                        self._pending_rx_paths = self._pending_rx_paths[-20:]

                    # Path Update (type 8): payload starts with announcing node's pubkey (64 hex chars)
                    if payload_type == 8 and len(raw_str) >= payload_hex_start + 64 and decoded_path:
                        try:
                            pl8 = raw_str[payload_hex_start:]
                            src_pubkey = pl8[0:64]  # 32-byte pubkey
                            if src_pubkey and len(src_pubkey) == 64:
                                src_pfx = {src_pubkey[:n].upper() for n in (2, 4, 6) if len(src_pubkey) >= n}
                                filtered8 = [h for h in decoded_path if h["id"].upper() not in src_pfx]
                                route8 = " > ".join(h["id"] for h in filtered8)
                                hops8 = len(filtered8)
                                ck8 = src_pubkey[:4].upper() if len(src_pubkey) >= 4 else src_pubkey[:2].upper()
                                _now8 = time.time()
                                self._contact_routes[ck8] = (hops8, route8, _now8)
                                if len(src_pubkey) >= 4:
                                    self._contact_routes[src_pubkey[:2].upper()] = (hops8, route8, _now8)
                                self._suppress_sdk_paths = False
                                logger.debug(f"[path update] {src_pubkey[:8]}: hops={hops8}, path={route8 or 'direct'}")
                                for r_cfg in cfg.get_repeaters():
                                    rpk = r_cfg.get("pubkey", "").upper()
                                    if rpk and (rpk.startswith(src_pubkey[:8].upper()) or src_pubkey[:8].upper().startswith(rpk[:8])):
                                        self.store.update_route(rpk, hops8, route8)
                                        break
                        except Exception:
                            pass

                    advert_data = None
                    if payload_type == 4 and len(raw_str) >= payload_hex_start + 218:
                        pl = raw_str[payload_hex_start:]
                        # pubkey: bytes 0-31 (64 hex chars)
                        pubkey_hex = pl[0:64]
                        # timestamp: bytes 32-35 little-endian uint32
                        advert_ts = None
                        if len(pl) >= 72:
                            advert_ts = int.from_bytes(bytes.fromhex(pl[64:72]), 'little')
                        # app_flags: byte 100
                        app_flags = None
                        if len(pl) >= 202:
                            app_flags = int(pl[200:202], 16)
                        # lat/lon: bytes 101-104, 105-108, signed int32 little-endian / 1e6
                        advert_lat = advert_lon = None
                        if len(pl) >= 218:
                            lat_int = int.from_bytes(bytes.fromhex(pl[202:210]), 'little', signed=True)
                            lon_int = int.from_bytes(bytes.fromhex(pl[210:218]), 'little', signed=True)
                            advert_lat = round(lat_int / 1e6, 6)
                            advert_lon = round(lon_int / 1e6, 6)
                        # name: bytes 109+ (218 hex chars into payload)
                        name_hex = pl[218:] if len(pl) > 218 else ""
                        if len(name_hex) % 2:
                            name_hex = name_hex[:-1]
                        name_str = ""
                        if name_hex:
                            name_bytes = bytes.fromhex(name_hex)
                            null_pos = name_bytes.find(b'\x00')
                            if null_pos >= 0:
                                name_bytes = name_bytes[:null_pos]
                            name_str = name_bytes.decode('utf-8', errors='ignore').strip()
                        if len(name_str) >= 2:
                            node_label = name_str
                            if pubkey_hex:
                                self._cache_node_name(pubkey_hex[0:2], name_str)  # 1-byte hop key
                                if len(pubkey_hex) >= 4:
                                    self._cache_node_name(pubkey_hex[0:4], name_str)  # 2-byte hop key
                                if len(pubkey_hex) >= 6:
                                    self._cache_node_name(pubkey_hex[0:6], name_str)  # 3-byte hop key
                        if pubkey_hex:
                            self.store.upsert_advert_node(
                                pubkey=pubkey_hex,
                                name=name_str or pubkey_hex[:8],
                                lat=advert_lat if advert_lat and advert_lat != 0.0 else None,
                                lon=advert_lon if advert_lon and advert_lon != 0.0 else None,
                            )
                        advert_data = {
                            "pubkey": pubkey_hex,
                            "ts": advert_ts,
                            "flags": app_flags,
                            "lat": advert_lat,
                            "lon": advert_lon,
                            "name": name_str,
                        }

                        # Use the decoded RF path as a route update for this node
                        if pubkey_hex and path_len >= 0:
                            # Strip any hop whose ID matches the source node's own pubkey prefix
                            # (some firmware includes the source's own ID as the first hop)
                            src_prefixes = {pubkey_hex[:n].upper() for n in (2, 4, 6) if len(pubkey_hex) >= n}
                            filtered_path = [h for h in decoded_path if h["id"].upper() not in src_prefixes]
                            route_str = " > ".join(h["id"] for h in filtered_path)
                            filtered_hops = len(filtered_path)
                            cache_key = pubkey_hex[:4].upper() if len(pubkey_hex) >= 4 else pubkey_hex[:2].upper()
                            _now = time.time()
                            self._contact_routes[cache_key] = (filtered_hops, route_str, _now)
                            # Also index by 2-char prefix so get_mesh_contacts() fallback lookup finds it
                            if len(pubkey_hex) >= 4:
                                self._contact_routes[pubkey_hex[:2].upper()] = (filtered_hops, route_str, _now)
                            # New real path arrived — lift the post-reset suppression
                            self._suppress_sdk_paths = False
                            logger.debug(f"[advert path] {name_str or pubkey_hex[:8]}: hops={filtered_hops}, path={route_str or 'direct'}")
                            # Push route into store so SSE stream updates dashboard immediately
                            for r_cfg in cfg.get_repeaters():
                                rpk = r_cfg.get("pubkey", "").upper()
                                if rpk and (rpk.startswith(pubkey_hex[:8].upper()) or pubkey_hex[:8].upper().startswith(rpk[:8])):
                                    self.store.update_route(rpk, filtered_hops, route_str)
                                    break

                    # For non-advert or if name not found: use first hop as node label
                    if not node_label and decoded_path:
                        node_label = decoded_path[0]["name"]

                except (ValueError, IndexError):
                    pass

            self._log_event("rx", snr=snr, rssi=rssi, pkt_type=pkt_type_label,
                            route=route, raw=raw_str, node=node_label,
                            path=decoded_path, direct=route_type != 1,
                            advert=advert_data)
        except Exception as e:
            logger.debug(f"Error handling RX_LOG_DATA: {e}")

    async def _on_advertisement(self, event):
        """Handle node advertisement beacons — update name cache only.
        Advert packets appear in the Packets feed via _on_rx_log with decoded name and path."""
        try:
            payload = event.payload if hasattr(event, "payload") else {}
            if not isinstance(payload, dict):
                return
            pubkey = str(payload.get("pubkey", payload.get("public_key", "")))
            name = self._resolve_contact_name(pubkey) if pubkey else ""
            # Cache pubkey first byte → name so _on_rx_log can resolve node IDs
            if pubkey and len(pubkey) >= 2 and name and name != pubkey[:8]:
                self._cache_node_name(pubkey[:2], name)
                if len(pubkey) >= 4:
                    self._cache_node_name(pubkey[:4], name)
                if len(pubkey) >= 6:
                    self._cache_node_name(pubkey[:6], name)
        except Exception as e:
            logger.debug(f"Error handling ADVERTISEMENT: {e}")

    async def _on_companion_telemetry(self, event):
        """Handle telemetry event from the companion node — extract battery."""
        try:
            logger.info(f"[companion] telemetry event: type={getattr(event, 'type', None)!r} event={event!r}")
            bat = None
            # Check direct attributes first (BATTERY event format)
            for attr in ("bat", "bat_mv", "battery", "battery_mv", "voltage", "level"):
                val = getattr(event, attr, None)
                if val is not None:
                    bat = val
                    break
            # Fall back to payload dict
            if bat is None:
                payload = event.payload if hasattr(event, "payload") else {}
                if isinstance(payload, dict):
                    bat = (payload.get("bat") or payload.get("bat_mv") or
                           payload.get("battery") or payload.get("battery_mv") or
                           payload.get("voltage") or payload.get("level"))
            if bat is not None and float(bat) > 0:
                # Values < 10 are likely in volts — convert to mV
                mv = int(float(bat) * 1000) if float(bat) < 10 else int(float(bat))
                self._companion_battery_mv = mv
                logger.info(f"[companion] battery from telemetry event: {mv}mV")
        except Exception as e:
            logger.debug(f"Error handling companion telemetry: {e}")

    def _extract_ack_code(self, send_result) -> str:
        """Extract the expected ACK code from a send_msg / send_chan_msg result."""
        try:
            payload = send_result.payload if hasattr(send_result, "payload") else {}
            if not isinstance(payload, dict):
                return ""
            code = payload.get("expected_ack", payload.get("ack_code", payload.get("code", b"")))
            if isinstance(code, bytes):
                return code.hex()
            return str(code) if code else ""
        except Exception:
            return ""

    def _extract_hops_path(self, payload: dict) -> tuple:
        """Extract hop count and route path from a message event payload.
        Returns (hops: int, path: str) — hops is -1 if unknown."""
        # Try several field names used across firmware versions
        hops = payload.get("hops", payload.get("num_hops", payload.get("path_len", -1)))
        if hops is None:
            hops = -1
        try:
            hops = int(hops)
        except (TypeError, ValueError):
            hops = -1
        # 255 (0xFF) is MeshCore's sentinel for "unknown hop count"
        if hops == 255:
            hops = -1
        # Values >= 64 are encoded path_len_bytes: bits 7-6 = hash size, bits 5-0 = hop count
        # e.g. 0x42 = 2-byte hash mode, 2 hops — decode to real hop count
        elif hops >= 64:
            hops = hops & 0x3F

        raw_path = payload.get("path", payload.get("route", payload.get("out_path", "")))
        path_str = ""
        if isinstance(raw_path, bytes) and raw_path:
            bytes_per_node = 1
            if hops > 0:
                detected = len(raw_path) // hops
                if detected in (1, 2):
                    bytes_per_node = detected
            path_str = " > ".join(raw_path[i:i+bytes_per_node].hex()
                                  for i in range(0, len(raw_path), bytes_per_node))
        elif isinstance(raw_path, str) and raw_path:
            # If no spaces, treat as compact hex — segment same way as bytes
            if ' ' not in raw_path and hops > 0:
                chars_per_node = len(raw_path) // hops
                if chars_per_node in (2, 4):
                    segs = [raw_path[i:i+chars_per_node] for i in range(0, len(raw_path), chars_per_node)
                            if len(raw_path[i:i+chars_per_node]) == chars_per_node]
                    path_str = ' > '.join(segs)
                else:
                    path_str = raw_path  # unknown format — store as-is
            else:
                path_str = raw_path  # already formatted or single segment

        return hops, path_str

    def _decode_path_chips(self, path_str: str) -> list:
        """Convert a path string like 'C2 > 1A > 04' into [{id, name}, ...] using the name cache."""
        if not path_str:
            return []
        chips = []
        for seg in path_str.split(">"):
            seg = seg.strip().upper()
            if not seg:
                continue
            # Use first 2 hex chars (1 byte) as the node ID key
            node_id = seg[:2]
            name = self._node_id_name_cache.get(node_id, "")
            if not name:
                resolved = self._resolve_contact_name(node_id)
                name = resolved if resolved != node_id else ""
            chips.append({"id": node_id, "name": name or node_id})
        return chips

    def _resolve_contact_name(self, pubkey_prefix: str) -> str:
        if not pubkey_prefix:
            return "Unknown"
        for key, contact in self._contacts.items():
            pk = key if isinstance(key, str) else (key.hex() if isinstance(key, bytes) else str(key))
            if (pk.startswith(pubkey_prefix[:8]) or pubkey_prefix.startswith(pk[:8])):
                name = contact.get("name", "") if isinstance(contact, dict) else ""
                return name if name else pubkey_prefix[:8]
        return pubkey_prefix[:8]

    async def send_channel_message(self, channel_idx: int, text: str) -> dict:
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}
        try:
            result = await self.mc.commands.send_chan_msg(channel_idx, text)
            if result.type == EventType.ERROR:
                return {"ok": False, "error": str(result.payload)}
            ack_code = self._extract_ack_code(result)
            if not ack_code:
                logger.debug(f"[msg] Send result payload fields: {list(result.payload.keys()) if isinstance(getattr(result, 'payload', None), dict) else result.payload}")
            self.store.store_message("out", channel_idx, "", "", text, ack_code=ack_code)
            logger.info(f"[msg] Sent to channel {channel_idx} (ack={ack_code or 'none'}): {text[:60]}")
            return {"ok": True}
        except Exception as e:
            logger.error(f"Channel send error: {e}")
            return {"ok": False, "error": str(e)}

    async def send_contact_message(self, pubkey: str, text: str) -> dict:
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}
        contact = self._find_contact(pubkey)
        if contact is None and cfg.get_pull_contacts_on_start():
            await self._refresh_contacts()
            contact = self._find_contact(pubkey)
        if contact is None:
            return {"ok": False, "error": "Contact not found — may be out of range"}
        try:
            result = await self.mc.commands.send_msg(contact, text)
            if result.type == EventType.ERROR:
                return {"ok": False, "error": str(result.payload)}
            name = contact.get("name", pubkey[:8]) if isinstance(contact, dict) else pubkey[:8]
            ack_code = self._extract_ack_code(result)
            if not ack_code:
                logger.debug(f"[msg] Send result payload fields: {list(result.payload.keys()) if isinstance(getattr(result, 'payload', None), dict) else result.payload}")
            self.store.store_message("out", None, pubkey, name, text, ack_code=ack_code)
            logger.info(f"[msg] Sent to {name} (ack={ack_code or 'none'}): {text[:60]}")
            return {"ok": True}
        except Exception as e:
            logger.error(f"Contact send error: {e}")
            return {"ok": False, "error": str(e)}

    # --- Contacts ---

    async def _refresh_contacts(self, configured_only: bool = False):
        """Fetch contacts from companion to populate routing table.

        configured_only=True: keep only contacts that match a configured repeater pubkey.
        Used when pull_contacts_on_start is off so the poll loop can still reach repeaters
        without bulk-importing all mesh contacts.
        """
        try:
            result = await self.mc.commands.get_contacts()
            if result.type == EventType.ERROR:
                logger.error(f"get_contacts failed: {result.payload}")
                return

            raw = result.payload
            if isinstance(raw, dict):
                all_contacts = raw
            elif isinstance(raw, list):
                all_contacts = {}
                for c in raw:
                    pk = c.get("public_key", "")
                    if pk:
                        all_contacts[pk] = c
            else:
                all_contacts = {}

            if configured_only:
                cfg_pubkeys = [r["pubkey"].lower() for r in cfg.get_repeaters()]
                filtered = {}
                for pk, c in all_contacts.items():
                    pk_lower = pk.lower() if isinstance(pk, str) else pk.hex().lower()
                    for cpk in cfg_pubkeys:
                        if pk_lower.startswith(cpk[:8]) or cpk.startswith(pk_lower[:8]):
                            filtered[pk] = c
                            break
                self._contacts = filtered
                logger.info(f"Loaded {len(all_contacts)} total contacts; kept {len(self._contacts)} configured")
            else:
                self._contacts = all_contacts
                logger.info(f"Loaded {len(self._contacts)} contacts from companion")

            # Pre-populate node ID → name cache from loaded contacts
            for key, contact in self._contacts.items():
                pk = key if isinstance(key, str) else (key.hex() if isinstance(key, bytes) else str(key))
                name = contact.get("name", "") if isinstance(contact, dict) else ""
                if pk and name and len(pk) >= 2:
                    self._cache_node_name(pk[:2], name)
        except Exception as e:
            logger.error(f"Contact refresh failed: {e}")

    def _sync_route_to_store(self, pubkey_pre: str, hops: int, route_path: str):
        """If pubkey_pre matches a configured repeater, write the route back to the store."""
        if not route_path and hops < 0:
            return
        pre = pubkey_pre.lower()
        for r in cfg.get_repeaters():
            pk = (r.get("pubkey") or "").lower()
            if not pk:
                continue
            if pk.startswith(pre[:4]) or pre.startswith(pk[:4]):
                self.store.update_route(pk, hops, route_path)
                return

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

    def get_mesh_contacts(self) -> list:
        """Return all known mesh contacts (configured + unknown) with GPS and routing data."""
        # Build a lookup of pubkey-prefix → last_seen Unix epoch from RF advert events
        # These are the only reliable timestamps (device SDK timestamps are relative, not epoch)
        advert_ts = {}
        try:
            for node in self.store.get_advert_nodes():
                pk = (node.get("pubkey") or "").lower()
                ts = node.get("last_seen")
                if pk and ts:
                    advert_ts[pk] = float(ts)
                    advert_ts[pk[:2]] = float(ts)  # also index by 2-char prefix
        except Exception:
            pass

        result = []
        for key, contact in self._contacts.items():
            if not isinstance(contact, dict):
                continue
            pk = key if isinstance(key, str) else (key.hex() if isinstance(key, bytes) else str(key))
            lat = contact.get("adv_lat", 0.0) or 0.0
            lon = contact.get("adv_lon", 0.0) or 0.0
            out_path_len = contact.get("out_path_len", contact.get("hops", contact.get("path_len", -1)))
            hops = out_path_len if (out_path_len is not None and out_path_len >= 0) else -1
            # Values >= 64 are encoded path_len_bytes (bits 6-7 = hash size, bits 0-5 = hop count)
            # e.g. 0x42 = 2-byte mode, 2 hops;  0x02 = 1-byte mode, 2 hops
            sdk_hash_size = 0  # 0 = 1-byte hops, 1 = 2-byte hops
            if hops >= 64:
                sdk_hash_size = (hops >> 6) & 0x03
                hops = hops & 0x3F
            route_path = ""
            _now = time.time()
            # If SDK says direct (hops=0) that is definitive — never override with a cached path
            _sdk_direct = (hops == 0)
            if not _sdk_direct:
                # RF-observed cache takes priority over SDK path for multi-hop nodes.
                # If the cache entry is fresh use it; if expired, route is treated as unknown.
                # Only fall through to SDK path when we have NO cache entry at all.
                _cr_key = pk[:4].upper() if len(pk) >= 4 else pk[:2].upper()
                _cached = (self._contact_routes.get(_cr_key) or
                           self._contact_routes.get(pk[:2].upper()) or
                           self._contact_routes.get(pk.upper()))
                if _cached:
                    _age = _now - (_cached[2] if len(_cached) > 2 else 0)
                    if _age < self.ROUTE_TTL_SECONDS:
                        _cached_segs = len([s for s in _cached[1].split('>') if s.strip()]) if _cached[1] else 0
                        _sdk_hops = hops  # SDK-reported hop count (may be -1 if unknown)
                        # Only use cached path if it roughly agrees with SDK hop count
                        # (reject stale many-hop paths when SDK says 0 or 1 hop)
                        _path_ok = (_sdk_hops < 0 or abs(_cached_segs - _sdk_hops) <= 1)
                        if _path_ok:
                            if hops < 0:
                                hops = _cached[0]
                            route_path = _cached[1]
                    # else: cache expired → leave route_path empty (stale path hidden)
            else:
                # No RF-observed entry — fall back to SDK path
                raw_path = "" if self._suppress_sdk_paths else contact.get("out_path", contact.get("path", contact.get("route", "")))
                # Use configured node_id_chars as the primary chars-per-hop hint
                # when the SDK path_len byte doesn't encode hash_size (< 64).
                # This avoids "0675" being split as ["06","75"] when 2-byte mode is configured.
                _cfg_chars = cfg.get_settings().get("node_id_chars", 2)
                _cfg_bytes = max(1, _cfg_chars // 2)
                if isinstance(raw_path, bytes) and raw_path:
                    path_bytes = raw_path.rstrip(b'\x00')
                    if path_bytes:
                        if sdk_hash_size >= 1:
                            bytes_per_node = sdk_hash_size + 1
                        else:
                            # Prefer configured mode; only auto-detect as fallback
                            bytes_per_node = _cfg_bytes
                            if len(path_bytes) % bytes_per_node != 0 and hops > 0:
                                detected = len(path_bytes) // hops
                                if detected in (1, 2, 3):
                                    bytes_per_node = detected
                        route_path = " > ".join(
                            path_bytes[i:i+bytes_per_node].hex().upper()
                            for i in range(0, len(path_bytes), bytes_per_node)
                            if len(path_bytes[i:i+bytes_per_node]) == bytes_per_node
                        )
                elif isinstance(raw_path, str) and raw_path:
                    path_str = raw_path
                    while len(path_str) >= 2 and path_str[-2:].upper() == '00':
                        path_str = path_str[:-2]
                    if path_str:
                        if sdk_hash_size >= 1:
                            chars_per_node = (sdk_hash_size + 1) * 2
                        else:
                            # Prefer configured mode; only auto-detect as fallback
                            chars_per_node = _cfg_chars
                            if len(path_str) % chars_per_node != 0 and hops > 0:
                                detected = len(path_str) // hops
                                if detected in (2, 4, 6):
                                    chars_per_node = detected
                        segs = [path_str[i:i+chars_per_node].upper() for i in range(0, len(path_str), chars_per_node)
                                if len(path_str[i:i+chars_per_node]) == chars_per_node]
                        route_path = " > ".join(segs)
            # Try several possible name fields the meshcore SDK may use
            name = (
                contact.get("name") or
                contact.get("adv_name") or
                contact.get("short_name") or
                contact.get("display_name") or
                ""
            )
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                name = pk[:8]   # fall back to pubkey prefix so it's never blank
            # Use advert_nodes table timestamp (proper Unix epoch from our server clock)
            # The SDK's last_advert is device-relative, not an epoch — don't use it for display
            last_seen = (
                advert_ts.get(pk.lower()) or
                advert_ts.get(pk[:2].lower()) or
                None
            )
            result.append({
                "pubkey": pk,
                "name": name,
                "lat": lat,
                "lon": lon,
                "hops": hops,
                "route_path": route_path,
                "last_seen": last_seen,
            })
        return result

    def remove_contact(self, pubkey: str):
        """Remove a contact from the in-memory contacts dict by pubkey prefix."""
        pk_lower = pubkey.lower()
        to_delete = [k for k in self._contacts if str(k).lower().startswith(pk_lower)]
        for k in to_delete:
            del self._contacts[k]

    async def _interruptible_sleep(self, seconds: float):
        """Sleep for up to `seconds`, waking immediately if a disconnect/reconnect is requested."""
        remaining = seconds
        while remaining > 0 and self._running and not self._needs_reconnect and not self._stay_disconnected:
            await asyncio.sleep(min(remaining, 0.5))
            remaining -= 0.5

    async def _poll_all_repeaters(self, repeaters: list):
        """Poll each configured repeater with staggered delays."""
        if cfg.get_pull_contacts_on_start():
            await self._refresh_contacts(configured_only=False)
        else:
            await self._refresh_contacts(configured_only=True)
        stagger = cfg.get_stagger_delay()

        for i, repeater_cfg in enumerate(repeaters):
            if not self._running or self._needs_reconnect or self._stay_disconnected:
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
            # MeshCore library uses out_path_len / out_path (not hops/path)
            # out_path_len: -1 = flood routing, 0 = direct (1 hop), N = N intermediate nodes
            hops = 0
            route_path = ""
            if isinstance(contact, dict):
                out_path_len = contact.get("out_path_len",
                                contact.get("hops",
                                contact.get("path_len", -1)))
                if out_path_len is not None and out_path_len >= 0:
                    hops = out_path_len
                    # Values >= 64 are encoded path_len_bytes (bits 6-7 = hash size, bits 0-5 = hop count)
                    if hops >= 64:
                        hops = hops & 0x3F
                    raw_path = contact.get("out_path",
                                contact.get("path",
                                contact.get("route", "")))
                    if isinstance(raw_path, str) and raw_path:
                        # Strip trailing '00' zero padding from fixed-size SDK buffer
                        path_str = raw_path
                        while len(path_str) >= 2 and path_str[-2:].upper() == '00':
                            path_str = path_str[:-2]
                        if path_str:
                            chars_per_node = 2
                            if hops > 0:
                                detected = len(path_str) // hops
                                if detected in (2, 4):
                                    chars_per_node = detected
                            segments = [path_str[i:i+chars_per_node] for i in range(0, len(path_str), chars_per_node)
                                        if len(path_str[i:i+chars_per_node]) == chars_per_node]
                            route_path = " > ".join(segments)
                    elif isinstance(raw_path, bytes) and raw_path:
                        # Strip trailing zero padding from fixed-size SDK buffer
                        path_bytes = raw_path.rstrip(b'\x00')
                        if path_bytes:
                            bytes_per_node = 1
                            if hops > 0:
                                detected = len(path_bytes) // hops
                                if detected in (1, 2):
                                    bytes_per_node = detected
                            route_path = " > ".join(path_bytes[i:i+bytes_per_node].hex() for i in range(0, len(path_bytes), bytes_per_node))
                    elif isinstance(raw_path, list) and raw_path:
                        route_path = " > ".join(f"{b:02x}" if isinstance(b, int) else str(b) for b in raw_path)
            elif hasattr(contact, "out_path_len"):
                opl = contact.out_path_len
                hops = opl if opl >= 0 else 0
                route_path = getattr(contact, "out_path", "") or ""
            elif hasattr(contact, "hops"):
                hops = contact.hops
            self.store.update_route(pubkey, hops, route_path)

            # If we know there are intermediate hops but the contact has no path data,
            # run a path discovery request to find the actual route.
            if hops > 0 and not route_path:
                await self._discover_path(contact, pubkey, name)

            # Extract GPS coordinates from contact if available
            if isinstance(contact, dict):
                lat = contact.get("adv_lat", 0.0) or 0.0
                lon = contact.get("adv_lon", 0.0) or 0.0
                if lat != 0.0 or lon != 0.0:
                    self.store.update_location(pubkey, lat, lon)

            route_desc = route_path if route_path else ("direct" if hops > 0 else "flood")
            logger.info(f"[{name}] Polling repeater ({i+1}/{len(repeaters)}), hops={hops}, route={route_desc}...")

            # Apply custom path if configured, otherwise use flood
            custom_path = repeater_cfg.get("path", "").strip()
            await self._apply_path(contact, pubkey, name, custom_path)

            # Login to repeater before requesting data
            # If admin_pass is absent or empty string, treat as guest (skip login)
            admin_pass = repeater_cfg.get("admin_pass", "password")
            if admin_pass:
                await self._login_to_repeater(contact, name, admin_pass)
            await self._interruptible_sleep(3)
            if not self._running or self._needs_reconnect or self._stay_disconnected:
                break

            await self._request_status(pubkey, name, contact)
            await self._interruptible_sleep(2)
            if not self._running or self._needs_reconnect or self._stay_disconnected:
                break

            await self._request_telemetry(pubkey, name, contact)

            # Check clock every 5 poll cycles (first cycle + every 5th after)
            self._clock_poll_counts[pubkey] = self._clock_poll_counts.get(pubkey, 0) + 1
            if self._clock_poll_counts[pubkey] % 5 == 1:
                await self._request_clock(pubkey, name, contact)

            if i < len(repeaters) - 1:
                logger.debug(f"Waiting {stagger}s before next repeater")
                await self._interruptible_sleep(stagger)

    async def _discover_path_for_contact(self, contact, pubkey: str, name: str):
        """Path discovery for non-configured contacts — stores result in _contact_routes."""
        try:
            result = await self.mc.commands.send_path_discovery(contact)
            if result.type == EventType.ERROR:
                return
            response = await self.mc.wait_for_event(
                EventType.PATH_RESPONSE,
                attribute_filters={"pubkey_pre": pubkey[:12]},
                timeout=10,
            )
            if response is None:
                return
            payload = response.payload
            new_hops = payload.get("out_path_len", -1)
            raw_path = payload.get("out_path", "")
            if new_hops >= 0:
                disc_route = ""
                if isinstance(raw_path, str) and raw_path:
                    segs = [raw_path[i:i+2] for i in range(0, len(raw_path), 2) if len(raw_path[i:i+2]) == 2]
                    # Strip hops that match the destination's own pubkey prefix
                    src_pfx = {pubkey[:n].upper() for n in (2, 4, 6) if len(pubkey) >= n}
                    segs = [s for s in segs if s.upper() not in src_pfx]
                    disc_route = " > ".join(segs)
                elif isinstance(raw_path, bytes) and raw_path:
                    segs_b = [raw_path[i:i+1].hex() for i in range(len(raw_path))]
                    src_pfx = {pubkey[:n].upper() for n in (2, 4, 6) if len(pubkey) >= n}
                    disc_route = " > ".join(s for s in segs_b if s.upper() not in src_pfx)
                self._contact_routes[pubkey.upper()[:4]] = (new_hops, disc_route, time.time())
                self._sync_route_to_store(pubkey, new_hops, disc_route)
                logger.info(f"[contact path] {name}: hops={new_hops}, path={disc_route or 'direct'}")
                self._log_event("path", name=name, pubkey=pubkey, hops=new_hops, route=disc_route)
        except Exception as e:
            logger.debug(f"[contact path] {name} discovery error: {e}")

    async def _discover_path(self, contact, pubkey: str, name: str):
        """Run a path discovery request to determine the actual route to a repeater."""
        try:
            result = await self.mc.commands.send_path_discovery(contact)
            if result.type == EventType.ERROR:
                logger.debug(f"[{name}] Path discovery send failed: {result.payload}")
                return
            # Wait up to 10s for the PATH_RESPONSE event
            response = await self.mc.wait_for_event(
                EventType.PATH_RESPONSE,
                attribute_filters={"pubkey_pre": pubkey[:12]},
                timeout=10,
            )
            if response is None:
                logger.debug(f"[{name}] Path discovery timed out")
                return
            payload = response.payload
            new_hops = payload.get("out_path_len", -1)
            raw_path = payload.get("out_path", "")
            if new_hops >= 0:
                disc_route = ""
                if isinstance(raw_path, str) and raw_path:
                    segs = [raw_path[i:i+2] for i in range(0, len(raw_path), 2) if len(raw_path[i:i+2]) == 2]
                    disc_route = " > ".join(segs)
                self.store.update_route(pubkey, new_hops, disc_route)
                logger.info(f"[{name}] Path discovered: hops={new_hops}, path={disc_route or 'direct'}")
                self._log_event("path", name=name, pubkey=pubkey, hops=new_hops, route=disc_route)
        except Exception as e:
            logger.debug(f"[{name}] Path discovery error: {e}")

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
            # Prefer send_login_sync (waits for mesh confirmation) if available
            if hasattr(self.mc.commands, "send_login_sync"):
                result = await self.mc.commands.send_login_sync(contact, password)
                if result is None:
                    logger.warning(f"[{name}] Login timed out")
                    return False
            else:
                result = await self.mc.commands.send_login(contact, password)
                if result.type == EventType.ERROR:
                    logger.warning(f"[{name}] Login failed: {result.payload}")
                    return False
            logger.info(f"[{name}] Login OK (pwd={'default' if password == 'password' else 'custom'})")
            return True
        except Exception as e:
            logger.error(f"[{name}] Login error: {e}")
            return False

    async def _request_status(self, pubkey: str, name: str, contact):
        """Request status from a repeater and update the store."""
        try:
            _t0 = time.monotonic()
            status = await self.mc.commands.req_status_sync(contact, timeout=30)
            _latency_ms = int((time.monotonic() - _t0) * 1000)
            if status is None:
                logger.warning(f"[{name}] Status request timed out")
                self.store.mark_poll_failed(pubkey)
                # Alert: node went offline (only once per offline period)
                state = self._alert_state.setdefault(pubkey, {})
                if not state.get("offline"):
                    state["offline"] = True
                    self.store.log_alert(name, f"{name} is not responding to polls (offline)")
                return

            updates = {}
            updates["_latency_ms"] = _latency_ms

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

            # Firmware version — try several key names used across firmware versions
            for fw_key in ("fw_version", "fw_ver", "firmware_version", "firmware", "version"):
                if fw_key in status and status[fw_key]:
                    updates["fw_version"] = str(status[fw_key])
                    break

            if updates:
                latency_ms = updates.pop("_latency_ms", None)
                self.store.update_repeater(pubkey, latency_ms=latency_ms, **updates)
                logger.info(
                    f"[{name}] Status: {updates.get('battery_mv', '?')}mV, "
                    f"RSSI={updates.get('rssi', '?')}dBm, "
                    f"SNR={updates.get('snr', '?')}dB, "
                    f"latency={latency_ms}ms"
                )
                # Validate stored route against SDK hop count — clear it if stale
                contact_data = self._contacts.get(pubkey, self._contacts.get(pubkey.lower(), {}))
                if isinstance(contact_data, dict):
                    opl = contact_data.get("out_path_len", -1)
                    sdk_poll_hops = (opl & 0x3F) if opl >= 64 else (opl if opl >= 0 else -1)
                    if sdk_poll_hops >= 0:
                        stored = self.store.get_route_by_prefix(pubkey[:8])
                        if stored and stored[1]:
                            segs = [s for s in stored[1].split('>') if s.strip()]
                            if abs(len(segs) - sdk_poll_hops) > 1:
                                self.store.update_route(pubkey, sdk_poll_hops, "")
                                logger.debug(f"[{name}] Cleared stale route ({len(segs)} segs vs {sdk_poll_hops} hops)")

                # Alert tracking
                state = self._alert_state.setdefault(pubkey, {})
                # Node came back online
                if state.get("offline"):
                    state["offline"] = False
                    self.store.log_alert(name, f"{name} is back online")
                # Low battery alert (below 20%, once per low-bat period)
                bat_mv = updates.get("battery_mv", 0)
                if bat_mv > 0:
                    from data_store import RepeaterState
                    low_thresh = 3500  # ~20% LiPo
                    if bat_mv < low_thresh and not state.get("low_bat"):
                        state["low_bat"] = True
                        pct = max(0, min(100, round(((bat_mv - 3000) / 1200) * 100)))
                        self.store.log_alert(name, f"{name} battery low: {pct}% ({bat_mv/1000:.2f}V)")
                    elif bat_mv >= low_thresh:
                        state["low_bat"] = False

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
                elif sensor_type == "temperature" and value is not None:
                    updates["temperature_c"] = round(float(value), 1)

            if updates:
                self.store.update_repeater(pubkey, **updates)
                logger.info(f"[{name}] Telemetry: {updates}")

        except Exception as e:
            logger.error(f"[{name}] Telemetry request error: {e}")

    async def _request_clock(self, pubkey: str, name: str, contact) -> None:
        """Send clock command to a logged-in repeater and store the time offset vs system."""
        try:
            from datetime import datetime, timezone as _dtz
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            pk_pre = pubkey[:12].lower()

            def _on_clock_resp(event):
                if future.done():
                    return
                payload = event.payload if hasattr(event, "payload") else {}
                if not isinstance(payload, dict):
                    return
                epk = str(payload.get("pubkey_prefix", "")).lower()
                if epk and (epk.startswith(pk_pre[:8]) or pk_pre.startswith(epk[:8])):
                    txt = payload.get("text", "")
                    if txt:
                        loop.call_soon_threadsafe(future.set_result, txt)

            sub = self.mc.subscribe(EventType.CONTACT_MSG_RECV, _on_clock_resp)
            try:
                self._dont_save_contact_msgs = True
                await self.mc.commands.send_cmd(contact, "clock")
                response = await asyncio.wait_for(future, timeout=15.0)
            except asyncio.TimeoutError:
                logger.debug(f"[{name}] clock command timed out")
                return
            finally:
                self.mc.unsubscribe(sub)
                self._dont_save_contact_msgs = False

            if not response:
                return
            response = response.strip()

            # Try plain integer (Unix timestamp)
            node_epoch = None
            try:
                node_epoch = int(response.split()[0])
            except (ValueError, IndexError):
                pass
            # Try common datetime formats
            if node_epoch is None:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(response[:19], fmt)
                        node_epoch = int(dt.replace(tzinfo=_dtz.utc).timestamp())
                        break
                    except ValueError:
                        continue
            if node_epoch is None:
                logger.debug(f"[{name}] Could not parse clock response: {response!r}")
                return

            offset_s = round(node_epoch - time.time(), 1)
            self.store.update_repeater(pubkey, clock_offset_s=offset_s)
            logger.info(f"[{name}] Clock offset: {offset_s:+.1f}s")
        except Exception as e:
            logger.debug(f"[{name}] Clock request error: {e}")

    async def ping_repeater(self, pubkey: str) -> dict:
        """Request fresh status and telemetry from a repeater, updating the store."""
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}

        contact = self._find_contact(pubkey)
        if contact is None:
            await self._refresh_contacts(configured_only=not cfg.get_pull_contacts_on_start())
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
            if admin_pass:
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

    async def reboot_device(self) -> dict:
        """Send reboot command to the companion device."""
        if not self.mc:
            return {"ok": False, "error": "Not connected"}
        try:
            await self.mc.commands.reboot()
            logger.info("[companion] reboot command sent")
            self._needs_reconnect = True
            return {"ok": True}
        except Exception as e:
            logger.error(f"[companion] reboot failed: {e}")
            return {"ok": False, "error": str(e)}

    async def get_node_id_mode(self) -> dict:
        """Read path_hash_mode from the companion device (0=1-byte, 1=2-byte, 2=3-byte)."""
        if not self.mc:
            # Fall back to inferring mode from saved node_id_chars
            chars = cfg.get_settings().get("node_id_chars", 2)
            mode = max(0, (int(chars) // 2) - 1)
            return {"ok": True, "mode": mode, "inferred": True}
        if not hasattr(self.mc.commands, "get_path_hash_mode"):
            chars = cfg.get_settings().get("node_id_chars", 2)
            mode = max(0, (int(chars) // 2) - 1)
            logger.info("[companion] SDK does not support get_path_hash_mode, using config value")
            return {"ok": True, "mode": mode, "inferred": True}
        try:
            mode = await self.mc.commands.get_path_hash_mode()
            return {"ok": True, "mode": mode}
        except Exception as e:
            logger.info(f"[companion] get_path_hash_mode failed: {e}")
            chars = cfg.get_settings().get("node_id_chars", 2)
            mode = max(0, (int(chars) // 2) - 1)
            return {"ok": True, "mode": mode, "inferred": True}

    async def set_node_id_mode(self, mode: int) -> dict:
        """Set path_hash_mode on the companion device (0=1-byte, 1=2-byte, 2=3-byte)."""
        if not self.mc:
            return {"ok": False, "error": "Not connected"}
        if not hasattr(self.mc.commands, "set_path_hash_mode"):
            return {"ok": False, "error": "SDK version does not support set_path_hash_mode — update meshcore package"}
        try:
            event = await self.mc.commands.set_path_hash_mode(mode)
            if event and event.type == EventType.ERROR:
                return {"ok": False, "error": str(event.payload)}
            logger.info(f"[companion] path_hash_mode set to {mode}")
            return {"ok": True, "mode": mode}
        except Exception as e:
            logger.error(f"[companion] set_path_hash_mode failed: {e}")
            return {"ok": False, "error": str(e)}

    async def send_advert(self, pubkey: str) -> dict:
        """Login to a repeater and command it to broadcast a flood advertisement."""
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}

        contact = self._find_contact(pubkey)
        if contact is None:
            await self._refresh_contacts(configured_only=not cfg.get_pull_contacts_on_start())
            contact = self._find_contact(pubkey)
        if contact is None:
            return {"ok": False, "error": "Repeater not found in contacts — may be out of range"}

        name = pubkey[:8]
        admin_pass = "password"
        for r in cfg.get_repeaters():
            pk = r["pubkey"]
            if pk == pubkey or pk.startswith(pubkey) or pubkey.startswith(pk):
                admin_pass = r.get("admin_pass", "password")
                name = r.get("name", name)
                break

        try:
            if admin_pass:
                await self._login_to_repeater(contact, name, admin_pass)
                await asyncio.sleep(5.0)  # Wait for login to propagate over RF before sending command
            await self.mc.commands.send_cmd(contact, "advert")
            logger.info(f"[{name}] Flood advertisement command sent")
            self._log_event("advert_sent", name=name, pubkey=pubkey)
            return {"ok": True}
        except Exception as e:
            logger.error(f"[{name}] Failed to send advert: {e}")
            return {"ok": False, "error": str(e)}

    async def login_repeater(self, pubkey: str) -> dict:
        """Login to a repeater and return success/failure."""
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}

        contact = self._find_contact(pubkey)
        if contact is None:
            await self._refresh_contacts(configured_only=not cfg.get_pull_contacts_on_start())
            contact = self._find_contact(pubkey)
        if contact is None:
            return {"ok": False, "error": "Repeater not found — may be out of range"}

        name = pubkey[:8]
        admin_pass = "password"
        for r in cfg.get_repeaters():
            pk = r["pubkey"]
            if pk == pubkey or pk.startswith(pubkey) or pubkey.startswith(pk):
                admin_pass = r.get("admin_pass", "password")
                name = r.get("name", name)
                break

        success = await self._login_to_repeater(contact, name, admin_pass)
        if not success:
            return {"ok": False, "error": f"Repeater unreachable — may be out of range"}

        return {"ok": True, "name": name}

    async def send_repeater_cmd(self, pubkey: str, cmd: str) -> dict:
        """Login to a repeater and send an arbitrary CLI command."""
        if not self.mc:
            return {"ok": False, "error": "Not connected to companion device"}

        contact = self._find_contact(pubkey)
        if contact is None:
            await self._refresh_contacts(configured_only=not cfg.get_pull_contacts_on_start())
            contact = self._find_contact(pubkey)
        if contact is None:
            return {"ok": False, "error": "Repeater not found — may be out of range"}

        name = pubkey[:8]
        admin_pass = "password"
        for r in cfg.get_repeaters():
            pk = r["pubkey"]
            if pk == pubkey or pk.startswith(pubkey) or pubkey.startswith(pk):
                admin_pass = r.get("admin_pass", "password")
                name = r.get("name", name)
                break

        try:
            # Commands that reboot the repeater never return a text response
            if cmd.lower().strip() in ("reboot", "clkreboot", "poweroff", "shutdown"):
                await self.mc.commands.send_cmd(contact, cmd)
                logger.info(f"[{name}] CMD sent (no response expected): {cmd}")
                self.store.log_cli(name, cmd, None)
                return {"ok": True, "response": f"({cmd} does not return a response)"}

            # Subscribe to CONTACT_MSG_RECV before sending so we never miss a fast reply.
            # We don't filter by pubkey_prefix here — the format varies by firmware version
            # and we guard against saving unrelated messages via _dont_save_contact_msgs.
            loop = asyncio.get_running_loop()
            future = loop.create_future()

            def on_cmd_response(event):
                if future.done():
                    return
                payload = event.payload if hasattr(event, "payload") else {}
                if not isinstance(payload, dict):
                    return
                txt = payload.get("text", "")
                if txt:
                    loop.call_soon_threadsafe(future.set_result, txt)

            sub = self.mc.subscribe(EventType.CONTACT_MSG_RECV, on_cmd_response)
            try:
                self._dont_save_contact_msgs = True
                await self.mc.commands.send_cmd(contact, cmd)
                logger.info(f"[{name}] CMD sent: {cmd}")
                response = await asyncio.wait_for(future, timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{name}] CMD response timed out after 15s: {cmd}")
                response = None
            finally:
                self.mc.unsubscribe(sub)
                self._dont_save_contact_msgs = False

            self.store.log_cli(name, cmd, response)
            if response is None:
                return {"ok": False, "error": f"No response from {name} (timed out)"}
            return {"ok": True, "response": response}
        except Exception as e:
            self._dont_save_contact_msgs = False
            logger.error(f"[{name}] CMD error: {e}")
            return {"ok": False, "error": str(e)}
