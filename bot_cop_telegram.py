import os
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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
DATABASE_PATH = os.getenv("DATABASE_PATH", "cop_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("Configure a variável TELEGRAM_BOT_TOKEN no Railway.")

fila_aguardando = []
em_atendimento = {}
usuarios_em_chamado = {}
atendentes_ativos = {}
painel_message_id = None


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protocolo TEXT UNIQUE,
            user_id INTEGER,
            user_name TEXT,
            categoria TEXT,
            contrato TEXT,
            status TEXT,
            atendente_id INTEGER,
            atendente_nome TEXT,
            fotos INTEGER DEFAULT 0,
            created_at TEXT,
            assumed_at TEXT,
            closed_at TEXT,
            last_message_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protocolo TEXT,
            sender_id INTEGER,
            sender_name TEXT,
            sender_role TEXT,
            message_type TEXT,
            text TEXT,
            created_at TEXT
        )
        """)


def now():
    return datetime.now().isoformat(timespec="seconds")


def criar_ticket_db(user_id, user_name, categoria, contrato=None):
    created = now()
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO tickets
            (user_id, user_name, categoria, contrato, status, created_at, last_message_at)
            VALUES (?, ?, ?, ?, 'aguardando', ?, ?)
        """, (user_id, user_name, categoria, contrato, created, created))
        ticket_id = cur.lastrowid
        protocolo = f"COP-{ticket_id:04d}"
        conn.execute("UPDATE tickets SET protocolo=? WHERE id=?", (protocolo, ticket_id))
    return protocolo


def atualizar_ticket(protocolo, **kwargs):
    if not kwargs:
        return
    cols = ", ".join([f"{k}=?" for k in kwargs])
    values = list(kwargs.values()) + [protocolo]
    with db() as conn:
        conn.execute(f"UPDATE tickets SET {cols} WHERE protocolo=?", values)


def registrar_msg(protocolo, sender_id, sender_name, sender_role, message_type, text=""):
    with db() as conn:
        conn.execute("""
            INSERT INTO messages
            (protocolo, sender_id, sender_name, sender_role, message_type, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (protocolo, sender_id, sender_name, sender_role, message_type, text or "", now()))
        conn.execute("UPDATE tickets SET last_message_at=? WHERE protocolo=?", (now(), protocolo))


def buscar_ticket(protocolo):
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE protocolo=?", (protocolo,)).fetchone()
        return dict(row) if row else None


def obter_ticket_ativo_usuario(user_id):
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM tickets
            WHERE user_id=? AND status IN ('aguardando','em_atendimento')
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()
        return dict(row) if row else None


def obter_ticket_ativo_atendente(atendente_id):
    protocolo = atendentes_ativos.get(atendente_id)
    if protocolo:
        return buscar_ticket(protocolo)
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM tickets
            WHERE atendente_id=? AND status='em_atendimento'
            ORDER BY assumed_at DESC LIMIT 1
        """, (atendente_id,)).fetchone()
        return dict(row) if row else None


def painel_texto():
    def minutos(dt_iso):
        try:
            dt = datetime.fromisoformat(dt_iso)
            return int((datetime.now() - dt).total_seconds() // 60)
        except:
            return 0

    with db() as conn:
        aguardando = conn.execute(
            "SELECT * FROM tickets WHERE status='aguardando' ORDER BY id ASC"
        ).fetchall()

        atendimento = conn.execute(
            "SELECT * FROM tickets WHERE status='em_atendimento' ORDER BY assumed_at ASC"
        ).fetchall()

        finalizados = conn.execute("""
            SELECT COUNT(*) c
            FROM tickets
            WHERE status='finalizado'
            AND date(closed_at)=date('now','localtime')
        """).fetchone()["c"]

    maior_espera = 0
    if aguardando:
        maior_espera = max(minutos(r["created_at"]) for r in aguardando)

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
            linhas.append(
                f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {atendente}"
            )
    else:
        linhas.append("Nenhum atendimento em andamento.")

    linhas.extend([
        "",
        "🟡 *AGUARDANDO*",
    ])

    if aguardando:
        for r in aguardando[:20]:
            espera = minutos(r["created_at"])
            linhas.append(
                f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {espera} min"
            )
    else:
        linhas.append("Fila vazia no momento.")

    linhas.extend([
        "",
        f"🕘 Atualizado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    ])

    return "\n".join(linhas)


async def atualizar_painel(context: ContextTypes.DEFAULT_TYPE):
    global painel_message_id
    if not ATENDENTES_CHAT_ID:
        return

    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Assumir próximo", callback_data="assumir_proximo")],
        [InlineKeyboardButton("📋 Meus atendimentos", callback_data="meus_atendimentos")],
        [InlineKeyboardButton("🔄 Atualizar painel", callback_data="atualizar_painel")],
    ])

    try:
        if painel_message_id:
            await context.bot.edit_message_text(
                chat_id=ATENDENTES_CHAT_ID,
                message_id=painel_message_id,
                text=painel_texto(),
                parse_mode="Markdown",
                reply_markup=teclado,
            )
        else:
            msg = await context.bot.send_message(
                chat_id=ATENDENTES_CHAT_ID,
                text=painel_texto(),
                parse_mode="Markdown",
                reply_markup=teclado,
            )
            painel_message_id = msg.message_id
    except Exception as e:
        logger.warning("Erro ao atualizar painel: %s", e)
        msg = await context.bot.send_message(
            chat_id=ATENDENTES_CHAT_ID,
            text=painel_texto(),
            parse_mode="Markdown",
            reply_markup=teclado,
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Bem-vindo ao COP CIP Telecom.\n\nEscolha uma opção:",
        reply_markup=menu_principal(),
    )


async def meus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enviar_meus_atendimentos(update.effective_user.id, context, update.effective_chat.id)


async def enviar_meus_atendimentos(atendente_id, context, chat_id):
    with db() as conn:
        rows = conn.execute("""
            SELECT protocolo, user_name, categoria
            FROM tickets
            WHERE atendente_id=? AND status='em_atendimento'
            ORDER BY assumed_at DESC
        """, (atendente_id,)).fetchall()

    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="Você não possui atendimentos em aberto.")
        return

    botoes = []
    texto = "📋 *Meus atendimentos:*\n\n"
    for r in rows:
        texto += f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']}\n"
        botoes.append([InlineKeyboardButton(f"Responder {r['protocolo']}", callback_data=f"responder:{r['protocolo']}")])
    await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(botoes))


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data.startswith("categoria:"):
        categoria = data.split(":", 1)[1]
        context.user_data["categoria"] = categoria
        context.user_data["etapa"] = "contrato"
        await query.message.reply_text("📄 Informe o número do contrato:")
        return

    if data == "assumir_proximo":
        await assumir_proximo(query, context)
        return

    if data == "atualizar_painel":
        await atualizar_painel(context)
        return

    if data == "meus_atendimentos":
        await enviar_meus_atendimentos(user.id, context, user.id)
        return

    if data.startswith("responder:"):
        protocolo = data.split(":", 1)[1]
        ticket = buscar_ticket(protocolo)
        if not ticket or ticket["status"] != "em_atendimento" or ticket["atendente_id"] != user.id:
            await query.message.reply_text("Esse atendimento não está disponível para você.")
            return
        atendentes_ativos[user.id] = protocolo
        await query.message.reply_text(
            f"✍️ Modo resposta ativado para *{protocolo}* - {ticket['user_name']}.\n\n"
            "Agora tudo que você enviar aqui irá para esse técnico.\nUse /meus para trocar de atendimento.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("finalizar:"):
        protocolo = data.split(":", 1)[1]
        await finalizar_chamado(protocolo, user, context, query.message.chat_id)
        return


async def assumir_proximo(query, context):
    user = query.from_user
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM tickets WHERE status='aguardando'
            ORDER BY id ASC LIMIT 1
        """).fetchone()
        if not row:
            await query.message.reply_text("Nenhum chamado aguardando no momento.")
            return
        ticket = dict(row)
        conn.execute("""
            UPDATE tickets
            SET status='em_atendimento', atendente_id=?, atendente_nome=?, assumed_at=?, last_message_at=?
            WHERE protocolo=?
        """, (user.id, user.full_name, now(), now(), ticket["protocolo"]))

    protocolo = ticket["protocolo"]
    atendentes_ativos[user.id] = protocolo
    em_atendimento[protocolo] = user.id

    teclado_atendente = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Responder {protocolo}", callback_data=f"responder:{protocolo}")],
        [InlineKeyboardButton(f"✅ Finalizar {protocolo}", callback_data=f"finalizar:{protocolo}")],
    ])

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"✅ Você assumiu um atendimento.\n\n"
                f"🎫 *{protocolo}*\n"
                f"👤 Técnico: *{ticket['user_name']}*\n"
                f"📂 Fila: *{ticket['categoria']}*\n\n"
                "Envie sua resposta aqui no privado do bot."
            ),
            parse_mode="Markdown",
            reply_markup=teclado_atendente,
        )
    except Exception:
        await query.message.reply_text(
            "⚠️ O atendente precisa abrir o bot no privado e enviar /start para receber mensagens privadas."
        )

    await context.bot.send_message(
        chat_id=ticket["user_id"],
        text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{user.full_name}*\n🎫 Protocolo: *{protocolo}*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await atualizar_painel(context)


async def finalizar_chamado(protocolo, user, context, chat_id):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        await context.bot.send_message(chat_id=chat_id, text="Chamado não encontrado.")
        return
    if ticket["atendente_id"] != user.id:
        await context.bot.send_message(chat_id=chat_id, text="Você não é o atendente responsável por esse chamado.")
        return

    atualizar_ticket(protocolo, status="finalizado", closed_at=now())
    atendentes_ativos.pop(user.id, None)
    try:
        await context.bot.send_message(chat_id=ticket["user_id"], text=f"✅ Atendimento {protocolo} finalizado pelo COP.")
    except Exception:
        pass
    await context.bot.send_message(chat_id=chat_id, text=f"✅ Atendimento {protocolo} finalizado.")
    await atualizar_painel(context)


async def tratar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    # Técnico preenchendo abertura
    if context.user_data.get("etapa") == "contrato":
        categoria = context.user_data.get("categoria", "Outros")
        contrato = msg.text.strip() if msg.text else ""
        protocolo = criar_ticket_db(user.id, user.full_name, categoria, contrato)
        context.user_data["protocolo"] = protocolo
        context.user_data["etapa"] = "fotos"
        usuarios_em_chamado[user.id] = protocolo

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

    # Finalizar fotos via botão
    if msg.text and msg.text.strip().lower() in ["✅ finalizar fotos", "finalizar fotos"]:
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        if not protocolo:
            await msg.reply_text("Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return
        registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Finalizou fotos")
        await msg.reply_text(
            f"✅ Fotos recebidas.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await atualizar_painel(context)
        return

    # Técnico ou atendente em conversa
    ticket_tecnico = obter_ticket_ativo_usuario(user.id)
    ticket_atendente = obter_ticket_ativo_atendente(user.id)

    if ticket_tecnico and ticket_tecnico["status"] == "em_atendimento":
        protocolo = ticket_tecnico["protocolo"]
        destino = ticket_tecnico["atendente_id"]
        registrar_msg(protocolo, user.id, user.full_name, "tecnico", "mensagem", msg.text or msg.caption or "")
        await encaminhar_mensagem(msg, context, destino, f"📩 {protocolo} - {user.full_name}")
        return

    if ticket_atendente and ticket_atendente["status"] == "em_atendimento":
        protocolo = ticket_atendente["protocolo"]
        destino = ticket_atendente["user_id"]
        registrar_msg(protocolo, user.id, user.full_name, "atendente", "mensagem", msg.text or msg.caption or "")
        await encaminhar_mensagem(msg, context, destino, f"📩 {protocolo} - COP {user.full_name}")
        return

    # Recebendo fotos antes de finalizar
    if context.user_data.get("etapa") == "fotos":
        protocolo = context.user_data.get("protocolo")
        if msg.photo or msg.document or msg.video:
            ticket = buscar_ticket(protocolo)
            fotos = (ticket.get("fotos") or 0) + 1
            atualizar_ticket(protocolo, fotos=fotos, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "foto", msg.caption or "")
            teclado = ReplyKeyboardMarkup([["✅ Finalizar fotos"]], resize_keyboard=True, one_time_keyboard=False)
            await msg.reply_text(
                f"✅ Foto recebida. Total: {fotos}\n\nToque em *✅ Finalizar fotos* quando terminar.",
                parse_mode="Markdown",
                reply_markup=teclado,
            )
            return

    await msg.reply_text("Use /start para iniciar um novo atendimento.")


async def encaminhar_mensagem(msg, context, destino, cabecalho):
    try:
        if msg.text:
            await context.bot.send_message(chat_id=destino, text=f"{cabecalho}\n\n{msg.text}")
        elif msg.photo:
            await context.bot.send_photo(chat_id=destino, photo=msg.photo[-1].file_id, caption=f"{cabecalho}\n\n{msg.caption or ''}")
        elif msg.document:
            await context.bot.send_document(chat_id=destino, document=msg.document.file_id, caption=f"{cabecalho}\n\n{msg.caption or ''}")
        elif msg.video:
            await context.bot.send_video(chat_id=destino, video=msg.video.file_id, caption=f"{cabecalho}\n\n{msg.caption or ''}")
        elif msg.voice:
            await context.bot.send_voice(chat_id=destino, voice=msg.voice.file_id, caption=cabecalho)
        elif msg.location:
            await context.bot.send_location(chat_id=destino, latitude=msg.location.latitude, longitude=msg.location.longitude)
            await context.bot.send_message(chat_id=destino, text=cabecalho)
        else:
            await msg.forward(chat_id=destino)
    except Exception as e:
        logger.exception("Erro ao encaminhar mensagem: %s", e)
        await msg.reply_text("Não consegui encaminhar a mensagem. Verifique se a outra pessoa iniciou o bot no privado.")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("meus", meus))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, tratar_mensagem))
    app.run_polling()


if __name__ == "__main__":
    main()
