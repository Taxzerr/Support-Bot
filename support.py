import discord
import os
import json
import shutil
import asyncio
import tempfile
import re
import unicodedata
import logging
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("fastsupport")

# ---------------- CONFIG ----------------
CONFIG_FILE = "guild_config.json"

# valeurs par défaut (compatibilité)
STAFF_ROLE = "Staff"
TICKET_CATEGORY_NAME = "Tickets"
LOG_CHANNEL_NAME = "📂・ticket-logs"
DEFAULT_SUPPORT_CHANNEL_NAME = "support"

# --- status search phrase (utilisé pour retrouver le champ d'état) ---
STATUS_SEARCH = "prise en charge"

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ Token manquant (.env)")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or('!'), intents=intents)

# global asyncio lock for file writes
SAVE_LOCK = asyncio.Lock()


# ---------------- Storage util ----------------
def load_config():
    if not os.path.isfile(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Erreur en lisant %s — utilisation d'une config vide", CONFIG_FILE)
        return {}


async def save_config(cfg):
    """
    Écriture atomique + backup horodaté + lock asyncio.
    - crée une copie de sauvegarde guild_config.json.bak-YYYYmmddHHMMSS si le fichier existe
    - écrit atomiquement dans un tmp puis remplace
    """
    async with SAVE_LOCK:
        try:
            # backup existing file
            if os.path.isfile(CONFIG_FILE):
                bname = f"{CONFIG_FILE}.bak-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                try:
                    shutil.copy2(CONFIG_FILE, bname)
                    logger.debug("Backup config créé: %s", bname)
                except Exception:
                    logger.exception("Impossible de créer la sauvegarde %s", bname)

            # write to temp file then replace
            dirpath = os.path.dirname(os.path.abspath(CONFIG_FILE)) or "."
            fd, tmp_path = tempfile.mkstemp(prefix="tmp_config_", suffix=".json", dir=dirpath)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmpf:
                    json.dump(cfg, tmpf, ensure_ascii=False, indent=2)
                os.replace(tmp_path, CONFIG_FILE)
                logger.debug("Config sauvegardée atomiquement dans %s", CONFIG_FILE)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
        except Exception:
            logger.exception("Échec de la sauvegarde de la config (atomique + backup). Tentative d'écriture simple.")
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except Exception:
                logger.exception("Échec d'écriture simple du fichier de config.")


def get_gcfg(cfg, guild_id):
    gid = str(guild_id)
    if gid not in cfg:
        cfg[gid] = {
            "support_channel_id": None,
            "categories": [
                {
                    "label": "Gestion Staff",
                    "description": "Candidature, rôles ou rank up",
                    "emoji": "🔰",
                    "notify_role_id": None,
                    "close_role_ids": []
                },
                {
                    "label": "Partenariat",
                    "description": "Demande de partenariat",
                    "emoji": "🤝",
                    "notify_role_id": None,
                    "close_role_ids": []
                },
                {
                    "label": "Autre",
                    "description": "Autre demande",
                    "emoji": "❓",
                    "notify_role_id": None,
                    "close_role_ids": []
                },
            ],
            # mapping str(channel.id) -> { channel_id, channel_name, owner_id, claimed_by, category, message_id }
            "open_tickets": {}
        }
    return cfg[gid]


# load once at startup (synchronous)
GCFG = load_config()


# ---------------- utilities ----------------
async def get_or_create_log_channel(guild: discord.Guild):
    log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_channel:
        return log_channel
    try:
        return await guild.create_text_channel(LOG_CHANNEL_NAME)
    except Exception:
        logger.exception("Impossible de créer le channel de log %s dans %s", LOG_CHANNEL_NAME, guild.name)
        return None


def build_support_embed():
    embed = discord.Embed(
        title="📩 Ouvrez un ticket !",
        color=discord.Color.from_rgb(54, 57, 63)
    )
    embed.add_field(name="", value="> **L’assistance est disponible 24h/24 et 7j/7.**", inline=False)
    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(name="", value="• Cliquez sur le menu déroulant ci-dessous !", inline=False)
    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(name="", value="• Sélectionnez la catégorie adaptée à votre demande !", inline=False)
    embed.set_footer(text="Fast Support • v2.5")
    return embed


# ---------------- helper: slugify pour noms de salon ----------------
def slugify(text):
    """
    normalise accents -> ascii, lowercase, remplace espaces par underscore,
    supprime les caractères non autorisés dans les noms de salon Discord.
    """
    text = unicodedata.normalize('NFKD', str(text))
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = text.lower().strip()
    text = re.sub(r'\s+', '_', text)                # espaces -> _
    text = re.sub(r'[^a-z0-9\-_]', '', text)        # garder a-z0-9 _ et -
    return text[:80]


# helper pour insérer / mettre à jour proprement le statut dans un embed
def set_status_in_embed(embed: discord.Embed, new_status: str):
    """
    Cherche un champ de statut existant (ligne commençant par "• Le ticket" ou contenant
    'pris en charge'/'en attente'). Si trouvé, remplace ce champ. Sinon, insère le statut
    juste après le séparateur (ou crée le séparateur si nécessaire).
    """
    idx = None
    for i, f in enumerate(embed.fields):
        try:
            val = (f.value or "").lower()
            # motifs de statut connus — adaptables si tu veux détecter d'autres formulations
            if val.strip().startswith("• le ticket") or "pris en charge" in val or "en attente" in val:
                idx = i
                break
        except Exception:
            continue

    if idx is not None:
        try:
            embed.set_field_at(idx, name="\u200b", value=new_status, inline=False)
            return
        except Exception:
            # fallback to insert si set_field_at plante pour une raison quelconque
            pass

    # statut non trouvé -> assurer la présence du séparateur après la ligne d'ouverture
    sep_value = "---------------------------------------------"
    if len(embed.fields) >= 1:
        # si le champ 1 ressemble déjà à un séparateur, on insère à la position 2
        if len(embed.fields) >= 2 and (re.match(r'^[\s\-\u2014\u2013]+$', (embed.fields[1].value or "")) or "----" in (embed.fields[1].value or "")):
            insert_pos = 2
        else:
            # insérer le séparateur en pos 1 puis statut en pos 2
            try:
                embed.insert_field_at(1, name="\u200b", value=sep_value, inline=False)
            except Exception:
                pass
            insert_pos = 2
    else:
        insert_pos = len(embed.fields)

    try:
        embed.insert_field_at(insert_pos, name="\u200b", value=new_status, inline=False)
    except Exception:
        embed.add_field(name="\u200b", value=new_status, inline=False)


# helper: suppression différée d'un message (utilisé pour supprimer les notifications après X secondes)
async def _delete_message_later(msg: discord.Message, delay: float = 3.0):

    """Supprime le message `msg` après `delay` secondes (silencieusement)."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        # ignore errors (permissions, message déjà supprimé, etc.)
        pass




# ---------------- Close ticket view (global) ----------------
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="fastsupport_close_ticket")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel

        cfg = get_gcfg(GCFG, guild.id)
        allowed = False

        # admin
        if interaction.user.guild_permissions.administrator:
            allowed = True
        else:
            # essayer d'identifier la catégorie via le topic
            category_label = None
            if isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:"):
                category_label = channel.topic.split("ticket_category:", 1)[1]

            if category_label:
                for c in cfg.get("categories", []):
                    if c.get("label") == category_label:
                        close_ids = c.get("close_role_ids", []) or []
                        user_role_ids = {r.id for r in interaction.user.roles}
                        if any(rid in user_role_ids for rid in close_ids):
                            allowed = True
                        break

        if not allowed:
            await interaction.response.send_message("⛔ Tu n'as pas la permission de fermer ce ticket.", ephemeral=True)
            return

        log_channel = await get_or_create_log_channel(guild)
        if log_channel:
            try:
                embed = discord.Embed(
                    title="📁 Ticket fermé",
                    description=f"**Salon :** {channel.name}\n**Fermé par :** {interaction.user.mention}\n**Heure :** {datetime.utcnow().isoformat()} UTC",
                    color=discord.Color.red()
                )
                await log_channel.send(embed=embed)
            except Exception:
                logger.exception("Impossible d'envoyer l'embed de log de fermeture")

        # cleanup persisted open_tickets (maintenant clé = str(channel.id))
        gcfg = get_gcfg(GCFG, guild.id)
        key = str(channel.id)
        if key in gcfg.get("open_tickets", {}):
            try:
                del gcfg["open_tickets"][key]
                await save_config(GCFG)
            except Exception:
                logger.exception("Erreur lors du cleanup open_tickets pour %s", key)

        await interaction.response.send_message("🔒 Ticket fermé.", ephemeral=True)
        try:
            await channel.delete()
        except Exception:
            logger.exception("Impossible de supprimer le channel %s", channel.name)


# ---------------- Ticket actions view (per-message) ----------------
class TicketActionsView(discord.ui.View):
    def __init__(self, guild_cfg, category_label, ticket_owner_member, channel_id):
        super().__init__(timeout=None)
        self.guild_cfg = guild_cfg                # dict for that guild (from GCFG)
        self.category_label = category_label
        self.ticket_owner = ticket_owner_member   # discord.Member (can be None if left)
        self.channel_id = channel_id              # int channel id used as key in open_tickets

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.secondary, custom_id="fastsupport_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        gcfg = get_gcfg(GCFG, guild.id)
        ot = gcfg.setdefault("open_tickets", {})
        entry = ot.get(str(self.channel_id))
        if not entry:
            await interaction.response.send_message("ℹ️ Impossible de retrouver l'état du ticket (peut-être redémarré).", ephemeral=True)
            return
        if entry.get("claimed_by"):
            claimed_member = guild.get_member(int(entry["claimed_by"]))
            await interaction.response.send_message(f"🛑 Ce ticket est déjà pris en charge par {claimed_member.mention if claimed_member else 'quelqu’un'}.", ephemeral=True)
            return

        entry["claimed_by"] = interaction.user.id
        try:
            await save_config(GCFG)
        except Exception:
            logger.exception("Erreur lors de la sauvegarde après claim")

        try:
            msg = interaction.message
            if msg and msg.embeds:
                embed = msg.embeds[0]

                # récupérer la mention du rôle notify si configuré (on conserve la description d'ouverture intacte)
                notify_mention = ""
                for c in self.guild_cfg.get("categories", []):
                    if c.get("label") == self.category_label:
                        nid = c.get("notify_role_id")
                        if nid:
                            role = guild.get_role(int(nid))
                            if role:
                                notify_mention = role.mention + "\n"
                        break

                new_status = f"• Le ticket a été pris en charge par {interaction.user.mention} !"
                set_status_in_embed(embed, new_status)

                # Mettre à jour le message (uniquement)
                try:
                    await msg.edit(embed=embed, view=self)
                except Exception:
                    logger.exception("Impossible de modifier le message du ticket lors d'une prise en charge")

            # notifier le propriétaire dans le channel
            try:
                owner_mention = self.ticket_owner.mention if self.ticket_owner else 'Utilisateur'
                notify_embed = discord.Embed(description=f"{owner_mention}, Votre ticket a été pris en charge par {interaction.user.mention} !", color=discord.Color.green())
                await interaction.channel.send(embed=notify_embed)
            except Exception:
                logger.exception("Impossible d'envoyer la notification au propriétaire après claim")

            await interaction.response.send_message("✅ Tu as pris en charge ce ticket.", ephemeral=True)
        except Exception:
            logger.exception("Erreur pendant la prise en charge du ticket")
            await interaction.response.send_message("❌ Impossible de mettre à jour le message.", ephemeral=True)

    @discord.ui.button(label="Résoudre", style=discord.ButtonStyle.primary, custom_id="fastsupport_resolve")
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel
        gcfg = get_gcfg(GCFG, guild.id)

        allowed = False
        # admin
        if interaction.user.guild_permissions.administrator:
            allowed = True

        # claim
        ot = gcfg.get("open_tickets", {})
        entry = ot.get(str(self.channel_id))
        if entry and entry.get("claimed_by") and int(entry["claimed_by"]) == interaction.user.id:
            allowed = True

        # category close roles
        for c in gcfg.get("categories", []):
            if c.get("label") == self.category_label:
                close_ids = c.get("close_role_ids", []) or []
                user_role_ids = {r.id for r in interaction.user.roles}
                if any(rid in user_role_ids for rid in close_ids):
                    allowed = True
                break

        # legacy staff role
        staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE)
        if staff_role and staff_role in interaction.user.roles:
            allowed = True

        if not allowed:
            await interaction.response.send_message("⛔ Tu n'as pas l'autorisation pour résoudre ce ticket.", ephemeral=True)
            return

        # log
        log_channel = await get_or_create_log_channel(guild)
        if log_channel:
            try:
                embed = discord.Embed(
                    title="📁 Ticket résolu",
                    description=(
                        f"**Salon :** {channel.name}\n**Résolu par :** {interaction.user.mention}\n"
                        f"**Utilisateur :** {self.ticket_owner.mention if self.ticket_owner else 'inconnu'}\n"
                        f"**Catégorie :** {self.category_label}\n**Heure :** {datetime.utcnow().isoformat()} UTC"
                    ),
                    color=discord.Color.blue()
                )
                await log_channel.send(embed=embed)
            except Exception:
                logger.exception("Impossible d'envoyer l'embed de log de résolution")

        # cleanup persisted open_tickets (clé = str(channel.id))
        try:
            key = str(channel.id)
            if key in gcfg.get("open_tickets", {}):
                del gcfg["open_tickets"][key]
                await save_config(GCFG)
        except Exception:
            logger.exception("Erreur lors du cleanup open_tickets pour resolve")

        await interaction.response.send_message("✅ Ticket résolu — fermeture du salon.", ephemeral=True)
        try:
            await channel.delete()
        except Exception:
            logger.exception("Impossible de supprimer le channel lors d'une résolution")

    @discord.ui.button(label="Fermer le ticket", style=discord.ButtonStyle.danger, custom_id="fastsupport_close_actions")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel
        gcfg = get_gcfg(GCFG, guild.id)

        allowed = False
        # admin
        if interaction.user.guild_permissions.administrator:
            allowed = True

        # claim
        ot = gcfg.get("open_tickets", {})
        entry = ot.get(str(self.channel_id))
        if entry and entry.get("claimed_by") and int(entry["claimed_by"]) == interaction.user.id:
            allowed = True

        # category close roles
        for c in gcfg.get("categories", []):
            if c.get("label") == self.category_label:
                close_ids = c.get("close_role_ids", []) or []
                user_role_ids = {r.id for r in interaction.user.roles}
                if any(rid in user_role_ids for rid in close_ids):
                    allowed = True
                break

        # legacy staff role
        staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE)
        if staff_role and staff_role in interaction.user.roles:
            allowed = True

        if not allowed:
            await interaction.response.send_message("⛔ Tu n'as pas la permission pour fermer ce ticket.", ephemeral=True)
            return

        # log fermeture
        log_channel = await get_or_create_log_channel(guild)
        if log_channel:
            try:
                embed = discord.Embed(
                    title="📁 Ticket fermé",
                    description=(
                        f"**Salon :** {channel.name}\n**Fermé par :** {interaction.user.mention}\n"
                        f"**Utilisateur :** {self.ticket_owner.mention if self.ticket_owner else 'inconnu'}\n"
                        f"**Catégorie :** {self.category_label}\n**Heure :** {datetime.utcnow().isoformat()} UTC"
                    ),
                    color=discord.Color.red()
                )
                await log_channel.send(embed=embed)
            except Exception:
                logger.exception("Impossible d'envoyer l'embed de log de fermeture (action)")

        # cleanup persisted open_tickets
        try:
            key = str(channel.id)
            if key in gcfg.get("open_tickets", {}):
                del gcfg["open_tickets"][key]
                await save_config(GCFG)
        except Exception:
            logger.exception("Erreur lors du cleanup open_tickets pour close action")

        await interaction.response.send_message("🔒 Ticket fermé — fermeture du salon.", ephemeral=True)
        try:
            await channel.delete()
        except Exception:
            logger.exception("Impossible de supprimer le channel lors d'une fermeture (action)")


# ---------------- Dynamic TicketSelect & View (per guild) ----------------
class TicketSelect(discord.ui.Select):
    def __init__(self, guild_id: int, categories: list):
        opts = []
        for c in categories:
            emoji_val = c.get("emoji")
            emoji_param = emoji_val if emoji_val and emoji_val.strip() and emoji_val != " " else None
            opts.append(discord.SelectOption(
                label=c.get("label", "Autre")[:100],
                description=c.get("description", "")[:100],
                emoji=emoji_param
            ))
        super().__init__(placeholder="🎫 Choisis le type de ticket",
                         min_values=1, max_values=1, options=opts,
                         custom_id=f"fastsupport_ticket_select_{guild_id}")

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        choice = self.values[0]

        cfg = get_gcfg(GCFG, guild.id)

        # require bot admin (you chose administrator earlier)
        if not guild.me.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Je n'ai pas la permission `Administrateur` dans ce serveur. "
                "Donnez-moi l'autorisation Administrateur ou au moins `Gérer les salons`.",
                ephemeral=True
            )
            return

        # retrouver la configuration de la catégorie choisie
        cat_cfg = None
        for c in cfg.get("categories", []):
            if c.get("label") == choice:
                cat_cfg = c
                break

        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            try:
                category = await guild.create_category(TICKET_CATEGORY_NAME)
            except Exception:
                logger.exception("Impossible de créer la catégorie %s", TICKET_CATEGORY_NAME)
                category = None

        # --- sécurité: empêcher la création de 2 tickets par utilisateur (quelles que soient les catégories) ---
        ot = cfg.get("open_tickets", {}) or {}

        # iterate over a static list to allow deletion while iterating
        for k, v in list(ot.items()):
            try:
                if int(v.get("owner_id", -1)) == int(member.id):
                    # retrouver le channel pour mention
                    existing_channel = None
                    cid = v.get("channel_id")
                    if cid:
                        existing_channel = guild.get_channel(int(cid))
                    # fallback: essayer par channel_name si présent
                    if not existing_channel and v.get("channel_name"):
                        existing_channel = discord.utils.get(guild.text_channels, name=v.get("channel_name"))

                    # si le salon existe -> bloquer la création
                    if existing_channel:
                        await interaction.response.send_message(f"⚠️ Tu as déjà un ticket ouvert : {existing_channel.mention}", ephemeral=True)
                        return

                    # si le salon n'existe plus -> nettoyage automatique (on supprime l'entrée et on continue)
                    else:
                        try:
                            del ot[k]
                            await save_config(GCFG)
                            logger.info("Nettoyage auto: ticket orphelin supprimé pour user %s (clé %s)", member.id, k)
                        except Exception:
                            logger.exception("Erreur lors du nettoyage auto d'un ticket orphelin (clé %s)", k)
                        # continue loop to check if other entries exist
            except Exception:
                logger.exception("Erreur lors de la vérification des tickets ouverts pour l'utilisateur %s", member.id)
                continue

        # --- construction du nom voulu : "categorie-username" ---
        category_slug = slugify(choice)        # ex: 'partenariat'
        user_slug = slugify(member.name)      # utiliser le username (member.name)
        base_channel_name = f"{category_slug}-{user_slug}"  # ex: 'partenariat-razox8'

        if category:
            for ch in category.text_channels:
                if ch.name == base_channel_name:
                    await interaction.response.send_message(f"⚠️ Tu as déjà un ticket ouvert : {ch.mention}", ephemeral=True)
                    return

        # overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }

        # if category has close roles, give those roles access
        if cat_cfg:
            for rid in cat_cfg.get("close_role_ids", []) or []:
                role = guild.get_role(int(rid))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        # legacy staff
        staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        # si un salon portant exactement ce nom existe déjà ailleurs, ajouter l'id pour garantir l'unicité
        channel_name = base_channel_name
        if discord.utils.get(guild.text_channels, name=channel_name):
            channel_name = f"{base_channel_name}-{member.id}"

        kwargs = dict(name=channel_name, overwrites=overwrites, topic=f"ticket_category:{choice}")
        if category:
            kwargs["category"] = category

        try:
            channel = await guild.create_text_channel(**kwargs)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Je n'ai pas la permission de créer le salon. Vérifiez mes permissions.", ephemeral=True)
            return
        except Exception:
            logger.exception("Erreur lors de la création du channel de ticket")
            await interaction.response.send_message("❌ Erreur lors de la création du ticket.", ephemeral=True)
            return

        # --- Build embed with separator between open line and status (no footer) ---
        embed = discord.Embed(
            title=choice,  # Always the category name (no mention inside embed)
            description="",
            color=discord.Color.from_rgb(54, 57, 63)
        )
        # Opening line: bullet + user mention + category bold (category only in bold)
        open_line = f"• {member.mention} a créé un ticket concernant les **{choice}** !"
        embed.add_field(name="\u200b", value=open_line, inline=False)

        # separator field (user requested)
        separator = "---------------------------------------------"
        embed.add_field(name="\u200b", value=separator, inline=False)

        # Status field (initial) - inserted right after separator
        status_line = f"• **Le ticket est en attente de prise en charge**"
        embed.add_field(name="\u200b", value=status_line, inline=False)

        view = TicketActionsView(cfg, choice, member, channel.id)

        # send message and persist info
        try:
            # content: ping user and optionally notify role mention before embed (keeps same behavior)
            notify_role = None
            if cat_cfg and cat_cfg.get("notify_role_id"):
                notify_role = guild.get_role(int(cat_cfg["notify_role_id"]))
            content = member.mention if not notify_role else f"{notify_role.mention} {member.mention}"
            msg = await channel.send(content=content, embed=embed, view=view)
        except Exception:
            logger.exception("Impossible d'envoyer le message initial dans le salon du ticket")
            await interaction.response.send_message("❌ Impossible d'envoyer le message initial dans le salon du ticket.", ephemeral=True)
            return

        # persist ticket state (owner, claimed_by, category, message_id) -- clé = str(channel.id)
        gcfg = get_gcfg(GCFG, guild.id)
        ot = gcfg.setdefault("open_tickets", {})
        ot[str(channel.id)] = {
            "channel_id": int(channel.id),
            "channel_name": channel.name,
            "owner_id": int(member.id),
            "claimed_by": None,
            "category": choice,
            "message_id": int(msg.id)
        }
        try:
            await save_config(GCFG)
        except Exception:
            logger.exception("Erreur lors de la sauvegarde open_tickets après création de ticket")

        log_channel = await get_or_create_log_channel(guild)
        if log_channel:
            try:
                log_embed = discord.Embed(
                    title="📂 Ticket ouvert",
                    description=f"**Utilisateur :** {member.mention}\n**Salon :** {channel.mention}\n**Catégorie :** {choice}\n**Heure :** {datetime.utcnow().isoformat()} UTC",
                    color=discord.Color.green()
                )
                await log_channel.send(embed=log_embed)
            except Exception:
                logger.exception("Impossible d'envoyer l'embed de log d'ouverture")

        await interaction.response.send_message(f"✅ Ticket créé : {channel.mention}", ephemeral=True)


class TicketView(discord.ui.View):
    def __init__(self, guild_id: int, categories: list):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(guild_id, categories))


# ---------------- Post automatique (uses guild config) ----------------
async def ensure_support_message(guild: discord.Guild):
    cfg = get_gcfg(GCFG, guild.id)
    ch = None
    if cfg.get("support_channel_id"):
        try:
            ch = guild.get_channel(int(cfg["support_channel_id"]))
        except Exception:
            ch = None
    if not ch:
        ch = discord.utils.get(guild.text_channels, name=DEFAULT_SUPPORT_CHANNEL_NAME)
    if not ch:
        return

    try:
        async for msg in ch.history(limit=150):
            if msg.author == bot.user and msg.embeds:
                if msg.embeds[0].title == "📩 Ouvrez un ticket !":
                    return
    except Exception:
        logger.exception("Erreur en parcourant l'historique pour ensure_support_message")

    categories = cfg.get("categories", [])
    try:
        await ch.send(embed=build_support_embed(), view=TicketView(guild.id, categories))
    except Exception:
        logger.exception("Impossible d'envoyer le message de support automatiquement")


# ---------------- Slash commands (separate, compatible) ----------------
def is_admin(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator


@bot.tree.command(name="set-channel", description="Définir le salon où poster le message support")
@app_commands.describe(channel="Salon où le message support sera envoyé automatiquement")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    cfg["support_channel_id"] = int(channel.id)
    await save_config(GCFG)
    bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))
    await interaction.response.send_message(f"✅ Salon support défini sur {channel.mention}", ephemeral=True)


@bot.tree.command(name="add-category", description="Ajouter une catégorie de ticket")
@app_commands.describe(label="Titre de la catégorie", description="Courte description", emoji="Emoji optionnel (ex: 🔔)")
async def add_category(interaction: discord.Interaction, label: str, description: str, emoji: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    if any(c["label"].lower() == label.lower() for c in cfg.get("categories", [])):
        await interaction.response.send_message("⚠️ Une catégorie avec ce nom existe déjà.", ephemeral=True)
        return
    stored_emoji = emoji if (emoji and emoji.strip()) else " "
    cfg.setdefault("categories", []).append({
        "label": label,
        "description": description,
        "emoji": stored_emoji,
        "notify_role_id": None,
        "close_role_ids": []
    })
    await save_config(GCFG)
    bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))
    await interaction.response.send_message(f"✅ Catégorie ajoutée : **{label}**", ephemeral=True)


@bot.tree.command(name="remove-category", description="Supprimer une catégorie (par son titre)")
@app_commands.describe(label="Titre de la catégorie à supprimer")
async def remove_category(interaction: discord.Interaction, label: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    before = len(cfg.get("categories", []))
    cfg["categories"] = [c for c in cfg.get("categories", []) if c["label"].lower() != label.lower()]
    after = len(cfg["categories"])
    await save_config(GCFG)
    bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))
    if before == after:
        await interaction.response.send_message("⚠️ Aucune catégorie trouvée avec ce titre.", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Catégorie **{label}** supprimée.", ephemeral=True)


@bot.tree.command(name="list-categories", description="Afficher les catégories configurées")
async def list_categories(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    categories = cfg.get("categories", [])
    if not categories:
        await interaction.response.send_message("Aucune catégorie configurée.", ephemeral=True)
        return
    def display_emoji(c):
        e = c.get("emoji")
        return e if (e and e.strip() and e != " ") else ""
    text = "\n".join(
        f"- {display_emoji(c)} **{c.get('label','???')}** — {c.get('description',' ')}"
        f"{' (notify: '+str(c.get('notify_role_id'))+')' if c.get('notify_role_id') else ''}"
        for c in categories
    )
    await interaction.response.send_message(f"**Catégories :**\n{text}", ephemeral=True)


@bot.tree.command(name="send-embed", description="Envoyer le message support dans le salon configuré maintenant")
async def send_embed(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    ch = None
    if cfg.get("support_channel_id"):
        ch = interaction.guild.get_channel(int(cfg["support_channel_id"]))
    if not ch:
        ch = discord.utils.get(interaction.guild.text_channels, name=DEFAULT_SUPPORT_CHANNEL_NAME)
    if not ch:
        await interaction.response.send_message("❌ Aucun salon configuré et aucun salon `support` trouvé.", ephemeral=True)
        return
    try:
        await ch.send(embed=build_support_embed(), view=TicketView(interaction.guild.id, cfg.get("categories", [])))
        await interaction.response.send_message(f"✅ Message support envoyé dans {ch.mention}", ephemeral=True)
    except Exception:
        logger.exception("Impossible d'envoyer le message (permissions?).")
        await interaction.response.send_message("❌ Impossible d'envoyer le message (permissions?).", ephemeral=True)


# ---------------- New commands to manage notify/close roles ----------------
@bot.tree.command(name="set-category-notify", description="Configurer le rôle à mentionner quand un ticket de cette catégorie est ouvert")
@app_commands.describe(label="Titre de la catégorie", role="Rôle à mentionner (ou ne rien choisir pour enlever)")
async def set_category_notify(interaction: discord.Interaction, label: str, role: discord.Role = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    found = False
    for c in cfg.get("categories", []):
        if c.get("label").lower() == label.lower():
            found = True
            if role is None:
                c["notify_role_id"] = None
                await interaction.response.send_message(f"✅ Notification désactivée pour la catégorie **{c['label']}**.", ephemeral=True)
            else:
                c["notify_role_id"] = int(role.id)
                await interaction.response.send_message(f"✅ Le rôle {role.mention} sera pingé pour la catégorie **{c['label']}**.", ephemeral=True)
            break
    if not found:
        await interaction.response.send_message("⚠️ Catégorie non trouvée.", ephemeral=True)
    await save_config(GCFG)
    bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))


@bot.tree.command(name="add-category-close-role", description="Ajouter un rôle pouvant fermer les tickets d'une catégorie")
@app_commands.describe(label="Titre de la catégorie", role="Rôle à ajouter")
async def add_category_close_role(interaction: discord.Interaction, label: str, role: discord.Role):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    for c in cfg.get("categories", []):
        if c.get("label").lower() == label.lower():
            lst = c.get("close_role_ids", []) or []
            if int(role.id) in lst:
                await interaction.response.send_message("⚠️ Ce rôle est déjà autorisé.", ephemeral=True)
                return
            lst.append(int(role.id))
            c["close_role_ids"] = lst
            await save_config(GCFG)
            bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))
            await interaction.response.send_message(f"✅ {role.mention} peut maintenant fermer les tickets de **{c['label']}**.", ephemeral=True)
            return
    await interaction.response.send_message("⚠️ Catégorie non trouvée.", ephemeral=True)


@bot.tree.command(name="remove-category-close-role", description="Retirer un rôle autorisé à fermer les tickets d'une catégorie")
@app_commands.describe(label="Titre de la catégorie", role="Rôle à retirer")
async def remove_category_close_role(interaction: discord.Interaction, label: str, role: discord.Role):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    for c in cfg.get("categories", []):
        if c.get("label").lower() == label.lower():
            lst = c.get("close_role_ids", []) or []
            if int(role.id) not in lst:
                await interaction.response.send_message("⚠️ Ce rôle n'était pas autorisé.", ephemeral=True)
                return
            lst = [rid for rid in lst if rid != int(role.id)]
            c["close_role_ids"] = lst
            await save_config(GCFG)
            bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))
            await interaction.response.send_message(f"✅ {role.mention} ne peut plus fermer les tickets de **{c['label']}**.", ephemeral=True)
            return
    await interaction.response.send_message("⚠️ Catégorie non trouvée.", ephemeral=True)


@bot.tree.command(name="show-category-roles", description="Afficher les rôles configurés pour une catégorie")
@app_commands.describe(label="Titre de la catégorie")
async def show_category_roles(interaction: discord.Interaction, label: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return
    cfg = get_gcfg(GCFG, interaction.guild.id)
    for c in cfg.get("categories", []):
        if c.get("label").lower() == label.lower():
            notify = None
            if c.get("notify_role_id"):
                notify = interaction.guild.get_role(int(c["notify_role_id"]))
            close_roles = []
            for rid in c.get("close_role_ids", []) or []:
                r = interaction.guild.get_role(int(rid))
                if r:
                    close_roles.append(r.mention)
            await interaction.response.send_message(
                f"**{c['label']}**\nNotify: {notify.mention if notify else 'aucun'}\nClose roles: {', '.join(close_roles) if close_roles else 'aucun'}",
                ephemeral=True
            )
            return
    await interaction.response.send_message("⚠️ Catégorie non trouvée.", ephemeral=True)


# ---------------- Events ----------------
async def migrate_open_tickets_for_guild(gcfg, guild: discord.Guild):
    """
    Si des clés open_tickets sont encore des noms (ancien format), essayer de migrer
    vers la clé str(channel.id).
    """
    old = gcfg.get("open_tickets", {}) or {}
    new = {}
    migrated = False
    for key, data in old.items():
        # si clé déjà numérique (id) -> garder
        if key.isdigit():
            new[key] = data
            if "channel_id" not in new[key]:
                try:
                    new[key]["channel_id"] = int(key)
                except Exception:
                    pass
            continue

        # ancien format: key = channel.name
        try:
            channel = discord.utils.get(guild.text_channels, name=key)
        except Exception:
            channel = None

        if channel:
            new_key = str(channel.id)
            new[new_key] = dict(data)
            new[new_key]["channel_id"] = channel.id
            new[new_key]["channel_name"] = channel.name
            migrated = True
        else:
            # on ignore les entrées orphelines (on ne peut pas retrouver le channel)
            continue

    if migrated:
        gcfg["open_tickets"] = new
        try:
            await save_config(GCFG)
        except Exception:
            logger.exception("Erreur lors de la sauvegarde après migration open_tickets")
    else:
        gcfg.setdefault("open_tickets", new if new else old)


async def cleanup_orphan_tickets_for_guild(gcfg, guild: discord.Guild):
    """
    Supprime les entrées open_tickets dont le salon n'existe plus.
    Utilisé au démarrage et lors de la jointure de la guilde.
    """
    ot = gcfg.get("open_tickets", {}) or {}
    removed = False
    for key in list(ot.keys()):
        try:
            entry = ot.get(key, {})
            cid = entry.get("channel_id")
            exists = False
            if cid:
                if guild.get_channel(int(cid)):
                    exists = True
            if not exists:
                # fallback: try by channel_name
                cname = entry.get("channel_name")
                if cname and discord.utils.get(guild.text_channels, name=cname):
                    exists = True
            if not exists:
                # remove orphan
                try:
                    del ot[key]
                    removed = True
                    logger.info("Nettoyage auto au démarrage: suppression ticket orphelin %s pour guilde %s", key, guild.id)
                except Exception:
                    logger.exception("Impossible de supprimer l'entrée orpheline %s", key)
        except Exception:
            logger.exception("Erreur pendant le nettoyage orphelin pour la clé %s", key)
    if removed:
        try:
            await save_config(GCFG)
        except Exception:
            logger.exception("Erreur lors de la sauvegarde après nettoyage orphelin")


@bot.event
async def on_guild_join(guild):
    cfg = get_gcfg(GCFG, guild.id)
    await save_config(GCFG)
    bot.add_view(TicketView(guild.id, cfg.get("categories", [])))
    # nettoie les tickets orphelins si besoin
    try:
        await cleanup_orphan_tickets_for_guild(cfg, guild)
    except Exception:
        logger.exception("Erreur lors du nettoyage orphelin au join")
    await ensure_support_message(guild)


@bot.event
async def on_ready():
    bot.add_view(CloseTicketView())
    for guild in bot.guilds:
        cfg = get_gcfg(GCFG, guild.id)
        # register ticket selector view
        bot.add_view(TicketView(guild.id, cfg.get("categories", [])))

        # cleanup orphelins avant migration/restauration
        try:
            await cleanup_orphan_tickets_for_guild(cfg, guild)
        except Exception:
            logger.exception("Erreur lors du nettoyage orphelin pour la guilde %s", guild.id)

        # MIGRATE old open_tickets (channel.name -> channel.id) if needed
        try:
            await migrate_open_tickets_for_guild(cfg, guild)
        except Exception:
            logger.exception("Erreur lors de la migration open_tickets pour la guilde %s", guild.id)

        # restore TicketActionsView for open tickets (if possible)
        ot = cfg.get("open_tickets", {}) or {}
        for ch_key, info in ot.items():
            try:
                try:
                    channel = guild.get_channel(int(ch_key))
                except Exception:
                    channel = None
                if not channel:
                    continue
                msg_id = info.get("message_id")
                if not msg_id:
                    continue
                try:
                    msg = await channel.fetch_message(int(msg_id))
                except Exception:
                    continue
                owner = guild.get_member(int(info.get("owner_id"))) if info.get("owner_id") else None
                view = TicketActionsView(cfg, info.get("category"), owner, channel.id)
                # if already claimed, set embed status accordingly (ONLY update the status field, keep description)
                if info.get("claimed_by"):
                    try:
                        embed = msg.embeds[0] if msg.embeds else None
                        if embed:
                            claimant = guild.get_member(int(info.get("claimed_by"))) if info.get("claimed_by") else None

                            # récupération du role si configuré (on n'insère PAS sa mention dans l'embed)
                            role = None
                            for c in cfg.get("categories", []):
                                if c.get("label") == info.get("category"):
                                    nid = c.get("notify_role_id")
                                    if nid:
                                        role = guild.get_role(int(nid))
                                    break

                            opener_name = owner.name if owner else 'Utilisateur'
                            # Reformater l'ouverture (SANS la mention du rôle)
                            try:
                                replaced_open = False
                                sep_value = "---------------------------------------------"
                                # best effort: detect existing opening by searching la phrase "a créé un ticket"
                                for i, f in enumerate(embed.fields):
                                    val = (f.value or "")
                                    if "a créé un ticket" in val and info.get("category") in val:
                                        # use mention if member still exists, else fallback to name
                                        opener_display = owner.mention if owner else (owner.name if owner else 'Utilisateur')
                                        embed.set_field_at(i, name="\u200b", value=f"• {opener_display} a créé un ticket concernant les **{info.get('category')}** !", inline=False)
                                        replaced_open = True
                                        # ensure separator right after opening
                                        if len(embed.fields) <= i + 1 or "----" not in (embed.fields[i + 1].value or ""):
                                            try:
                                                embed.insert_field_at(i + 1, name="\u200b", value=sep_value, inline=False)
                                            except Exception:
                                                pass
                                        break

                                if not replaced_open:
                                    opener_display = owner.mention if owner else (owner.name if owner else 'Utilisateur')
                                    # insert opening at 0 and separator at 1 if not present
                                    try:
                                        embed.insert_field_at(0, name="\u200b", value=f"• {opener_display} a créé un ticket concernant les **{info.get('category')}** !", inline=False)
                                    except Exception:
                                        pass
                                    try:
                                        if len(embed.fields) <= 1 or "----" not in (embed.fields[1].value or ""):
                                            embed.insert_field_at(1, name="\u200b", value=sep_value, inline=False)
                                    except Exception:
                                        pass
                            except Exception:
                                try:
                                    opener_name = owner.name if owner else 'Utilisateur'
                                    embed.description = f"**{opener_name} a créé un ticket concernant {info.get('category')}**"
                                except Exception:
                                    pass

                            # set claimed status
                            claimed_text = f"• Le ticket a été pris en charge par {claimant.mention if claimant else '—'} !"
                            set_status_in_embed(embed, claimed_text)

                            try:
                                await msg.edit(embed=embed, view=view)
                            except Exception:
                                logger.exception("Impossible d'éditer l'embed restauré pour ticket déjà pris en charge")
                    except Exception:
                        logger.exception("Erreur pendant la restauration d'un ticket pris en charge")
                try:
                    bot.add_view(view, message_id=int(msg_id))
                except Exception:
                    bot.add_view(view)
            except Exception:
                logger.exception("Erreur lors de la restauration d'un ticket au démarrage")

    await save_config(GCFG)
    try:
        await bot.tree.sync()
    except Exception:
        logger.exception("Erreur lors du sync des commandes")

    for guild in bot.guilds:
        try:
            await ensure_support_message(guild)
        except Exception:
            logger.exception("Erreur lors de l'envoi automatique du message support pour une guilde")

    logger.info("✅ Connecté en tant que %s", bot.user)




# ---------- Slash /support (public) ----------
@bot.tree.command(name="support", description="🎫 Ouvrir un ticket support")
async def support(interaction: discord.Interaction):
    cfg = get_gcfg(GCFG, interaction.guild.id)
    await interaction.response.send_message(embed=build_support_embed(), view=TicketView(interaction.guild.id, cfg.get("categories", [])), ephemeral=False)


# ------------------ Nouveaux: modify / move / help ------------------

async def _update_support_message_view_for_guild(guild: discord.Guild, categories: list):
    """
    Tente de retrouver le message d'aide ("📩 Ouvrez un ticket !") dans le salon configuré
    ou dans un salon appelé DEFAULT_SUPPORT_CHANNEL_NAME et met à jour sa view pour refléter
    les catégories actuelles (TicketView).
    """
    cfg = get_gcfg(GCFG, guild.id)
    ch = None
    if cfg.get("support_channel_id"):
        try:
            ch = guild.get_channel(int(cfg["support_channel_id"]))
        except Exception:
            ch = None
    if not ch:
        ch = discord.utils.get(guild.text_channels, name=DEFAULT_SUPPORT_CHANNEL_NAME)
    if not ch:
        return False

    try:
        async for msg in ch.history(limit=150):
            if msg.author == bot.user and msg.embeds:
                if msg.embeds[0].title == "📩 Ouvrez un ticket !":
                    try:
                        await msg.edit(view=TicketView(guild.id, categories))
                        return True
                    except Exception:
                        # fallback: resend new support message (best-effort)
                        try:
                            await ch.send(embed=build_support_embed(), view=TicketView(guild.id, categories))
                            return True
                        except Exception:
                            return False
    except Exception:
        logger.exception("Erreur en parcourant l'historique pour update_support_message_view")
    return False


@bot.tree.command(name="modify-category", description="Modifier le titre, description ou emoji d'une catégorie")
@app_commands.describe(old_label="Titre actuel de la catégorie", new_label="Nouveau titre (laisser vide pour ne pas changer)", new_description="Nouvelle description (laisser vide pour ne pas changer)", new_emoji="Nouvel emoji (laisser vide pour ne pas changer)")
async def modify_category(interaction: discord.Interaction, old_label: str, new_label: str = None, new_description: str = None, new_emoji: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return

    cfg = get_gcfg(GCFG, interaction.guild.id)
    categories = cfg.get("categories", [])
    # find category (case-insensitive)
    idx = None
    for i, c in enumerate(categories):
        if c.get("label", "").lower() == old_label.lower():
            idx = i
            break
    if idx is None:
        await interaction.response.send_message("⚠️ Catégorie introuvable.", ephemeral=True)
        return

    cat = categories[idx]
    old_label_real = cat.get("label")

    # Check uniqueness if renaming
    if new_label and any(c.get("label","").lower() == new_label.lower() for i2,c in enumerate(categories) if i2 != idx):
        await interaction.response.send_message("⚠️ Une autre catégorie porte déjà ce nom.", ephemeral=True)
        return

    changed = []
    if new_label and new_label.strip():
        cat["label"] = new_label.strip()
        changed.append("nom")
    if new_description is not None and new_description != "":
        cat["description"] = new_description
        changed.append("description")
    if new_emoji is not None:
        stored_emoji = new_emoji if (new_emoji and new_emoji.strip()) else " "
        cat["emoji"] = stored_emoji
        changed.append("emoji")

    # persist changes
    await save_config(GCFG)
    bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))

    # Update any open_tickets entries that referenced the old label
    updated_tickets = 0
    for key, entry in (cfg.get("open_tickets", {}) or {}).items():
        try:
            if entry.get("category") == old_label_real:
                entry["category"] = cat["label"]
                updated_tickets += 1

                # update channel topic if present
                cid = entry.get("channel_id")
                if cid:
                    ch = interaction.guild.get_channel(int(cid))
                    if ch and isinstance(ch.topic, str) and ch.topic.startswith("ticket_category:"):
                        try:
                            await ch.edit(topic=f"ticket_category:{cat['label']}")
                        except Exception:
                            logger.exception("Impossible de mettre à jour le topic du channel %s", ch.id)

                # try to update the message embed title/status if possible (best-effort)
                try:
                    msg_id = entry.get("message_id")
                    if cid and msg_id:
                        ch = interaction.guild.get_channel(int(cid))
                        if ch:
                            try:
                                msg = await ch.fetch_message(int(msg_id))
                                if msg and msg.embeds:
                                    embed = msg.embeds[0]
                                    # update title if it matches old label
                                    try:
                                        if (embed.title or "") == old_label_real:
                                            embed.title = cat["label"]
                                        # also update opening line if it contains the old label
                                        for i_field, f in enumerate(embed.fields):
                                            if isinstance(f.value, str) and old_label_real in f.value:
                                                new_val = f.value.replace(old_label_real, cat["label"])
                                                embed.set_field_at(i_field, name=f.name or "\u200b", value=new_val, inline=f.inline)
                                                break
                                    except Exception:
                                        pass
                                    try:
                                        await msg.edit(embed=embed, view=TicketView(interaction.guild.id, cfg.get("categories", [])))
                                    except Exception:
                                        logger.exception("Impossible d'éditer le message du ticket %s", msg.id)
                            except Exception:
                                # fetch_message may fail if message deleted
                                pass
                except Exception:
                    logger.exception("Erreur lors de la mise à jour d'un message après renommage de catégorie")
        except Exception:
            logger.exception("Erreur lors de la migration d'une entrée open_tickets")

    # Try to update the support message view so the selector reflects the new names/order
    try:
        await _update_support_message_view_for_guild(interaction.guild, cfg.get("categories", []))
    except Exception:
        logger.exception("Erreur lors de la mise à jour du message support après modification de catégorie")

    await interaction.response.send_message(f"✅ Catégorie **{old_label_real}** modifiée ({', '.join(changed) if changed else 'rien'}) — {updated_tickets} ticket(s) mis à jour.", ephemeral=True)


@bot.tree.command(name="move-category", description="Déplacer une catégorie à une position donnée (1 = première)")
@app_commands.describe(label="Titre de la catégorie à déplacer", position="Nouvelle position (1 = en haut)")
async def move_category(interaction: discord.Interaction, label: str, position: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Tu dois être administrateur pour utiliser cette commande.", ephemeral=True)
        return

    cfg = get_gcfg(GCFG, interaction.guild.id)
    cats = cfg.get("categories", [])
    if not cats:
        await interaction.response.send_message("⚠️ Aucune catégorie configurée.", ephemeral=True)
        return

    # find index
    idx = None
    for i, c in enumerate(cats):
        if c.get("label", "").lower() == label.lower():
            idx = i
            break
    if idx is None:
        await interaction.response.send_message("⚠️ Catégorie introuvable.", ephemeral=True)
        return

    # clamp position
    pos = max(1, min(position, len(cats)))
    if idx == pos - 1:
        await interaction.response.send_message("ℹ️ La catégorie est déjà à cette position.", ephemeral=True)
        return

    # move
    cat = cats.pop(idx)
    cats.insert(pos - 1, cat)
    cfg["categories"] = cats
    await save_config(GCFG)
    bot.add_view(TicketView(interaction.guild.id, cfg.get("categories", [])))

    # update support message view
    try:
        updated = await _update_support_message_view_for_guild(interaction.guild, cfg.get("categories", []))
    except Exception:
        updated = False

    await interaction.response.send_message(f"✅ Catégorie **{cat.get('label')}** déplacée en position {pos}." + ("" if updated else " (Le message support n'a pas pu être mis à jour automatiquement — utiliser /send-embed si nécessaire)"), ephemeral=True)


@bot.tree.command(name="help", description="Affiche l'aide des commandes FastSupport")
async def help_support(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🆘 FastSupport — Aide",
        description="Voici la liste des commandes et fonctionnalités disponibles.",
        color=discord.Color.from_rgb(54, 57, 63)
    )



    embed.add_field(
        name="🛠️ Administration",
        value=(
            "• `/help` — Afficher la listes des commandes\n"
            "• `/support` — Ouvrir un ticket support\n"
            "• `/set-channel` — Définir le salon support\n"
            "• `/send-embed` — Envoyer le message support\n"
            "• `/add-category` — Ajouter une catégorie\n"
            "• `/remove-category` — Supprimer une catégorie\n"
            "• `/modify-category` — Modifier une catégorie\n"
            "• `/move-category` — Changer l’ordre des catégories\n"
            "• `/list-categories` — Voir les catégories configurés"
        ),
        inline=False
    )

    embed.add_field(
        name="👥 Gestion des rôles",
        value=(
            "• `/set-category-notify` — Rôle ping à l’ouverture du ticket\n"
            "• `/add-category-close-role` — Autoriser un rôle à fermer le ticket\n"
            "• `/remove-category-close-role` — Retirer l’autorisation à fermer le ticket\n"
            "• `/show-category-roles` — Voir les rôles d’une catégorie"
        ),
        inline=False
    )

    embed.add_field(
    name="🔐 Commandes utiles",
    value=(
        "• `!close` — Fermer le ticket\n"
        "• `!add @user` — Ajouter un membre au ticket\n"
        "• `!remove @user` — Retirer un membre du ticket\n"
        "• `!rename <nom>` — Renommer le ticket"

    ),
    inline=False
    )

    embed.set_footer(text="FastSupport • v2.5")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- Helpers + commands (slash + prefix) : close / rename / add / remove ----------------

def _user_has_ticket_manage_privs(user: discord.Member, guild: discord.Guild, gcfg: dict, entry: dict) -> bool:
    """
    Retourne True si l'utilisateur peut gérer/fermer/renommer/ajouter/retirer sur ce ticket.
    Logique identique à celle utilisée ailleurs (admin / claim / close_role_ids / legacy staff role).
    """
    # admin
    try:
        if user.guild_permissions.administrator:
            return True
    except Exception:
        pass

    # claim (celui qui a pris en charge)
    try:
        if entry and entry.get("claimed_by") and int(entry["claimed_by"]) == int(user.id):
            return True
    except Exception:
        pass

    # category close roles
    try:
        cat_label = entry.get("category") if entry else None
        for c in (gcfg.get("categories", []) or []):
            if c.get("label") == cat_label:
                close_ids = c.get("close_role_ids", []) or []
                user_role_ids = {r.id for r in user.roles}
                if any(rid in user_role_ids for rid in close_ids):
                    return True
                break
    except Exception:
        pass

    # legacy staff role
    try:
        staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE)
        if staff_role and staff_role in user.roles:
            return True
    except Exception:
        pass

    return False


async def _get_ticket_entry_and_gcfg(channel: discord.TextChannel):
    """
    Retourne (entry, gcfg) si channel est connu dans open_tickets (clé = str(channel.id)).
    entry peut être None même si gcfg existe.
    """
    gcfg = get_gcfg(GCFG, channel.guild.id)
    key = str(channel.id)
    entry = (gcfg.get("open_tickets", {}) or {}).get(key)
    return entry, gcfg


# ---------------- Slash commands: ticket-close / ticket-rename / ticket-add / ticket-remove ----------------

@bot.tree.command(name="ticket-close", description="🔒 Fermer ce ticket (doit être exécuté depuis le salon ticket)")
async def ticket_close(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande doit être utilisée dans un salon texte.", ephemeral=True)
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    is_ticket = bool(entry) or (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:"))
    if not is_ticket:
        await interaction.response.send_message("⚠️ Ce salon ne semble pas être un ticket.", ephemeral=True)
        return

    if not _user_has_ticket_manage_privs(interaction.user, guild, gcfg, entry or {}):
        await interaction.response.send_message("⛔ Tu n'as pas la permission de fermer ce ticket.", ephemeral=True)
        return

    # log
    log_channel = await get_or_create_log_channel(guild)
    if log_channel:
        try:
            owner_mention = "inconnu"
            if entry and entry.get("owner_id"):
                try:
                    owner = guild.get_member(int(entry.get("owner_id")))
                    owner_mention = owner.mention if owner else "inconnu"
                except Exception:
                    owner_mention = "inconnu"
            embed = discord.Embed(
                title="📁 Ticket fermé",
                description=(
                    f"**Salon :** {channel.name}\n**Fermé par :** {interaction.user.mention}\n"
                    f"**Utilisateur :** {owner_mention}\n"
                    f"**Heure :** {datetime.utcnow().isoformat()} UTC"
                ),
                color=discord.Color.red()
            )
            await log_channel.send(embed=embed)
        except Exception:
            logger.exception("Impossible d'envoyer le log de fermeture")

    # cleanup persisted open_tickets
    try:
        key = str(channel.id)
        if key in gcfg.get("open_tickets", {}):
            del gcfg["open_tickets"][key]
            await save_config(GCFG)
    except Exception:
        logger.exception("Erreur lors du cleanup open_tickets pour ticket-close")

    await interaction.response.send_message("🔒 Ticket fermé — suppression du salon.", ephemeral=True)
    try:
        await channel.delete()
    except Exception:
        logger.exception("Impossible de supprimer le channel lors de ticket-close")


@bot.tree.command(name="ticket-rename", description="✏️ Renommer complètement le salon du ticket")
@app_commands.describe(new_name="Nouveau nom du salon")
async def ticket_rename(interaction: discord.Interaction, new_name: str):
    channel = interaction.channel
    guild = interaction.guild

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande doit être utilisée dans un salon texte.", ephemeral=True)
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    if not entry and not (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:")):
        await interaction.response.send_message("⚠️ Ce salon ne semble pas être un ticket.", ephemeral=True)
        return

    if not _user_has_ticket_manage_privs(interaction.user, guild, gcfg, entry or {}):
        await interaction.response.send_message("⛔ Tu n'as pas la permission de renommer ce ticket.", ephemeral=True)
        return

    candidate = slugify(new_name)

    existing = discord.utils.get(guild.text_channels, name=candidate)
    if existing and existing != channel:
        candidate = f"{candidate}-{channel.id}"

    try:
        await channel.edit(name=candidate)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Je n'ai pas la permission de renommer le salon.", ephemeral=True)
        return
    except Exception:
        logger.exception("Erreur lors du renommage du channel")
        await interaction.response.send_message("❌ Erreur lors du renommage.", ephemeral=True)
        return

    try:
        if entry:
            entry["channel_name"] = candidate
            await save_config(GCFG)
    except Exception:
        logger.exception("Erreur lors de la sauvegarde après renommage")

    await interaction.response.send_message(f"✅ Salon renommé en `{candidate}`.", ephemeral=True)


# ---------------- Slash: /ticket-add ----------------
@bot.tree.command(name="ticket-add", description="➕ Ajouter un utilisateur visible au ticket (permission view/send)")
@app_commands.describe(member="Utilisateur à ajouter au ticket")
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel
    guild = interaction.guild

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande doit être utilisée dans un salon texte.", ephemeral=True)
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    if not entry and not (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:")):
        await interaction.response.send_message("⚠️ Ce salon ne semble pas être un ticket.", ephemeral=True)
        return

    if not _user_has_ticket_manage_privs(interaction.user, guild, gcfg, entry or {}):
        await interaction.response.send_message("⛔ Tu n'as pas la permission d'ajouter un utilisateur à ce ticket.", ephemeral=True)
        return

    try:
        await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Je n'ai pas la permission de modifier les permissions du salon.", ephemeral=True)
        return
    except Exception:
        logger.exception("Erreur lors de l'ajout de la permission")
        await interaction.response.send_message("❌ Erreur lors de l'ajout de l'utilisateur.", ephemeral=True)
        return

    # ack l'interaction sans poster de message visible
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        try:
            await interaction.response.send_message("", ephemeral=True)
        except Exception:
            pass

    # envoyer UNE notification publique dans le salon et la supprimer après 5s
    try:
        sent = await channel.send(f"🔔 {member.mention} a été ajouté au ticket par {interaction.user.mention}.")
        asyncio.create_task(_delete_message_later(sent, 3.0))
    except Exception:
        logger.exception("Impossible d'envoyer la notification d'ajout")
        try:
            await interaction.followup.send("⚠️ Impossible d'envoyer la notification dans le salon.", ephemeral=True)
        except Exception:
            pass


# ---------------- Slash: /ticket-remove ----------------
@bot.tree.command(name="ticket-remove", description="➖ Retirer un utilisateur du ticket (retire overwrite explicite)")
@app_commands.describe(member="Utilisateur à retirer du ticket")
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel
    guild = interaction.guild

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande doit être utilisée dans un salon texte.", ephemeral=True)
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    if not entry and not (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:")):
        await interaction.response.send_message("⚠️ Ce salon ne semble pas être un ticket.", ephemeral=True)
        return

    if not _user_has_ticket_manage_privs(interaction.user, guild, gcfg, entry or {}):
        await interaction.response.send_message("⛔ Tu n'as pas la permission de retirer un utilisateur de ce ticket.", ephemeral=True)
        return

    try:
        await channel.set_permissions(member, overwrite=None)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Je n'ai pas la permission de modifier les permissions du salon.", ephemeral=True)
        return
    except Exception:
        logger.exception("Erreur lors de la suppression de la permission")
        await interaction.response.send_message("❌ Erreur lors du retrait de l'utilisateur.", ephemeral=True)
        return

    # ack l'interaction sans poster de message visible
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        try:
            await interaction.response.send_message("", ephemeral=True)
        except Exception:
            pass

    # envoyer UNE notification publique dans le salon et la supprimer après 5s
    try:
        sent = await channel.send(f"ℹ️ {member.mention} a été retiré du ticket par {interaction.user.mention}.")
        asyncio.create_task(_delete_message_later(sent, 3.0))
    except Exception:
        logger.exception("Impossible d'envoyer la notification de retrait")
        try:
            await interaction.followup.send("⚠️ Impossible d'envoyer la notification dans le salon.", ephemeral=True)
        except Exception:
            pass

 # ---------------- Prefix: +close----------------

@bot.command(name="close")
@commands.guild_only()
async def plus_close(ctx: commands.Context):
    """+close — fermer et supprimer ce ticket (doit être exécuté dans le salon ticket)."""
    channel = ctx.channel
    guild = ctx.guild
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ Cette commande doit être utilisée dans un salon texte.")
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    is_ticket = bool(entry) or (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:"))
    if not is_ticket:
        await ctx.send("⚠️ Ce salon ne semble pas être un ticket.")
        return

    if not _user_has_ticket_manage_privs(ctx.author, guild, gcfg, entry or {}):
        await ctx.send("⛔ Tu n'as pas la permission de fermer ce ticket.")
        return

    # log
    log_channel = await get_or_create_log_channel(guild)
    if log_channel:
        try:
            embed = discord.Embed(
                title="📁 Ticket fermé",
                description=(
                    f"**Salon :** {channel.name}\n**Fermé par :** {ctx.author.mention}\n"
                    f"**Heure :** {datetime.utcnow().isoformat()} UTC"
                ),
                color=discord.Color.red()
            )
            await log_channel.send(embed=embed)
        except Exception:
            logger.exception("Impossible d'envoyer le log de fermeture")

    try:
        key = str(channel.id)
        if key in gcfg.get("open_tickets", {}):
            del gcfg["open_tickets"][key]
            await save_config(GCFG)
    except Exception:
        logger.exception("Erreur lors du cleanup open_tickets pour +close")

    await ctx.send("🔒 Ticket fermé — suppression du salon.")
    try:
        await channel.delete()
    except Exception:
        logger.exception("Impossible de supprimer le channel lors de +close")


# ---------------- Prefix: +add ----------------
@bot.command(name="add")
@commands.guild_only()
async def plus_add(ctx: commands.Context, member: discord.Member):
    """+add @user — ajouter l'utilisateur au ticket (view/send)."""
    channel = ctx.channel
    guild = ctx.guild

    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ Cette commande doit être utilisée dans un salon texte.")
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    if not entry and not (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:")):
        await ctx.send("⚠️ Ce salon ne semble pas être un ticket.")
        return

    if not _user_has_ticket_manage_privs(ctx.author, guild, gcfg, entry or {}):
        await ctx.send("⛔ Tu n'as pas la permission d'ajouter un utilisateur à ce ticket.")
        return

    try:
        await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    except discord.Forbidden:
        await ctx.send("❌ Je n'ai pas la permission de modifier les permissions du salon.")
        return
    except Exception:
        logger.exception("Erreur lors de l'ajout de la permission")
        await ctx.send("❌ Erreur lors de l'ajout de l'utilisateur.")
        return

    # notification publique et suppression auto après 5s
    try:
        sent = await channel.send(f"🔔 {member.mention} a été ajouté au ticket par {ctx.author.mention}.")
        asyncio.create_task(_delete_message_later(sent, 3.0))
    except Exception:
        logger.exception("Impossible d'envoyer la notification d'ajout")

    # optionnel: supprimer le message de commande de l'auteur pour éviter le bruit
    try:
        await ctx.message.delete()
    except Exception:
        pass


# ---------------- Prefix: +remove ----------------
@bot.command(name="remove")
@commands.guild_only()
async def plus_remove(ctx: commands.Context, member: discord.Member):
    """+remove @user — retirer l'utilisateur du ticket (supprime overwrite explicite)."""
    channel = ctx.channel
    guild = ctx.guild

    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ Cette commande doit être utilisée dans un salon texte.")
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    if not entry and not (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:")):
        await ctx.send("⚠️ Ce salon ne semble pas être un ticket.")
        return

    if not _user_has_ticket_manage_privs(ctx.author, guild, gcfg, entry or {}):
        await ctx.send("⛔ Tu n'as pas la permission de retirer un utilisateur de ce ticket.")
        return

    try:
        await channel.set_permissions(member, overwrite=None)
    except discord.Forbidden:
        await ctx.send("❌ Je n'ai pas la permission de modifier les permissions du salon.")
        return
    except Exception:
        logger.exception("Erreur lors de la suppression de la permission")
        await ctx.send("❌ Erreur lors du retrait de l'utilisateur.")
        return

    # notification publique et suppression auto après 5s
    try:
        sent = await channel.send(f"ℹ️ {member.mention} a été retiré du ticket par {ctx.author.mention}.")
        asyncio.create_task(_delete_message_later(sent, 3.0))
    except Exception:
        logger.exception("Impossible d'envoyer la notification de retrait")

    # optionnel: supprimer le message de commande de l'auteur pour éviter le bruit
    try:
        await ctx.message.delete()
    except Exception:
        pass

        
# ---------------- Prefix: +rename ----------------

@bot.command(name="rename")
@commands.guild_only()
async def plus_rename(ctx: commands.Context, *, new_name: str):
    """+rename <nouveau nom> — renomme complètement le salon du ticket."""
    channel = ctx.channel
    guild = ctx.guild

    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ Cette commande doit être utilisée dans un salon texte.")
        return

    entry, gcfg = await _get_ticket_entry_and_gcfg(channel)
    if not entry and not (isinstance(channel.topic, str) and channel.topic.startswith("ticket_category:")):
        await ctx.send("⚠️ Ce salon ne semble pas être un ticket.")
        return

    if not _user_has_ticket_manage_privs(ctx.author, guild, gcfg, entry or {}):
        await ctx.send("⛔ Tu n'as pas la permission de renommer ce ticket.")
        return

    # 🔥 rename COMPLET : uniquement basé sur ce que l'utilisateur écrit
    candidate = slugify(new_name)

    # sécurité : éviter collision de noms
    existing = discord.utils.get(guild.text_channels, name=candidate)
    if existing and existing != channel:
        candidate = f"{candidate}-{channel.id}"

    try:
        await channel.edit(name=candidate)
    except discord.Forbidden:
        await ctx.send("❌ Je n'ai pas la permission de renommer le salon.")
        return
    except Exception:
        logger.exception("Erreur lors du renommage du channel")
        await ctx.send("❌ Erreur lors du renommage.")
        return
    

    
    # sauvegarde si ticket persistant
    try:
        if entry:
            entry["channel_name"] = candidate
            await save_config(GCFG)
    except Exception:
        logger.exception("Erreur lors de la sauvegarde après renommage")

    await ctx.send(f"✅ Salon renommé en `{candidate}`.")

# ---------- Run ----------
bot.run(TOKEN)