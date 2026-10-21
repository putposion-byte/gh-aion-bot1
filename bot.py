"""Discord-бот: панель с выбором услуги и приватные тикеты.

Все тексты, услуги, цены и баннер лежат в config.json.
Токен берётся из переменной окружения DISCORD_TOKEN или из файла .env.
"""

import asyncio
import contextvars
import io
import json
import os
import re
import time
from pathlib import Path

import discord
from discord import app_commands, ui

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data" / "users.json"

# ---------------------------------------------------------------- config / data

_config_cache = {"mtime": None, "data": None}


_guild_ctx = contextvars.ContextVar("guild_id", default=None)


def use_guild(guild_id: int | None):
    """Задаёт сервер, для которого cfg() вернёт настройки (основной или из раздела "servers")."""
    _guild_ctx.set(guild_id)


def base_cfg() -> dict:
    """Читает config.json заново, если файл изменился (перезапуск не нужен)."""
    mtime = CONFIG_PATH.stat().st_mtime
    if _config_cache["mtime"] != mtime:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _config_cache["data"] = json.load(f)
        _config_cache["mtime"] = mtime
    return _config_cache["data"]


def cfg() -> dict:
    """Настройки текущего сервера: основные + переопределения из "servers"."""
    c = base_cfg()
    override = c.get("servers", {}).get(str(_guild_ctx.get()))
    if not override:
        return c
    merged = {**c, **override}
    merged["panel"] = {**c["panel"], **override.get("panel", {})}
    return merged


def all_guild_ids() -> list[int]:
    c = base_cfg()
    return [g for g in [c.get("guild_id")] + [int(k) for k in c.get("servers", {})] if g]


def load_users() -> dict:
    if DATA_PATH.exists():
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_user(user_id: int, **fields):
    users = load_users()
    users.setdefault(str(user_id), {}).update(fields)
    DATA_PATH.parent.mkdir(exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def load_token() -> str:
    token = os.getenv("DISCORD_TOKEN")
    env_file = BASE_DIR / ".env"
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DISCORD_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        raise SystemExit("No token: create a .env file with the line DISCORD_TOKEN=... (see README)")
    return token


def accent() -> discord.Colour:
    return discord.Colour(int(cfg().get("accent_color", "#5865F2").lstrip("#"), 16))


def get_service(service_id: str) -> dict | None:
    return next((s for s in cfg()["services"] if s["id"] == service_id), None)


def get_payment(method_id: str) -> dict | None:
    return next((m for m in cfg()["payment_methods"] if m["id"] == method_id), None)


def is_staff(member: discord.Member) -> bool:
    # персонал: роль staff_role_id и роли из ping_role_ids (CEO, Manager)
    staff_ids = {cfg().get("staff_role_id"), *cfg().get("ping_role_ids", [])}
    return member.guild_permissions.administrator or any(r.id in staff_ids for r in member.roles)


# ---------------------------------------------------------------- panel

def build_panel() -> tuple[ui.LayoutView, discord.File | None]:
    """Собирает панель в стиле Components V2 из config.json."""
    c = cfg()
    p = c["panel"]
    view = ui.LayoutView(timeout=None)
    box = ui.Container(accent_colour=accent())
    file = None

    banner = p.get("banner", "")
    if banner:
        if banner.startswith("http"):
            box.add_item(ui.MediaGallery(discord.MediaGalleryItem(banner)))
        elif (BASE_DIR / banner).exists():
            # Локальная картинка рядом с bot.py, например banner.png
            file = discord.File(BASE_DIR / banner, filename="banner" + Path(banner).suffix)
            box.add_item(ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{file.filename}")))

    box.add_item(ui.TextDisplay(f"## {p['title']}\n{p['subtitle']}"))
    box.add_item(ui.Separator())

    for sec in p["sections"]:
        btn = sec.get("button")
        if not btn:
            box.add_item(ui.TextDisplay(sec["text"]))
            continue
        if btn["type"] == "link":
            accessory = ui.Button(label=btn["label"], emoji=btn.get("emoji"), url=btn["url"])
        elif btn["type"] == "payments":
            accessory = ui.Button(label=btn["label"], emoji=btn.get("emoji"), custom_id="panel:payments")
        else:
            accessory = ui.Button(label=btn["label"], emoji=btn.get("emoji"), custom_id=f"panel:info:{btn['id']}")
        box.add_item(ui.Section(sec["text"], accessory=accessory))

    box.add_item(ui.Separator())
    box.add_item(ui.TextDisplay(f"## {p['services_title']}"))

    # Кнопки услуг, по 5 в ряд (ограничение Discord)
    services = c["services"]
    for i in range(0, len(services), 5):
        row = ui.ActionRow()
        for s in services[i:i + 5]:
            row.add_item(ui.Button(label=s["name"], emoji=s.get("emoji"), custom_id=f"panel:svc:{s['id']}"))
        box.add_item(row)

    box.add_item(ui.Separator())
    box.add_item(ui.ActionRow(
        ui.Button(label=p["fast_button_label"], emoji="⚡", style=discord.ButtonStyle.success, custom_id="panel:order:fast"),
        ui.Button(label=p["bulk_button_label"], emoji="📦", custom_id="panel:order:bulk"),
    ))
    if p.get("footer"):
        box.add_item(ui.TextDisplay(f"-# {p['footer']}"))

    view.add_item(box)
    return view, file


# ---------------------------------------------------------------- order flow

MODE_NAMES = {"fast": "Quick Order", "bulk": "Bulk Order"}


async def vanish(message_or_interaction, delay: float):
    """Удаляет эфемерное сообщение (followup или исходный ответ) через delay секунд."""
    async def _later():
        await asyncio.sleep(delay)
        try:
            if isinstance(message_or_interaction, discord.Interaction):
                await message_or_interaction.delete_original_response()
            else:
                await message_or_interaction.delete()
        except discord.HTTPException:
            pass
    asyncio.get_running_loop().create_task(_later())


class OrderFlowView(ui.View):
    """Заказ только кликами: услуга → вариант (если есть в config) → способ оплаты → тикет."""

    def __init__(self, service: dict, mode: str):
        super().__init__(timeout=300)
        self.service = service
        self.mode = mode
        self.option = None
        self.build()

    def needs_option(self) -> bool:
        return bool(self.service.get("options", {}).get("choices")) and self.option is None

    def build(self):
        self.clear_items()
        if self.needs_option():
            opts = self.service["options"]
            select = ui.Select(placeholder=opts.get("title", "Choose an option"), options=[
                discord.SelectOption(label=o["label"][:100], value=str(i), description=(o.get("price") or None))
                for i, o in enumerate(opts["choices"][:25])
            ])
            select.callback = self.option_picked
        else:
            select = ui.Select(placeholder="Choose a payment method", options=[
                discord.SelectOption(label=m["name"], value=m["id"], emoji=m.get("emoji"))
                for m in cfg()["payment_methods"]
            ])
            select.callback = self.payment_picked
        self.select = select
        self.add_item(select)

    def text(self) -> str:
        s = self.service
        lines = [f"## {s.get('emoji', '')} {s['name']}"]
        if s.get("description"):
            lines.append(s["description"])
        if self.option:
            lines.append(f"**Selected:** {self.option['label']}")
        price = (self.option or {}).get("price") or s.get("price")
        if price:
            lines.append(f"**Price:** {price}")
        step = s.get("options", {}).get("title", "Choose an option") if self.needs_option() else "Choose a payment method"
        lines.append(f"-# {step} below.")
        return "\n".join(lines)

    async def option_picked(self, interaction: discord.Interaction):
        use_guild(interaction.guild_id)
        self.option = self.service["options"]["choices"][int(self.select.values[0])]
        self.build()
        await interaction.response.edit_message(content=self.text(), view=self)

    async def payment_picked(self, interaction: discord.Interaction):
        use_guild(interaction.guild_id)
        method = get_payment(self.select.values[0])
        await interaction.response.edit_message(content="⏳ Creating your ticket…", view=None)
        await create_ticket(interaction, self.service, self.mode, self.option, method)
        await vanish(interaction, 0)


class ServicePickView(ui.View):
    """Выбор услуги для «Quick Order» / «Bulk Order»."""

    def __init__(self, mode: str):
        super().__init__(timeout=300)
        self.mode = mode
        select = ui.Select(placeholder="Choose a service", options=[
            discord.SelectOption(label=s["name"], value=s["id"], emoji=s.get("emoji"),
                                 description=s.get("price", "")[:100] or None)
            for s in cfg()["services"]
        ])
        select.callback = self.picked
        self.select = select
        self.add_item(select)

    async def picked(self, interaction: discord.Interaction):
        use_guild(interaction.guild_id)
        flow = OrderFlowView(get_service(self.select.values[0]), self.mode)
        await interaction.response.edit_message(content=flow.text(), view=flow)


# ---------------------------------------------------------------- tickets

def ticket_owner_id(channel: discord.abc.GuildChannel) -> int | None:
    m = re.match(r"ticket:(\d+)", getattr(channel, "topic", None) or "")
    return int(m.group(1)) if m else None


async def get_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat = guild.get_channel(cfg().get("ticket_category_id") or 0)
    if isinstance(cat, discord.CategoryChannel):
        return cat
    # Если категория не указана в конфиге, ищем/создаём «Тикеты»
    cat = discord.utils.get(guild.categories, name="Tickets")
    if cat is None:
        cat = await guild.create_category("Tickets", overwrites={
            guild.default_role: discord.PermissionOverwrite(view_channel=False)})
    return cat


def slug(text: str) -> str:
    text = re.sub(r"[^\w-]+", "-", text.lower(), flags=re.UNICODE).strip("-")
    return text[:30] or "user"


async def create_ticket(interaction: discord.Interaction, service: dict, mode: str,
                        option: dict | None, method: dict | None):
    c = cfg()
    guild = interaction.guild
    user = interaction.user
    category = await get_category(guild)

    open_tickets = [ch for ch in category.text_channels if ticket_owner_id(ch) == user.id]
    if len(open_tickets) >= c.get("max_open_tickets_per_user", 1):
        msg = await interaction.followup.send(
            "You already have an open ticket. Close it to open a new one.",
            view=ui.View().add_item(ui.Button(label="Go to ticket", emoji="🎫", url=open_tickets[0].jump_url)),
            ephemeral=True, wait=True)
        await vanish(msg, 15)
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                          read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                              read_message_history=True),
    }
    staff_role = guild.get_role(c.get("staff_role_id") or 0)
    # Роли, которые пингуются при новом тикете (например CEO и Manager), тоже должны его видеть
    ping_roles = [r for r in (guild.get_role(i) for i in c.get("ping_role_ids", [])) if r]
    for role in [staff_role, *ping_roles]:
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           attach_files=True, read_message_history=True)

    channel = await guild.create_text_channel(
        name=f"{c.get('ticket_name_prefix', 'ticket')}-{slug(user.name)}",
        category=category,
        overwrites=overwrites,
        topic=f"ticket:{user.id}:{service['id']}:{mode}",
        reason=f"Ticket from {user}",
    )

    payment = f"{method.get('emoji', '')} {method['name']}" if method else "—"
    price = (option or {}).get("price") or service.get("price", "—")

    staff_ping = staff_role.mention if staff_role else ""
    lines = [
        f"## {service.get('emoji', '')} {service['name']} · {MODE_NAMES[mode]}",
        f"Customer: {user.mention} {staff_ping}",
        f"**Selected:** {option['label']}" if option else "",
        f"**Price:** {price}",
        f"**Payment:** {payment}",
    ]
    lines = [line for line in lines if line]
    lines.append("-# A manager will reply soon. When your order is done, press «Order completed».")

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay("\n".join(lines)),
        ui.Separator(),
        ui.ActionRow(
            ui.Button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="ticket:claim"),
            ui.Button(label="Order completed", emoji="✅", style=discord.ButtonStyle.success, custom_id="ticket:done"),
            ui.Button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close"),
        ),
        accent_colour=accent(),
    ))
    await channel.send(view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    if ping_roles:
        await channel.send(f"🔔 New order! {' '.join(r.mention for r in ping_roles)}",
                           allowed_mentions=discord.AllowedMentions(roles=True))
    msg = await interaction.followup.send(
        "Ticket created.",
        view=ui.View().add_item(ui.Button(label="Go to ticket", emoji="🎫", url=channel.jump_url)),
        ephemeral=True, wait=True)
    await vanish(msg, 15)
    await log(guild, f"🟢 {user.mention} opened ticket {channel.mention} ({service['name']}, {MODE_NAMES[mode]})")


async def log(guild: discord.Guild, text: str, file: discord.File | None = None):
    channel = guild.get_channel(cfg().get("log_channel_id") or 0)
    if channel:
        await channel.send(text, file=file, allowed_mentions=discord.AllowedMentions.none())


class CloseConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="Yes, close", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        use_guild(interaction.guild_id)
        await interaction.response.edit_message(content="Closing ticket…", view=None)
        await close_ticket(interaction.channel, interaction.user)


async def close_ticket(channel: discord.TextChannel, closer: discord.abc.User, note: str = ""):
    transcript = None
    if cfg().get("save_transcripts"):
        rows = []
        async for m in channel.history(limit=None, oldest_first=True):
            when = m.created_at.strftime("%Y-%m-%d %H:%M")
            body = m.content or ("[panel]" if m.components else "")
            files = " ".join(a.url for a in m.attachments)
            rows.append(f"[{when}] {m.author}: {body} {files}".rstrip())
        transcript = discord.File(io.BytesIO("\n".join(rows).encode("utf-8")), filename=f"{channel.name}.txt")

    owner = ticket_owner_id(channel)
    await log(channel.guild, f"🔴 {closer.mention} closed ticket `{channel.name}`"
                             + (f" (customer <@{owner}>)" if owner else "") + note, transcript)
    await channel.delete(reason=f"Ticket closed by {closer}")


# ---------------------------------------------------------------- permanent panel

PANEL_STATE_PATH = BASE_DIR / "data" / "panel.json"
AUTO_CLOSE_SECONDS = 24 * 60 * 60  # сколько завершённый тикет висит у персонала


def build_prices_embed() -> discord.Embed:
    """Прайс из config.json: услуги и их варианты с ценами."""
    embed = discord.Embed(title="💰 Price list", colour=accent())
    wide, columns, short = [], [], []  # (name, value, inline)
    for s in cfg()["services"]:
        title = f"{s.get('emoji', '')} {s['name']}"
        choices = s.get("options", {}).get("choices", [])
        price = "\n".join(f"**{line}**" for line in s.get("price", "on request").split("\n"))
        if any(o.get("price") for o in choices) and not any(o.get("column") for o in choices):
            # свои цены у вариантов, без колонок — компактный список «Stage 1 · 4$»
            wide.append((title, "\n".join(f"{o['label']} · **{o.get('price') or s.get('price', 'on request')}**"
                                          for o in choices), False))
        elif any(o.get("price") for o in choices):
            # варианты с одинаковой ценой — одна колонка на цену, колонки стоят рядом
            # колонка задаётся полем "column" у варианта, иначе — по цене
            groups: dict[object, list[dict]] = {}
            for o in choices:
                groups.setdefault(o.get("column") or o.get("price") or s.get("price"), []).append(o)
            def column(opts):
                prices = [o.get("price") or s.get("price", "on request") for o in opts]
                if len(opts) == 1:  # один вариант в колонке — название, под ним цена
                    return f"{opts[0]['label']}\n**{prices[0]}**"
                if len(set(prices)) == 1:  # одна цена на колонку — заголовком
                    return f"**{prices[0]}**\n" + "\n".join(f"• {o['label']}" for o in opts)
                return "\n".join(f"• {o['label']} — **{pr}**" for o, pr in zip(opts, prices))
            cols = [(title if i == 0 else "​", column(opts), True) for i, opts in enumerate(groups.values())]
            cols += [("​", "​", True)] * (-len(cols) % 3)  # добиваем ряд до 3 колонок
            columns += cols
        elif choices:
            # у вариантов нет своих цен: показываем их списком и общую цену услуги один раз
            wide.append((title, " • ".join(o["label"] for o in choices) + "\n" + price, False))
        elif price.count("\n") == 0 and len(price) <= 40:
            short.append((title, price, True))  # короткие услуги — по 3 в ряд
        else:
            wide.append((title, price, False))
    for name, value, inline in wide + columns + short:
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Prices are updated automatically · Order in 🛒・open-ticket")
    return embed


async def sync_prices(client: discord.Client):
    """Держит прайс в prices_channel_id и обновляет его при изменении цен."""
    channel = client.get_channel(cfg().get("prices_channel_id") or 0)
    if channel is None:
        return
    state_path = BASE_DIR / "data" / "prices.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    embed = build_prices_embed()
    if state.get("channel_id") == channel.id and state.get("message_id"):
        try:
            msg = await channel.fetch_message(state["message_id"])
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            pass
    msg = await channel.send(embed=embed)
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps({"channel_id": channel.id, "message_id": msg.id}), encoding="utf-8")


async def sync_panel(client: discord.Client):
    """Отправляет панель в panel_channel_id или редактирует уже отправленную."""
    channel = client.get_channel(cfg().get("panel_channel_id") or 0)
    if channel is None:
        return
    # у основного сервера файл panel.json, у остальных panel_<id>.json
    gid = _guild_ctx.get()
    state_path = PANEL_STATE_PATH if gid in (None, base_cfg().get("guild_id")) else \
        PANEL_STATE_PATH.with_name(f"panel_{gid}.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    view, file = build_panel()
    if state.get("channel_id") == channel.id and state.get("message_id"):
        try:
            msg = await channel.fetch_message(state["message_id"])
            await msg.edit(view=view, attachments=[file] if file else [])
            return
        except discord.NotFound:
            pass
    msg = await channel.send(view=view, **({"file": file} if file else {}))
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps({"channel_id": channel.id, "message_id": msg.id}), encoding="utf-8")


# ---------------------------------------------------------------- bot

class ShopBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Для сохранения переписки тикета нужен Message Content Intent (см. README)
        intents.message_content = bool(cfg().get("save_transcripts"))
        # для приветствия новых участников (Server Members Intent включён в Developer Portal)
        intents.members = bool(cfg().get("greet_channel_id"))
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild_ids = all_guild_ids()
        for guild_id in guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        if not guild_ids:
            await self.tree.sync()
        self.loop.create_task(self.keep_panel())
        self.loop.create_task(self.auto_close_tickets())

    async def auto_close_tickets(self):
        """Удаляет завершённые тикеты, у которых истекли сутки (метка until= в топике канала)."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for guild in self.guilds:
                    for channel in guild.text_channels:
                        topic = channel.topic or ""
                        m = re.search(r":until=(\d+)", topic)
                        if topic.startswith("ticket:") and m and int(m.group(1)) <= time.time():
                            use_guild(guild.id)
                            await close_ticket(channel, self.user, " · auto-closed 24h after completion")
            except Exception as e:
                print(f"Auto-close error: {e}")
            await asyncio.sleep(600)

    async def keep_panel(self):
        """Держит панель в канале panel_channel_id и обновляет её при изменении config.json."""
        await self.wait_until_ready()
        last_mtime = None
        while not self.is_closed():
            try:
                mtime = CONFIG_PATH.stat().st_mtime
                if mtime != last_mtime:
                    for guild_id in all_guild_ids():
                        use_guild(guild_id)
                        await sync_panel(self)
                        await sync_prices(self)
                    last_mtime = mtime
            except Exception as e:
                print(f"Panel sync error: {e}")
            await asyncio.sleep(15)

    async def on_member_join(self, member: discord.Member):
        """Приветствие нового участника в отдельном канале greet_channel_id; удаляется через минуту."""
        use_guild(member.guild.id)
        if member.bot:
            return
        role = member.guild.get_role(cfg().get("auto_role_id") or 0)  # роль новичку, например Buyer
        if role:
            try:
                await member.add_roles(role, reason="Auto role for new member")
            except discord.HTTPException as e:
                print(f"Auto role error: {e}")
        text = cfg().get("greet_text", "👋 Welcome, {user}!").replace("{user}", member.mention)
        channel = member.guild.get_channel(cfg().get("greet_channel_id") or 0)
        if channel:
            await channel.send(text, delete_after=60, allowed_mentions=discord.AllowedMentions(users=True))

    async def on_ready(self):
        print(f"Bot started as {self.user} (id {self.user.id})")

    async def on_interaction(self, interaction: discord.Interaction):
        """Все кнопки панели и тикетов обрабатываются здесь по custom_id,
        поэтому они продолжают работать после перезапуска бота."""
        if interaction.type != discord.InteractionType.component:
            return
        use_guild(interaction.guild_id)
        cid = (interaction.data or {}).get("custom_id", "")
        print(f"Click: {interaction.user} -> {cid}", flush=True)
        if cid.startswith("panel:"):
            await handle_panel(interaction, cid)
        elif cid.startswith("ticket:"):
            await handle_ticket(interaction, cid)


async def handle_panel(interaction: discord.Interaction, cid: str):
    _, action, *rest = cid.split(":")
    if action == "info":
        text = cfg()["panel"].get("info_texts", {}).get(rest[0], "No text for this button.")
        await interaction.response.send_message(text, ephemeral=True, delete_after=60)
    elif action == "svc":
        service = get_service(rest[0])
        if not service:
            await interaction.response.send_message("This service is no longer available.", ephemeral=True,
                                                    delete_after=10)
            return
        flow = OrderFlowView(service, "fast")
        await interaction.response.send_message(flow.text(), view=flow, ephemeral=True, delete_after=300)
    elif action == "order":
        mode = rest[0]
        await interaction.response.send_message(f"**{MODE_NAMES[mode]}**: choose a service",
                                                view=ServicePickView(mode), ephemeral=True,
                                                delete_after=300)


async def handle_ticket(interaction: discord.Interaction, cid: str):
    action = cid.split(":")[1]
    member = interaction.user
    owner = ticket_owner_id(interaction.channel)
    if action == "claim":
        if not is_staff(member):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True, delete_after=10)
            return
        customer = f"<@{owner}> " if owner else ""
        await interaction.response.send_message(
            f"{customer}👋 Hello! I'll be taking care of your order.\n\n"
            "Thank you for choosing our team! I'll make sure everything goes smoothly and provide you "
            "with the best possible service. 😊\n"
            f"-# Claimed by {member.mention}",
            allowed_mentions=discord.AllowedMentions(users=True))
    elif action == "close":
        if not is_staff(member):
            await interaction.response.send_message("Only staff can close this ticket.", ephemeral=True,
                                                    delete_after=10)
            return
        await interaction.response.send_message("Close this ticket? The channel will be deleted.",
                                                view=CloseConfirmView(), ephemeral=True)
    elif action == "done":
        # Заказ завершён, когда подтвердили и клиент (флаг c), и персонал (флаг s)
        channel = interaction.channel
        if member.id == owner:
            flag = "c"
        elif is_staff(member):
            flag = "s"
        else:
            await interaction.response.send_message("Only the customer or staff can confirm the order.",
                                                    ephemeral=True, delete_after=10)
            return
        flags = set((channel.topic or "").split(":")[4:])
        if flag in flags:
            await interaction.response.send_message("You have already confirmed. Waiting for the other side.",
                                                    ephemeral=True, delete_after=10)
            return
        flags.add(flag)
        status = (f"Customer: {'✅' if 'c' in flags else '⏳'}   Staff: {'✅' if 's' in flags else '⏳'}")

        if flags >= {"c", "s"}:
            # Клиент теряет доступ сразу, у персонала тикет остаётся ещё на сутки
            await interaction.response.send_message(
                f"## ✅ Order completed\n{status}\nThank you! The ticket is now closed for the customer.\n"
                f"-# Staff: the channel will be deleted automatically in 24 hours, or close it with 🔒 Close.")
            until = int(time.time()) + AUTO_CLOSE_SECONDS
            await channel.edit(topic=(channel.topic or "") + f":{flag}:until={until}")
            if owner:
                await channel.set_permissions(discord.Object(id=owner), overwrite=None)
            await log(interaction.guild, f"✅ Order completed in `{channel.name}` (confirmed by customer and staff)")
            reviews = next((s["button"]["url"] for s in cfg()["panel"]["sections"]
                            if s.get("button", {}).get("type") == "link"), None)
            customer = interaction.guild.get_member(owner) if owner else None
            if customer and reviews:
                try:
                    await customer.send(
                        "Thank you for your order! We'd love your feedback.",
                        view=ui.View().add_item(ui.Button(label="Leave a review", emoji="⭐", url=reviews)))
                except discord.HTTPException:
                    pass
            return

        other = "staff" if flag == "c" else "the customer"
        staff_role = interaction.guild.get_role(cfg().get("staff_role_id") or 0)
        ping = (staff_role.mention if staff_role else "") if flag == "c" else (f"<@{owner}>" if owner else "")
        await interaction.response.send_message(
            f"☑️ {member.mention} confirmed the order is completed.\n{status}\n"
            f"Waiting for {other} to press «Order completed». {ping}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await channel.edit(topic=(channel.topic or "") + ":" + flag)


bot = ShopBot()


@bot.tree.command(name="panel", description="Post the order panel in this channel")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def panel_cmd(interaction: discord.Interaction):
    use_guild(interaction.guild_id)
    view, file = build_panel()
    kwargs = {"view": view}
    if file:
        kwargs["file"] = file
    await interaction.channel.send(**kwargs)
    await interaction.response.send_message("Panel posted.", ephemeral=True)


async def service_autocomplete(interaction: discord.Interaction, current: str):
    use_guild(interaction.guild_id)
    return [app_commands.Choice(name=s["name"], value=s["id"]) for s in cfg()["services"]
            if current.lower() in s["name"].lower()][:25]


async def option_autocomplete(interaction: discord.Interaction, current: str):
    use_guild(interaction.guild_id)
    service = get_service(getattr(interaction.namespace, "service", "") or "")
    choices = (service or {}).get("options", {}).get("choices", [])
    return [app_commands.Choice(name=o["label"], value=o["label"]) for o in choices
            if current.lower() in o["label"].lower()][:25]


@bot.tree.command(name="setprice", description="Change a price in the price list")
@app_commands.describe(service="Service", option="Option (leave empty for the whole service)", price="New price, e.g. 15$")
@app_commands.autocomplete(service=service_autocomplete, option=option_autocomplete)
@app_commands.guild_only()
async def setprice_cmd(interaction: discord.Interaction, service: str, price: str, option: str | None = None):
    use_guild(interaction.guild_id)
    if not is_staff(interaction.user):
        await interaction.response.send_message("Only staff can change prices.", ephemeral=True, delete_after=10)
        return
    # Цены этого сервера лежат в servers.<id> или в корне config.json
    raw = base_cfg()
    target = raw.get("servers", {}).get(str(interaction.guild_id), raw)
    svc = next((s for s in target.get("services", []) if s["id"] == service), None)
    if not svc:
        await interaction.response.send_message("Unknown service.", ephemeral=True, delete_after=10)
        return
    if option:
        opt = next((o for o in svc.get("options", {}).get("choices", []) if o["label"] == option), None)
        if not opt:
            await interaction.response.send_message("Unknown option.", ephemeral=True, delete_after=10)
            return
        opt["price"] = price
    else:
        svc["price"] = price
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    await interaction.response.send_message(
        f"✅ **{svc['name']}{' · ' + option if option else ''}** now costs **{price}**. "
        "The price list and shop panel will update in a few seconds.", ephemeral=True, delete_after=20)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = f"Error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    raise error


if __name__ == "__main__":
    bot.run(load_token())
