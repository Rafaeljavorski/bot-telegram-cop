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
    protocolo: str
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
    assumido_em: Optional[str] = None

chamados: Dict[int, Chamado] = {}
fila_aguardando: List[int] = []
em_atendimento: List[int] = []
user_chamado_aberto: Dict[int, int] = {}             # técnico/cliente -> chamado aberto
atendente_chamados: Dict[int, List[int]] = {}         # atendente -> vários chamados
atendente_chamado_selecionado: Dict[int, int] = {}    # atendente -> chamado atual para responder
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
    [InlineKeyboardButton("📋 Meus atendimentos", callback_data="fila:meus")],
    [InlineKeyboardButton("🔄 Atualizar fila", callback_data="fila:atualizar")],
])

FINALIZAR_FOTOS_BOTAO = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Finalizar fotos", callback_data="fotos:finalizar")]
])

# Botão grande no teclado do Telegram (mais fácil para o técnico do que digitar)
FINALIZAR_FOTOS_TECLADO = ReplyKeyboardMarkup(
    [[KeyboardButton("✅ Finalizar fotos")]],
    resize_keyboard=True,
    one_time_keyboard=False,
)


def protocolo(cid: int) -> str:
    return f"COP-{cid:04d}"


def nome_usuario(update: Update) -> str:
    u = update.effective_user
    return u.full_name or u.first_name or "Técnico"


def username_usuario(update: Update) -> str:
    u = update.effective_user
    return f"@{u.username}" if u and u.username else "sem username"


def meus_atendimentos_keyboard(atendente_id: int) -> InlineKeyboardMarkup:
    linhas = []
    ids = atendente_chamados.get(atendente_id, [])
    for cid in ids:
        c = chamados.get(cid)
        if c and c.status == "em_atendimento":
            marcador = "✅" if atendente_chamado_selecionado.get(atendente_id) == cid else "💬"
            linhas.append([InlineKeyboardButton(f"{marcador} {c.protocolo} - {c.nome} - {c.fila}", callback_data=f"atendente:selecionar:{cid}")])
    linhas.append([InlineKeyboardButton("🔄 Atualizar", callback_data="fila:meus")])
    return InlineKeyboardMarkup(linhas)


def resumo_chamado(c: Chamado) -> str:
    return (
        f"🎫 *{c.protocolo}*\n"
        f"📋 Fila: *{c.fila}*\n"
        f"👤 Técnico: *{c.nome}* {c.username}\n"
        f"📄 Contrato: {c.contrato or 'não informado'}\n"
        f"📍 Localização: {c.localizacao or 'não enviada'}\n"
        f"📝 Resumo: {c.resumo or 'sem resumo'}\n"
        f"🕒 Criado: {c.criado_em}"
    )


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
        for pos, cid in enumerate(em_atendimento[-15:], start=1):
            c = chamados[cid]
            texto.append(f"{pos}. *{c.protocolo}* — {c.nome} — {c.fila} — 👤 {c.atendente_nome or 'Atendente'}")
    else:
        texto.append("Nenhum atendimento em andamento.")

    texto += ["", "🟡 *AGUARDANDO*"]
    if fila_aguardando:
        for pos, cid in enumerate(fila_aguardando[:25], start=1):
            c = chamados[cid]
            texto.append(f"{pos}. *{c.protocolo}* — {c.nome} — {c.fila} — {c.criado_em}")
        if len(fila_aguardando) > 25:
            texto.append(f"... e mais {len(fila_aguardando) - 25} aguardando.")
    else:
        texto.append("Fila vazia no momento.")

    texto += ["", f"🕒 Atualizado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"]
    return "\n".join(texto)


def botoes_atendente(chamado_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Responder este chamado", callback_data=f"atendente:selecionar:{chamado_id}")],
        [InlineKeyboardButton("📋 Meus atendimentos", callback_data="fila:meus")],
        [InlineKeyboardButton("✅ Finalizar atendimento", callback_data=f"chamado:finalizar:{chamado_id}")],
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


async def meus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    atendente_id = update.effective_user.id
    ids = [cid for cid in atendente_chamados.get(atendente_id, []) if chamados.get(cid) and chamados[cid].status == "em_atendimento"]
    atendente_chamados[atendente_id] = ids
    if not ids:
        await update.message.reply_text("📋 Você não possui atendimentos em andamento.")
        return
    await update.message.reply_text(
        "📋 *Seus atendimentos*\n\nToque no atendimento que deseja responder. Depois disso, tudo que você enviar aqui irá para o técnico selecionado.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=meus_atendimentos_keyboard(atendente_id),
    )


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
            "📸 Envie as evidências/fotos necessárias.\n\nQuando terminar, toque no botão *✅ Finalizar fotos* abaixo.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=FINALIZAR_FOTOS_TECLADO,
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
    total = len(context.user_data.get("fotos", []))
    await update.message.reply_text(
        f"✅ Foto recebida. Total: {total}\n\nEnvie mais fotos ou toque no botão *✅ Finalizar fotos* abaixo.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=FINALIZAR_FOTOS_TECLADO,
    )


async def fotos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_etapa.get(user_id) != "fotos":
        await query.message.reply_text("Use /start para iniciar um atendimento.")
        return
    # cria chamado usando dados do botão (query não tem message do técnico como Update normal com effective_user sim)
    await criar_chamado(update, context, origem_query=True)


async def finalizar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_etapa.get(user_id) != "fotos":
        await update.message.reply_text("Use /start para iniciar um atendimento.")
        return
    await criar_chamado(update, context)


async def criar_chamado(update: Update, context: ContextTypes.DEFAULT_TYPE, origem_query: bool = False):
    global proximo_id
    user_id = update.effective_user.id

    if user_id in user_chamado_aberto:
        chamado_existente = chamados.get(user_chamado_aberto[user_id])
        if chamado_existente and chamado_existente.status != "finalizado":
            texto = f"⚠️ Você já possui um chamado aberto: *{chamado_existente.protocolo}*\nAguarde o atendimento ou peça para finalizarem."
            if origem_query:
                await update.callback_query.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
            return

    cid = proximo_id
    proximo_id += 1

    chamado = Chamado(
        id=cid,
        protocolo=protocolo(cid),
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

    texto = (
        f"✅ Seu chamado entrou na fila.\n\n"
        f"🎫 Protocolo: *{chamado.protocolo}*\n"
        f"📋 Fila: {chamado.fila}\n\n"
        "Aguarde, um atendente irá assumir seu atendimento."
    )
    if origem_query:
        await update.callback_query.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    await atualizar_painel(context)


async def fila_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acao = query.data.split(":", 1)[1]

    if acao == "atualizar":
        await atualizar_painel(context)
        return

    if acao == "meus":
        ids = [cid for cid in atendente_chamados.get(query.from_user.id, []) if chamados.get(cid) and chamados[cid].status == "em_atendimento"]
        atendente_chamados[query.from_user.id] = ids
        if not ids:
            await query.message.reply_text("📋 Você não possui atendimentos em andamento.")
            return
        await query.message.reply_text(
            "📋 *Seus atendimentos*\n\nToque no atendimento que deseja responder. O atendimento marcado com ✅ é o selecionado no momento.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=meus_atendimentos_keyboard(query.from_user.id),
        )
        return

    if acao == "assumir_proximo":
        if not fila_aguardando:
            await query.answer("Não há chamados aguardando.", show_alert=True)
            return
        cid = fila_aguardando[0]
        await assumir_chamado(cid, query, context)


async def atendente_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, acao, cid_txt = query.data.split(":")
    cid = int(cid_txt)
    c = chamados.get(cid)
    if not c or c.status != "em_atendimento":
        await query.message.reply_text("⚠️ Esse atendimento não está mais em andamento.")
        return
    if c.atendente_id != query.from_user.id:
        await query.answer("Esse atendimento não é seu.", show_alert=True)
        return

    if acao == "selecionar":
        atendente_chamado_selecionado[query.from_user.id] = cid
        await query.message.reply_text(
            f"💬 Atendimento selecionado para resposta:\n\n{resumo_chamado(c)}\n\nAgora tudo que você enviar aqui no privado irá para este técnico.\n\nPara trocar, use /meus.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_atendente(cid),
        )


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
    chamado.assumido_em = datetime.now().strftime("%d/%m/%Y %H:%M")

    if cid in fila_aguardando:
        fila_aguardando.remove(cid)
    if cid not in em_atendimento:
        em_atendimento.append(cid)

    atendente_chamados.setdefault(chamado.atendente_id, [])
    if cid not in atendente_chamados[chamado.atendente_id]:
        atendente_chamados[chamado.atendente_id].append(cid)
    atendente_chamado_selecionado[chamado.atendente_id] = cid

    try:
        await context.bot.send_message(
            chat_id=chamado.user_id,
            text=(
                "🔷 *CIP Telecom*\n\n"
                f"Seu atendimento *{chamado.protocolo}* foi iniciado por:\n"
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
                f"{resumo_chamado(chamado)}\n\n"
                "💬 Este atendimento já ficou selecionado para resposta.\n"
                "Tudo que você enviar aqui no privado será enviado para este técnico.\n\n"
                "Se você assumir mais atendimentos, use /meus para escolher qual deseja responder."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=botoes_atendente(chamado.id),
        )
    except Exception as e:
        logger.warning("Nao consegui enviar privado ao atendente %s: %s", chamado.atendente_id, e)
        await query.answer("Você assumiu, mas abra o bot no privado e clique em /start para receber e responder.", show_alert=True)

    for file_id in chamado.fotos[:10]:
        try:
            await context.bot.send_photo(chat_id=chamado.atendente_id, photo=file_id, caption=f"📸 Evidência — {chamado.protocolo}")
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
        if chamado.atendente_id in atendente_chamados:
            atendente_chamados[chamado.atendente_id] = [x for x in atendente_chamados[chamado.atendente_id] if x != cid]
        if atendente_chamado_selecionado.get(chamado.atendente_id) == cid:
            restantes = [x for x in atendente_chamados.get(chamado.atendente_id, []) if chamados.get(x) and chamados[x].status == "em_atendimento"]
            if restantes:
                atendente_chamado_selecionado[chamado.atendente_id] = restantes[-1]
            else:
                atendente_chamado_selecionado.pop(chamado.atendente_id, None)

    await context.bot.send_message(
        chat_id=chamado.user_id,
        text=(
            "✅ *Atendimento finalizado*\n\n"
            f"Chamado *{chamado.protocolo}* finalizado por: *{query.from_user.full_name}*\n"
            "Obrigado por entrar em contato com o COP CIP Telecom."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        await context.bot.send_message(chat_id=query.from_user.id, text=f"✅ Atendimento {chamado.protocolo} finalizado com sucesso.")
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
            prefixo = f"💬 *Técnico {c.nome}* — *{c.protocolo}*:"
            # quando técnico envia algo, seleciona esse chamado para o atendente responder mais rápido
            atendente_chamado_selecionado[c.atendente_id] = cid

    # Atendente mandando para técnico: precisa haver atendimento selecionado
    if not destino_id:
        cid_att = atendente_chamado_selecionado.get(remetente_id)
        c = chamados.get(cid_att) if cid_att else None
        if c and c.status == "em_atendimento" and c.atendente_id == remetente_id:
            cid = cid_att
            destino_id = c.user_id
            prefixo = f"💬 *COP {c.atendente_nome or 'Atendente'}* — *{c.protocolo}*:"
        elif remetente_id in atendente_chamados and atendente_chamados.get(remetente_id):
            await update.message.reply_text(
                "⚠️ Você possui mais de um atendimento. Use /meus e selecione qual atendimento deseja responder antes de enviar a mensagem."
            )
            return True

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
    app.add_handler(CommandHandler("meus", meus))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(fila_callback, pattern=r"^fila:"))
    app.add_handler(CallbackQueryHandler(atendente_callback, pattern=r"^atendente:"))
    app.add_handler(CallbackQueryHandler(chamado_callback, pattern=r"^chamado:"))
    app.add_handler(CallbackQueryHandler(fotos_callback, pattern=r"^fotos:finalizar$"))
    app.add_handler(MessageHandler(filters.LOCATION, receber_localizacao))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    # Mantém compatibilidade: se alguém digitar, também funciona
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(✅\s*)?finalizar fotos$"), finalizar_fotos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_texto))
    app.add_handler(MessageHandler((filters.Document.ALL | filters.VIDEO | filters.VOICE | filters.AUDIO) & ~filters.COMMAND, encaminhar_conversa))
    app.add_error_handler(erro_handler)

    logger.info("Bot COP iniciado com protocolos, seleção de atendimento e botão finalizar fotos")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
