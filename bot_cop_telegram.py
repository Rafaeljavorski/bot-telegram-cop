import os
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
ATENDENTES_CHAT_ID = int(os.getenv("ATENDENTES_CHAT_ID", "0"))

if not TOKEN:
    raise RuntimeError("Variavel TELEGRAM_BOT_TOKEN nao configurada no Railway")
if not ATENDENTES_CHAT_ID:
    raise RuntimeError("Variavel ATENDENTES_CHAT_ID nao configurada no Railway")

@dataclass
class Chamado:
    id: int
    user_id: int
    nome: str
    username: str
    fila: str
    contrato: Optional[str] = None
    localizacao: Optional[str] = None
    resumo: Optional[str] = None
    fotos: List[str] = field(default_factory=list)
    status: str = "aguardando"  # aguardando | em_atendimento | finalizado
    atendente_id: Optional[int] = None
    atendente_nome: Optional[str] = None
    criado_em: str = field(default_factory=lambda: datetime.now().strftime("%d/%m/%Y %H:%M"))

chamados: Dict[int, Chamado] = {}
fila_aguardando: List[int] = []
em_atendimento: List[int] = []
user_chamado_aberto: Dict[int, int] = {}          # técnico/cliente -> chamado
atendente_chamado_aberto: Dict[int, int] = {}     # atendente -> chamado assumido
user_etapa: Dict[int, str] = {}
painel_msg_id: Optional[int] = None
proximo_id = 1

MENU_PRINCIPAL = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ PSW", callback_data="menu:PSW"), InlineKeyboardButton("📍 NAP", callback_data="menu:NAP")],
    [InlineKeyboardButton("📞 Ativo", callback_data="menu:Ativo"), InlineKeyboardButton("📶 Atenuação", callback_data="menu:Atenuação")],
    [InlineKeyboardButton("📄 Outros", callback_data="menu:Outros")],
])

PAINEL_BOTOES = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Assumir próximo", callback_data="fila:assumir_proximo")],
    [InlineKeyboardButton("🔄 Atualizar fila", callback_data="fila:atualizar")],
])


def nome_usuario(update: Update) -> str:
    u = update.effective_user
    return u.full_name or u.first_name or "Técnico"


def username_usuario(update: Update) -> str:
    u = update.effective_user
    return f"@{u.username}" if u and u.username else "sem username"


def montar_texto_painel() -> str:
    texto = [
        "📋 *FILA COP - ATENDIMENTOS*",
        "",
        f"🟡 Aguardando: *{len(fila_aguardando)}*",
        f"🟢 Em atendimento: *{len(em_atendimento)}*",
        "",
        "🟢 *EM ATENDIMENTO*",
    ]

    if em_atendimento:
        for pos, cid in enumerate(em_atendimento[-10:], start=1):
            c = chamados[cid]
            texto.append(f"{pos}. #{c.id} — {c.nome} — {c.fila} — 👤 {c.atendente_nome or 'Atendente'}")
    else:
        texto.append("Nenhum atendimento em andamento.")

    texto += ["", "🟡 *AGUARDANDO*"]
    if fila_aguardando:
        for pos, cid in enumerate(fila_aguardando[:20], start=1):
            c = chamados[cid]
            texto.append(f"{pos}. #{c.id} — {c.nome} — {c.fila} — {c.criado_em}")
        if len(fila_aguardando) > 20:
            texto.append(f"... e mais {len(fila_aguardando) - 20} aguardando.")
    else:
        texto.append("Fila vazia no momento.")

    texto += ["", f"🕒 Atualizado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"]
    return "\n".join(texto)


def botoes_finalizar(chamado_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Finalizar atendimento", callback_data=f"chamado:finalizar:{chamado_id}")]
    ])


async def atualizar_painel(context: ContextTypes.DEFAULT_TYPE):
    global painel_msg_id
    texto = montar_texto_painel()
    try:
        if painel_msg_id:
            await context.bot.edit_message_text(
                chat_id=ATENDENTES_CHAT_ID,
                message_id=painel_msg_id,
                text=texto,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=PAINEL_BOTOES,
            )
        else:
            msg = await context.bot.send_message(
                chat_id=ATENDENTES_CHAT_ID,
                text=texto,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=PAINEL_BOTOES,
            )
            painel_msg_id = msg.message_id
    except Exception as e:
        logger.warning("Falha ao atualizar painel, criando novo. Erro: %s", e)
        msg = await context.bot.send_message(
            chat_id=ATENDENTES_CHAT_ID,
            text=texto,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=PAINEL_BOTOES,
        )
        painel_msg_id = msg.message_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Seja bem-vindo(a) à Central de Atendimento COP CIP Telecom.\n\nEscolha uma opção abaixo:",
        reply_markup=MENU_PRINCIPAL,
    )


async def painel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await atualizar_painel(context)
    await update.message.reply_text("✅ Painel da fila atualizado no grupo de atendentes.")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fila = query.data.split(":", 1)[1]

    context.user_data.clear()
    context.user_data["fila"] = fila
    user_etapa[query.from_user.id] = "contrato"

    await query.message.reply_text(
        f"📄 Você selecionou: *{fila}*\n\nInforme o número do contrato:",
        parse_mode=ParseMode.MARKDOWN,
    )


async def receber_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Se for conversa após chamado assumido, faz ponte técnico <-> atendente
    if await encaminhar_conversa(update, context):
        return

    user_id = update.effective_user.id
    etapa = user_etapa.get(user_id)

    if not etapa:
        await update.message.reply_text("Use /start para iniciar um novo atendimento.")
        return

    if etapa == "contrato":
        context.user_data["contrato"] = update.message.text.strip()
        user_etapa[user_id] = "localizacao"
        botao_local = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Enviar localização", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text("📍 Agora envie sua localização atual pelo botão abaixo.", reply_markup=botao_local)
        return

    if etapa == "resumo":
        context.user_data["resumo"] = update.message.text.strip()
        await criar_chamado(update, context)
        return

    await update.message.reply_text("Continue seguindo as instruções do atendimento.")


async def receber_localizacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await encaminhar_conversa(update, context):
        return

    user_id = update.effective_user.id
    loc = update.message.location
    context.user_data["localizacao"] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"

    fila = context.user_data.get("fila", "Outros")
    if fila in ["PSW", "Ativo", "Atenuação"]:
        user_etapa[user_id] = "fotos"
        await update.message.reply_text(
            "📸 Envie as evidências/fotos necessárias.\n\nQuando terminar, digite: *finalizar fotos*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
    elif fila == "NAP":
        user_etapa[user_id] = "resumo"
        await update.message.reply_text(
            "📍 Informe a localização da NAP, número da NAP ou detalhe da solicitação:",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        user_etapa[user_id] = "resumo"
        await update.message.reply_text("📄 Resuma sua solicitação em poucas palavras:", reply_markup=ReplyKeyboardRemove())


async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Se já estiver em atendimento, foto vira conversa e vai para o outro lado
    if await encaminhar_conversa(update, context):
        return

    user_id = update.effective_user.id
    if user_etapa.get(user_id) != "fotos":
        return

    foto = update.message.photo[-1]
    context.user_data.setdefault("fotos", []).append(foto.file_id)
    await update.message.reply_text(
        f"✅ Foto recebida. Total: {len(context.user_data.get('fotos', []))}\n\nEnvie mais fotos ou digite *finalizar fotos*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def finalizar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_etapa.get(user_id) != "fotos":
        await update.message.reply_text("Use /start para iniciar um atendimento.")
        return
    await criar_chamado(update, context)


async def criar_chamado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global proximo_id
    user_id = update.effective_user.id

    if user_id in user_chamado_aberto:
        chamado_existente = chamados.get(user_chamado_aberto[user_id])
        if chamado_existente and chamado_existente.status != "finalizado":
            await update.message.reply_text(f"⚠️ Você já possui um chamado aberto: #{chamado_existente.id}\nAguarde o atendimento ou peça para finalizarem.")
            return

    cid = proximo_id
    proximo_id += 1

    chamado = Chamado(
        id=cid,
        user_id=user_id,
        nome=nome_usuario(update),
        username=username_usuario(update),
        fila=context.user_data.get("fila", "Outros"),
        contrato=context.user_data.get("contrato"),
        localizacao=context.user_data.get("localizacao"),
        resumo=context.user_data.get("resumo"),
        fotos=context.user_data.get("fotos", []),
    )

    chamados[cid] = chamado
    fila_aguardando.append(cid)
    user_chamado_aberto[user_id] = cid
    user_etapa.pop(user_id, None)
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ Seu chamado entrou na fila.\n\n🆔 Chamado: #{cid}\n📋 Fila: {chamado.fila}\n\nAguarde, um atendente irá assumir seu atendimento.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await atualizar_painel(context)


async def fila_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acao = query.data.split(":", 1)[1]

    if acao == "atualizar":
        await atualizar_painel(context)
        return

    if acao == "assumir_proximo":
        if not fila_aguardando:
            await query.answer("Não há chamados aguardando.", show_alert=True)
            return
        cid = fila_aguardando[0]
        await assumir_chamado(cid, query, context)


async def chamado_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, acao, cid_txt = query.data.split(":")
    cid = int(cid_txt)
    if acao == "finalizar":
        await finalizar_chamado(cid, query, context)


async def assumir_chamado(cid: int, query, context: ContextTypes.DEFAULT_TYPE):
    chamado = chamados.get(cid)
    if not chamado:
        await query.answer("Chamado não encontrado.", show_alert=True)
        return
    if chamado.status != "aguardando":
        await query.answer("Esse chamado já foi assumido ou finalizado.", show_alert=True)
        return

    chamado.status = "em_atendimento"
    chamado.atendente_id = query.from_user.id
    chamado.atendente_nome = query.from_user.full_name

    if cid in fila_aguardando:
        fila_aguardando.remove(cid)
    if cid not in em_atendimento:
        em_atendimento.append(cid)
    atendente_chamado_aberto[chamado.atendente_id] = cid

    try:
        await context.bot.send_message(
            chat_id=chamado.user_id,
            text=(
                "🔷 *CIP Telecom*\n\n"
                f"Seu atendimento #{chamado.id} foi iniciado por:\n"
                f"👤 *{chamado.atendente_nome}*\n\n"
                "A partir de agora, envie suas mensagens aqui mesmo. O COP receberá e responderá por este chat."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning("Nao consegui avisar usuario %s: %s", chamado.user_id, e)

    try:
        await context.bot.send_message(
            chat_id=chamado.atendente_id,
            text=(
                "✅ *Você assumiu um atendimento*\n\n"
                f"🆔 Chamado: #{chamado.id}\n"
                f"📋 Fila: {chamado.fila}\n"
                f"👤 Técnico: {chamado.nome} {chamado.username}\n"
                f"📄 Contrato: {chamado.contrato or 'não informado'}\n"
                f"📍 Localização: {chamado.localizacao or 'não enviada'}\n"
                f"📝 Resumo: {chamado.resumo or 'sem resumo'}\n\n"
                "💬 Responda aqui no privado do bot. Sua mensagem será enviada ao técnico."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_finalizar(chamado.id),
        )
    except Exception as e:
        logger.warning("Nao consegui enviar privado ao atendente %s: %s", chamado.atendente_id, e)
        await query.answer("Você assumiu, mas abra o bot no privado e clique em /start para receber e responder.", show_alert=True)

    for file_id in chamado.fotos[:10]:
        try:
            await context.bot.send_photo(chat_id=chamado.atendente_id, photo=file_id, caption=f"📸 Evidência do chamado #{chamado.id}")
        except Exception as e:
            logger.warning("Falha ao enviar foto ao atendente: %s", e)

    await atualizar_painel(context)


async def finalizar_chamado(cid: int, query, context: ContextTypes.DEFAULT_TYPE):
    chamado = chamados.get(cid)
    if not chamado:
        await query.answer("Chamado não encontrado.", show_alert=True)
        return
    if chamado.status == "finalizado":
        await query.answer("Esse chamado já foi finalizado.", show_alert=True)
        return
    if chamado.atendente_id and chamado.atendente_id != query.from_user.id:
        await query.answer("Somente o atendente que assumiu pode finalizar.", show_alert=True)
        return

    chamado.status = "finalizado"
    if cid in fila_aguardando:
        fila_aguardando.remove(cid)
    if cid in em_atendimento:
        em_atendimento.remove(cid)
    user_chamado_aberto.pop(chamado.user_id, None)
    if chamado.atendente_id:
        atendente_chamado_aberto.pop(chamado.atendente_id, None)

    await context.bot.send_message(
        chat_id=chamado.user_id,
        text=(
            "✅ *Atendimento finalizado*\n\n"
            f"Chamado #{chamado.id} finalizado por: *{query.from_user.full_name}*\n"
            "Obrigado por entrar em contato com o COP CIP Telecom."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        await context.bot.send_message(chat_id=query.from_user.id, text=f"✅ Atendimento #{chamado.id} finalizado com sucesso.")
    except Exception:
        pass
    await atualizar_painel(context)


async def encaminhar_conversa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Faz a ponte privado técnico <-> privado atendente durante atendimento."""
    if not update.effective_chat or update.effective_chat.type != "private":
        return False

    remetente_id = update.effective_user.id
    cid = None
    destino_id = None
    prefixo = None

    # Técnico/cliente mandando para o COP
    cid_user = user_chamado_aberto.get(remetente_id)
    if cid_user:
        c = chamados.get(cid_user)
        if c and c.status == "em_atendimento" and c.atendente_id:
            cid = cid_user
            destino_id = c.atendente_id
            prefixo = f"💬 *Técnico {c.nome}* — Chamado #{c.id}:"

    # Atendente mandando para técnico
    if not destino_id:
        cid_att = atendente_chamado_aberto.get(remetente_id)
        if cid_att:
            c = chamados.get(cid_att)
            if c and c.status == "em_atendimento":
                cid = cid_att
                destino_id = c.user_id
                prefixo = f"💬 *COP {c.atendente_nome or 'Atendente'}* — Chamado #{c.id}:"

    if not destino_id or not cid:
        return False

    msg = update.message
    try:
        if msg.text and not msg.text.startswith("/"):
            await context.bot.send_message(chat_id=destino_id, text=f"{prefixo}\n\n{msg.text}", parse_mode=ParseMode.MARKDOWN)
        elif msg.photo:
            await context.bot.send_photo(chat_id=destino_id, photo=msg.photo[-1].file_id, caption=prefixo, parse_mode=ParseMode.MARKDOWN)
        elif msg.document:
            await context.bot.send_document(chat_id=destino_id, document=msg.document.file_id, caption=prefixo, parse_mode=ParseMode.MARKDOWN)
        elif msg.video:
            await context.bot.send_video(chat_id=destino_id, video=msg.video.file_id, caption=prefixo, parse_mode=ParseMode.MARKDOWN)
        elif msg.audio:
            await context.bot.send_audio(chat_id=destino_id, audio=msg.audio.file_id, caption=prefixo, parse_mode=ParseMode.MARKDOWN)
        elif msg.voice:
            await context.bot.send_voice(chat_id=destino_id, voice=msg.voice.file_id, caption=prefixo, parse_mode=ParseMode.MARKDOWN)
        elif msg.location:
            await context.bot.send_message(chat_id=destino_id, text=prefixo, parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_location(chat_id=destino_id, latitude=msg.location.latitude, longitude=msg.location.longitude)
        else:
            await context.bot.copy_message(chat_id=destino_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
        return True
    except Exception as e:
        logger.warning("Falha ao encaminhar conversa do chamado %s: %s", cid, e)
        await msg.reply_text("⚠️ Não consegui enviar essa mensagem. Verifique se a outra pessoa iniciou o bot no privado.")
        return True


async def erro_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Erro no bot:", exc_info=context.error)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("painel", painel))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(fila_callback, pattern=r"^fila:"))
    app.add_handler(CallbackQueryHandler(chamado_callback, pattern=r"^chamado:"))
    app.add_handler(MessageHandler(filters.LOCATION, receber_localizacao))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^finalizar fotos$"), finalizar_fotos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_texto))
    app.add_handler(MessageHandler((filters.Document.ALL | filters.VIDEO | filters.VOICE | filters.AUDIO) & ~filters.COMMAND, encaminhar_conversa))
    app.add_error_handler(erro_handler)

    logger.info("Bot COP iniciado com ponte técnico <-> atendente")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
