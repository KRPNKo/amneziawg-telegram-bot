import asyncio
import html
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp
import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv


load_dotenv()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("amneziawg-telegram-bot")

current_bot_version = 'v26.06.11 RC4'
DEFAULT_DNS_SERVERS = ["1.1.1.1", "1.0.0.1", "8.8.8.8", "9.9.9.9"]
MAX_CLIENTS_24 = 253
LIMIT_PERIODS = {"never", "day", "week", "month"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    bootstrap_owner_id: int
    awg_web_base_url: str
    awg_web_api_token: str
    bot_db: str
    request_timeout: int
    peers_page_size: int
    events_limit: int
    traffic_check_interval: int


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    owner_raw = os.getenv("BOOTSTRAP_OWNER_ID", "").strip()
    api_token = os.getenv("AWG_WEB_API_TOKEN", "").strip()

    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN")
    if not owner_raw.isdigit():
        raise RuntimeError("BOOTSTRAP_OWNER_ID должен быть числом")
    if not api_token:
        raise RuntimeError("Не задан AWG_WEB_API_TOKEN")

    return Config(
        bot_token=bot_token,
        bootstrap_owner_id=int(owner_raw),
        awg_web_base_url=os.getenv("AWG_WEB_BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/") + "/",
        awg_web_api_token=api_token,
        bot_db=os.getenv("BOT_DB", "/root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3").strip(),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        peers_page_size=int(os.getenv("PEERS_PAGE_SIZE", "8")),
        events_limit=int(os.getenv("EVENTS_LIMIT", "20")),
        traffic_check_interval=int(os.getenv("TRAFFIC_CHECK_INTERVAL", "300")),
    )


cfg = load_config()
router = Router()


class BotState(StatesGroup):
    waiting_new_user_name = State()
    waiting_admin_id = State()
    waiting_remove_admin_id = State()
    waiting_i_value = State()
    waiting_dns_preset_name = State()
    waiting_dns_preset_servers = State()
    waiting_peer_display_name = State()
    waiting_peer_comment = State()
    waiting_limit_bytes = State()


class ApiError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_now_display() -> str:
    return datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")


def format_datetime_local(value: Any) -> str:
    if value is None:
        return "—"

    text = str(value).strip()
    if not text:
        return "—"

    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y/%m/%d %H:%M:%S")
    except ValueError:
        return text


def current_period_key(period: str) -> str:
    now = datetime.now(timezone.utc)
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period == "week":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"
    if period == "month":
        return now.strftime("%Y-%m")
    return "never"


def h(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def fmt_bytes(value: Any) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return "—"

    units = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024
        idx += 1

    if idx == 0:
        return f"{int(num)} {units[idx]}"
    return f"{num:.2f} {units[idx]}"


def parse_bytes(text: str) -> Optional[int]:
    value = text.strip().replace(",", ".")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgtp]?b?|[кмгтп]?б?)?", value, flags=re.IGNORECASE)
    if not match:
        return None

    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()

    multipliers = {
        "": 1,
        "b": 1,
        "б": 1,
        "kb": 1024,
        "кб": 1024,
        "k": 1024,
        "к": 1024,
        "mb": 1024**2,
        "мб": 1024**2,
        "m": 1024**2,
        "м": 1024**2,
        "gb": 1024**3,
        "гб": 1024**3,
        "g": 1024**3,
        "г": 1024**3,
        "tb": 1024**4,
        "тб": 1024**4,
        "t": 1024**4,
        "т": 1024**4,
        "pb": 1024**5,
        "пб": 1024**5,
        "p": 1024**5,
        "п": 1024**5,
    }

    multiplier = multipliers.get(unit)
    if multiplier is None:
        return None

    result = int(number * multiplier)
    return result if result > 0 else None


def status_icon(status: str) -> str:
    status = (status or "").lower()
    if status == "online":
        return "🟢"
    if status == "inactive":
        return "🟡"
    if status == "never":
        return "⚪"
    if status == "disabled":
        return "🔴"
    return "⚫"


def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    return name[:80] or "client"


def peer_title(peer: dict[str, Any]) -> str:
    return str(
        peer.get("name")
        or peer.get("friendly_name")
        or peer.get("config_name")
        or f"peer-{str(peer.get('public_key', ''))[:8]}"
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_input")]]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]]
    )


class BotDB:
    def __init__(self, path: str, bootstrap_owner_id: int) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap_owner_id = bootstrap_owner_id
        self.init()

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init(self) -> None:
        with closing(self.conn()) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_admins (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    role TEXT NOT NULL CHECK(role IN ('owner','admin')),
                    added_by INTEGER,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dns_presets (
                    name TEXT PRIMARY KEY,
                    servers TEXT NOT NULL,
                    created_by INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_telegram_id INTEGER,
                    actor_username TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    target_name TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peer_daily_usage (
                    day TEXT NOT NULL,
                    peer_id INTEGER NOT NULL,
                    peer_name TEXT,
                    rx_bytes INTEGER NOT NULL DEFAULT 0,
                    tx_bytes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(day, peer_id)
                );

                CREATE TABLE IF NOT EXISTS server_daily_usage (
                    day TEXT PRIMARY KEY,
                    rx_bytes INTEGER NOT NULL DEFAULT 0,
                    tx_bytes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_created_peers (
                    peer_id INTEGER PRIMARY KEY,
                    public_key TEXT,
                    peer_name TEXT,
                    created_at TEXT NOT NULL,
                    created_by_telegram_id INTEGER,
                    created_by_username TEXT
                );

                CREATE TABLE IF NOT EXISTS pending_created_clients (
                    client_name TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    created_by_telegram_id INTEGER,
                    created_by_username TEXT
                );

                CREATE TABLE IF NOT EXISTS peer_traffic_limits (
                    peer_id INTEGER PRIMARY KEY,
                    peer_name TEXT,
                    period TEXT NOT NULL CHECK(period IN ('never','day','week','month')),
                    limit_bytes INTEGER NOT NULL,
                    current_period_key TEXT NOT NULL,
                    bot_disabled INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

            owner_exists = con.execute(
                "SELECT 1 FROM bot_admins WHERE telegram_id = ?",
                (self.bootstrap_owner_id,),
            ).fetchone()

            if owner_exists is None:
                con.execute(
                    """
                    INSERT INTO bot_admins
                    (telegram_id, username, full_name, role, added_by, added_at)
                    VALUES (?, ?, ?, 'owner', NULL, ?)
                    """,
                    (self.bootstrap_owner_id, None, "bootstrap owner", now_iso()),
                )

            cloudflare = con.execute("SELECT servers FROM dns_presets WHERE name = 'cloudflare'").fetchone()
            if cloudflare is None:
                con.execute(
                    """
                    INSERT INTO dns_presets
                    (name, servers, created_by, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "cloudflare",
                        json.dumps(DEFAULT_DNS_SERVERS, ensure_ascii=False),
                        self.bootstrap_owner_id,
                        now_iso(),
                    ),
                )
            else:
                try:
                    existing = json.loads(cloudflare["servers"])
                except Exception:
                    existing = []
                if existing == ["1.1.1.1", "1.0.0.1"]:
                    con.execute(
                        "UPDATE dns_presets SET servers = ? WHERE name = 'cloudflare'",
                        (json.dumps(DEFAULT_DNS_SERVERS, ensure_ascii=False),),
                    )

            con.execute(
                """
                INSERT OR IGNORE INTO bot_settings
                (key, value, updated_by, updated_at)
                VALUES ('active_dns_preset', 'cloudflare', ?, ?)
                """,
                (self.bootstrap_owner_id, now_iso()),
            )
            con.execute(
                """
                INSERT OR IGNORE INTO bot_settings
                (key, value, updated_by, updated_at)
                VALUES ('remove_ipv6_from_address', '1', ?, ?)
                """,
                (self.bootstrap_owner_id, now_iso()),
            )
            con.commit()

    def get_admin(self, telegram_id: int) -> Optional[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute("SELECT * FROM bot_admins WHERE telegram_id = ?", (telegram_id,)).fetchone()

    def is_admin(self, telegram_id: Optional[int]) -> bool:
        return bool(telegram_id and self.get_admin(telegram_id))

    def is_owner(self, telegram_id: Optional[int]) -> bool:
        if not telegram_id:
            return False
        row = self.get_admin(telegram_id)
        return bool(row and row["role"] == "owner")

    def list_admins(self) -> list[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute("SELECT * FROM bot_admins ORDER BY role DESC, telegram_id").fetchall()

    def add_admin(
        self,
        telegram_id: int,
        role: str,
        added_by: int,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> None:
        if role not in {"owner", "admin"}:
            raise ValueError("role must be owner/admin")

        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO bot_admins
                (telegram_id, username, full_name, role, added_by, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    role = excluded.role,
                    username = excluded.username,
                    full_name = excluded.full_name
                """,
                (telegram_id, username, full_name, role, added_by, now_iso()),
            )
            con.commit()

    def remove_admin(self, telegram_id: int) -> None:
        with closing(self.conn()) as con:
            row = con.execute("SELECT role FROM bot_admins WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if row and row["role"] == "owner":
                owners = con.execute("SELECT COUNT(*) AS c FROM bot_admins WHERE role = 'owner'").fetchone()["c"]
                if owners <= 1:
                    raise ValueError("нельзя удалить последнего owner")
            con.execute("DELETE FROM bot_admins WHERE telegram_id = ?", (telegram_id,))
            con.commit()

    def set_setting(self, key: str, value: Optional[str], updated_by: int) -> None:
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO bot_settings (key, value, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_by, now_iso()),
            )
            con.commit()

    def get_setting(self, key: str) -> Optional[str]:
        with closing(self.conn()) as con:
            row = con.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def bool_setting(self, key: str, default: bool) -> bool:
        value = self.get_setting(key)
        if value is None:
            return default
        return value == "1"

    def get_i_values(self) -> dict[str, Optional[str]]:
        return {f"I{i}": self.get_setting(f"i{i}") for i in range(1, 6)}

    def set_i_value(self, i_num: int, value: Optional[str], actor: int) -> None:
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO bot_settings (key, value, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (f"i{i_num}", value, actor, now_iso()),
            )

            if i_num == 1 and not value:
                for n in range(2, 6):
                    con.execute(
                        """
                        INSERT INTO bot_settings (key, value, updated_by, updated_at)
                        VALUES (?, NULL, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = NULL,
                            updated_by = excluded.updated_by,
                            updated_at = excluded.updated_at
                        """,
                        (f"i{n}", actor, now_iso()),
                    )
            con.commit()

    def list_dns_presets(self) -> list[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute("SELECT * FROM dns_presets ORDER BY name").fetchall()

    def get_dns_preset(self, name: str) -> Optional[list[str]]:
        with closing(self.conn()) as con:
            row = con.execute("SELECT servers FROM dns_presets WHERE name = ?", (name,)).fetchone()
            if not row:
                return None
            try:
                data = json.loads(row["servers"])
                return [str(x).strip() for x in data if str(x).strip()]
            except Exception:
                return None

    def set_dns_preset(self, name: str, servers: list[str], actor: int) -> None:
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO dns_presets (name, servers, created_by, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    servers = excluded.servers
                """,
                (name, json.dumps(servers, ensure_ascii=False), actor, now_iso()),
            )
            con.commit()

    def delete_dns_preset(self, name: str) -> None:
        with closing(self.conn()) as con:
            active = self.get_setting("active_dns_preset")
            if active == name:
                raise ValueError("нельзя удалить активный DNS-пресет")
            con.execute("DELETE FROM dns_presets WHERE name = ?", (name,))
            con.commit()

    def active_dns(self) -> tuple[Optional[str], list[str]]:
        name = self.get_setting("active_dns_preset")
        if not name:
            return None, []
        return name, self.get_dns_preset(name) or []

    def remember_pending_created_peer(
        self,
        client_name: str,
        actor_id: Optional[int],
        actor_username: Optional[str],
    ) -> str:
        created_at = now_iso()
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO pending_created_clients
                (client_name, created_at, created_by_telegram_id, created_by_username)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_name) DO UPDATE SET
                    created_at = excluded.created_at,
                    created_by_telegram_id = excluded.created_by_telegram_id,
                    created_by_username = excluded.created_by_username
                """,
                (client_name, created_at, actor_id, actor_username),
            )
            con.commit()
        return created_at

    def remember_bot_created_peer(
        self,
        peer_id: int,
        public_key: Optional[str],
        peer_name: str,
        actor_id: Optional[int],
        actor_username: Optional[str],
        created_at: Optional[str] = None,
    ) -> None:
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO bot_created_peers
                (peer_id, public_key, peer_name, created_at,
                 created_by_telegram_id, created_by_username)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    public_key = excluded.public_key,
                    peer_name = excluded.peer_name
                """,
                (peer_id, public_key, peer_name, created_at or now_iso(), actor_id, actor_username),
            )
            con.execute("DELETE FROM pending_created_clients WHERE client_name = ?", (peer_name,))
            con.commit()

    def get_bot_created_peer(self, peer_id: int) -> Optional[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute("SELECT * FROM bot_created_peers WHERE peer_id = ?", (peer_id,)).fetchone()

    def reconcile_pending_created_peers(self, peers: list[dict[str, Any]]) -> None:
        with closing(self.conn()) as con:
            pending = con.execute("SELECT * FROM pending_created_clients").fetchall()
            for pending_row in pending:
                pending_name = str(pending_row["client_name"] or "")
                for peer in peers:
                    peer_id = peer.get("id")
                    if peer_id is None:
                        continue
                    candidates = {
                        str(peer.get("name") or ""),
                        str(peer.get("friendly_name") or ""),
                        str(peer.get("config_name") or ""),
                        peer_title(peer),
                    }
                    if pending_name in candidates or f"-client-{pending_name}" in " ".join(candidates):
                        con.execute(
                            """
                            INSERT INTO bot_created_peers
                            (peer_id, public_key, peer_name, created_at,
                             created_by_telegram_id, created_by_username)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(peer_id) DO NOTHING
                            """,
                            (
                                int(peer_id),
                                peer.get("public_key"),
                                peer_title(peer),
                                pending_row["created_at"],
                                pending_row["created_by_telegram_id"],
                                pending_row["created_by_username"],
                            ),
                        )
                        con.execute("DELETE FROM pending_created_clients WHERE client_name = ?", (pending_name,))
                        break
            con.commit()

    def set_peer_limit(self, peer_id: int, peer_name: str, period: str, limit_bytes: int, actor: int) -> None:
        if period not in LIMIT_PERIODS:
            raise ValueError("Некорректный период лимита")

        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO peer_traffic_limits
                (peer_id, peer_name, period, limit_bytes, current_period_key,
                 bot_disabled, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    peer_name = excluded.peer_name,
                    period = excluded.period,
                    limit_bytes = excluded.limit_bytes,
                    current_period_key = excluded.current_period_key,
                    updated_at = excluded.updated_at
                """,
                (peer_id, peer_name, period, limit_bytes, current_period_key(period), actor, now_iso(), now_iso()),
            )
            con.commit()

    def get_peer_limit(self, peer_id: int) -> Optional[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute("SELECT * FROM peer_traffic_limits WHERE peer_id = ?", (peer_id,)).fetchone()

    def list_peer_limits(self) -> list[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute("SELECT * FROM peer_traffic_limits ORDER BY peer_id").fetchall()

    def delete_peer_limit(self, peer_id: int) -> None:
        with closing(self.conn()) as con:
            con.execute("DELETE FROM peer_traffic_limits WHERE peer_id = ?", (peer_id,))
            con.commit()

    def mark_limit_disabled(self, peer_id: int, disabled: bool) -> None:
        with closing(self.conn()) as con:
            con.execute(
                "UPDATE peer_traffic_limits SET bot_disabled = ?, updated_at = ? WHERE peer_id = ?",
                (1 if disabled else 0, now_iso(), peer_id),
            )
            con.commit()

    def update_limit_period_key(self, peer_id: int, period_key: str) -> None:
        with closing(self.conn()) as con:
            con.execute(
                "UPDATE peer_traffic_limits SET current_period_key = ?, updated_at = ? WHERE peer_id = ?",
                (period_key, now_iso(), peer_id),
            )
            con.commit()

    def store_server_daily(self, day: str, rx: int, tx: int) -> None:
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO server_daily_usage (day, rx_bytes, tx_bytes, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    rx_bytes = excluded.rx_bytes,
                    tx_bytes = excluded.tx_bytes,
                    updated_at = excluded.updated_at
                """,
                (day, rx, tx, now_iso()),
            )
            con.commit()

    def yearly_server_usage(self) -> tuple[int, int]:
        with closing(self.conn()) as con:
            rows = con.execute(
                """
                SELECT rx_bytes, tx_bytes FROM server_daily_usage
                WHERE day >= date('now', '-365 day')
                """
            ).fetchall()
            return (
                sum(int(r["rx_bytes"] or 0) for r in rows),
                sum(int(r["tx_bytes"] or 0) for r in rows),
            )

    def store_peer_daily(self, day: str, peer_id: int, peer_name: str, rx: int, tx: int) -> None:
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO peer_daily_usage
                (day, peer_id, peer_name, rx_bytes, tx_bytes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, peer_id) DO UPDATE SET
                    peer_name = excluded.peer_name,
                    rx_bytes = excluded.rx_bytes,
                    tx_bytes = excluded.tx_bytes,
                    updated_at = excluded.updated_at
                """,
                (day, peer_id, peer_name, rx, tx, now_iso()),
            )
            con.commit()

    def yearly_peer_usage(self, peer_id: int) -> tuple[int, int]:
        with closing(self.conn()) as con:
            rows = con.execute(
                """
                SELECT rx_bytes, tx_bytes FROM peer_daily_usage
                WHERE peer_id = ?
                  AND day >= date('now', '-365 day')
                """,
                (peer_id,),
            ).fetchall()
            return (
                sum(int(r["rx_bytes"] or 0) for r in rows),
                sum(int(r["tx_bytes"] or 0) for r in rows),
            )

    def clear_bot_statistics(self) -> None:
        with closing(self.conn()) as con:
            con.execute("DELETE FROM peer_daily_usage")
            con.execute("DELETE FROM server_daily_usage")
            con.commit()

    def clear_peer_bot_statistics(self, peer_id: int) -> None:
        with closing(self.conn()) as con:
            con.execute("DELETE FROM peer_daily_usage WHERE peer_id = ?", (peer_id,))
            con.commit()

    def audit(
        self,
        actor_id: Optional[int],
        actor_username: Optional[str],
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        target_name: Optional[str] = None,
        details: Optional[dict[str, Any] | str] = None,
    ) -> None:
        details_text = json.dumps(details, ensure_ascii=False) if isinstance(details, dict) else details
        with closing(self.conn()) as con:
            con.execute(
                """
                INSERT INTO bot_audit_log
                (actor_telegram_id, actor_username, action, target_type,
                 target_id, target_name, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (actor_id, actor_username, action, target_type, target_id, target_name, details_text, now_iso()),
            )
            con.commit()

    def list_audit(self, limit: int = 30) -> list[sqlite3.Row]:
        with closing(self.conn()) as con:
            return con.execute(
                "SELECT * FROM bot_audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()


db = BotDB(cfg.bot_db, cfg.bootstrap_owner_id)


class AmneziaWGWebApi:
    def __init__(self, base_url: str, token: str, timeout: int) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "*/*" if raw else "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.request(method, url, headers=headers, json=json_body, params=params) as resp:
                    data = await resp.read()

                    if resp.status < 200 or resp.status >= 300:
                        text = data.decode(errors="replace")
                        raise ApiError(f"HTTP {resp.status} {method} {path}: {text[:800]}")

                    if raw:
                        return data, resp.headers

                    ctype = resp.headers.get("Content-Type", "")
                    text = data.decode(errors="replace")
                    if "application/json" in ctype:
                        return json.loads(text) if text else None
                    return text

        except aiohttp.ClientError as exc:
            raise ApiError(f"Ошибка соединения с amneziawg-web: {exc}") from exc

    async def health(self) -> Any:
        return await self.request("GET", "/api/health")

    async def peers(self) -> list[dict[str, Any]]:
        data = await self.request("GET", "/api/peers")
        return data if isinstance(data, list) else []

    async def peer(self, peer_id: int) -> dict[str, Any]:
        data = await self.request("GET", f"/api/peers/{peer_id}")
        return data if isinstance(data, dict) else {}

    async def update_peer(
        self,
        peer_id: int,
        *,
        display_name: Optional[str] = None,
        comment: Optional[str] = None,
        disabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if display_name is not None:
            body["display_name"] = display_name
        if comment is not None:
            body["comment"] = comment
        if disabled is not None:
            body["disabled"] = disabled
        data = await self.request("PATCH", f"/api/peers/{peer_id}", json_body=body)
        return data if isinstance(data, dict) else {}

    async def create_user(
        self,
        name: str,
        ipv4_address: Optional[str] = None,
        ipv6_address: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if ipv4_address:
            body["ipv4_address"] = ipv4_address
        if ipv6_address:
            body["ipv6_address"] = ipv6_address
        data = await self.request("POST", "/api/admin/users", json_body=body)
        return data if isinstance(data, dict) else {}

    async def remove_user(self, peer_id: int) -> Any:
        return await self.request("POST", f"/api/admin/users/{peer_id}/remove", json_body={})

    async def usage_all(self, period: str) -> dict[str, Any]:
        data = await self.request("GET", "/api/usage", params={"period": period})
        return data if isinstance(data, dict) else {}

    async def usage_peer_summary(self, peer_id: int) -> dict[str, Any]:
        data = await self.request("GET", f"/api/peers/{peer_id}/usage/summary")
        return data if isinstance(data, dict) else {}

    async def events(self, limit: int) -> list[dict[str, Any]]:
        data = await self.request("GET", "/api/events", params={"limit": limit})
        return data if isinstance(data, list) else []

    async def config_raw(self, peer_id: int) -> str:
        data, _headers = await self.request("GET", f"/api/peers/{peer_id}/config", raw=True)
        return data.decode("utf-8", errors="replace")

    async def next_ips(self) -> dict[str, Any]:
        data = await self.request("GET", "/api/admin/next-ips")
        return data if isinstance(data, dict) else {}


api = AmneziaWGWebApi(cfg.awg_web_base_url, cfg.awg_web_api_token, cfg.request_timeout)


def actor_info(message_or_callback: Message | CallbackQuery) -> tuple[Optional[int], Optional[str]]:
    user = message_or_callback.from_user
    if not user:
        return None, None
    username = f"@{user.username}" if user.username else None
    return user.id, username

# Оригинал
#async def reject_message_if_not_admin(message: Message) -> bool:
#    user_id = message.from_user.id if message.from_user else None
#    if not db.is_admin(user_id):
#        await message.answer("⛔ Доступ запрещён.")
#        db.audit(user_id, None, "access_denied", details={"type": "message"})
#        return True
#    return False

# Вишенка на торте
async def reject_message_if_not_admin(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if not db.is_admin(user_id):
        # Отправляем фото по URL с подписью
        await message.answer_photo(
            photo="https://images.meme-arsenal.com/b9d933256b7dd5d53ea2c89583b7a80d.jpg",
            caption="⛔ Доступ запрещён."
        )
        db.audit(user_id, None, "access_denied", details={"type": "message"})
        return True
    return False


async def reject_callback_if_not_admin(callback: CallbackQuery) -> bool:
    user_id = callback.from_user.id if callback.from_user else None
    if not db.is_admin(user_id):
        await callback.answer("Доступ запрещён", show_alert=True)
        db.audit(user_id, None, "access_denied", details={"type": "callback"})
        return True
    return False


async def reject_message_if_not_owner(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if not db.is_owner(user_id):
        await message.answer("⛔ Это действие доступно только владельцу.")
        return True
    return False


async def reject_callback_if_not_owner(callback: CallbackQuery) -> bool:
    user_id = callback.from_user.id if callback.from_user else None
    if not db.is_owner(user_id):
        await callback.answer("Только владелец", show_alert=True)
        return True
    return False


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Пользователи", callback_data="peers:0:all"),
                InlineKeyboardButton(text="📊 Стат. сервера", callback_data="server"),
            ],
            [
                InlineKeyboardButton(text="➕ Созд. польз.", callback_data="create_user"),
                InlineKeyboardButton(text="🩺 Healthcheck", callback_data="health"),
            ],
            [
                InlineKeyboardButton(text="⚙️ I1-I5", callback_data="i_settings"),
                InlineKeyboardButton(text="🌐 DNS", callback_data="dns"),
            ],
            [
                InlineKeyboardButton(text="🧩 IPv6", callback_data="config_settings"),
                InlineKeyboardButton(text="👮 Админы", callback_data="admins"),
            ],
            [
                InlineKeyboardButton(text="📜 Лог бота", callback_data="bot_log"),
                InlineKeyboardButton(text="📋 Лог web-панели", callback_data="web_events"),
            ],
        ]
    )


def filter_peers(peers: list[dict[str, Any]], status_filter: str) -> list[dict[str, Any]]:
    if status_filter == "all":
        return peers

    out = []
    for p in peers:
        conn = str(p.get("connection_status") or "").lower()
        if status_filter == "online" and conn == "online":
            out.append(p)
        elif status_filter == "offline" and conn in {"inactive", "never"}:
            out.append(p)
        elif status_filter == "disabled" and conn == "disabled":
            out.append(p)
        elif status_filter == "never" and conn == "never":
            out.append(p)
    return out


def peers_keyboard(peers: list[dict[str, Any]], page: int, status_filter: str) -> InlineKeyboardMarkup:
    filtered = filter_peers(peers, status_filter)
    start = page * cfg.peers_page_size
    end = start + cfg.peers_page_size

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="Все", callback_data="peers:0:all"),
            InlineKeyboardButton(text="В сети", callback_data="peers:0:online"),
            InlineKeyboardButton(text="Не в сети", callback_data="peers:0:offline"),
        ],
        [
            InlineKeyboardButton(text="Деактивированные", callback_data="peers:0:disabled"),
            InlineKeyboardButton(text="Никогда не подкл.", callback_data="peers:0:never"),
        ],
    ]

    for p in filtered[start:end]:
        pid = int(p["id"])
        conn = str(p.get("connection_status") or p.get("status") or "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon(conn)} {peer_title(p)}"[:64],
                    callback_data=f"peer:{pid}",
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"peers:{page - 1}:{status_filter}"))
    if end < len(filtered):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"peers:{page + 1}:{status_filter}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def peer_keyboard(peer_id: int, disabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"peer:{peer_id}")],
            [
                InlineKeyboardButton(text="📥 .conf", callback_data=f"conf:{peer_id}"),
                InlineKeyboardButton(text="▦ QR", callback_data=f"qr:{peer_id}"),
            ],
            [
                InlineKeyboardButton(text="📈 Трафик", callback_data=f"peer_usage:{peer_id}"),
                InlineKeyboardButton(text="🚦 Лимит", callback_data=f"limit:{peer_id}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Имя", callback_data=f"edit_name:{peer_id}"),
                InlineKeyboardButton(text="💬 Комментарий", callback_data=f"edit_comment:{peer_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Активировать" if disabled else "⛔ Деактивировать",
                    callback_data=f"toggle:{peer_id}:{0 if disabled else 1}",
                ),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"remove_confirm:{peer_id}"),
            ],
            [
                InlineKeyboardButton(text="👥 К списку", callback_data="peers:0:all"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu"),
            ],
        ]
    )


def server_keyboard(user_id: Optional[int]) -> InlineKeyboardMarkup:
    rows = []
    if db.is_owner(user_id):
        rows.append([InlineKeyboardButton(text="🧹 Сбросить статистику бота", callback_data="stats_reset_confirm")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit(callback: CallbackQuery, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        if callback.message:
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


def format_peers(peers: list[dict[str, Any]], status_filter: str) -> str:
    total = len(peers)
    online = sum(1 for p in peers if str(p.get("connection_status")) == "online")
    inactive = sum(1 for p in peers if str(p.get("connection_status")) == "inactive")
    never = sum(1 for p in peers if str(p.get("connection_status")) == "never")
    disabled = sum(1 for p in peers if str(p.get("connection_status")) == "disabled")
    free = max(MAX_CLIENTS_24 - total, 0)

    return (
        "<b>👥 Пользователи AmneziaWG2</b>\n\n"
        f"Создано: <b>{total}</b>\n"
        f"Можно создать ещё: <b>{free}</b>\n\n"
        f"🟢 В сети: <b>{online}</b>\n"
        f"🟡 Не в сети: <b>{inactive}</b>\n"
        f"⚪ Никогда не подкл.: <b>{never}</b>\n"
        f"🔴 Деактивированных: <b>{disabled}</b>\n\n"
        f"Текущий фильтр: <b>{h(status_filter)}</b>\n\n"
        f"➖➖➖\n"
        f"<b>🕑 Обновлено:</b> {h(local_now_display())}"
    )


def format_peer(peer: dict[str, Any]) -> str:
    conn = str(peer.get("connection_status") or "")
    ident = str(peer.get("identity_status") or "")
    peer_id = int(peer.get("id") or 0)
    client_ip_address = h(peer.get('endpoint').split(':')[0])
    awg2_ipv4_address = h(peer.get('allowed_ips', '').split(',')[0].split('/')[0])

    created = db.get_bot_created_peer(peer_id) if peer_id else None
    if created:
        created_text = h(format_datetime_local(created["created_at"]))
        created_by = f"\n<b>Создал:</b> <code>{h(created['created_by_telegram_id'])}</code> {h(created['created_by_username'])}"
    else:
        created_text = "-не через бота-"
        created_by = ""

    limit = db.get_peer_limit(peer_id) if peer_id else None
    if limit:
        limit_text = (
            f"{fmt_bytes(limit['limit_bytes'])}, период: {h(limit['period'])}, "
            f"ключ периода: <code>{h(limit['current_period_key'])}</code>, "
            f"отключён ботом: {h(bool(limit['bot_disabled']))}"
        )
    else:
        limit_text = "-не задан-"

    return (
        f"<b>{status_icon(conn)} {h(peer_title(peer))}</b>\n"
        f"  <b>Создан:</b> {created_text}"
        f"  {created_by}\n\n"
        f"  <b>Деактивирован:</b> {h(peer.get('disabled'))}\n"
        f"<b>⏳ Последнее 🤝:</b> {h(format_datetime_local(peer.get('latest_handshake_at')))}\n"
        f"<b>🌐 Внутренний IPv4:</b> <code>{awg2_ipv4_address}</code>\n"
        f"<b>🌐 IP клиента:</b> <tg-spoiler><a href='http://check-host.net/ip-info?host={client_ip_address}'>{client_ip_address}</a></tg-spoiler>\n"
        f"<b>🔽 Входящий трафик:</b> ↓ {fmt_bytes(peer.get('tx_bytes'))}\n"
        f"<b>🔼 Исходящий трафик:</b> ↑ {fmt_bytes(peer.get('rx_bytes'))}\n"
        f"<b>📅 Лимит:</b> {limit_text}\n"
        f"<b>📝 Комментарий:</b> {h(peer.get('comment'))}\n\n"
        f"➖➖➖\n"
        f"<b>🕑 Обновлено:</b> {h(local_now_display())}"
    )


def format_usage_summary(data: dict[str, Any]) -> str:
    peer_id = int(data.get("peer_id") or 0)

    def pair(period: str) -> str:
        item = data.get(period) or {}
        return f"↓ {fmt_bytes(item.get('tx_bytes'))} / ↑ {fmt_bytes(item.get('rx_bytes'))}"

    day = data.get("day") or {}
    today = datetime.now(timezone.utc).date().isoformat()

    if peer_id:
        db.store_peer_daily(
            today,
            peer_id,
            str(data.get("name") or ""),
            int(day.get("rx_bytes") or 0),
            int(day.get("tx_bytes") or 0),
        )

    year_rx, year_tx = db.yearly_peer_usage(peer_id) if peer_id else (0, 0)

    return (
        f"<b>📈 Трафик пользователя {h(data.get('name'))}</b>\n\n"
        f"<b>🔽 Download / 🔼 Upload</b>\n"
        f"➖➖➖➖➖➖➖➖\n"
        f"<b>⏰ За день:</b> {pair('day')}\n"
        f"<b>📆 За неделю:</b> {pair('week')}\n"
        f"<b>🌙 За месяц:</b> {pair('month')}\n"
        f"<b>🎄 За год:</b> ↓ {fmt_bytes(year_tx)} / ↑ {fmt_bytes(year_rx)}"
    )


def format_server_usage(day: dict[str, Any], week: dict[str, Any], month: dict[str, Any]) -> str:
    def total(data: dict[str, Any]) -> tuple[int, int]:
        rx = 0
        tx = 0
        for p in data.get("peers", []):
            rx += int(p.get("rx_bytes") or 0)
            tx += int(p.get("tx_bytes") or 0)
        return rx, tx

    day_rx, day_tx = total(day)
    week_rx, week_tx = total(week)
    month_rx, month_tx = total(month)

    today = datetime.now(timezone.utc).date().isoformat()
    db.store_server_daily(today, day_rx, day_tx)
    year_rx, year_tx = db.yearly_server_usage()

    return (
        "<b>📊 Статистика сервера AmneziaWG2</b>\n\n"
        f"<b>🔽 Download / 🔼 Upload</b>\n"
        f"➖➖➖➖➖➖\n"
        f"<b>⏰ За день:</b> ↓ {fmt_bytes(day_tx)} / ↑ {fmt_bytes(day_rx)}\n"
        f"<b>📆 За неделю:</b> ↓ {fmt_bytes(week_tx)} / ↑ {fmt_bytes(week_rx)}\n"
        f"<b>🌙 За месяц:</b> ↓ {fmt_bytes(month_tx)} / ↑ {fmt_bytes(month_rx)}\n"
        f"<b>🎄 За год:</b> ↓ {fmt_bytes(year_tx)} / ↑ {fmt_bytes(year_rx)}\n\n"
        "Годовая статистика берётся из SQLite бота и начинает копиться с момента установки бота."
    )


def transform_config(raw: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    active_dns_name, dns_servers = db.active_dns()
    i_values = db.get_i_values()
    remove_ipv6 = db.bool_setting("remove_ipv6_from_address", True)

    if not i_values.get("I1"):
        warnings.append("I1 не задан")

    lines = raw.splitlines()
    out: list[str] = []
    inserted_i = False

    for line in lines:
        stripped = line.strip()

        if re.match(r"^Address\s*=", stripped, flags=re.IGNORECASE):
            if remove_ipv6:
                key, value = line.split("=", 1)
                addrs = [x.strip() for x in value.split(",") if x.strip()]
                ipv4_only = [x for x in addrs if ":" not in x]
                if ipv4_only:
                    out.append(f"{key.strip()} = {', '.join(ipv4_only)}")
                else:
                    out.append(line)
                    warnings.append("IPv4 в Address не найден; Address оставлен без изменений")
            else:
                out.append(line)
                warnings.append("IPv6 в Address оставлен по настройке бота")
            continue

        if re.match(r"^DNS\s*=", stripped, flags=re.IGNORECASE):
            if dns_servers:
                out.append(f"DNS = {', '.join(dns_servers)}")
            else:
                out.append(line)
                warnings.append("активный DNS-пресет пуст; DNS оставлен без изменений")
            continue

        if re.match(r"^(I[1-5])\s*=", stripped, flags=re.IGNORECASE):
            continue

        out.append(line)

        if re.match(r"^H4\s*=", stripped, flags=re.IGNORECASE):
            if i_values.get("I1"):
                for i in range(1, 6):
                    val = i_values.get(f"I{i}")
                    if val:
                        out.append(f"I{i} = {val}")
            inserted_i = True

    if not inserted_i:
        warnings.append("строка H4 не найдена; I1-I5 не добавлены")

    if active_dns_name:
        warnings.append(f"DNS-пресет: {active_dns_name}")

    return "\n".join(out).rstrip() + "\n", warnings


def make_qr_png_bytes(text: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


async def find_peer_after_create(name: str, attempts: int = 10) -> Optional[dict[str, Any]]:
    for _ in range(attempts):
        peers = await api.peers()
        db.reconcile_pending_created_peers(peers)
        for p in peers:
            candidates = {
                str(p.get("name") or ""),
                str(p.get("friendly_name") or ""),
                str(p.get("config_name") or ""),
            }
            if name in candidates or f"-client-{name}" in " ".join(candidates):
                return p
        await asyncio.sleep(1)
    return None


def get_usage_for_limit(peer: dict[str, Any], summary: dict[str, Any], period: str) -> int:
    if period == "never":
        return int(peer.get("rx_bytes") or 0) + int(peer.get("tx_bytes") or 0)

    item = summary.get(period) or {}
    return int(item.get("rx_bytes") or 0) + int(item.get("tx_bytes") or 0)


async def enforce_traffic_limits_once() -> None:
    limits = db.list_peer_limits()
    if not limits:
        return

    try:
        peers = await api.peers()
        db.reconcile_pending_created_peers(peers)
    except ApiError as exc:
        log.warning("Не удалось получить peers для проверки лимитов: %s", exc)
        return

    peers_by_id = {int(p["id"]): p for p in peers if p.get("id") is not None}

    for limit in limits:
        peer_id = int(limit["peer_id"])
        peer = peers_by_id.get(peer_id)
        if not peer:
            continue

        period = str(limit["period"])
        expected_key = current_period_key(period)
        stored_key = str(limit["current_period_key"])

        if period != "never" and expected_key != stored_key:
            db.update_limit_period_key(peer_id, expected_key)
            if int(limit["bot_disabled"] or 0):
                try:
                    await api.update_peer(peer_id, disabled=False)
                    db.mark_limit_disabled(peer_id, False)
                    db.audit(
                        None,
                        None,
                        "traffic_limit_auto_enable",
                        "peer",
                        str(peer_id),
                        peer_title(peer),
                        {"period": period, "period_key": expected_key},
                    )
                except ApiError as exc:
                    log.warning("Не удалось включить peer %s после смены периода: %s", peer_id, exc)
            continue

        try:
            summary = await api.usage_peer_summary(peer_id)
            used = get_usage_for_limit(peer, summary, period)
        except ApiError as exc:
            log.warning("Не удалось получить usage peer %s: %s", peer_id, exc)
            continue

        limit_bytes = int(limit["limit_bytes"])
        disabled_now = bool(peer.get("disabled"))

        if used >= limit_bytes and not disabled_now:
            try:
                await api.update_peer(peer_id, disabled=True)
                db.mark_limit_disabled(peer_id, True)
                db.audit(
                    None,
                    None,
                    "traffic_limit_auto_disable",
                    "peer",
                    str(peer_id),
                    peer_title(peer),
                    {"period": period, "used": used, "limit": limit_bytes},
                )
            except ApiError as exc:
                log.warning("Не удалось отключить peer %s по лимиту: %s", peer_id, exc)


async def traffic_limit_worker() -> None:
    while True:
        try:
            await enforce_traffic_limits_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка фоновой проверки лимитов")
        await asyncio.sleep(max(cfg.traffic_check_interval, 30))


@router.message(CommandStart())
async def start(message: Message) -> None:
    if await reject_message_if_not_admin(message):
        return

    text = (
        "<b>🛡 AmneziaWG2 Telegram Admin</b> <code>{current_bot_version}</code>\n\n"
        "Бот управляет <b>amneziawg-web</b> через его HTTP API.\n"
        "Сам VPN/backend слой — это AmneziaWG/amneziawg-proxy, а бот напрямую с ним не работает.\n\n"
        "Файлы .conf на диске бот не изменяет: исправления DNS, Address и I1-I5 применяются только к копии, отправляемой в Telegram."
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return
    await state.clear()
    await message.answer("Ввод отменён.", reply_markup=main_menu())


@router.callback_query(F.data == "cancel_input")
async def cb_cancel_input(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Ввод отменён.", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return
    await safe_edit(callback, f"<b>🏠 Главное меню</b>\n\n<code>{current_bot_version}</code>", main_menu())
    await callback.answer()


@router.callback_query(F.data == "health")
async def cb_health(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return
    try:
        data = await api.health()
        await safe_edit(callback, f"<b>🩺 Health</b>\n\n<code>{h(data)}</code>", back_menu())
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка</b>\n\n<code>{h(exc)}</code>", back_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("peers:"))
async def cb_peers(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    _, page_raw, status_filter = callback.data.split(":", 2)
    page = int(page_raw)

    try:
        peers = await api.peers()
        db.reconcile_pending_created_peers(peers)
        filtered = filter_peers(peers, status_filter)
        max_page = max((len(filtered) - 1) // cfg.peers_page_size, 0)
        page = max(0, min(page, max_page))
        await safe_edit(callback, format_peers(peers, status_filter), peers_keyboard(peers, page, status_filter))
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка списка клиентов</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("peer:"))
async def cb_peer(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        peer = await api.peer(peer_id)
        await safe_edit(callback, format_peer(peer), peer_keyboard(peer_id, bool(peer.get("disabled"))))
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка peer</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("peer_usage:"))
async def cb_peer_usage(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        data = await api.usage_peer_summary(peer_id)
        await safe_edit(
            callback,
            format_usage_summary(data),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🧹 Сбросить статистику бота", callback_data=f"peer_stats_reset_confirm:{peer_id}")],
                    [InlineKeyboardButton(text="⬅️ К клиенту", callback_data=f"peer:{peer_id}")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
                ]
            ),
        )
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка трафика</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("peer_stats_reset_confirm:"))
async def cb_peer_stats_reset_confirm(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        peer = await api.peer(peer_id)
        await safe_edit(
            callback,
            "<b>🧹 Сброс статистики клиента</b>\n\n"
            f"Клиент: <b>{h(peer_title(peer))}</b>\n"
            f"Peer ID: <code>{peer_id}</code>\n\n"
            "Будет очищена только статистика клиента, которую хранит SQLite бота.\n"
            "Статистика amneziawg-web через API не сбрасывается.\n\n"
            "Подтвердить?",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Да, сбросить", callback_data=f"peer_stats_reset:{peer_id}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"peer_usage:{peer_id}")],
                ]
            ),
        )
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("peer_stats_reset:"))
async def cb_peer_stats_reset(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])
    actor_id, actor_username = actor_info(callback)

    db.clear_peer_bot_statistics(peer_id)
    db.audit(actor_id, actor_username, "peer_bot_statistics_reset", "peer", str(peer_id))

    await safe_edit(
        callback,
        "<b>✅ Статистика клиента в SQLite бота сброшена</b>\n\n"
        "Статистика amneziawg-web не изменялась.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К трафику клиента", callback_data=f"peer_usage:{peer_id}")],
                [InlineKeyboardButton(text="⬅️ К клиенту", callback_data=f"peer:{peer_id}")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("limit:"))
async def cb_limit(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        peer = await api.peer(peer_id)
        limit = db.get_peer_limit(peer_id)

        if limit:
            limit_text = (
                f"Текущий лимит: <b>{fmt_bytes(limit['limit_bytes'])}</b>\n"
                f"Период: <b>{h(limit['period'])}</b>\n"
                f"Ключ периода: <code>{h(limit['current_period_key'])}</code>\n"
                f"Отключён ботом: <b>{h(bool(limit['bot_disabled']))}</b>"
            )
        else:
            limit_text = "Лимит не задан."

        text = (
            "<b>🚦 Лимит трафика пользователя {h(peer_title(peer))}</b>\n\n"
            f"{limit_text}\n\n"
            "Чтобы задать или изменить лимит, нажмите одну из кнопок <b>Задать</b> ниже.\n"
            "После выбора периода бот попросит ввести само значение лимита.\n\n"
            "При превышении лимита бот отключает клиента через API.\n"
            "В новом периоде бот автоматически включает клиента обратно, если отключил его именно по лимиту."
        )

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Задать: никогда", callback_data=f"limit_period:{peer_id}:never")],
                [
                    InlineKeyboardButton(text="Задать: день", callback_data=f"limit_period:{peer_id}:day"),
                    InlineKeyboardButton(text="Задать: неделя", callback_data=f"limit_period:{peer_id}:week"),
                ],
                [InlineKeyboardButton(text="Задать: месяц", callback_data=f"limit_period:{peer_id}:month")],
                [
                    InlineKeyboardButton(text="Сбросить лимит", callback_data=f"limit_clear:{peer_id}"),
                    InlineKeyboardButton(text="Проверить сейчас", callback_data=f"limit_check:{peer_id}"),
                ],
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data=f"peer:{peer_id}"),
                    InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu"),
                ],
            ]
        )

        await safe_edit(callback, text, markup)

    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка лимита</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("limit_period:"))
async def cb_limit_period(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    _, peer_id_raw, period = callback.data.split(":")
    peer_id = int(peer_id_raw)

    if period not in LIMIT_PERIODS:
        await callback.answer("Некорректный период", show_alert=True)
        return

    await state.set_state(BotState.waiting_limit_bytes)
    await state.update_data(peer_id=peer_id, period=period)

    if callback.message:
        await callback.message.answer(
            "<b>🚦 Установка лимита</b>\n\n"
            f"Период: <b>{h(period)}</b>\n\n"
            "Введите лимит трафика. Примеры:\n"
            "<code>10GB</code>\n"
            "<code>500MB</code>\n"
            "<code>10737418240</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_keyboard(),
        )

    await callback.answer()


@router.message(BotState.waiting_limit_bytes)
async def state_limit_bytes(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    value = parse_bytes(message.text or "")
    if value is None:
        await message.answer(
            "Некорректный лимит. Примеры: 10GB, 500MB, 10737418240.",
            reply_markup=cancel_keyboard(),
        )
        return

    data = await state.get_data()
    peer_id = int(data["peer_id"])
    period = str(data["period"])
    actor_id, actor_username = actor_info(message)

    try:
        peer = await api.peer(peer_id)
        db.set_peer_limit(peer_id, peer_title(peer), period, value, actor_id or 0)
        db.audit(
            actor_id,
            actor_username,
            "traffic_limit_set",
            "peer",
            str(peer_id),
            peer_title(peer),
            {"period": period, "limit_bytes": value},
        )
        await state.clear()
        await message.answer(
            f"✅ Лимит установлен: {fmt_bytes(value)}, период: {period}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть клиента", callback_data=f"peer:{peer_id}")]]
            ),
        )
    except ApiError as exc:
        await state.clear()
        await message.answer(f"❌ Ошибка: {exc}", reply_markup=main_menu())


@router.callback_query(F.data.startswith("limit_clear:"))
async def cb_limit_clear(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])
    actor_id, actor_username = actor_info(callback)

    db.delete_peer_limit(peer_id)
    db.audit(actor_id, actor_username, "traffic_limit_clear", "peer", str(peer_id))
    await callback.answer("Лимит сброшен", show_alert=True)
    await cb_peer(callback)


@router.callback_query(F.data.startswith("limit_check:"))
async def cb_limit_check(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    await enforce_traffic_limits_once()
    await callback.answer("Проверка лимитов выполнена", show_alert=True)
    await cb_limit(callback)


@router.callback_query(F.data == "server")
async def cb_server(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    try:
        day, week, month = await asyncio.gather(
            api.usage_all("day"),
            api.usage_all("week"),
            api.usage_all("month"),
        )
        await safe_edit(callback, format_server_usage(day, week, month), server_keyboard(callback.from_user.id))
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка статистики</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data == "stats_reset_confirm")
async def cb_stats_reset_confirm(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_owner(callback):
        return

    await safe_edit(
        callback,
        "<b>🧹 Сброс статистики бота</b>\n\n"
        "Будет очищена только статистика, которую хранит SQLite бота.\n"
        "Статистика amneziawg-web через API не сбрасывается.\n\n"
        "Подтвердить?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да, сбросить", callback_data="stats_reset")],
                [InlineKeyboardButton(text="Отмена", callback_data="server")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "stats_reset")
async def cb_stats_reset(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_owner(callback):
        return

    actor_id, actor_username = actor_info(callback)
    db.clear_bot_statistics()
    db.audit(actor_id, actor_username, "bot_statistics_reset")
    await safe_edit(callback, "<b>✅ Статистика бота сброшена</b>", main_menu())
    await callback.answer()


@router.callback_query(F.data == "create_user")
async def cb_create_user(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    i1 = db.get_setting("i1")
    warning = (
        "\n\n⚠️ <b>I1 не задан.</b> Конфиг будет создан, но в отправляемую копию I1 не будет добавлен."
        if not i1
        else ""
    )

    await state.set_state(BotState.waiting_new_user_name)
    if callback.message:
        await callback.message.answer(
            "<b>➕ Создание клиента</b>\n\n"
            "Введите имя клиента.\n"
            "Разрешены: латиница, цифры, подчёркивание и дефис.\n"
            "Максимум 15 символов."
            f"{warning}",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(BotState.waiting_new_user_name)
async def state_create_user(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    name = (message.text or "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", name):
        await message.answer(
            "Некорректное имя. Разрешены только A-Z, a-z, 0-9, _ и -. Максимум 15 символов.",
            reply_markup=cancel_keyboard(),
        )
        return

    actor_id, actor_username = actor_info(message)
    created_at = db.remember_pending_created_peer(name, actor_id, actor_username)

    try:
        result = await api.create_user(name)
        db.audit(actor_id, actor_username, "awg_user_create", "peer", None, name, result)

        peer = await find_peer_after_create(name)
        await state.clear()

        if not peer:
            await message.answer(
                "<b>✅ Клиент создан</b>\n\n"
                f"Имя: <b>{h(name)}</b>\n"
                f"Создан ботом: <code>{h(format_datetime_local(created_at))}</code>\n\n"
                "Но бот не смог найти peer в API для скачивания конфига. "
                "Откройте список клиентов через несколько секунд.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )
            return

        peer_id = int(peer["id"])
        db.remember_bot_created_peer(
            peer_id,
            peer.get("public_key"),
            peer_title(peer),
            actor_id,
            actor_username,
            created_at,
        )

        await send_fixed_config_and_qr(message, peer_id, peer_title(peer))

    except ApiError as exc:
        db.audit(actor_id, actor_username, "awg_user_create_failed", "peer", None, name, str(exc))
        await state.clear()
        await message.answer(
            f"<b>❌ Ошибка создания клиента</b>\n\n<code>{h(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )


async def send_fixed_config_and_qr(message: Message, peer_id: int, name: str) -> None:
    raw = await api.config_raw(peer_id)
    fixed, warnings = transform_config(raw)

    conf_name = f"{safe_filename(name)}.conf"
    qr_name = f"{safe_filename(name)}-qr.png"
    remove_ipv6 = db.bool_setting("remove_ipv6_from_address", True)

    await message.answer(
        "<b>✅ Клиент готов</b>\n\n"
        f"Peer ID: <code>{peer_id}</code>\n"
        f"Имя: <b>{h(name)}</b>\n\n"
        "<b>Изменения в отправляемой копии:</b>\n"
        f"• IPv6 {'удалён из Address' if remove_ipv6 else 'оставлен в Address'}\n"
        "• DNS заменён на активный пресет\n"
        "• I1-I5 добавлены после H4, если задан I1\n\n"
        + ("\n".join(f"⚠️ {h(w)}" for w in warnings) if warnings else "Предупреждений нет."),
        parse_mode=ParseMode.HTML,
    )

    await message.answer_document(
        BufferedInputFile(fixed.encode("utf-8"), filename=conf_name),
        caption="📥 Исправленный конфиг",
    )

    qr_bytes = make_qr_png_bytes(fixed)
    await message.answer_photo(
        BufferedInputFile(qr_bytes, filename=qr_name),
        caption="▦ QR исправленного конфига",
    )


@router.callback_query(F.data.startswith("conf:"))
async def cb_conf(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        peer = await api.peer(peer_id)
        raw = await api.config_raw(peer_id)
        fixed, warnings = transform_config(raw)

        if callback.message:
            await callback.message.answer_document(
                BufferedInputFile(fixed.encode("utf-8"), filename=f"{safe_filename(peer_title(peer))}.conf"),
                caption="📥 Исправленный конфиг\n" + "\n".join(f"⚠️ {w}" for w in warnings),
            )

        actor_id, actor_username = actor_info(callback)
        db.audit(actor_id, actor_username, "config_download", "peer", str(peer_id), peer_title(peer))

    except ApiError as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)

    await callback.answer()


@router.callback_query(F.data.startswith("qr:"))
async def cb_qr(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        peer = await api.peer(peer_id)
        raw = await api.config_raw(peer_id)
        fixed, warnings = transform_config(raw)
        qr_bytes = make_qr_png_bytes(fixed)

        if callback.message:
            await callback.message.answer_photo(
                BufferedInputFile(qr_bytes, filename=f"{safe_filename(peer_title(peer))}-qr.png"),
                caption="▦ QR исправленного конфига\n" + "\n".join(f"⚠️ {w}" for w in warnings),
            )

        actor_id, actor_username = actor_info(callback)
        db.audit(actor_id, actor_username, "config_qr", "peer", str(peer_id), peer_title(peer))

    except ApiError as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)

    await callback.answer()


@router.callback_query(F.data.startswith("remove_confirm:"))
async def cb_remove_confirm(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])

    try:
        peer = await api.peer(peer_id)
        await safe_edit(
            callback,
            "<b>🗑 Подтверждение удаления</b>\n\n"
            f"Клиент: <b>{h(peer_title(peer))}</b>\n"
            f"Peer ID: <code>{peer_id}</code>\n\n"
            "Удаление вызовет API amneziawg-web и удалит пользователя AmneziaWG.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Да, удалить", callback_data=f"remove:{peer_id}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"peer:{peer_id}")],
                ]
            ),
        )
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("remove:"))
async def cb_remove(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])
    actor_id, actor_username = actor_info(callback)

    try:
        peer = await api.peer(peer_id)
        name = peer_title(peer)
        result = await api.remove_user(peer_id)

        db.audit(
            actor_id,
            actor_username,
            "awg_user_remove",
            "peer",
            str(peer_id),
            name,
            {"api_result": result},
        )

        await safe_edit(
            callback,
            "<b>✅ Клиент удалён</b>\n\n"
            f"Кто удалил: <code>{h(actor_id)}</code> {h(actor_username)}\n"
            f"Peer ID: <code>{peer_id}</code>\n"
            f"Имя: <b>{h(name)}</b>",
            main_menu(),
        )

    except ApiError as exc:
        db.audit(actor_id, actor_username, "awg_user_remove_failed", "peer", str(peer_id), None, str(exc))
        await safe_edit(callback, f"<b>❌ Ошибка удаления</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    _, peer_id_raw, disabled_raw = callback.data.split(":")
    peer_id = int(peer_id_raw)
    disabled = bool(int(disabled_raw))
    actor_id, actor_username = actor_info(callback)

    try:
        peer = await api.update_peer(peer_id, disabled=disabled)

        if not disabled:
            limit = db.get_peer_limit(peer_id)
            if limit and int(limit["bot_disabled"] or 0):
                db.mark_limit_disabled(peer_id, False)

        db.audit(
            actor_id,
            actor_username,
            "peer_disabled_set",
            "peer",
            str(peer_id),
            peer_title(peer),
            {"disabled": disabled},
        )
        await safe_edit(callback, format_peer(peer), peer_keyboard(peer_id, bool(peer.get("disabled"))))
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


@router.callback_query(F.data.startswith("edit_name:"))
async def cb_edit_name(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])
    await state.set_state(BotState.waiting_peer_display_name)
    await state.update_data(peer_id=peer_id)

    if callback.message:
        await callback.message.answer(
            "Введите новое отображаемое имя.\n"
            "Чтобы очистить — отправьте один дефис: -",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(BotState.waiting_peer_display_name)
async def state_peer_name(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    data = await state.get_data()
    peer_id = int(data["peer_id"])
    value = (message.text or "").strip()
    if value == "-":
        value = ""

    try:
        peer = await api.update_peer(peer_id, display_name=value)
        actor_id, actor_username = actor_info(message)
        db.audit(actor_id, actor_username, "peer_display_name_update", "peer", str(peer_id), peer_title(peer))
        await state.clear()
        await message.answer(
            "<b>✅ Имя обновлено</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть клиента", callback_data=f"peer:{peer_id}")]]
            ),
        )
    except ApiError as exc:
        await message.answer(f"❌ Ошибка: {exc}")


@router.callback_query(F.data.startswith("edit_comment:"))
async def cb_edit_comment(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    peer_id = int(callback.data.split(":")[1])
    await state.set_state(BotState.waiting_peer_comment)
    await state.update_data(peer_id=peer_id)

    if callback.message:
        await callback.message.answer(
            "Введите новый комментарий.\n"
            "Чтобы очистить — отправьте один дефис: -",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(BotState.waiting_peer_comment)
async def state_peer_comment(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    data = await state.get_data()
    peer_id = int(data["peer_id"])
    value = (message.text or "").strip()
    if value == "-":
        value = ""

    try:
        peer = await api.update_peer(peer_id, comment=value)
        actor_id, actor_username = actor_info(message)
        db.audit(actor_id, actor_username, "peer_comment_update", "peer", str(peer_id), peer_title(peer))
        await state.clear()
        await message.answer(
            "<b>✅ Комментарий обновлён</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть клиента", callback_data=f"peer:{peer_id}")]]
            ),
        )
    except ApiError as exc:
        await message.answer(f"❌ Ошибка: {exc}")


@router.callback_query(F.data == "i_settings")
async def cb_i_settings(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    vals = db.get_i_values()
    i1 = vals.get("I1")

    lines = ["<b>⚙️ Параметры I1-I5</b>", ""]
    if not i1:
        lines.append("<b>I1:</b> ⚠️ НЕ ЗАДАН ⚠️")
        lines.append("")
        lines.append("Пока I1 не задан, I2-I5 недоступны.")
    else:
        for i in range(1, 6):
            lines.append(f"<b>I{i}:</b> <code>{h(vals.get(f'I{i}'))}</code>\n➖➖➖\n")

    rows = []
    if not i1:
        rows.append([InlineKeyboardButton(text="Изменить I1", callback_data="set_i:1")])
    else:
        for i in range(1, 6):
            rows.append([InlineKeyboardButton(text=f"Изменить I{i}", callback_data=f"set_i:{i}")])

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])

    await safe_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("set_i:"))
async def cb_set_i(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    i_num = int(callback.data.split(":")[1])
    i1 = db.get_setting("i1")

    if i_num > 1 and not i1:
        await callback.answer("Сначала задайте I1", show_alert=True)
        return

    await state.set_state(BotState.waiting_i_value)
    await state.update_data(i_num=i_num)

    if callback.message:
        extra = "\nЕсли очистить I1, I2-I5 тоже будут очищены." if i_num == 1 else ""
        await callback.message.answer(
            f"Введите значение I{i_num} полностью.\n"
            "Чтобы очистить — отправьте один дефис: -"
            f"{extra}",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(BotState.waiting_i_value)
async def state_i_value(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    data = await state.get_data()
    i_num = int(data["i_num"])
    value = (message.text or "").strip()
    if value == "-":
        value = None

    actor_id, actor_username = actor_info(message)
    db.set_i_value(i_num, value, actor_id or 0)
    db.audit(
        actor_id,
        actor_username,
        "i_setting_update",
        "setting",
        f"I{i_num}",
        f"I{i_num}",
        {"set": bool(value), "cleared_i2_i5": i_num == 1 and not value},
    )

    await state.clear()
    await message.answer(f"✅ I{i_num} обновлён.", reply_markup=main_menu())


@router.callback_query(F.data == "config_settings")
async def cb_config_settings(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    remove_ipv6 = db.bool_setting("remove_ipv6_from_address", True)

    await safe_edit(
        callback,
        "<b>🧩 Настройки отправляемого конфига</b>\n\n"
        f"Удалять IPv6 из Address: <b>{'да' if remove_ipv6 else 'нет'}</b>\n\n"
        "Эта настройка применяется только к копии .conf, которую бот отправляет в Telegram.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Оставлять IPv6" if remove_ipv6 else "Удалять IPv6",
                        callback_data="toggle_remove_ipv6",
                    )
                ],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "toggle_remove_ipv6")
async def cb_toggle_remove_ipv6(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    actor_id, actor_username = actor_info(callback)
    current = db.bool_setting("remove_ipv6_from_address", True)
    new_value = "0" if current else "1"

    db.set_setting("remove_ipv6_from_address", new_value, actor_id or 0)
    db.audit(
        actor_id,
        actor_username,
        "remove_ipv6_setting_update",
        "setting",
        "remove_ipv6_from_address",
        details={"value": bool(int(new_value))},
    )

    await callback.answer("Настройка изменена", show_alert=True)
    await cb_config_settings(callback)


@router.callback_query(F.data == "dns")
async def cb_dns(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    active, servers = db.active_dns()
    presets = db.list_dns_presets()

    lines = [
        "<b>🌐 DNS-пресеты</b>",
        "",
        f"Активный: <b>{h(active)}</b>",
        f"DNS: <code>{h(', '.join(servers))}</code>",
        "",
    ]

    for p in presets:
        dns_list = json.loads(p["servers"])
        lines.append(f"• <b>{h(p['name'])}</b>: <code>{h(', '.join(dns_list))}</code>")

    rows = []
    for p in presets:
        rows.append(
            [
                InlineKeyboardButton(text=f"Выбрать {p['name']}", callback_data=f"dns_active:{p['name']}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"dns_del:{p['name']}"),
            ]
        )

    rows.append([InlineKeyboardButton(text="➕ Добавить пресет", callback_data="dns_add")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])

    await safe_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("dns_active:"))
async def cb_dns_active(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    name = callback.data.split(":", 1)[1]
    if db.get_dns_preset(name) is None:
        await callback.answer("Пресет не найден", show_alert=True)
        return

    actor_id, actor_username = actor_info(callback)
    db.set_setting("active_dns_preset", name, actor_id or 0)
    db.audit(actor_id, actor_username, "dns_active_set", "dns", name, name)

    await callback.answer("DNS-пресет выбран", show_alert=True)
    await cb_dns(callback)


@router.callback_query(F.data.startswith("dns_del:"))
async def cb_dns_del(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    name = callback.data.split(":", 1)[1]
    try:
        db.delete_dns_preset(name)
        actor_id, actor_username = actor_info(callback)
        db.audit(actor_id, actor_username, "dns_preset_delete", "dns", name, name)
        await callback.answer("Удалено", show_alert=True)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)

    await cb_dns(callback)


@router.callback_query(F.data == "dns_add")
async def cb_dns_add(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    await state.set_state(BotState.waiting_dns_preset_name)
    if callback.message:
        await callback.message.answer(
            "Введите имя DNS-пресета. Например: cloudflare или google",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(BotState.waiting_dns_preset_name)
async def state_dns_name(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    name = (message.text or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        await message.answer("Некорректное имя. Разрешены a-z, 0-9, _ и -, максимум 32 символа.")
        return

    await state.update_data(dns_name=name)
    await state.set_state(BotState.waiting_dns_preset_servers)
    await message.answer(
        "Введите DNS через запятую. Например:\n1.1.1.1, 1.0.0.1, 8.8.8.8, 9.9.9.9",
        reply_markup=cancel_keyboard(),
    )


@router.message(BotState.waiting_dns_preset_servers)
async def state_dns_servers(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_admin(message):
        return

    servers = [x.strip() for x in (message.text or "").split(",") if x.strip()]
    if not servers:
        await message.answer("Список DNS пуст.", reply_markup=cancel_keyboard())
        return

    data = await state.get_data()
    name = data["dns_name"]

    actor_id, actor_username = actor_info(message)
    db.set_dns_preset(name, servers, actor_id or 0)
    db.audit(actor_id, actor_username, "dns_preset_set", "dns", name, name, {"servers": servers})

    await state.clear()
    await message.answer("✅ DNS-пресет сохранён.", reply_markup=main_menu())


@router.callback_query(F.data == "admins")
async def cb_admins(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    rows = db.list_admins()
    lines = ["<b>👮 Админы бота</b>", ""]
    for r in rows:
        lines.append(f"• <code>{r['telegram_id']}</code> — <b>{h(r['role'])}</b> {h(r['username'])}")

    markup_rows = []
    if db.is_owner(callback.from_user.id):
        markup_rows.append([InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add:admin")])
        markup_rows.append([InlineKeyboardButton(text="➕ Добавить владельца", callback_data="admin_add:owner")])
        markup_rows.append([InlineKeyboardButton(text="🗑 Удалить админа", callback_data="admin_remove")])
    markup_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])

    await safe_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=markup_rows))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_add:"))
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_owner(callback):
        return

    role = callback.data.split(":")[1]
    await state.set_state(BotState.waiting_admin_id)
    await state.update_data(role=role)

    if callback.message:
        await callback.message.answer(f"Введите Telegram ID для роли {role}.", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(BotState.waiting_admin_id)
async def state_admin_id(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_owner(message):
        return

    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("Telegram ID должен быть числом.", reply_markup=cancel_keyboard())
        return

    data = await state.get_data()
    role = data["role"]
    target_id = int(value)
    actor_id, actor_username = actor_info(message)

    db.add_admin(target_id, role, actor_id or 0)
    db.audit(actor_id, actor_username, "bot_admin_add", "admin", str(target_id), None, {"role": role})

    await state.clear()
    await message.answer(f"✅ Админ добавлен: {target_id} как {role}", reply_markup=main_menu())


@router.callback_query(F.data == "admin_remove")
async def cb_admin_remove(callback: CallbackQuery, state: FSMContext) -> None:
    if await reject_callback_if_not_owner(callback):
        return

    await state.set_state(BotState.waiting_remove_admin_id)
    if callback.message:
        await callback.message.answer("Введите Telegram ID админа для удаления.", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(BotState.waiting_remove_admin_id)
async def state_remove_admin(message: Message, state: FSMContext) -> None:
    if await reject_message_if_not_owner(message):
        return

    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("Telegram ID должен быть числом.", reply_markup=cancel_keyboard())
        return

    target_id = int(value)
    actor_id, actor_username = actor_info(message)

    try:
        db.remove_admin(target_id)
        db.audit(actor_id, actor_username, "bot_admin_remove", "admin", str(target_id))
        await state.clear()
        await message.answer(f"✅ Админ удалён: {target_id}", reply_markup=main_menu())
    except ValueError as exc:
        await message.answer(f"❌ {exc}")


@router.callback_query(F.data == "bot_log")
async def cb_bot_log(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    rows = db.list_audit(30)
    lines = ["<b>📜 Журнал действий бота</b>", ""]
    if not rows:
        lines.append("Пусто.")
    for r in rows:
        lines.append(
            f"• 🕰 {h(format_datetime_local(r['created_at']))} — <b>{h(r['action'])}</b>\n"
            f"  🤷 Кто: {h(r['actor_username'])} (<code>{h(r['actor_telegram_id'])}</code>)\n"
            f"  🤔 Кого: {h(r['target_type'])} <code>{h(r['target_id'])}</code> {h(r['target_name'])}"
        )

    await safe_edit(callback, "\n".join(lines), back_menu())
    await callback.answer()


@router.callback_query(F.data == "web_events")
async def cb_web_events(callback: CallbackQuery) -> None:
    if await reject_callback_if_not_admin(callback):
        return

    try:
        events = await api.events(cfg.events_limit)
        lines = ["<b>📋 Журнал amneziawg-web</b>", ""]
        if not events:
            lines.append("Пусто.")
        for e in events:
            lines.append(
                f"• {h(format_datetime_local(e.get('created_at')))} — <b>{h(e.get('event_type'))}</b>\n"
                f"  Кто: {h(e.get('actor'))}\n"
                f"  peer: <code>{h(e.get('peer_id'))}</code>\n"
                f"  Данные: <code>{h(e.get('payload'))}</code>"
            )
        await safe_edit(callback, "\n".join(lines), back_menu())
    except ApiError as exc:
        await safe_edit(callback, f"<b>❌ Ошибка журнала</b>\n\n<code>{h(exc)}</code>", back_menu())

    await callback.answer()


async def run_polling_once() -> None:
    log.info("Starting bot")
    log.info("AWG_WEB_BASE_URL=%s", cfg.awg_web_base_url)
    log.info("BOT_DB=%s", cfg.bot_db)

    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    traffic_task = asyncio.create_task(traffic_limit_worker())

    try:
        await dp.start_polling(bot)
    finally:
        traffic_task.cancel()
        try:
            await traffic_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


async def main() -> None:
    delay = 5

    while True:
        try:
            await run_polling_once()
            delay = 5

        except TelegramNetworkError as exc:
            log.error("Telegram API недоступен или сетевая ошибка Telegram: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

        except aiohttp.ClientError as exc:
            log.error("aiohttp ошибка Telegram/API: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception("Критическая ошибка polling")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


if __name__ == "__main__":
    asyncio.run(main())