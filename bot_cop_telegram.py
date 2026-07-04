import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
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

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ATENDENTES_CHAT_ID = os.getenv("ATENDENTES_CHAT_ID")  # ID do grupo dos atendentes/COP

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Armazena chamados em memória. Para produção, ideal salvar em banco/planilha.
chamados = {}


MENU_PRINCIPAL = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ PSW", callback_data="menu_psw")],
    [InlineKeyboardButton("📍 NAP", callback_data="menu_nap")],
    [InlineKeyboardButton("📞 Ativo", callback_data="menu_ativo")],
    [InlineKeyboardButton("📶 Atenuação", callback_data="menu_atenuacao")],
    [InlineKeyboardButton("📄 Outros", callback_data="menu_outros")],
    [InlineKeyboardButton("✔️ Finalizar atendimento", callback_data="finalizar_usuario")],
])

MENU_NAP = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔎 Localização da NAP", callback_data="nap_localizacao")],
    [InlineKeyboardButton("📡 NAP mais próxima", callback_data="nap_mais_proxima")],
    [InlineKeyboardButton("🆔 ID da NAP", callback_data="nap_id")],
    [InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_menu")],
])

MENU_ATIVO = InlineKeyboardMarkup([
    [InlineKeyboardButton("👤🚫 Cliente ausente", callback_data="ativo_cliente_ausente")],
    [InlineKeyboardButton("📍❓ Endereço não localizado", callback_data="ativo_endereco_nao_localizado")],
    [InlineKeyboardButton("📄 Outros", callback_data="ativo_outros")],
    [InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_menu")],
])


def novo_chamado(user, tipo):
    chamado_id = f"{user.id}-{int(datetime.now().timestamp())}"
    chamados[chamado_id] = {
        "id": chamado_id,
        "tipo": tipo,
        "user_id": user.id,
        "nome": user.full_name,
        "username": user.username,
        "status": "coletando_dados",
        "etapa": None,
        "contrato": None,
        "localizacao": None,
        "resumo": None,
        "fotos": [],
        "atendente": None,
        "criado_em": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
    return chamado_id


def chamado_do_usuario(user_id):
    pendentes = [c for c in chamados.values() if c["user_id"] == user_id and c["status"] != "finalizado"]
    return pendentes[-1] if pendentes else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome = update.effective_user.first_name or "técnico"
    await update.message.reply_text(
        f"👋 Olá {nome}, tudo bem?\n\n"
        "Seja bem-vindo(a) à Central de Atendimento do COP CIP TELECOM.\n\n"
        "Estou aqui para te ajudar 😊\n"
        "Escolha uma das opções abaixo para continuar 👇",
        reply_markup=MENU_PRINCIPAL,
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "voltar_menu":
        await query.edit_message_text("Escolha uma das opções abaixo 👇", reply_markup=MENU_PRINCIPAL)
        return

    if data == "finalizar_usuario":
        chamado = chamado_do_usuario(user.id)
        if chamado:
            chamado["status"] = "finalizado"
        await query.edit_message_text("✅ Atendimento finalizado.\nObrigado por entrar em contato com o COP CIP TELECOM.")
        return

    if data == "menu_nap":
        await query.edit_message_text("📍 NAP\nEscolha uma das opções abaixo 👇", reply_markup=MENU_NAP)
        return

    if data == "menu_ativo":
        await query.edit_message_text("📞 Ativo\nEscolha uma das opções abaixo 👇", reply_markup=MENU_ATIVO)
        return

    if data == "menu_psw":
        chamado_id = novo_chamado(user, "PSW")
        chamados[chamado_id]["etapa"] = "contrato"
        context.user_data["chamado_id"] = chamado_id
        await query.edit_message_text(
            "✅ PSW\n\n📄 Para prosseguir com o atendimento, envie por favor o *número do contrato*.",
            parse_mode="Markdown",
        )
        return

    if data == "menu_atenuacao":
        chamado_id = novo_chamado(user, "ATENUAÇÃO")
        chamados[chamado_id]["etapa"] = "contrato"
        context.user_data["chamado_id"] = chamado_id
        await query.edit_message_text(
            "📶 Atenuação\n\n📄 Para prosseguir, envie o *número do contrato*.\n"
            "Depois vou solicitar localização/NAP e evidências.",
            parse_mode="Markdown",
        )
        return

    if data == "menu_outros":
        chamado_id = novo_chamado(user, "OUTROS")
        chamados[chamado_id]["etapa"] = "resumo"
        context.user_data["chamado_id"] = chamado_id
        await query.edit_message_text("📄 Outros\n\nResuma em poucas palavras sua solicitação.")
        return

    if data.startswith("nap_"):
        tipo = {
            "nap_localizacao": "NAP - Localização da NAP",
            "nap_mais_proxima": "NAP - NAP mais próxima",
            "nap_id": "NAP - ID da NAP",
        }.get(data, "NAP")
        chamado_id = novo_chamado(user, tipo)
        chamados[chamado_id]["etapa"] = "localizacao"
        context.user_data["chamado_id"] = chamado_id
        await query.edit_message_text(
            "📍 Favor encaminhar sua localização atual (GPS) para validação do atendimento."
        )
        await enviar_botao_localizacao(query.message.chat_id, context)
        return

    if data.startswith("ativo_"):
        tipo = {
            "ativo_cliente_ausente": "ATIVO - Cliente ausente",
            "ativo_endereco_nao_localizado": "ATIVO - Endereço não localizado",
            "ativo_outros": "ATIVO - Outros",
        }.get(data, "ATIVO")
        chamado_id = novo_chamado(user, tipo)
        chamados[chamado_id]["etapa"] = "contrato"
        context.user_data["chamado_id"] = chamado_id
        await query.edit_message_text(
            "📄 Para prosseguir com o atendimento, envie por favor:\n\n"
            "➡️ Número do contrato\n"
            "Depois envie a foto da fachada do cliente."
        )
        return

    if data.startswith("assumir:"):
        chamado_id = data.split(":", 1)[1]
        chamado = chamados.get(chamado_id)
        if not chamado:
            await query.edit_message_text("Chamado não encontrado ou já expirado.")
            return
        chamado["status"] = "em_atendimento"
        chamado["atendente"] = query.from_user.full_name
        await query.edit_message_text(
            f"✅ Chamado assumido por {query.from_user.full_name}.\n"
            f"Tipo: {chamado['tipo']}\nContrato: {chamado.get('contrato') or 'não informado'}"
        )
        await context.bot.send_message(
            chat_id=chamado["user_id"],
            text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{query.from_user.full_name}*",
            parse_mode="Markdown",
        )
        return

    if data.startswith("finalizar_chamado:"):
        chamado_id = data.split(":", 1)[1]
        chamado = chamados.get(chamado_id)
        if chamado:
            chamado["status"] = "finalizado"
            await context.bot.send_message(
                chat_id=chamado["user_id"],
                text="✅ Atendimento finalizado.\nObrigado por entrar em contato com o COP CIP TELECOM.",
            )
        await query.edit_message_text("✅ Chamado finalizado.")


async def enviar_botao_localizacao(chat_id, context):
    teclado = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Enviar localização", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Clique no botão abaixo para enviar sua localização atual.",
        reply_markup=teclado,
    )


async def receber_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chamado_id = context.user_data.get("chamado_id")
    chamado = chamados.get(chamado_id) if chamado_id else chamado_do_usuario(update.effective_user.id)

    if not chamado:
        await update.message.reply_text("Escolha uma opção para iniciar o atendimento.", reply_markup=MENU_PRINCIPAL)
        return

    texto = update.message.text.strip()
    etapa = chamado.get("etapa")

    if etapa == "contrato":
        chamado["contrato"] = texto
        if chamado["tipo"] == "PSW":
            chamado["etapa"] = "localizacao"
            await update.message.reply_text(
                "📌 Contrato recebido.\n\nAgora envie sua localização atual (GPS)."
            )
            await enviar_botao_localizacao(update.effective_chat.id, context)
        elif "ATIVO" in chamado["tipo"]:
            chamado["etapa"] = "foto_fachada"
            await update.message.reply_text(
                "📸 Contrato recebido.\n\nAgora envie a *foto da fachada do cliente*.",
                parse_mode="Markdown",
            )
        elif chamado["tipo"] == "ATENUAÇÃO":
            chamado["etapa"] = "nap_ou_localizacao"
            await update.message.reply_text(
                "📍 Informe a localização da NAP ou o número da NAP."
            )
        else:
            chamado["etapa"] = "resumo"
            await update.message.reply_text("Resuma sua solicitação.")
        return

    if etapa == "nap_ou_localizacao":
        chamado["resumo"] = texto
        await transferir_para_atendimento(update, context, chamado)
        return

    if etapa == "resumo":
        chamado["resumo"] = texto
        await transferir_para_atendimento(update, context, chamado)
        return

    await update.message.reply_text("Informação recebida. Aguarde a continuidade do atendimento.")


async def receber_localizacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chamado_id = context.user_data.get("chamado_id")
    chamado = chamados.get(chamado_id) if chamado_id else chamado_do_usuario(update.effective_user.id)

    if not chamado:
        await update.message.reply_text("Localização recebida, mas não encontrei chamado aberto. Use /start.")
        return

    loc = update.message.location
    chamado["localizacao"] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"

    await update.message.reply_text("📍 Localização recebida.", reply_markup=ReplyKeyboardRemove())

    if chamado["tipo"] == "PSW":
        chamado["etapa"] = "fotos_psw"
        await update.message.reply_text(
            "⚠️ *ALERTA DE OBRIGATORIEDADE*\n\n"
            "As evidências abaixo são obrigatórias para validação do atendimento:\n\n"
            "📸 ONT – Vista superior\n"
            "📸 ONT – Vista inferior com serial legível\n"
            "📸 Cliente navegando\n"
            "📸 Medição do Power Meter\n"
            "📸 Print do inventário\n\n"
            "📍 GEO obrigatório em todas as fotos.\n"
            "🕒 Data e hora visíveis.\n\n"
            "Envie as fotos agora. Quando terminar, digite *finalizar psw*.",
            parse_mode="Markdown",
        )
    else:
        await transferir_para_atendimento(update, context, chamado)


async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chamado_id = context.user_data.get("chamado_id")
    chamado = chamados.get(chamado_id) if chamado_id else chamado_do_usuario(update.effective_user.id)

    if not chamado:
        await update.message.reply_text("Foto recebida, mas não encontrei chamado aberto. Use /start.")
        return

    foto_id = update.message.photo[-1].file_id
    chamado["fotos"].append(foto_id)

    if chamado.get("etapa") == "foto_fachada":
        await update.message.reply_text("📸 Foto da fachada recebida.")
        await transferir_para_atendimento(update, context, chamado)
    elif chamado.get("etapa") == "fotos_psw":
        await update.message.reply_text(
            f"📸 Foto recebida. Total enviado: {len(chamado['fotos'])}.\n"
            "Quando terminar, digite *finalizar psw*.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("📸 Foto recebida.")


async def comando_finalizar_psw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chamado_id = context.user_data.get("chamado_id")
    chamado = chamados.get(chamado_id) if chamado_id else chamado_do_usuario(update.effective_user.id)
    if chamado and chamado.get("etapa") == "fotos_psw":
        await transferir_para_atendimento(update, context, chamado)
    else:
        await update.message.reply_text("Não encontrei um PSW em andamento.")


async def transferir_para_atendimento(update: Update, context: ContextTypes.DEFAULT_TYPE, chamado):
    chamado["status"] = "aguardando_atendente"
    chamado["etapa"] = "aguardando"

    await context.bot.send_message(
        chat_id=chamado["user_id"],
        text="⏳ Estamos direcionando seu atendimento.\nAguarde, um atendente irá assumir seu chamado.",
    )

    resumo = (
        "🚨 *Novo atendimento COP*\n\n"
        f"🆔 Chamado: `{chamado['id']}`\n"
        f"📌 Tipo: *{chamado['tipo']}*\n"
        f"👤 Técnico: {chamado['nome']}\n"
        f"📄 Contrato: {chamado.get('contrato') or 'não informado'}\n"
        f"📍 Localização: {chamado.get('localizacao') or 'não enviada'}\n"
        f"📝 Resumo: {chamado.get('resumo') or 'sem resumo'}\n"
        f"📸 Fotos: {len(chamado.get('fotos', []))}\n"
        f"🕒 Criado em: {chamado['criado_em']}"
    )

    botoes = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Assumir atendimento", callback_data=f"assumir:{chamado['id']}")],
        [InlineKeyboardButton("✅ Finalizar chamado", callback_data=f"finalizar_chamado:{chamado['id']}")],
    ])

    if ATENDENTES_CHAT_ID:
        await context.bot.send_message(
            chat_id=ATENDENTES_CHAT_ID,
            text=resumo,
            parse_mode="Markdown",
            reply_markup=botoes,
        )
        # Envia fotos para o grupo, se houver
        for foto_id in chamado.get("fotos", []):
            await context.bot.send_photo(chat_id=ATENDENTES_CHAT_ID, photo=foto_id)
    else:
        await context.bot.send_message(
            chat_id=chamado["user_id"],
            text="⚠️ Grupo de atendentes não configurado. Configure ATENDENTES_CHAT_ID no arquivo .env.",
        )


async def texto_finalizar_psw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower().strip() == "finalizar psw":
        await comando_finalizar_psw(update, context)


def main():
    if not TOKEN:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN no arquivo .env")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.LOCATION, receber_localizacao))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(?i)^finalizar psw$"), texto_finalizar_psw))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_texto))

    print("Bot COP Telegram iniciado...")
    app.run_polling()


if __name__ == "__main__":
    main()
