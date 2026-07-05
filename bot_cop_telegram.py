import os
import psycopg
from psycopg.rows import dict_row
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
ATENDENTES_CHAT_ID = int(os.getenv("ATENDENTES_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
ALERTA_ESPERA_MIN = int(os.getenv("ALERTA_ESPERA_MIN", "10"))

ADM_IDS = [
    int(x.strip())
    for x in os.getenv("ADM_IDS", "").split(",")
    if x.strip()
]

if not BOT_TOKEN:
    raise RuntimeError("Configure a variável TELEGRAM_BOT_TOKEN no Railway.")

if not DATABASE_URL:
    raise RuntimeError("Configure a variável DATABASE_URL no Railway.")

usuarios_em_chamado = {}
painel_message_id = None


def eh_admin(user_id: int) -> bool:
    return user_id in ADM_IDS


class CursorResult:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.lastrowid = None

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class DBWrapper:
    def __init__(self):
        self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def cursor(self):
        return self.conn.cursor()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def execute(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return CursorResult(cur.fetchall())
            return CursorResult()


def db():
    return DBWrapper()


def add_column_if_missing(conn, table, column, ddl):
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
        conn.commit()
    except Exception:
        conn.rollback()

def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                protocolo TEXT UNIQUE,
                user_id BIGINT,
                user_name TEXT,
                categoria TEXT,
                contrato TEXT,
                status TEXT,
                atendente_id BIGINT,
                atendente_nome TEXT,
                fotos INTEGER DEFAULT 0,
                created_at TEXT,
                assumed_at TEXT,
                closed_at TEXT,
                last_message_at TEXT,
                message_thread_id BIGINT,
                subcategoria TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                historico_enviado INTEGER DEFAULT 0,
                alertado_espera INTEGER DEFAULT 0
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                protocolo TEXT,
                sender_id BIGINT,
                sender_name TEXT,
                sender_role TEXT,
                message_type TEXT,
                text TEXT,
                file_id TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                created_at TEXT
            )
            """)
        conn.commit()

        add_column_if_missing(conn, "tickets", "message_thread_id", "BIGINT")
        add_column_if_missing(conn, "tickets", "subcategoria", "TEXT")
        add_column_if_missing(conn, "tickets", "latitude", "DOUBLE PRECISION")
        add_column_if_missing(conn, "tickets", "longitude", "DOUBLE PRECISION")
        add_column_if_missing(conn, "tickets", "historico_enviado", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "tickets", "alertado_espera", "INTEGER DEFAULT 0")

        add_column_if_missing(conn, "messages", "file_id", "TEXT")
        add_column_if_missing(conn, "messages", "latitude", "DOUBLE PRECISION")
        add_column_if_missing(conn, "messages", "longitude", "DOUBLE PRECISION")

def now():
    return datetime.now().isoformat(timespec="seconds")


def minutos(dt_iso):
    try:
        dt = datetime.fromisoformat(dt_iso)
        return int((datetime.now() - dt).total_seconds() // 60)
    except Exception:
        return 0


def criar_ticket_db(user_id, user_name, categoria, contrato=None, subcategoria=None):
    created = now()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tickets
                (user_id, user_name, categoria, contrato, subcategoria, status, created_at, last_message_at)
                VALUES (%s, %s, %s, %s, %s, 'aguardando', %s, %s)
                RETURNING id
            """, (user_id, user_name, categoria, contrato, subcategoria, created, created))
            ticket_id = cur.fetchone()["id"]
            protocolo = f"COP-{ticket_id:04d}"
            cur.execute("UPDATE tickets SET protocolo=%s WHERE id=%s", (protocolo, ticket_id))
    return protocolo


def atualizar_ticket(protocolo, **kwargs):
    if not kwargs:
        return
    cols = ", ".join([f"{k}=%s" for k in kwargs])
    values = list(kwargs.values()) + [protocolo]
    with db() as conn:
        conn.execute(f"UPDATE tickets SET {cols} WHERE protocolo=%s", values)


def registrar_msg(protocolo, sender_id, sender_name, sender_role, message_type, text="", file_id=None, latitude=None, longitude=None):
    with db() as conn:
        conn.execute("""
            INSERT INTO messages
            (protocolo, sender_id, sender_name, sender_role, message_type, text, file_id, latitude, longitude, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            protocolo,
            sender_id,
            sender_name,
            sender_role,
            message_type,
            text or "",
            file_id,
            latitude,
            longitude,
            now(),
        ))
        conn.execute("UPDATE tickets SET last_message_at=%s WHERE protocolo=%s", (now(), protocolo))


def buscar_ticket(protocolo):
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE protocolo=%s", (protocolo,)).fetchone()
        return dict(row) if row else None


def buscar_ticket_por_thread(thread_id):
    if not thread_id:
        return None
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM tickets
            WHERE message_thread_id=%s AND status IN ('aguardando','em_atendimento')
            ORDER BY id DESC LIMIT 1
        """, (thread_id,)).fetchone()
        return dict(row) if row else None


def obter_ticket_ativo_usuario(user_id):
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM tickets
            WHERE user_id=%s AND status IN ('aguardando','em_atendimento')
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()
        return dict(row) if row else None


def limpar_sessao_usuario(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    usuarios_em_chamado.pop(user_id, None)
    # context.user_data only exists for current interacting user. We clear it in handlers when needed.
    try:
        context.user_data.clear()
    except Exception:
        pass


def painel_texto():
    with db() as conn:
        aguardando = conn.execute("SELECT * FROM tickets WHERE status='aguardando' ORDER BY id ASC").fetchall()
        atendimento = conn.execute("SELECT * FROM tickets WHERE status='em_atendimento' ORDER BY assumed_at ASC").fetchall()
        finalizados = conn.execute("""
            SELECT COUNT(*) c FROM tickets
            WHERE status='finalizado' AND DATE(closed_at::timestamp)=CURRENT_DATE
        """).fetchone()["c"]

    maior_espera = max([minutos(r["created_at"]) for r in aguardando] or [0])

    linhas = [
        "📋 *FILA COP - ATENDIMENTOS*",
        "",
        f"🟡 Aguardando: *{len(aguardando)}*",
        f"🟢 Em atendimento: *{len(atendimento)}*",
        f"✅ Finalizados hoje: *{finalizados}*",
        f"⏱️ Maior espera: *{maior_espera} min*",
        "",
        "🟢 *EM ATENDIMENTO*",
    ]

    if atendimento:
        for r in atendimento[:10]:
            atendente = r["atendente_nome"] or "Sem atendente"
            sub = f" / {r['subcategoria']}" if r["subcategoria"] else ""
            linhas.append(f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']}{sub} - {atendente}")
    else:
        linhas.append("Nenhum atendimento em andamento.")

    linhas.extend(["", "🟡 *AGUARDANDO*"])

    if aguardando:
        for r in aguardando[:20]:
            espera = minutos(r["created_at"])
            sub = f" / {r['subcategoria']}" if r["subcategoria"] else ""
            linhas.append(f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']}{sub} - {espera} min")
    else:
        linhas.append("Fila vazia no momento.")

    linhas.extend(["", f"🕘 Atualizado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"])
    return "\n".join(linhas)


def teclado_painel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Atender próximo", callback_data="assumir_proximo")],
        [InlineKeyboardButton("📌 Escolher atendimento", callback_data="adm_escolher")],
        [InlineKeyboardButton("🛠️ Gestão ADM", callback_data="adm_gestao")],
        [InlineKeyboardButton("🔄 Atualizar painel", callback_data="atualizar_painel")],
    ])


async def atualizar_painel(context: ContextTypes.DEFAULT_TYPE):
    global painel_message_id
    if not ATENDENTES_CHAT_ID:
        return

    try:
        if painel_message_id:
            await context.bot.edit_message_text(
                chat_id=ATENDENTES_CHAT_ID,
                message_id=painel_message_id,
                text=painel_texto(),
                parse_mode="Markdown",
                reply_markup=teclado_painel(),
            )
        else:
            msg = await context.bot.send_message(
                chat_id=ATENDENTES_CHAT_ID,
                text=painel_texto(),
                parse_mode="Markdown",
                reply_markup=teclado_painel(),
            )
            painel_message_id = msg.message_id
            try:
                await context.bot.pin_chat_message(
                    chat_id=ATENDENTES_CHAT_ID,
                    message_id=painel_message_id,
                    disable_notification=True,
                )
            except Exception:
                pass
    except Exception as e:
        logger.warning("Erro ao atualizar painel: %s", e)
        msg = await context.bot.send_message(
            chat_id=ATENDENTES_CHAT_ID,
            text=painel_texto(),
            parse_mode="Markdown",
            reply_markup=teclado_painel(),
        )
        painel_message_id = msg.message_id


def menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ PSW", callback_data="categoria:PSW")],
        [InlineKeyboardButton("📍 NAP", callback_data="categoria:NAP")],
        [InlineKeyboardButton("📞 Ativo", callback_data="categoria:Ativo")],
        [InlineKeyboardButton("📶 Atenuação", callback_data="categoria:Atenuacao")],
        [InlineKeyboardButton("📄 Outros", callback_data="categoria:Outros")],
    ])


def menu_ativo():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤🚫 Cliente ausente", callback_data="ativo:Cliente ausente")],
        [InlineKeyboardButton("📍❓ Endereço não localizado", callback_data="ativo:Endereço não localizado")],
        [InlineKeyboardButton("📄 Outros", callback_data="ativo:Outros")],
    ])


def teclado_localizacao():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Enviar localização", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def criar_topico_do_chamado(context, protocolo, user_name, categoria):
    ticket = buscar_ticket(protocolo)
    if ticket and ticket.get("message_thread_id"):
        return ticket["message_thread_id"]

    titulo = f"{protocolo} - {user_name[:18]} - {categoria}"
    topico = await context.bot.create_forum_topic(
        chat_id=ATENDENTES_CHAT_ID,
        name=titulo[:128],
    )
    thread_id = topico.message_thread_id
    atualizar_ticket(protocolo, message_thread_id=thread_id)
    return thread_id


async def enviar_cabecalho_topico(context, protocolo):
    ticket = buscar_ticket(protocolo)
    if not ticket or not ticket.get("message_thread_id"):
        return

    sub = f"\n📌 Motivo: *{ticket['subcategoria']}*" if ticket.get("subcategoria") else ""
    loc = ""
    if ticket.get("latitude") and ticket.get("longitude"):
        loc = "\n📍 Localização recebida."

    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Assumir {protocolo}", callback_data=f"topico_assumir:{protocolo}")],
        [InlineKeyboardButton(f"✅ Finalizar {protocolo}", callback_data=f"topico_finalizar:{protocolo}")],
        [InlineKeyboardButton("🔄 Devolver para fila", callback_data=f"adm_devolver:{protocolo}")],
    ])

    msg = await context.bot.send_message(
        chat_id=ATENDENTES_CHAT_ID,
        message_thread_id=ticket["message_thread_id"],
        text=(
            f"🎫 *{protocolo}*\n"
            f"👤 Técnico: *{ticket['user_name']}*\n"
            f"📂 Fila: *{ticket['categoria']}*{sub}\n"
            f"📄 Contrato: *{ticket.get('contrato') or '-'}*{loc}\n\n"
            "Responda neste tópico para conversar com o técnico."
        ),
        parse_mode="Markdown",
        reply_markup=teclado,
    )

    try:
        await context.bot.pin_chat_message(
            chat_id=ATENDENTES_CHAT_ID,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        pass


async def reenviar_historico_para_topico(context, protocolo):
    ticket = buscar_ticket(protocolo)
    if not ticket or not ticket.get("message_thread_id"):
        return

    if ticket.get("historico_enviado"):
        return

    thread_id = ticket["message_thread_id"]

    with db() as conn:
        msgs = conn.execute("""
            SELECT * FROM messages
            WHERE protocolo=%s AND sender_role='tecnico'
            ORDER BY id ASC
        """, (protocolo,)).fetchall()

    if not msgs:
        atualizar_ticket(protocolo, historico_enviado=1)
        return

    await context.bot.send_message(
        chat_id=ATENDENTES_CHAT_ID,
        message_thread_id=thread_id,
        text="📎 *Histórico/evidências enviados pelo técnico:*",
        parse_mode="Markdown",
    )

    for m in msgs:
        try:
            legenda = f"📩 {protocolo} - {m['sender_name']}"
            if m["text"]:
                legenda += f"\n\n{m['text']}"

            if m["message_type"] == "text":
                await context.bot.send_message(chat_id=ATENDENTES_CHAT_ID, message_thread_id=thread_id, text=legenda)
            elif m["message_type"] == "photo":
                await context.bot.send_photo(chat_id=ATENDENTES_CHAT_ID, message_thread_id=thread_id, photo=m["file_id"], caption=legenda)
            elif m["message_type"] == "document":
                await context.bot.send_document(chat_id=ATENDENTES_CHAT_ID, message_thread_id=thread_id, document=m["file_id"], caption=legenda)
            elif m["message_type"] == "video":
                await context.bot.send_video(chat_id=ATENDENTES_CHAT_ID, message_thread_id=thread_id, video=m["file_id"], caption=legenda)
            elif m["message_type"] == "voice":
                await context.bot.send_voice(chat_id=ATENDENTES_CHAT_ID, message_thread_id=thread_id, voice=m["file_id"], caption=legenda)
            elif m["message_type"] == "location":
                await context.bot.send_location(
                    chat_id=ATENDENTES_CHAT_ID,
                    message_thread_id=thread_id,
                    latitude=m["latitude"],
                    longitude=m["longitude"],
                )
                await context.bot.send_message(chat_id=ATENDENTES_CHAT_ID, message_thread_id=thread_id, text=legenda)
        except Exception as e:
            logger.warning("Falha ao reenviar item do histórico: %s", e)

    atualizar_ticket(protocolo, historico_enviado=1)


def tipo_mensagem(msg):
    if msg.photo:
        return "photo", msg.photo[-1].file_id, msg.caption or ""
    if msg.document:
        return "document", msg.document.file_id, msg.caption or ""
    if msg.video:
        return "video", msg.video.file_id, msg.caption or ""
    if msg.voice:
        return "voice", msg.voice.file_id, ""
    if msg.location:
        return "location", None, "Localização enviada"
    return "text", None, msg.text or ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Bem-vindo ao COP CIP Telecom.\n\nEscolha uma opção:",
        reply_markup=menu_principal(),
    )


async def adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Apenas administradores podem acessar este menu.")
        return
    await enviar_menu_adm(context, update.effective_chat.id)


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    termo = " ".join(context.args).strip()
    if not termo:
        await update.message.reply_text("Use assim:\n/buscar COP-0001\n/buscar 7163621\n/buscar Rafael\n/buscar PSW")
        return

    like = f"%{termo}%"
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE protocolo LIKE %s
               OR contrato LIKE %s
               OR user_name LIKE %s
               OR categoria LIKE %s
               OR subcategoria LIKE %s
               OR atendente_nome LIKE %s
            ORDER BY id DESC
            LIMIT 10
        """, (like, like, like, like, like, like)).fetchall()

    if not rows:
        await update.message.reply_text("Nenhum atendimento encontrado.")
        return

    linhas = ["🔎 *Resultado da busca:*", ""]
    for r in rows:
        sub = f" / {r['subcategoria']}" if r["subcategoria"] else ""
        atendente = r["atendente_nome"] or "-"
        linhas.append(
            f"🎫 *{r['protocolo']}*\n"
            f"👤 Técnico: {r['user_name']}\n"
            f"📂 Fila: {r['categoria']}{sub}\n"
            f"📄 Contrato: {r['contrato'] or '-'}\n"
            f"📊 Status: {r['status']}\n"
            f"👨‍💻 Atendente: {atendente}\n"
        )

    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def enviar_menu_adm(context, chat_id):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Escolher atendimento aguardando", callback_data="adm_escolher")],
        [InlineKeyboardButton("🟢 Ver chamados em atendimento", callback_data="adm_gestao")],
        [InlineKeyboardButton("🔄 Atualizar painel", callback_data="atualizar_painel")],
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text="🛠️ *Painel ADM*\n\nEscolha uma opção:",
        parse_mode="Markdown",
        reply_markup=teclado,
    )


async def listar_aguardando_adm(query, context):
    with db() as conn:
        rows = conn.execute("""
            SELECT protocolo, user_name, categoria, subcategoria, created_at
            FROM tickets WHERE status='aguardando'
            ORDER BY id ASC LIMIT 30
        """).fetchall()

    if not rows:
        await query.message.reply_text("Nenhum chamado aguardando no momento.")
        return

    botoes = []
    texto = "📌 *Escolha o atendimento para assumir:*\n\n"
    for r in rows:
        espera = minutos(r["created_at"])
        sub = f" / {r['subcategoria']}" if r["subcategoria"] else ""
        texto += f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']}{sub} - {espera} min\n"
        botoes.append([InlineKeyboardButton(
            f"Assumir {r['protocolo']} - {r['user_name']}",
            callback_data=f"adm_assumir:{r['protocolo']}"
        )])

    await query.message.reply_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(botoes))


async def listar_gestao_adm(query, context):
    with db() as conn:
        rows = conn.execute("""
            SELECT protocolo, user_name, categoria, atendente_nome, assumed_at
            FROM tickets WHERE status='em_atendimento'
            ORDER BY assumed_at ASC LIMIT 30
        """).fetchall()

    if not rows:
        await query.message.reply_text("Nenhum chamado em atendimento no momento.")
        return

    texto = "🛠️ *Chamados em atendimento:*\n\n"
    botoes = []
    for r in rows:
        tempo = minutos(r["assumed_at"])
        texto += f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {r['atendente_nome']} - {tempo} min\n"
        botoes.append([
            InlineKeyboardButton(f"✅ Encerrar {r['protocolo']}", callback_data=f"adm_encerrar:{r['protocolo']}"),
            InlineKeyboardButton(f"🔄 Devolver {r['protocolo']}", callback_data=f"adm_devolver:{r['protocolo']}"),
        ])

    await query.message.reply_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(botoes))


async def assumir_proximo(query, context):
    with db() as conn:
        row = conn.execute("""
            SELECT protocolo
            FROM tickets
            WHERE status='aguardando'
            ORDER BY id ASC
            LIMIT 1
        """).fetchone()

    if not row:
        await query.message.reply_text("Nenhum chamado aguardando no momento.")
        return

    await assumir_ticket(row["protocolo"], query.from_user, context, query.message.chat_id)


async def assumir_ticket(protocolo, user, context, origem_chat_id=None):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if origem_chat_id:
            await context.bot.send_message(chat_id=origem_chat_id, text="Chamado não encontrado.")
        return

    if ticket["status"] == "finalizado":
        if origem_chat_id:
            await context.bot.send_message(chat_id=origem_chat_id, text=f"⚠️ {protocolo} já foi finalizado.")
        return

    if ticket["status"] == "em_atendimento":
        if origem_chat_id:
            atendente = ticket.get("atendente_nome") or "outro atendente"
            await context.bot.send_message(
                chat_id=origem_chat_id,
                text=f"⚠️ {protocolo} já foi assumido por {atendente}."
            )
        return

    if not ticket.get("message_thread_id"):
        try:
            await criar_topico_do_chamado(context, protocolo, ticket["user_name"], ticket["categoria"])
            await enviar_cabecalho_topico(context, protocolo)
        except Exception as e:
            logger.exception("Erro ao criar tópico ao assumir: %s", e)
            if origem_chat_id:
                await context.bot.send_message(
                    chat_id=origem_chat_id,
                    text="⚠️ Não consegui criar o tópico. Verifique se o bot é admin e pode gerenciar tópicos."
                )
            return

    atualizar_ticket(
        protocolo,
        status="em_atendimento",
        atendente_id=user.id,
        atendente_nome=user.full_name,
        assumed_at=now(),
        last_message_at=now(),
    )

    ticket = buscar_ticket(protocolo)

    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{user.full_name}*\n🎫 Protocolo: *{protocolo}*",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass

    await reenviar_historico_para_topico(context, protocolo)

    if ticket.get("message_thread_id"):
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Finalizar {protocolo}", callback_data=f"topico_finalizar:{protocolo}")],
            [InlineKeyboardButton("🔄 Devolver para fila", callback_data=f"adm_devolver:{protocolo}")],
        ])

        await context.bot.send_message(
            chat_id=ATENDENTES_CHAT_ID,
            message_thread_id=ticket["message_thread_id"],
            text=f"✅ Atendimento assumido por *{user.full_name}*.",
            parse_mode="Markdown",
            reply_markup=teclado,
        )

    await atualizar_painel(context)


async def finalizar_ticket(protocolo, user, context, chat_id=None, admin=False):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Chamado não encontrado.")
        return

    if ticket["status"] == "finalizado":
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ {protocolo} já foi finalizado.")
        return

    if not admin and ticket.get("atendente_id") and ticket["atendente_id"] != user.id:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Você não é o responsável por esse chamado.")
        return

    atualizar_ticket(protocolo, status="finalizado", closed_at=now(), last_message_at=now())
    usuarios_em_chamado.pop(ticket["user_id"], None)

    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=(
                f"✅ Atendimento {protocolo} finalizado pelo COP.\n\n"
                "Se precisar abrir um novo atendimento, utilize /start."
            ),
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass

    if ticket.get("message_thread_id"):
        try:
            await context.bot.edit_forum_topic(
                chat_id=ATENDENTES_CHAT_ID,
                message_thread_id=ticket["message_thread_id"],
                name=f"{protocolo} - FINALIZADO",
            )
        except Exception:
            pass

        # Fecha primeiro para a mensagem automática do Telegram não ficar por último.
        try:
            await context.bot.close_forum_topic(
                chat_id=ATENDENTES_CHAT_ID,
                message_thread_id=ticket["message_thread_id"],
            )
        except Exception:
            pass

        final_text = (
            "━━━━━━━━━━━━━━\n"
            "✅ *Atendimento Finalizado*\n"
            f"🎫 Protocolo: *{protocolo}*\n"
            f"👨‍💻 Atendente: *{user.full_name}*\n"
            f"🕒 Horário: *{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}*\n"
            "━━━━━━━━━━━━━━"
        )

        try:
            await context.bot.send_message(
                chat_id=ATENDENTES_CHAT_ID,
                message_thread_id=ticket["message_thread_id"],
                text=final_text,
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            try:
                await context.bot.reopen_forum_topic(
                    chat_id=ATENDENTES_CHAT_ID,
                    message_thread_id=ticket["message_thread_id"],
                )
                await context.bot.send_message(
                    chat_id=ATENDENTES_CHAT_ID,
                    message_thread_id=ticket["message_thread_id"],
                    text=final_text,
                    parse_mode="Markdown",
                    reply_markup=None,
                )
                await context.bot.close_forum_topic(
                    chat_id=ATENDENTES_CHAT_ID,
                    message_thread_id=ticket["message_thread_id"],
                )
            except Exception:
                pass

    await atualizar_painel(context)


async def devolver_ticket(protocolo, user, context, chat_id=None):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Chamado não encontrado.")
        return

    atualizar_ticket(
        protocolo,
        status="aguardando",
        atendente_id=None,
        atendente_nome=None,
        assumed_at=None,
        last_message_at=now(),
    )

    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=f"🔄 Atendimento {protocolo} voltou para a fila do COP."
        )
    except Exception:
        pass

    if ticket.get("message_thread_id"):
        await context.bot.send_message(
            chat_id=ATENDENTES_CHAT_ID,
            message_thread_id=ticket["message_thread_id"],
            text=f"🔄 Atendimento devolvido para a fila por {user.full_name}.",
        )

    await atualizar_painel(context)


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await query.answer()

    if data.startswith("categoria:"):
        categoria = data.split(":", 1)[1]

        if categoria == "Ativo":
            context.user_data["categoria"] = "Ativo"
            await query.message.reply_text("📞 Escolha o motivo do Ativo:", reply_markup=menu_ativo())
            return

        context.user_data["categoria"] = categoria
        context.user_data["etapa"] = "contrato"
        await query.message.reply_text("📄 Informe o número do contrato:")
        return

    if data.startswith("ativo:"):
        subcategoria = data.split(":", 1)[1]
        context.user_data["categoria"] = "Ativo"
        context.user_data["subcategoria"] = subcategoria
        context.user_data["etapa"] = "contrato"
        await query.message.reply_text(
            f"📞 Motivo selecionado: *{subcategoria}*\n\n📄 Informe o número do contrato:",
            parse_mode="Markdown",
        )
        return

    if data == "assumir_proximo":
        await assumir_proximo(query, context)
        return

    if data == "adm_escolher":
        if not eh_admin(user.id):
            await query.answer("⛔ Apenas administradores podem escolher atendimentos.", show_alert=True)
            return
        await listar_aguardando_adm(query, context)
        return

    if data == "adm_gestao":
        if not eh_admin(user.id):
            await query.answer("⛔ Apenas administradores podem acessar a gestão.", show_alert=True)
            return
        await listar_gestao_adm(query, context)
        return

    if data == "atualizar_painel":
        await atualizar_painel(context)
        return

    if data.startswith("adm_assumir:"):
        if not eh_admin(user.id):
            await query.answer("⛔ Apenas administradores podem assumir atendimento específico.", show_alert=True)
            return
        protocolo = data.split(":", 1)[1]
        await assumir_ticket(protocolo, user, context, query.message.chat_id)
        return

    if data.startswith("topico_assumir:"):
        protocolo = data.split(":", 1)[1]
        await assumir_ticket(protocolo, user, context, query.message.chat_id)
        return

    if data.startswith("topico_finalizar:"):
        protocolo = data.split(":", 1)[1]
        await finalizar_ticket(protocolo, user, context, query.message.chat_id)
        return

    if data.startswith("adm_encerrar:"):
        if not eh_admin(user.id):
            await query.answer("⛔ Apenas administradores podem encerrar chamados.", show_alert=True)
            return
        protocolo = data.split(":", 1)[1]
        await finalizar_ticket(protocolo, user, context, query.message.chat_id, admin=True)
        return

    if data.startswith("adm_devolver:"):
        if not eh_admin(user.id):
            await query.answer("⛔ Apenas administradores podem devolver chamados.", show_alert=True)
            return
        protocolo = data.split(":", 1)[1]
        await devolver_ticket(protocolo, user, context, query.message.chat_id)
        return


async def tratar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    # Limpa sessão antiga se o usuário não tem ticket ativo.
    ticket_ativo = obter_ticket_ativo_usuario(user.id)
    if not ticket_ativo and context.user_data.get("protocolo"):
        context.user_data.clear()
        usuarios_em_chamado.pop(user.id, None)

    # Mensagem enviada dentro de um tópico do grupo: vai para o técnico correto.
    if update.effective_chat and update.effective_chat.id == ATENDENTES_CHAT_ID and msg.message_thread_id:
        ticket = buscar_ticket_por_thread(msg.message_thread_id)
        if ticket and ticket["status"] in ["aguardando", "em_atendimento"]:
            if msg.text and msg.text.startswith("/"):
                return

            if ticket["status"] == "aguardando":
                atualizar_ticket(
                    ticket["protocolo"],
                    status="em_atendimento",
                    atendente_id=user.id,
                    atendente_nome=user.full_name,
                    assumed_at=now(),
                    last_message_at=now(),
                )
                try:
                    await context.bot.send_message(
                        chat_id=ticket["user_id"],
                        text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{user.full_name}*\n🎫 Protocolo: *{ticket['protocolo']}*",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            registrar_msg(ticket["protocolo"], user.id, user.full_name, "atendente", "mensagem", msg.text or msg.caption or "")
            await encaminhar_mensagem(msg, context, ticket["user_id"], f"📩 {ticket['protocolo']} - COP {user.full_name}")
            await atualizar_painel(context)
        return

    # Técnico informando contrato.
    if context.user_data.get("etapa") == "contrato":
        categoria = context.user_data.get("categoria", "Outros")
        subcategoria = context.user_data.get("subcategoria")
        contrato = msg.text.strip() if msg.text else ""

        protocolo = criar_ticket_db(user.id, user.full_name, categoria, contrato, subcategoria)
        context.user_data["protocolo"] = protocolo
        usuarios_em_chamado[user.id] = protocolo

        if categoria == "PSW":
            context.user_data["etapa"] = "localizacao"
            await msg.reply_text(
                f"🎫 Protocolo: *{protocolo}*\n\n"
                "📍 Agora envie a localização atual pelo botão abaixo.",
                parse_mode="Markdown",
                reply_markup=teclado_localizacao(),
            )
            await atualizar_painel(context)
            return

        context.user_data["etapa"] = "fotos"
        teclado = ReplyKeyboardMarkup([["✅ Finalizar fotos"]], resize_keyboard=True, one_time_keyboard=False)

        await msg.reply_text(
            f"🎫 Protocolo: *{protocolo}*\n\n"
            "📸 Envie as evidências/fotos necessárias.\n\n"
            "Quando terminar, toque no botão abaixo:",
            parse_mode="Markdown",
            reply_markup=teclado,
        )
        await atualizar_painel(context)
        return

    # PSW recebendo localização obrigatória.
    if context.user_data.get("etapa") == "localizacao":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        if not protocolo:
            context.user_data.clear()
            await msg.reply_text("Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        ticket = buscar_ticket(protocolo)
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text(
                "Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        if msg.location:
            atualizar_ticket(protocolo, latitude=msg.location.latitude, longitude=msg.location.longitude, last_message_at=now())
            registrar_msg(
                protocolo,
                user.id,
                user.full_name,
                "tecnico",
                "location",
                "Localização enviada",
                latitude=msg.location.latitude,
                longitude=msg.location.longitude,
            )

            context.user_data["etapa"] = "fotos"
            teclado = ReplyKeyboardMarkup([["✅ Finalizar fotos"]], resize_keyboard=True, one_time_keyboard=False)
            await msg.reply_text(
                "✅ Localização recebida.\n\n"
                "📸 Agora envie as evidências/fotos necessárias.\n\n"
                "Quando terminar, toque no botão abaixo:",
                reply_markup=teclado,
            )
            return

        await msg.reply_text(
            "⚠️ Para PSW é obrigatório enviar a localização pelo botão abaixo.",
            reply_markup=teclado_localizacao(),
        )
        return

    # Técnico finalizando fotos.
    if msg.text and msg.text.strip().lower() in ["✅ finalizar fotos", "finalizar fotos"]:
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        if not protocolo:
            context.user_data.clear()
            await msg.reply_text("Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        ticket = buscar_ticket(protocolo)
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text(
                "Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Finalizou fotos")
        await msg.reply_text(
            f"✅ Fotos recebidas.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
            reply_markup=ReplyKeyboardRemove(),
        )

        # Chamado já está na fila, mas continua aceitando novas evidências.
        # Se o técnico enviar algo depois disso, será anexado ao histórico.
        context.user_data["etapa"] = "aguardando_cop"
        await atualizar_painel(context)
        return

    # Técnico já finalizou as fotos e está aguardando COP.
    # Novas fotos/mensagens são anexadas ao chamado sem pedir para finalizar novamente.
    if context.user_data.get("etapa") == "aguardando_cop":
        ticket = obter_ticket_ativo_usuario(user.id)

        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text(
                "Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        msg_type, file_id, text = tipo_mensagem(msg)

        fotos = ticket.get("fotos") or 0
        if msg_type in ["photo", "document", "video", "location"]:
            fotos += 1

        atualizar_ticket(ticket["protocolo"], fotos=fotos, last_message_at=now())
        registrar_msg(
            ticket["protocolo"],
            user.id,
            user.full_name,
            "tecnico",
            msg_type,
            text,
            file_id=file_id,
            latitude=msg.location.latitude if msg.location else None,
            longitude=msg.location.longitude if msg.location else None,
        )

        if ticket.get("message_thread_id"):
            await encaminhar_mensagem(
                msg,
                context,
                ATENDENTES_CHAT_ID,
                f"📩 {ticket['protocolo']} - {user.full_name}",
                thread_id=ticket["message_thread_id"],
            )
        else:
            await msg.reply_text(f"✅ Informação adicionada ao chamado {ticket['protocolo']}.")

        return

    # Técnico enviando mensagem depois do chamado criado: vai para o tópico.
    ticket_tecnico = obter_ticket_ativo_usuario(user.id)
    if ticket_tecnico and ticket_tecnico.get("message_thread_id"):
        msg_type, file_id, text = tipo_mensagem(msg)
        registrar_msg(
            ticket_tecnico["protocolo"],
            user.id,
            user.full_name,
            "tecnico",
            msg_type,
            text,
            file_id=file_id,
            latitude=msg.location.latitude if msg.location else None,
            longitude=msg.location.longitude if msg.location else None,
        )
        await encaminhar_mensagem(
            msg,
            context,
            ATENDENTES_CHAT_ID,
            f"📩 {ticket_tecnico['protocolo']} - {user.full_name}",
            thread_id=ticket_tecnico["message_thread_id"],
        )
        return

    # Recebendo fotos/evidências antes de finalizar.
    if context.user_data.get("etapa") == "fotos":
        protocolo = context.user_data.get("protocolo")
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text(
                "Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        if msg.photo or msg.document or msg.video or msg.location or msg.voice or msg.text:
            msg_type, file_id, text = tipo_mensagem(msg)

            if msg_type in ["photo", "document", "video", "location"]:
                fotos = (ticket.get("fotos") or 0) + 1
            else:
                fotos = ticket.get("fotos") or 0

            atualizar_ticket(protocolo, fotos=fotos, last_message_at=now())
            registrar_msg(
                protocolo,
                user.id,
                user.full_name,
                "tecnico",
                msg_type,
                text,
                file_id=file_id,
                latitude=msg.location.latitude if msg.location else None,
                longitude=msg.location.longitude if msg.location else None,
            )

            teclado = ReplyKeyboardMarkup([["✅ Finalizar fotos"]], resize_keyboard=True, one_time_keyboard=False)
            await msg.reply_text(
                f"✅ Evidência recebida. Total: {fotos}\n\nToque em *✅ Finalizar fotos* quando terminar.",
                parse_mode="Markdown",
                reply_markup=teclado,
            )
            return

    await msg.reply_text("Use /start para iniciar um novo atendimento.")


async def encaminhar_mensagem(msg, context, destino, cabecalho, thread_id=None):
    kwargs = {"chat_id": destino}
    if thread_id:
        kwargs["message_thread_id"] = thread_id

    try:
        if msg.text:
            await context.bot.send_message(**kwargs, text=f"{cabecalho}\n\n{msg.text}")
        elif msg.photo:
            await context.bot.send_photo(**kwargs, photo=msg.photo[-1].file_id, caption=f"{cabecalho}\n\n{msg.caption or ''}")
        elif msg.document:
            await context.bot.send_document(**kwargs, document=msg.document.file_id, caption=f"{cabecalho}\n\n{msg.caption or ''}")
        elif msg.video:
            await context.bot.send_video(**kwargs, video=msg.video.file_id, caption=f"{cabecalho}\n\n{msg.caption or ''}")
        elif msg.voice:
            await context.bot.send_voice(**kwargs, voice=msg.voice.file_id, caption=cabecalho)
        elif msg.location:
            await context.bot.send_location(**kwargs, latitude=msg.location.latitude, longitude=msg.location.longitude)
            await context.bot.send_message(**kwargs, text=cabecalho)
        else:
            await msg.forward(chat_id=destino)
    except Exception as e:
        logger.exception("Erro ao encaminhar mensagem: %s", e)
        try:
            await msg.reply_text("Não consegui encaminhar a mensagem.")
        except Exception:
            pass


async def alerta_espera_job(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE status='aguardando' AND COALESCE(alertado_espera, 0)=0
            ORDER BY id ASC
        """).fetchall()

    for r in rows:
        espera = minutos(r["created_at"])
        if espera >= ALERTA_ESPERA_MIN:
            try:
                await context.bot.send_message(
                    chat_id=ATENDENTES_CHAT_ID,
                    text=(
                        f"🚨 *Alerta de espera*\n\n"
                        f"🎫 {r['protocolo']}\n"
                        f"👤 Técnico: {r['user_name']}\n"
                        f"📂 Fila: {r['categoria']}\n"
                        f"⏱️ Aguardando há {espera} min"
                    ),
                    parse_mode="Markdown",
                )
                atualizar_ticket(r["protocolo"], alertado_espera=1)
            except Exception as e:
                logger.warning("Erro ao enviar alerta de espera: %s", e)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("adm", adm))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, tratar_mensagem))

    try:
        app.job_queue.run_repeating(alerta_espera_job, interval=60, first=60)
    except Exception as e:
        logger.warning("Job queue não inicializada: %s", e)

    app.run_polling()


if __name__ == "__main__":
    main()
