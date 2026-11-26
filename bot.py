"""TUF Indexing Session v5 — standalone Discord bot for Go Up for Brainrots.

Python 3.10+, discord.py 2.4+. Start with /setup in the desired channel.
Regular members use buttons only. Staff can maintain the catalog from Discord.
"""
import asyncio
import logging
import os
import sqlite3
import io
import re
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN", "")
DB_PATH = Path(os.getenv("TUF_DB_PATH", "tuf_indexing.sqlite3"))
STAFF_ROLE_IDS = {int(s) for s in os.getenv("TUF_STAFF_ROLE_IDS", "").split(",") if s.strip().isdigit()}
SESSION_NOTIFY_ROLE_ID = os.getenv("TUF_SESSION_NOTIFY_ROLE_ID", "").strip()
# Initial mutation names can be edited/extended from the staff panel.
# The Brainrot catalog intentionally starts EMPTY until the verified names are provided.
INITIAL_MUTATIONS = [
    "Default", "Silver", "Gold", "Crystal", "BloodMoon", "Rainbow", "Candy",
    "Cyber", "Party", "Black & White", "Aqua", "Lava", "Football",
    "Summer", "Dreamy", "Galaxy", "Radioactive", "Halloween",
    "Día de Muertos", "Hacker", "Fairy",
]
log = logging.getLogger("tuf_indexing")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@contextmanager
def connect():
    """Commit/rollback safely and always close SQLite connections."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        with db:
            yield db
    finally:
        db.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS brainrots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL COLLATE NOCASE UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS mutations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL COLLATE NOCASE UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild_id INTEGER NOT NULL,
          channel_id INTEGER NOT NULL,
          message_id INTEGER,
          host_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_items (
          session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
          brainrot_id INTEGER NOT NULL REFERENCES brainrots(id),
          mutation_id INTEGER NOT NULL REFERENCES mutations(id),
          PRIMARY KEY (session_id, brainrot_id, mutation_id)
        );
        CREATE TABLE IF NOT EXISTS participants (
          session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL,
          joined_at TEXT NOT NULL,
          PRIMARY KEY (session_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS participant_items (
          session_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          brainrot_id INTEGER NOT NULL,
          mutation_id INTEGER NOT NULL,
          PRIMARY KEY (session_id, user_id, brainrot_id, mutation_id),
          FOREIGN KEY (session_id, user_id) REFERENCES participants(session_id, user_id) ON DELETE CASCADE,
          FOREIGN KEY (session_id, brainrot_id, mutation_id)
            REFERENCES session_items(session_id, brainrot_id, mutation_id) ON DELETE CASCADE
        );
        """)
        for col, definition in {
            "epic_name": "TEXT", "expires_at": "INTEGER", "thread_id": "INTEGER",
            "image_url": "TEXT", "closed_at": "TEXT"
        }.items():
            existing = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
            if col not in existing:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {definition}")
        # Custom names are stored as non-catalog Brainrots. They may be used in
        # sessions but never appear in the shared catalog until staff approves.
        columns = {r[1] for r in db.execute("PRAGMA table_info(brainrots)")}
        if "is_custom" not in columns:
            db.execute("ALTER TABLE brainrots ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0")
        db.execute("""CREATE TABLE IF NOT EXISTS catalog_suggestions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          brainrot_id INTEGER NOT NULL UNIQUE REFERENCES brainrots(id),
          suggested_by INTEGER NOT NULL,
          guild_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          suggested_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS guild_settings (
          guild_id INTEGER PRIMARY KEY, logs_channel_id INTEGER
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status_expires ON sessions(status, expires_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_guild_status ON sessions(guild_id, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_participants_session ON participants(session_id)")
        if not db.execute("SELECT 1 FROM mutations LIMIT 1").fetchone():
            db.executemany("INSERT OR IGNORE INTO mutations(name) VALUES (?)", [(m,) for m in INITIAL_MUTATIONS])


def query(sql, args=()):
    with connect() as db:
        return db.execute(sql, args).fetchall()


def execute(sql, args=()):
    with connect() as db:
        cur = db.execute(sql, args)
        return cur.lastrowid


def staff(member):
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator or member.guild_permissions.manage_guild
        or bool(STAFF_ROLE_IDS.intersection({r.id for r in member.roles}))
    )


def active_brainrots():
    return query("SELECT * FROM brainrots WHERE enabled=1 AND is_custom=0 ORDER BY name COLLATE NOCASE")


def active_mutations():
    return query("SELECT * FROM mutations WHERE enabled=1 ORDER BY name COLLATE NOCASE")


def session(session_id):
    rows = query("SELECT * FROM sessions WHERE id=?", (session_id,))
    return rows[0] if rows else None


def session_items(sid):
    return query("""SELECT si.brainrot_id, si.mutation_id, b.name AS brainrot, m.name AS mutation
        FROM session_items si JOIN brainrots b ON b.id=si.brainrot_id
        JOIN mutations m ON m.id=si.mutation_id WHERE si.session_id=?
        ORDER BY m.name COLLATE NOCASE, b.name COLLATE NOCASE""", (sid,))


def authorized(interaction, row):
    return row and interaction.guild_id == row["guild_id"] and (
        interaction.user.id == row["host_id"] or staff(interaction.user))


def make_session_embed(sid):
    row = session(sid)
    if not row:
        return discord.Embed(title="Session unavailable")
    items = session_items(sid)
    count = query("SELECT COUNT(*) n FROM participants WHERE session_id=?", (sid,))[0]["n"]
    status_icons = {"open": "🟢", "paused": "⏸️", "closed": "🔒"}
    embed = discord.Embed(title=f"📚 TUF Indexing Session #{sid}", color=0x8B5CF6,
                          description=f"**Go Up for Brainrots**\n👑 Host: <@{row['host_id']}>\n"
                                      f"{status_icons.get(row['status'], '📋')} Status: **{row['status'].title()}**\n"
                                      f"👥 Participants: **{count}**\n📦 Item/mutation combinations: **{len(items)}**")
    groups = defaultdict(list)
    for item in items:
        groups[item["mutation"]].append(item["brainrot"])
    text = "\n".join(f"**{mutation}** — {', '.join(names)}" for mutation, names in groups.items())
    if not text:
        text = "No items selected yet."
    if len(text) > 3400:
        text = text[:3380].rsplit(",", 1)[0] + "…\n*Use View Items for the full list.*"
    extra = []
    if row["expires_at"]:
        extra.append(f"⏳ Available until: <t:{row['expires_at']}:F> • <t:{row['expires_at']}:R>")
    if row["epic_name"]:
        extra.append(f"🎮 Epic: **{discord.utils.escape_markdown(row['epic_name'])}**")
    if row["thread_id"]:
        extra.append(f"💬 Post in session: <#{row['thread_id']}>")
    if extra:
        embed.add_field(name="📅 Session Details", value="\n".join(extra)[:1024], inline=False)
    if row["image_url"]:
        embed.set_image(url=row["image_url"])
    embed.add_field(name="🧬 Available Brainrots", value=text[:1024] if len(text) <= 1024 else text[:1000] + "…\n*Use View Items for the full list.*", inline=False)
    embed.set_footer(text="Members can choose the exact items they need using the buttons below.")
    embed.description = discord.utils.escape_mentions(embed.description or "")
    return embed


async def reply(interaction, content=None, *, embed=None, view=None, ephemeral=True):
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)


# Permanent public guide panel. Each guide button answers privately so the
# published guide stays clean and does not need a separate command per topic.
def guide_embed(section="overview"):
    e = discord.Embed(color=0x8B5CF6)
    if section == "members":
        e.title = "✅ TUF Guide • Joining a Session"
        e.description = (
            "**1.** Open **📚 TUF Indexing Session** and tap **Active Sessions**.\n"
            "**2.** Open a session, then tap **Join / Choose Items**.\n"
            "**3.** Tap every Brainrot + mutation combination you need; selected items turn green.\n"
            "**4.** Tap **Save My Items** to join.\n"
            "**5.** Use **Post / Chat** to open the session's discussion thread and talk to the host or share pictures.\n"
            "**6.** Use **Leave Session** if you can no longer attend.\n\n"
            "**Tip:** **View Items** shows what's available; **Participants** shows who's joining. "
            "Session discussions are visible to people who can access the thread."
        )
    elif section == "hosts":
        e.title = "🟢 TUF Guide • Hosting a Session"
        e.description = (
            "**1.** Tap **Create Session** on the main panel.\n"
            "**2.** Choose a **mutation**, then tap multiple Brainrot buttons to select items.\n"
            "**3.** Use **Change Mutation** to add more combinations to the same session.\n"
            "**4.** Missing a Brainrot? Choose **Custom Name**, then enter names one per line. "
            "Your custom names can stay in this session without changing the public catalog.\n"
            "**5.** Tap **Review / Publish**, check your list, and choose **Publish / Save**.\n"
            "**6.** On the published session, tap **Host Controls → Set Time / Epic**. "
            "Enter available **minutes** (0 removes the timer) and optionally your **Epic username**.\n"
            "**7.** Use **Host Controls** to edit items, pause, reopen, upload a screenshot, or close.\n\n"
            "**⏳ Timer:** Discord shows the end time and relative countdown. At expiry, "
            "the bot closes the session and attempts to archive its discussion. Pausing does **not** stop the timer."
        )
    elif section == "images":
        e.title = "📸 TUF Guide • Pictures & Custom Names"
        e.description = (
            "**Hosts — add an item picture:** Open **Host Controls → Upload Item Picture**, "
            "then upload your screenshot in the **session discussion thread** when prompted.\n"
            "• **Save Detected Names** saves the picture to the session panel; it does **not** add items.\n"
            "• **Edit / Enter Names** lets you correct the OCR text and enter a mutation, "
            "then adds those combinations to your session.\n\n"
            "**Staff — add names to the shared catalog:** In **Brainrot Management**, "
            "choose **Add From Picture**, upload your image in that channel, "
            "review or correct the names, and confirm.\n\n"
            "**Note:** Upload within **5 minutes** of the prompt. OCR can mistake labels for names, "
            "so always verify before saving. Image recognition requires Tesseract on the bot host."
        )
    elif section == "staff":
        e.title = "⚙️ TUF Guide • Staff Management"
        e.description = (
            "**Post panels (staff only):**\n"
            "• `/setup` — main indexing panel.\n"
            "• `/setup_guide` — this public help panel.\n"
            "• `/setup_management` — Brainrot management panel in a **staff-only** channel.\n"
            "• `/setup_logs` — choose where closed-session summaries/transcripts go.\n\n"
            "**Catalog:** Use the management buttons to add names (manually or by screenshot), "
            "rename, enable/disable, review custom-name suggestions, and manage mutations.\n"
            "**Logs:** Closed sessions appear under **Closed Session Logs** on the main panel; "
            "the configured log channel receives a summary and an attempted thread transcript.\n"
            "**Permissions:** Management actions require Manage Server, Administrator, or a "
            "configured TUF staff role. Keep the management and log channels restricted."
        )
    else:
        e.title = "📚 TUF Indexing Session • How to Use"
        e.description = (
            "**Go Up for Brainrots** 💜\n\n"
            "Welcome! Use the buttons below to learn how to **join a session**, "
            "**host multiple Brainrots and mutations in one session**, "
            "**upload screenshots**, and **manage the catalog**.\n\n"
            "**👥 Members:** Join a live session and select the exact items you need.\n"
            "**🟢 Hosts:** Publish a list of available items, set an optional Epic name and countdown, "
            "and chat with members in the session thread.\n"
            "**📸 Images:** Screenshots can suggest names, which must be reviewed.\n"
            "**⚙️ Staff:** Add new Brainrots and mutations; review suggestions; configure logs.\n\n"
            "**Start from the main TUF Indexing Session panel.**"
        )
        e.set_footer(text="Choose a guide button below. Answers are shown privately; this panel stays posted.")
    return e


class GuidePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Overview", emoji="📚", style=discord.ButtonStyle.secondary, custom_id="tuf:guide:overview")
    async def overview(self, interaction, button):
        await reply(interaction, embed=guide_embed("overview"))

    @discord.ui.button(label="For Members", emoji="👥", style=discord.ButtonStyle.primary, custom_id="tuf:guide:members")
    async def members(self, interaction, button):
        await reply(interaction, embed=guide_embed("members"))

    @discord.ui.button(label="For Hosts", emoji="🟢", style=discord.ButtonStyle.success, custom_id="tuf:guide:hosts")
    async def hosts(self, interaction, button):
        await reply(interaction, embed=guide_embed("hosts"))

    @discord.ui.button(label="Pictures", emoji="📸", style=discord.ButtonStyle.secondary, custom_id="tuf:guide:images")
    async def images(self, interaction, button):
        await reply(interaction, embed=guide_embed("images"))

    @discord.ui.button(label="Staff Guide", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="tuf:guide:staff")
    async def staff_guide(self, interaction, button):
        # This is instructional, not staff-only data; staff actions are permission checked.
        await reply(interaction, embed=guide_embed("staff"))


class MainPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Session", emoji="🟢", style=discord.ButtonStyle.success, custom_id="tuf:main:create")
    async def create(self, interaction, button):
        draft = SessionDraft(interaction.user.id)
        await reply(interaction, embed=draft.embed(), view=MutationPicker(draft))

    @discord.ui.button(label="Active Sessions", emoji="📋", style=discord.ButtonStyle.primary, custom_id="tuf:main:active")
    async def active(self, interaction, button):
        rows = query("SELECT id, host_id, status FROM sessions WHERE guild_id=? AND status != 'closed' ORDER BY id DESC LIMIT 20", (interaction.guild_id,))
        msg = "\n".join(f"**#{r['id']}** • {r['status']} • <@{r['host_id']}>" for r in rows) or "No active sessions yet."
        await reply(interaction, embed=discord.Embed(title="📋 Active TUF Sessions", description=msg, color=0x8B5CF6), view=BrowseSessions(rows))

    @discord.ui.button(label="View Brainrots", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="tuf:main:catalog")
    async def catalog(self, interaction, button):
        await send_catalog(interaction, "brainrots")

    @discord.ui.button(label="My Sessions", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="tuf:main:mine")
    async def mine(self, interaction, button):
        rows = query("""SELECT DISTINCT s.id, s.host_id, s.status FROM sessions s
            LEFT JOIN participants p ON p.session_id=s.id
            WHERE s.guild_id=? AND (s.host_id=? OR p.user_id=?)
            ORDER BY s.id DESC LIMIT 20""", (interaction.guild_id, interaction.user.id, interaction.user.id))
        msg = "\n".join(f"**#{r['id']}** • {r['status']} • <@{r['host_id']}>" for r in rows) or "You haven't joined or hosted a session yet."
        await reply(interaction, embed=discord.Embed(title="👤 My Sessions", description=msg, color=0x8B5CF6), view=BrowseSessions(rows))


    @discord.ui.button(label="Closed Session Logs", emoji="📁", style=discord.ButtonStyle.secondary, custom_id="tuf:main:closed")
    async def closed(self, interaction, button):
        rows = query("SELECT id, host_id, status FROM sessions WHERE guild_id=? AND status='closed' ORDER BY id DESC LIMIT 20", (interaction.guild_id,))
        desc = "\n".join(f"**#{r['id']}** • <@{r['host_id']}> • Closed" for r in rows) or "No closed sessions yet."
        await reply(interaction, embed=discord.Embed(title="📁 Closed TUF Indexing Sessions", description=desc, color=0x8B5CF6), view=BrowseSessions(rows))


    @discord.ui.button(label="How to Use", emoji="❔", style=discord.ButtonStyle.secondary, custom_id="tuf:main:guide", row=1)
    async def guide(self, interaction, button):
        await reply(interaction, embed=guide_embed(), view=GuidePanel())


class BrowseSessions(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=180)
        for r in rows[:20]:
            self.add_item(OpenSessionButton(r["id"]))


class OpenSessionButton(discord.ui.Button):
    def __init__(self, sid):
        super().__init__(label=f"Session #{sid}", style=discord.ButtonStyle.secondary)
        self.sid = sid

    async def callback(self, interaction):
        row = session(self.sid)
        if not row or row["guild_id"] != interaction.guild_id:
            return await reply(interaction, "Session not found.")
        await reply(interaction, embed=make_session_embed(self.sid), view=SessionButtons(self.sid, persistent=False))


class SessionDraft:
    def __init__(self, owner_id, sid=None):
        self.owner_id = owner_id
        self.sid = sid
        self.items = {(r["brainrot_id"], r["mutation_id"]) for r in session_items(sid)} if sid else set()
        self.mutation_id = None

    def embed(self):
        names = {r["id"]: r["name"] for r in active_mutations()}
        return discord.Embed(title="🧬 TUF — Build Your Session", color=0x8B5CF6,
            description=f"Select a mutation, then tap multiple Brainrot buttons to add/remove them.\n"
                        f"You can switch mutations and keep adding items to **one session**.\n\n"
                        f"📦 **{len(self.items)} item/mutation combinations selected**\n"
                        f"🧬 Current mutation: **{names.get(self.mutation_id, 'None selected')}**\n\n"
                        "Use **Custom Name** to add Brainrots not in the catalog.\nPress **Review / Publish** when finished.")


def owner_only(interaction, draft):
    return interaction.user.id == draft.owner_id


class MutationPicker(discord.ui.View):
    def __init__(self, draft, page=0):
        super().__init__(timeout=900)
        self.draft, self.page = draft, page
        rows = active_mutations()
        self.pages = max(1, (len(rows) + 14) // 15)
        self.page = min(max(0, page), self.pages - 1)
        for row in rows[self.page * 15:(self.page + 1) * 15]:
            self.add_item(MutationButton(draft, row["id"], row["name"]))
        self.add_item(DraftPageButton("◀ Previous", self, -1))
        self.add_item(DraftPageButton("Next ▶", self, 1))
        self.add_item(DraftActionButton("Review / Publish", draft, "review", discord.ButtonStyle.success))
        self.add_item(DraftActionButton("Cancel", draft, "cancel", discord.ButtonStyle.danger))


class MutationButton(discord.ui.Button):
    def __init__(self, draft, mid, name):
        super().__init__(label=name[:80], style=discord.ButtonStyle.primary)
        self.draft, self.mid = draft, mid

    async def callback(self, interaction):
        if not owner_only(interaction, self.draft):
            return await reply(interaction, "Only the session creator can edit this draft.")
        self.draft.mutation_id = self.mid
        await interaction.response.edit_message(embed=self.draft.embed(), view=BrainrotPicker(self.draft))


class DraftPageButton(discord.ui.Button):
    def __init__(self, label, parent, delta):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=not (0 <= parent.page + delta < parent.pages))
        self.parent, self.delta = parent, delta

    async def callback(self, interaction):
        if not owner_only(interaction, self.parent.draft):
            return await reply(interaction, "This isn't your draft.")
        klass = MutationPicker if isinstance(self.parent, MutationPicker) else BrainrotPicker
        await interaction.response.edit_message(embed=self.parent.draft.embed(), view=klass(self.parent.draft, self.parent.page + self.delta))


class BrainrotPicker(discord.ui.View):
    def __init__(self, draft, page=0):
        super().__init__(timeout=900)
        self.draft, self.page = draft, page
        rows = active_brainrots()
        self.pages = max(1, (len(rows) + 14) // 15)
        self.page = min(max(0, page), self.pages - 1)
        for row in rows[self.page * 15:(self.page + 1) * 15]:
            selected = (row["id"], draft.mutation_id) in draft.items
            self.add_item(BrainrotButton(draft, row["id"], row["name"], selected))
        self.add_item(DraftPageButton("◀ Previous", self, -1))
        self.add_item(DraftPageButton("Next ▶", self, 1))
        self.add_item(DraftActionButton("Custom Name", draft, "custom", discord.ButtonStyle.success))
        self.add_item(DraftActionButton("My Custom Items", draft, "custom_list", discord.ButtonStyle.secondary))
        self.add_item(DraftActionButton("Change Mutation", draft, "mutation", discord.ButtonStyle.primary))
        self.add_item(DraftActionButton("Review / Publish", draft, "review", discord.ButtonStyle.success))
        self.add_item(DraftActionButton("Cancel", draft, "cancel", discord.ButtonStyle.danger))


class BrainrotButton(discord.ui.Button):
    def __init__(self, draft, bid, name, selected):
        super().__init__(label=name[:72], emoji="✅" if selected else "➕",
                         style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary)
        self.draft, self.bid = draft, bid

    async def callback(self, interaction):
        if not owner_only(interaction, self.draft):
            return await reply(interaction, "This isn't your draft.")
        item = (self.bid, self.draft.mutation_id)
        if item in self.draft.items:
            self.draft.items.remove(item)
        else:
            self.draft.items.add(item)
        parent = self.view
        await interaction.response.edit_message(embed=self.draft.embed(), view=BrainrotPicker(self.draft, parent.page))


class DraftActionButton(discord.ui.Button):
    def __init__(self, label, draft, action, style):
        super().__init__(label=label, style=style)
        self.draft, self.action = draft, action

    async def callback(self, interaction):
        if not owner_only(interaction, self.draft):
            return await reply(interaction, "This isn't your draft.")
        if self.action == "custom":
            if not self.draft.mutation_id:
                return await reply(interaction, "Select a mutation first.")
            return await interaction.response.send_modal(CustomBrainrotModal(self.draft))
        if self.action == "custom_list":
            return await interaction.response.edit_message(embed=self.draft.embed(), view=CustomItemsView(self.draft))
        if self.action == "items":
            return await interaction.response.edit_message(embed=self.draft.embed(), view=BrainrotPicker(self.draft))
        if self.action == "suggest":
            added = submit_suggestions(self.draft, interaction.guild_id, interaction.user.id)
            return await reply(interaction, f"📥 Submitted **{added}** new custom Brainrot names for staff review. Existing pending suggestions were skipped.")
        if self.action == "mutation":
            return await interaction.response.edit_message(embed=self.draft.embed(), view=MutationPicker(self.draft))
        if self.action == "cancel":
            return await interaction.response.edit_message(content="Session draft cancelled.", embed=None, view=None)
        if self.action == "review":
            await interaction.response.edit_message(embed=draft_review(self.draft), view=DraftConfirm(self.draft))



class CustomBrainrotModal(discord.ui.Modal, title="✏️ Add Custom Brainrots"):
    names = discord.ui.TextInput(
        label="Brainrot names (one per line)",
        placeholder="Enter one or several names for the selected mutation",
        style=discord.TextStyle.paragraph, min_length=1, max_length=900)

    def __init__(self, draft):
        super().__init__()
        self.draft = draft
        self.mutation_id = draft.mutation_id

    async def on_submit(self, interaction):
        if not owner_only(interaction, self.draft):
            return await reply(interaction, "This isn't your session draft.")
        if self.mutation_id not in {r["id"] for r in active_mutations()}:
            return await reply(interaction, "That mutation is no longer available. Please select another.")
        raw = [re.sub(r"\s+", " ", n).strip() for n in str(self.names).splitlines()]
        names = list(dict.fromkeys(n for n in raw if n))
        if not names or len(names) > 20 or any(len(n) > 100 for n in names):
            return await reply(interaction, "Enter 1–20 names, each no more than 100 characters.")
        added = 0
        with connect() as db:
            for name in names:
                row = db.execute("SELECT id FROM brainrots WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                if row:
                    bid = row["id"]
                else:
                    bid = db.execute(
                        "INSERT INTO brainrots(name, enabled, is_custom) VALUES (?,0,1)", (name,)).lastrowid
                item = (bid, self.mutation_id)
                if item not in self.draft.items:
                    self.draft.items.add(item)
                    added += 1
        await reply(interaction,
                    f"✅ Added **{added}** item/mutation combinations to your draft. "
                    "Custom names stay outside the public catalog unless staff approves them.",
                    embed=self.draft.embed(), view=BrainrotPicker(self.draft))


class CustomItemsView(discord.ui.View):
    def __init__(self, draft, page=0):
        super().__init__(timeout=900)
        self.draft = draft
        ids = {r["id"] for r in query("SELECT id FROM brainrots WHERE is_custom=1")}
        rows = [(bid, mid) for bid, mid in draft.items if bid in ids]
        mu = {r["id"]: r["name"] for r in query("SELECT * FROM mutations")}
        br = {r["id"]: r["name"] for r in query("SELECT * FROM brainrots WHERE is_custom=1")}
        rows.sort(key=lambda pair: (br.get(pair[0], "").casefold(), mu.get(pair[1], "")))
        self.pages = max(1, (len(rows) + 14) // 15)
        self.page = max(0, min(page, self.pages - 1))
        for bid, mid in rows[self.page * 15:(self.page + 1) * 15]:
            self.add_item(RemoveCustomButton(draft, bid, mid, f"{mu.get(mid,'?')} • {br.get(bid,'?')}"))
        self.add_item(CustomNavButton(draft, self.page - 1, "◀ Previous", self.page == 0))
        self.add_item(CustomNavButton(draft, self.page + 1, "Next ▶", self.page + 1 >= self.pages))
        self.add_item(DraftActionButton("Back to Items", draft, "items", discord.ButtonStyle.primary))
        self.add_item(DraftActionButton("Suggest to Catalog", draft, "suggest", discord.ButtonStyle.success))
        self.add_item(DraftActionButton("Review / Publish", draft, "review", discord.ButtonStyle.secondary))


class RemoveCustomButton(discord.ui.Button):
    def __init__(self, draft, bid, mid, name):
        super().__init__(label=name[:80], emoji="❌", style=discord.ButtonStyle.danger)
        self.draft, self.bid, self.mid = draft, bid, mid

    async def callback(self, interaction):
        if not owner_only(interaction, self.draft):
            return await reply(interaction, "This isn't your session draft.")
        self.draft.items.discard((self.bid, self.mid))
        await interaction.response.edit_message(embed=self.draft.embed(), view=CustomItemsView(self.draft))


class CustomNavButton(discord.ui.Button):
    def __init__(self, draft, page, label, disabled):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.draft, self.page = draft, page

    async def callback(self, interaction):
        if not owner_only(interaction, self.draft):
            return await reply(interaction, "This isn't your session draft.")
        await interaction.response.edit_message(embed=self.draft.embed(), view=CustomItemsView(self.draft, self.page))


def submit_suggestions(draft, guild_id, author_id):
    custom_ids = {r["id"] for r in query("SELECT id FROM brainrots WHERE is_custom=1")}
    ids = {bid for bid, _ in draft.items if bid in custom_ids}
    added = 0
    with connect() as db:
        for bid in ids:
            before = db.total_changes
            db.execute("""INSERT OR IGNORE INTO catalog_suggestions
                (brainrot_id, suggested_by, guild_id, suggested_at) VALUES (?,?,?,?)""",
                (bid, author_id, guild_id, datetime.now(timezone.utc).isoformat()))
            if db.total_changes > before:
                added += 1
    return added


class CatalogSuggestions(discord.ui.View):
    def __init__(self, guild_id, page=0):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        rows = query("""SELECT s.id, b.name FROM catalog_suggestions s
            JOIN brainrots b ON b.id=s.brainrot_id WHERE s.guild_id=? AND s.status='pending'
            ORDER BY s.id LIMIT 300""", (guild_id,))
        self.pages = max(1, (len(rows)+9)//10)
        self.page = max(0, min(page, self.pages-1))
        for r in rows[self.page*10:(self.page+1)*10]:
            self.add_item(SuggestionButton(r["id"], r["name"], "approve"))
            self.add_item(SuggestionButton(r["id"], r["name"], "deny"))
        self.add_item(SuggestionsNav(guild_id, self.page-1, "◀ Previous", self.page == 0))
        self.add_item(SuggestionsNav(guild_id, self.page+1, "Next ▶", self.page+1 >= self.pages))


class SuggestionButton(discord.ui.Button):
    def __init__(self, suggestion_id, name, action):
        super().__init__(label=("Approve " if action=="approve" else "Deny ") + name[:60],
                         emoji="✅" if action=="approve" else "❌",
                         style=discord.ButtonStyle.success if action=="approve" else discord.ButtonStyle.danger)
        self.suggestion_id, self.action = suggestion_id, action

    async def callback(self, interaction):
        if not staff(interaction.user):
            return await reply(interaction, "Only staff can review catalog suggestions.")
        with connect() as db:
            suggestion = db.execute("SELECT * FROM catalog_suggestions WHERE id=? AND guild_id=? AND status='pending'",
                                    (self.suggestion_id, interaction.guild_id)).fetchone()
            if not suggestion:
                return await reply(interaction, "This suggestion was already reviewed.")
            db.execute("UPDATE catalog_suggestions SET status=? WHERE id=?",
                       ("approved" if self.action=="approve" else "denied", self.suggestion_id))
            if self.action == "approve":
                db.execute("UPDATE brainrots SET is_custom=0, enabled=1 WHERE id=?", (suggestion["brainrot_id"],))
        await interaction.response.edit_message(content=f"✅ Suggestion {self.action}d.", view=CatalogSuggestions(interaction.guild_id))


class SuggestionsNav(discord.ui.Button):
    def __init__(self, guild_id, page, label, disabled):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.guild_id, self.page = guild_id, page

    async def callback(self, interaction):
        if not staff(interaction.user):
            return await reply(interaction, "Only staff can review suggestions.")
        await interaction.response.edit_message(view=CatalogSuggestions(self.guild_id, self.page))

def draft_review(draft):
    br = {r["id"]: r["name"] for r in query("SELECT * FROM brainrots")}
    mu = {r["id"]: r["name"] for r in query("SELECT * FROM mutations")}
    groups = defaultdict(list)
    for bid, mid in sorted(draft.items, key=lambda x: (mu.get(x[1], ""), br.get(x[0], ""))):
        groups[mu.get(mid, "Unknown")].append(br.get(bid, "Unknown"))
    msg = "\n".join(f"**{m}**: {', '.join(b)}" for m, b in groups.items()) or "Nothing selected."
    if len(msg) > 3800:
        msg = msg[:3750] + "…"
    return discord.Embed(title="📚 Review TUF Indexing Session", color=0x8B5CF6,
                         description=f"**{len(draft.items)} combinations**\n\n{msg}")


class DraftConfirm(discord.ui.View):
    def __init__(self, draft):
        super().__init__(timeout=900)
        self.draft = draft

    @discord.ui.button(label="Back to Items", emoji="➕", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        if not owner_only(interaction, self.draft): return await reply(interaction, "This isn't your draft.")
        await interaction.response.edit_message(embed=self.draft.embed(), view=MutationPicker(self.draft))

    @discord.ui.button(label="Publish / Save", emoji="✅", style=discord.ButtonStyle.success)
    async def publish(self, interaction, button):
        d = self.draft
        if not owner_only(interaction, d): return await reply(interaction, "This isn't your draft.")
        if not d.items: return await reply(interaction, "Add at least one Brainrot and mutation first.")
        if getattr(d, "publishing", False):
            return await reply(interaction, "⏳ This session is already being published.")
        d.publishing = True
        if d.sid:
            row = session(d.sid)
            if not authorized(interaction, row) or row["status"] == "closed":
                return await reply(interaction, "You can't edit this session.")
            with connect() as db:
                # Keep participant selections where the exact item is still offered.
                for r in session_items(d.sid):
                    if (r["brainrot_id"], r["mutation_id"]) not in d.items:
                        db.execute("DELETE FROM session_items WHERE session_id=? AND brainrot_id=? AND mutation_id=?",
                                   (d.sid, r["brainrot_id"], r["mutation_id"]))
                db.executemany("INSERT OR IGNORE INTO session_items VALUES (?,?,?)", [(d.sid, b, m) for b, m in d.items])
            await refresh_session(d.sid)
            await interaction.response.edit_message(content=f"✅ Session #{d.sid} updated.", embed=None, view=None)
            d.publishing = False
            return
        await interaction.response.defer(ephemeral=True)
        try:
            with connect() as db:
                cur = db.execute("INSERT INTO sessions (guild_id,channel_id,host_id,created_at) VALUES (?,?,?,?)",
                                 (interaction.guild_id, interaction.channel_id, interaction.user.id, datetime.now(timezone.utc).isoformat()))
                sid = cur.lastrowid
                db.executemany("INSERT INTO session_items VALUES (?,?,?)", [(sid, b, m) for b, m in d.items])
            # Post publicly to the channel containing the panel.
            role_mention = f"<@&{SESSION_NOTIFY_ROLE_ID}>" if SESSION_NOTIFY_ROLE_ID.isdigit() else None
            msg = await interaction.channel.send(content=role_mention, embed=make_session_embed(sid), view=SessionButtons(sid),
                                                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False) if role_mention else discord.AllowedMentions.none())
            execute("UPDATE sessions SET message_id=? WHERE id=?", (msg.id, sid))
            try:
                thread = await msg.create_thread(name=f"TUF Indexing Session #{sid}", auto_archive_duration=1440)
                execute("UPDATE sessions SET thread_id=? WHERE id=?", (thread.id, sid))
                await thread.send("📚 This is the discussion for this indexing session. Members can post, ask questions and share item screenshots here. The host can set a countdown and upload an item picture from **Host Controls**.")
                await refresh_session(sid)
            except discord.HTTPException:
                log.warning("Thread creation failed for session %s; check Create Public Threads permissions", sid)
            await interaction.edit_original_response(content=f"✅ Session #{sid} published in {interaction.channel.mention}!", embed=None, view=None)
        except (discord.HTTPException, sqlite3.Error) as e:
            log.exception("Could not publish session")
            d.publishing = False
            await interaction.followup.send("⚠️ Could not publish the session. Staff can check the bot console. If a session panel was posted, don't publish again until checking it.", ephemeral=True)


async def refresh_session(sid):
    row = session(sid)
    if not row or not row["message_id"]:
        return
    try:
        channel = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(row["channel_id"])
        msg = await channel.fetch_message(row["message_id"])
        await msg.edit(embed=make_session_embed(sid), view=SessionButtons(sid))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Cannot refresh published session %s", sid)


class SessionButtons(discord.ui.View):
    def __init__(self, sid, persistent=True):
        super().__init__(timeout=None if persistent else 180)
        self.sid = sid
        self.add_item(SessionButton("Join / Choose Items", "✅", "join", sid, discord.ButtonStyle.success))
        self.add_item(SessionButton("View Items", "📋", "items", sid, discord.ButtonStyle.primary))
        self.add_item(SessionButton("Participants", "👥", "people", sid, discord.ButtonStyle.secondary))
        self.add_item(SessionButton("Leave Session", "🚪", "leave", sid, discord.ButtonStyle.secondary))
        self.add_item(SessionButton("Host Controls", "⚙️", "manage", sid, discord.ButtonStyle.secondary))
        self.add_item(SessionButton("Post / Chat", "💬", "thread", sid, discord.ButtonStyle.primary))


class SessionButton(discord.ui.Button):
    def __init__(self, label, emoji, action, sid, style):
        super().__init__(label=label, emoji=emoji, style=style, custom_id=f"tuf:s:{sid}:{action}")
        self.sid, self.action = sid, action

    async def callback(self, interaction):
        row = session(self.sid)
        if not row or row["guild_id"] != interaction.guild_id:
            return await reply(interaction, "Session not found in this server.")
        if self.action == "thread":
            return await reply(interaction, f"💬 Post messages and screenshots in <#{row['thread_id']}>." if row["thread_id"] else "No session thread was created. Ask staff to grant Create Public Threads permission.")
        if self.action == "join":
            if row["status"] != "open": return await reply(interaction, "This session isn't open for joining.")
            return await reply(interaction, embed=discord.Embed(title=f"✅ Session #{self.sid}: Choose What You Need",
                description="Tap each available item to select it. Selected buttons turn green. Then press **Save My Items**.", color=0x8B5CF6),
                view=ParticipantPicker(self.sid, interaction.user.id))
        if self.action == "items":
            items = session_items(self.sid)
            lines = [f"• **{r['mutation']}** — {r['brainrot']}" for r in items]
            return await reply(interaction, embed=discord.Embed(title=f"📦 Session #{self.sid} Items", description="\n".join(lines[:35])[:3900] or "No items.", color=0x8B5CF6),
                               view=ItemListPages(self.sid, 0))
        if self.action == "people":
            rows = query("""SELECT p.user_id, COUNT(pi.brainrot_id) AS items
                FROM participants p LEFT JOIN participant_items pi
                ON pi.session_id=p.session_id AND pi.user_id=p.user_id
                WHERE p.session_id=? GROUP BY p.user_id ORDER BY p.joined_at""", (self.sid,))
            desc = "\n".join(f"<@{r['user_id']}> — {r['items']} items" for r in rows)
            return await reply(interaction, embed=discord.Embed(title=f"👥 Session #{self.sid} Participants", description=desc[:3900] or "Nobody has joined yet.", color=0x8B5CF6))
        if self.action == "leave":
            execute("DELETE FROM participants WHERE session_id=? AND user_id=?", (self.sid, interaction.user.id))
            await refresh_session(self.sid)
            return await reply(interaction, "✅ You left this session.")
        if self.action == "manage":
            if not authorized(interaction, row): return await reply(interaction, "Only the host or authorized staff can manage this session.")
            return await reply(interaction, embed=make_session_embed(self.sid), view=HostControls(self.sid))


class ItemListPages(discord.ui.View):
    def __init__(self, sid, page):
        super().__init__(timeout=180)
        self.sid, self.page = sid, page
        items = session_items(sid)
        self.pages = max(1, (len(items) + 34) // 35)
        self.add_item(ItemNavButton("◀ Previous", sid, max(0, page - 1), page == 0))
        self.add_item(ItemNavButton("Next ▶", sid, page + 1, page + 1 >= self.pages))


class ItemNavButton(discord.ui.Button):
    def __init__(self, label, sid, page, disabled):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.sid, self.page = sid, page

    async def callback(self, interaction):
        items = session_items(self.sid)
        lines = [f"• **{r['mutation']}** — {r['brainrot']}" for r in items[self.page * 35:(self.page + 1) * 35]]
        await interaction.response.edit_message(embed=discord.Embed(title=f"📦 Session #{self.sid} Items — Page {self.page + 1}",
             description="\n".join(lines)[:3900] or "No items.", color=0x8B5CF6), view=ItemListPages(self.sid, self.page))


class ParticipantPicker(discord.ui.View):
    def __init__(self, sid, user_id, page=0, selected=None):
        super().__init__(timeout=900)
        self.sid, self.user_id, self.page = sid, user_id, page
        self.selected = selected if selected is not None else {
            (r["brainrot_id"], r["mutation_id"]) for r in query("SELECT brainrot_id,mutation_id FROM participant_items WHERE session_id=? AND user_id=?", (sid, user_id))}
        rows = session_items(sid)
        self.pages = max(1, (len(rows) + 14) // 15)
        self.page = min(max(0, page), self.pages - 1)
        for r in rows[self.page * 15:(self.page + 1) * 15]:
            self.add_item(ParticipantItemButton(self, r))
        self.add_item(ParticipantNav("◀ Previous", self, -1))
        self.add_item(ParticipantNav("Next ▶", self, 1))
        self.add_item(ParticipantSave(self))


class ParticipantItemButton(discord.ui.Button):
    def __init__(self, parent, row):
        key = (row["brainrot_id"], row["mutation_id"])
        is_selected = key in parent.selected
        super().__init__(label=f"{row['mutation']} • {row['brainrot']}"[:80], emoji="✅" if is_selected else "➕",
                         style=discord.ButtonStyle.success if is_selected else discord.ButtonStyle.secondary)
        self.parent, self.key = parent, key

    async def callback(self, interaction):
        p = self.parent
        if interaction.user.id != p.user_id: return await reply(interaction, "This selection belongs to another member.")
        if self.key in p.selected: p.selected.remove(self.key)
        else: p.selected.add(self.key)
        await interaction.response.edit_message(view=ParticipantPicker(p.sid, p.user_id, p.page, p.selected))


class ParticipantNav(discord.ui.Button):
    def __init__(self, label, parent, delta):
        super().__init__(label=label, style=discord.ButtonStyle.secondary,
                         disabled=not (0 <= parent.page + delta < parent.pages))
        self.parent, self.delta = parent, delta

    async def callback(self, interaction):
        p = self.parent
        if interaction.user.id != p.user_id: return await reply(interaction, "This selection belongs to another member.")
        await interaction.response.edit_message(view=ParticipantPicker(p.sid, p.user_id, p.page + self.delta, p.selected))


class ParticipantSave(discord.ui.Button):
    def __init__(self, parent):
        super().__init__(label="Save My Items", emoji="💾", style=discord.ButtonStyle.primary)
        self.parent = parent

    async def callback(self, interaction):
        p = self.parent
        if interaction.user.id != p.user_id: return await reply(interaction, "This selection belongs to another member.")
        row = session(p.sid)
        if not row or row["status"] != "open": return await reply(interaction, "This session is no longer open.")
        valid = {(r["brainrot_id"], r["mutation_id"]) for r in session_items(p.sid)}
        selected = p.selected & valid
        with connect() as db:
            if not selected:
                db.execute("DELETE FROM participants WHERE session_id=? AND user_id=?", (p.sid, p.user_id))
            else:
                db.execute("INSERT OR IGNORE INTO participants VALUES (?,?,?)", (p.sid, p.user_id, datetime.now(timezone.utc).isoformat()))
                db.execute("DELETE FROM participant_items WHERE session_id=? AND user_id=?", (p.sid, p.user_id))
                db.executemany("INSERT INTO participant_items VALUES (?,?,?,?)", [(p.sid, p.user_id, b, m) for b, m in selected])
        await interaction.response.edit_message(content=f"✅ Saved **{len(selected)}** requested item/mutation combinations for session #{p.sid}.", embed=None, view=None)
        await refresh_session(p.sid)


class HostControls(discord.ui.View):
    def __init__(self, sid):
        super().__init__(timeout=180)
        self.sid = sid

    async def check(self, interaction):
        if not authorized(interaction, session(self.sid)):
            await reply(interaction, "Only the host or authorized staff can manage this session.")
            return False
        return True

    @discord.ui.button(label="Add / Remove Items", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        if not await self.check(interaction): return
        row = session(self.sid)
        if row["status"] == "closed": return await reply(interaction, "Closed sessions cannot be edited.")
        d = SessionDraft(interaction.user.id, self.sid)
        await interaction.response.edit_message(embed=d.embed(), view=MutationPicker(d))

    @discord.ui.button(label="Open", emoji="🟢", style=discord.ButtonStyle.success)
    async def open_session(self, interaction, button): await self.set_status(interaction, "open")

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction, button): await self.set_status(interaction, "paused")

    @discord.ui.button(label="Set Time / Epic", emoji="⏳", style=discord.ButtonStyle.primary, row=1)
    async def details(self, interaction, button):
        if not await self.check(interaction): return
        if session(self.sid)["status"] == "closed": return await reply(interaction, "Closed sessions cannot be edited.")
        await interaction.response.send_modal(SessionDetailsModal(self.sid))

    @discord.ui.button(label="Upload Item Picture", emoji="📸", style=discord.ButtonStyle.secondary, row=1)
    async def picture(self, interaction, button):
        if not await self.check(interaction): return
        row = session(self.sid)
        if row["status"] == "closed": return await reply(interaction, "This session is closed.")
        pending_uploads[(interaction.guild_id, interaction.user.id)] = ("session", self.sid, time.monotonic() + 300, row["thread_id"] or row["channel_id"])
        await reply(interaction, f"📸 Upload your item screenshot as a **new message** in <#{row['thread_id'] or row['channel_id']}> within 5 minutes. I'll preview detected catalog names before adding them. You can upload a picture even if OCR misses some names.")

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        if not await self.check(interaction): return
        await reply(interaction, "Close this session? This cannot be reopened.", view=ConfirmClose(self.sid))

    async def set_status(self, interaction, status):
        if not await self.check(interaction): return
        if session(self.sid)["status"] == "closed": return await reply(interaction, "Closed sessions cannot be reopened.")
        execute("UPDATE sessions SET status=? WHERE id=?", (status, self.sid))
        await interaction.response.edit_message(embed=make_session_embed(self.sid), view=HostControls(self.sid))
        await refresh_session(self.sid)


class ConfirmClose(discord.ui.View):
    def __init__(self, sid):
        super().__init__(timeout=90)
        self.sid = sid

    @discord.ui.button(label="Yes, Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        if not authorized(interaction, session(self.sid)): return await reply(interaction, "Not authorized.")
        execute("UPDATE sessions SET status='closed', closed_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.sid))
        await archive_session(self.sid)
        await interaction.response.edit_message(content=f"🔒 Session #{self.sid} closed.", view=None)
        await refresh_session(self.sid)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Close cancelled.", view=None)


class CatalogPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def verify(self, interaction):
        if not staff(interaction.user):
            await reply(interaction, "This panel is for authorized TUF staff only.")
            return False
        return True

    @discord.ui.button(label="Add Brainrot", emoji="➕", style=discord.ButtonStyle.success, custom_id="tuf:catalog:add")
    async def add(self, interaction, button):
        if await self.verify(interaction): await interaction.response.send_modal(CatalogModal("brainrots", "add"))

    @discord.ui.button(label="Add From Picture", emoji="📸", style=discord.ButtonStyle.success, custom_id="tuf:catalog:image", row=1)
    async def image_add(self, interaction, button):
        if not await self.verify(interaction): return
        pending_uploads[(interaction.guild_id, interaction.user.id)] = ("catalog", None, time.monotonic() + 300, interaction.channel_id)
        await reply(interaction, "📸 Send an image **as a new message in this channel** within 5 minutes. I'll read the text and show the detected names for your approval. This requires OCR dependencies; see README.")

    @discord.ui.button(label="Rename Brainrot", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="tuf:catalog:rename")
    async def rename(self, interaction, button):
        if await self.verify(interaction): await interaction.response.send_modal(CatalogModal("brainrots", "rename"))

    @discord.ui.button(label="Enable / Disable", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="tuf:catalog:toggle")
    async def disable(self, interaction, button):
        if await self.verify(interaction): await interaction.response.send_modal(CatalogModal("brainrots", "toggle"))

    @discord.ui.button(label="View All", emoji="📋", style=discord.ButtonStyle.primary, custom_id="tuf:catalog:view")
    async def view_catalog(self, interaction, button):
        if await self.verify(interaction): await send_catalog(interaction, "brainrots", all_items=True)

    @discord.ui.button(label="Review Suggestions", emoji="📥", style=discord.ButtonStyle.primary, custom_id="tuf:catalog:suggestions", row=1)
    async def suggestions(self, interaction, button):
        if not await self.verify(interaction): return
        rows = query("SELECT COUNT(*) n FROM catalog_suggestions WHERE guild_id=? AND status='pending'", (interaction.guild_id,))
        await reply(interaction, f"📥 **{rows[0]['n']}** pending Brainrot suggestions. Use Approve or Deny:",
                    view=CatalogSuggestions(interaction.guild_id))

    @discord.ui.button(label="Manage Mutations", emoji="🧬", style=discord.ButtonStyle.primary, custom_id="tuf:catalog:mutations")
    async def mutations(self, interaction, button):
        if await self.verify(interaction): await reply(interaction, "Manage Go Up mutations:", view=MutationAdmin())


class MutationAdmin(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)

    @discord.ui.button(label="Add Mutation", emoji="➕", style=discord.ButtonStyle.success)
    async def add(self, interaction, button):
        if staff(interaction.user): await interaction.response.send_modal(CatalogModal("mutations", "add"))
        else: await reply(interaction, "Staff only.")

    @discord.ui.button(label="Rename Mutation", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def rename(self, interaction, button):
        if staff(interaction.user): await interaction.response.send_modal(CatalogModal("mutations", "rename"))
        else: await reply(interaction, "Staff only.")

    @discord.ui.button(label="Enable / Disable", emoji="🚫", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction, button):
        if staff(interaction.user): await interaction.response.send_modal(CatalogModal("mutations", "toggle"))
        else: await reply(interaction, "Staff only.")

    @discord.ui.button(label="View Mutations", emoji="📋", style=discord.ButtonStyle.primary)
    async def view_all(self, interaction, button):
        if staff(interaction.user): await send_catalog(interaction, "mutations", all_items=True)
        else: await reply(interaction, "Staff only.")


class CatalogModal(discord.ui.Modal):
    def __init__(self, table, operation):
        self.table, self.operation = table, operation
        super().__init__(title=f"{operation.title()} {'Brainrot' if table == 'brainrots' else 'Mutation'}")
        self.current = discord.ui.TextInput(label="Existing name" if operation != "add" else "New name",
                                           placeholder="Exactly as shown in Go Up", max_length=100)
        self.add_item(self.current)
        if operation == "rename":
            self.new_name = discord.ui.TextInput(label="New name", max_length=100)
            self.add_item(self.new_name)

    async def on_submit(self, interaction):
        if not staff(interaction.user): return await reply(interaction, "Staff only.")
        name = str(self.current.value).strip()
        if not name: return await reply(interaction, "Name cannot be empty.")
        try:
            if self.operation == "add":
                existing = query(f"SELECT * FROM {self.table} WHERE name=? COLLATE NOCASE", (name,))
                if existing:
                    if self.table == "brainrots" and existing[0]["is_custom"]:
                        execute("UPDATE brainrots SET enabled=1, is_custom=0 WHERE id=?", (existing[0]["id"],))
                        return await reply(interaction, f"✅ Approved **{name}** for the main Brainrot catalog.")
                    if not existing[0]["enabled"]:
                        execute(f"UPDATE {self.table} SET enabled=1 WHERE id=?", (existing[0]["id"],))
                        return await reply(interaction, f"✅ Re-enabled **{name}**.")
                    return await reply(interaction, f"⚠️ **{name}** already exists.")
                execute(f"INSERT INTO {self.table}(name) VALUES (?)", (name,))
                return await reply(interaction, f"✅ Added **{name}** to the {'Brainrot' if self.table == 'brainrots' else 'mutation'} catalog.")
            existing = query(f"SELECT * FROM {self.table} WHERE name=? COLLATE NOCASE", (name,))
            if not existing: return await reply(interaction, f"⚠️ **{name}** was not found.")
            if self.operation == "rename":
                new = str(self.new_name.value).strip()
                if not new: return await reply(interaction, "New name cannot be empty.")
                execute(f"UPDATE {self.table} SET name=? WHERE id=?", (new, existing[0]["id"]))
                return await reply(interaction, f"✅ Renamed **{name}** → **{new}**.")
            val = 0 if existing[0]["enabled"] else 1
            execute(f"UPDATE {self.table} SET enabled=? WHERE id=?", (val, existing[0]["id"]))
            return await reply(interaction, f"✅ **{name}** is now {'enabled' if val else 'disabled'} for new sessions.")
        except sqlite3.IntegrityError:
            await reply(interaction, "⚠️ That name already exists. Nothing was changed.")


async def send_catalog(interaction, table, all_items=False, page=0):
    sql = f"SELECT * FROM {table}" + ("" if all_items else " WHERE enabled=1") + " ORDER BY name COLLATE NOCASE"
    rows = query(sql)
    page = max(0, min(page, max(0, (len(rows) - 1) // 35)))
    chunk = rows[page * 35:(page + 1) * 35]
    lines = [f"{'✅' if r['enabled'] else '🚫'} {r['name']}" for r in chunk]
    embed = discord.Embed(title=f"📋 TUF {'Brainrots' if table == 'brainrots' else 'Mutations'} ({len(rows)})",
                          description="\n".join(lines)[:3800] or "No entries yet.", color=0x8B5CF6)
    embed.set_footer(text=f"Page {page + 1}/{max(1, (len(rows) + 34) // 35)}")
    await reply(interaction, embed=embed, view=CatalogPages(table, all_items, page, len(rows)))


class CatalogPages(discord.ui.View):
    def __init__(self, table, all_items, page, count):
        super().__init__(timeout=180)
        self.add_item(CatalogNav("◀ Previous", table, all_items, page - 1, page == 0))
        self.add_item(CatalogNav("Next ▶", table, all_items, page + 1, (page + 1) * 35 >= count))


class CatalogNav(discord.ui.Button):
    def __init__(self, label, table, all_items, page, disabled):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.table, self.all_items, self.page = table, all_items, page

    async def callback(self, interaction):
        if self.all_items and not staff(interaction.user): return await reply(interaction, "Staff only.")
        sql = f"SELECT * FROM {self.table}" + ("" if self.all_items else " WHERE enabled=1") + " ORDER BY name COLLATE NOCASE"
        rows = query(sql)
        page = max(0, min(self.page, max(0, (len(rows) - 1) // 35)))
        lines = [f"{'✅' if r['enabled'] else '🚫'} {r['name']}" for r in rows[page * 35:(page + 1) * 35]]
        embed = discord.Embed(title=f"📋 TUF {'Brainrots' if self.table == 'brainrots' else 'Mutations'} ({len(rows)})",
                              description="\n".join(lines)[:3800] or "No entries yet.", color=0x8B5CF6)
        embed.set_footer(text=f"Page {page + 1}/{max(1, (len(rows) + 34) // 35)}")
        await interaction.response.edit_message(embed=embed, view=CatalogPages(self.table, self.all_items, page, len(rows)))


# Staff upload requests are deliberately short-lived and per-user, not global.
pending_uploads = {}


class SessionDetailsModal(discord.ui.Modal, title="⏳ Session Time & Epic"):
    minutes = discord.ui.TextInput(label="Available for how many minutes?", placeholder="Example: 60 (0 clears the timer)", required=True, max_length=6)
    epic = discord.ui.TextInput(label="Epic username (optional)", required=False, max_length=60)

    def __init__(self, sid):
        super().__init__()
        self.sid = sid

    async def on_submit(self, interaction):
        if not authorized(interaction, session(self.sid)) or session(self.sid)["status"] == "closed":
            return await reply(interaction, "Not authorized or session already closed.")
        try:
            mins = int(str(self.minutes))
            if not 0 <= mins <= 43200: raise ValueError
        except ValueError:
            return await reply(interaction, "Enter a number of minutes between 0 and 43200.")
        expires = int(datetime.now(timezone.utc).timestamp()) + mins * 60 if mins else None
        execute("UPDATE sessions SET expires_at=?, epic_name=? WHERE id=?", (expires, str(self.epic).strip() or None, self.sid))
        await refresh_session(self.sid)
        await reply(interaction, "✅ Session time and optional Epic name updated. Countdown is shown in viewers' local time.")


def ocr_image(data):
    """Read image in worker thread; OCR is advisory and never auto-saves names."""
    from PIL import Image, ImageOps, ImageEnhance
    import pytesseract
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((2500, 2500))
    im = ImageEnhance.Contrast(ImageOps.autocontrast(im)).enhance(1.7)
    return pytesseract.image_to_string(im, config="--psm 11")


def detect_names(ocr_text):
    """Match *known* catalog names. Unknown names require review/edit, not hallucination."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in ocr_text.splitlines() if line.strip()]
    known = active_brainrots()
    matches = []
    for r in known:
        candidate = r["name"].casefold()
        if any(candidate in ln.casefold() for ln in lines): matches.append(r)
    return lines, matches


class ImageNamesPreview(discord.ui.View):
    def __init__(self, actor_id, mode, sid, names, matched, image_url):
        super().__init__(timeout=300)
        self.actor_id, self.mode, self.sid = actor_id, mode, sid
        self.names, self.matched, self.image_url = names, matched, image_url

    async def permitted(self, interaction):
        if interaction.user.id != self.actor_id or (self.mode == "catalog" and not staff(interaction.user)) or (self.mode == "session" and not authorized(interaction, session(self.sid))):
            await reply(interaction, "Only the person who uploaded this picture can confirm it.")
            return False
        return True

    @discord.ui.button(label="Save Detected Names", emoji="✅", style=discord.ButtonStyle.success)
    async def save(self, interaction, button):
        if not await self.permitted(interaction): return
        if self.mode == "catalog":
            # OCR text may contain labels, stats, or noise; require manual confirmation
            # by entering exact name(s) in a modal rather than auto-inserting all text.
            await interaction.response.send_modal(ConfirmOCRNamesModal(self.names))
            return
        if self.sid and session(self.sid) and session(self.sid)["status"] != "closed":
            execute("UPDATE sessions SET image_url=? WHERE id=?", (self.image_url, self.sid))
            await refresh_session(self.sid)
        await reply(interaction, "✅ Session image saved. To add detected or custom names, click **Edit / Enter Names** on the screenshot preview, then confirm the mutation.")

    @discord.ui.button(label="Edit / Enter Names", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        if not await self.permitted(interaction): return
        if self.mode == "session":
            return await interaction.response.send_modal(SessionOCRNamesModal(self.sid, self.names))
        await interaction.response.send_modal(ConfirmOCRNamesModal(self.names))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if not await self.permitted(interaction): return
        await interaction.response.edit_message(content="Image import cancelled.", embed=None, view=None)


class SessionOCRNamesModal(discord.ui.Modal, title="📸 Add Names From Screenshot"):
    names = discord.ui.TextInput(label="Brainrot names (one per line)", style=discord.TextStyle.paragraph,
                                 max_length=1400, required=True)
    mutation = discord.ui.TextInput(label="Mutation (exact name in catalog)",
                                    placeholder="Example: Rainbow", max_length=100, required=True)

    def __init__(self, sid, text_lines):
        super().__init__()
        self.sid = sid
        self.names.default = "\n".join(text_lines)[:1400]

    async def on_submit(self, interaction):
        row = session(self.sid)
        if not authorized(interaction, row) or row["status"] == "closed":
            return await reply(interaction, "Only the host or staff can add items to an open session.")
        mutation = next((m for m in active_mutations() if m["name"].casefold() == str(self.mutation).strip().casefold()), None)
        if not mutation:
            return await reply(interaction, "Mutation not recognized. Use the exact mutation name shown in the Go Up mutation list.")
        names = list(dict.fromkeys(re.sub(r"\s+", " ", n).strip() for n in str(self.names).splitlines() if n.strip()))
        if not names or len(names)>25 or any(len(n)>100 for n in names):
            return await reply(interaction, "Enter 1–25 valid names, max 100 characters per name.")
        added = 0
        with connect() as db:
            for name in names:
                found = db.execute("SELECT id FROM brainrots WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                bid = found["id"] if found else db.execute(
                    "INSERT INTO brainrots(name,enabled,is_custom) VALUES (?,0,1)", (name,)).lastrowid
                before = db.total_changes
                db.execute("INSERT OR IGNORE INTO session_items(session_id,brainrot_id,mutation_id) VALUES (?,?,?)",
                           (self.sid, bid, mutation["id"]))
                if db.total_changes > before: added += 1
        await refresh_session(self.sid)
        await reply(interaction, f"✅ Added **{added}** item/mutation combinations from the reviewed screenshot. "
                                 "Unrecognized names remain outside the shared catalog until approved.")


class ConfirmOCRNamesModal(discord.ui.Modal, title="Review Names From Image"):
    names = discord.ui.TextInput(label="Brainrot names (one per line)", style=discord.TextStyle.paragraph, max_length=1900)

    def __init__(self, text_lines):
        super().__init__()
        self.names.default = "\n".join(text_lines)[:1900]

    async def on_submit(self, interaction):
        if not staff(interaction.user): return await reply(interaction, "Only staff can add names to the master catalog.")
        names = list(dict.fromkeys(line.strip() for line in str(self.names).splitlines() if line.strip()))
        if not names or len(names) > 60: return await reply(interaction, "Enter 1–60 Brainrot names, one per line.")
        added = 0
        approved = 0
        skipped = 0
        with connect() as db:
            for name in names:
                name = re.sub(r"\s+", " ", name).strip()
                if not name or len(name) > 100:
                    skipped += 1
                    continue
                existing = db.execute("SELECT id,enabled,is_custom FROM brainrots WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                if existing:
                    if existing["is_custom"] or not existing["enabled"]:
                        db.execute("UPDATE brainrots SET is_custom=0, enabled=1 WHERE id=?", (existing["id"],))
                        approved += 1
                    else:
                        skipped += 1
                else:
                    db.execute("INSERT INTO brainrots(name,enabled,is_custom) VALUES (?,1,0)", (name,))
                    added += 1
        await reply(interaction, f"✅ **{added}** new Brainrots added, **{approved}** existing custom/disabled names approved, **{skipped}** skipped. Review the catalog to confirm spelling.")


async def archive_session(sid):
    row = session(sid)
    if not row: return
    cfg = query("SELECT logs_channel_id FROM guild_settings WHERE guild_id=?", (row["guild_id"],))
    transcript = None
    if row["thread_id"]:
        source = bot.get_channel(row["thread_id"])
        if source is None:
            try: source = await bot.fetch_channel(row["thread_id"])
            except discord.HTTPException: source = None
        if source:
            try:
                lines = []
                async for msg in source.history(limit=1500, oldest_first=True):
                    stamp = msg.created_at.strftime("%Y-%m-%d %H:%M UTC")
                    attachments = " ".join(a.url for a in msg.attachments)
                    body = (msg.content or "[embed / component]").replace("\n", " ")
                    lines.append(f"[{stamp}] {msg.author} ({msg.author.id}): {body} {attachments}")
                transcript = io.BytesIO("\n".join(lines).encode("utf-8"))
            except discord.HTTPException:
                log.warning("Could not read transcript for session %s", sid)
    if cfg and cfg[0]["logs_channel_id"]:
        channel = bot.get_channel(cfg[0]["logs_channel_id"])
        if channel is None:
            try: channel = await bot.fetch_channel(cfg[0]["logs_channel_id"])
            except discord.HTTPException: channel = None
        if channel:
            embed = make_session_embed(sid)
            embed.title = f"📁 Closed TUF Indexing Session #{sid}"
            embed.add_field(name="Closed", value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>")
            if row["thread_id"]: embed.add_field(name="Session Discussion", value=f"<#{row['thread_id']}>")
            try:
                upload = discord.File(transcript, filename=f"tuf_session_{sid}_transcript.txt") if transcript is not None else None
                await channel.send(embed=embed, file=upload)
            except discord.HTTPException: log.warning("Could not post closed session log %s", sid)
    if row["thread_id"]:
        channel = bot.get_channel(row["thread_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(row["thread_id"])
            except discord.HTTPException:
                channel = None
        if channel:
            try:
                await channel.send("🔒 Session closed. The discussion is now archived.")
                await channel.edit(archived=True, locked=True)
            except discord.HTTPException:
                log.warning("Could not lock/archive thread for session %s", sid)


intents = discord.Intents.default()
# Needed to receive image attachments in ordinary guild messages for the
# staff catalog import and host screenshot upload workflows.
# Enable MESSAGE CONTENT INTENT for this bot in Discord Developer Portal too.
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild or not message.attachments: return
    key = (message.guild.id, message.author.id)
    pending = pending_uploads.get(key)
    if not pending: return
    mode, sid, deadline, expected_channel_id = pending
    if message.channel.id != expected_channel_id: return
    if time.monotonic() > deadline:
        pending_uploads.pop(key, None)
        return
    if mode == "catalog" and not staff(message.author): return
    if mode == "session" and not (session(sid) and (message.author.id == session(sid)["host_id"] or staff(message.author))): return
    if mode == "session" and (session(sid)["status"] == "closed" or message.channel.id not in (session(sid)["thread_id"], session(sid)["channel_id"])): return
    attach = next((a for a in message.attachments if (a.content_type or "").startswith("image/") or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))), None)
    if not attach: return
    pending_uploads.pop(key, None)
    if attach.size > 8_000_000:
        return await message.reply("Image too large; please upload a picture under 8 MB.", mention_author=False)
    try:
        data = await attach.read()
        raw_text = await asyncio.to_thread(ocr_image, data)
        lines, matched = detect_names(raw_text)
        error = None
    except (ImportError, OSError, ValueError) as e:
        lines, matched, error = [], [], str(e)
    except Exception:
        log.exception("Image recognition failed")
        lines, matched, error = [], [], "OCR failed for this image"
    preview = discord.Embed(title="📸 TUF Image Recognition Preview", color=0x8B5CF6,
        description=("**OCR text (please correct any mistakes):**\n" + "\n".join(lines[:28]))[:3500] if lines else "No readable text detected. You can still enter the names manually.")
    if matched: preview.add_field(name="✅ Known Brainrots Found", value=", ".join(r["name"] for r in matched)[:1024])
    if error: preview.add_field(name="⚠️ Recognition Unavailable", value=error[:1000])
    preview.set_image(url=attach.url)
    await message.reply(embed=preview, view=ImageNamesPreview(message.author.id, mode, sid, lines or [""], matched, attach.url), mention_author=False)


@tasks.loop(seconds=30)
async def expire_sessions():
    now = int(datetime.now(timezone.utc).timestamp())
    due = query("SELECT id FROM sessions WHERE status != 'closed' AND expires_at IS NOT NULL AND expires_at <= ?", (now,))
    for r in due:
        sid = r["id"]
        execute("UPDATE sessions SET status='closed', closed_at=? WHERE id=? AND status!='closed'", (datetime.now(timezone.utc).isoformat(), sid))
        try:
            await archive_session(sid)
            await refresh_session(sid)
        except Exception:
            log.exception("Could not archive expired session %s", sid)


@expire_sessions.before_loop
async def before_expire_sessions():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Ready as %s (%s)", bot.user, bot.user.id)
    if not expire_sessions.is_running(): expire_sessions.start()


async def bot_setup_hook():
    init_db()
    bot.add_view(MainPanel())
    bot.add_view(GuidePanel())
    bot.add_view(CatalogPanel())
    for row in query("SELECT id FROM sessions WHERE message_id IS NOT NULL AND status != 'closed'"):
        bot.add_view(SessionButtons(row["id"]))
    # Closed panels keep functional View Items and host controls after restart as well.
    for row in query("SELECT id FROM sessions WHERE message_id IS NOT NULL AND status='closed'"):
        bot.add_view(SessionButtons(row["id"]))
    await bot.tree.sync()


bot.setup_hook = bot_setup_hook


@bot.tree.command(name="setup_logs", description="Set closed indexing session log channel (staff only)")
@app_commands.guild_only()
@app_commands.describe(channel="Channel where closed session summaries are posted")
async def setup_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not staff(interaction.user): return await reply(interaction, "Staff only.")
    execute("INSERT INTO guild_settings(guild_id,logs_channel_id) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET logs_channel_id=excluded.logs_channel_id", (interaction.guild_id, channel.id))
    await reply(interaction, f"✅ Closed session logs will be posted to {channel.mention}.")


@bot.tree.command(name="setup", description="Post the TUF Indexing Session panel (TUF staff only)")
@app_commands.guild_only()
async def setup_command(interaction: discord.Interaction):
    if not staff(interaction.user): return await reply(interaction, "You need Manage Server or a configured TUF staff role.")
    embed = discord.Embed(title="📚 TUF Indexing Session", color=0x8B5CF6,
        description="**Go Up for Brainrots**\n\nHost or join indexing sessions, browse available Brainrots, and select multiple item/mutation combinations in one session.\n\nUse the buttons below to get started.")
    banner = Path(__file__).with_name("tuf_banner.png")
    if banner.is_file():
        embed.set_image(url="attachment://tuf_banner.png")
        await interaction.channel.send(embed=embed, view=MainPanel(), file=discord.File(banner, filename="tuf_banner.png"))
    else:
        await interaction.channel.send(embed=embed, view=MainPanel())
    await reply(interaction, "✅ TUF Indexing Session panel posted.")


@bot.tree.command(name="setup_guide", description="Post the permanent TUF How to Use guide panel (TUF staff only)")
@app_commands.guild_only()
async def setup_guide(interaction: discord.Interaction):
    if not staff(interaction.user):
        return await reply(interaction, "You need Manage Server or a configured TUF staff role.")
    await interaction.response.defer(ephemeral=True)
    embed = guide_embed("overview")
    # Use a distinct Discord upload name so the guide has a persistent attached banner.
    banner = Path(__file__).with_name("tuf_banner.png")
    if banner.is_file():
        embed.set_image(url="attachment://tuf_guide_banner.png")
        await interaction.channel.send(embed=embed, view=GuidePanel(), file=discord.File(banner, filename="tuf_guide_banner.png"))
    else:
        await interaction.channel.send(embed=embed, view=GuidePanel())
    await interaction.followup.send("✅ TUF How to Use guide posted. You can pin its message in this channel.", ephemeral=True)


@bot.tree.command(name="setup_management", description="Post the TUF Brainrot Management panel (TUF staff only)")
@app_commands.guild_only()
async def setup_management(interaction: discord.Interaction):
    if not staff(interaction.user): return await reply(interaction, "You need Manage Server or a configured TUF staff role.")
    embed = discord.Embed(title="⚙️ TUF Brainrot Management", color=0x8B5CF6,
        description="**Go Up catalog controls**\nAdd, rename, and disable Brainrots, or manage mutations.\n\n🔒 Only authorized staff can use these controls. New names become available without restarting the bot.")
    await interaction.channel.send(embed=embed, view=CatalogPanel())
    await reply(interaction, "✅ TUF Brainrot Management panel posted. Restrict this channel to staff for privacy.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Add it to your environment or .env (see README).")
    bot.run(TOKEN)
