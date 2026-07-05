import os
import sqlite3
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('BOT_TOKEN')
ATENDENTES_CHAT_ID = int(os.getenv('ATENDENTES_CHAT_ID', '0'))
DATABASE_PATH = os.getenv('DATABASE_PATH', 'cop_bot.db')
ADM_IDS = [int(x.strip()) for x in os.getenv('ADM_IDS', '').split(',') if x.strip()]

if not BOT_TOKEN:
    raise RuntimeError('Configure a variável TELEGRAM_BOT_TOKEN no Railway.')

usuarios_em_chamado = {}
painel_message_id = None

def eh_admin(user_id: int) -> bool:
    return user_id in ADM_IDS

def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute('''
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
            last_message_at TEXT,
            message_thread_id INTEGER
        )
        ''')
        conn.execute('''
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
        ''')
        try:
            conn.execute('ALTER TABLE tickets ADD COLUMN message_thread_id INTEGER')
        except sqlite3.OperationalError:
            pass

def now():
    return datetime.now().isoformat(timespec='seconds')

def minutos(dt_iso):
    try:
        dt = datetime.fromisoformat(dt_iso)
        return int((datetime.now() - dt).total_seconds() // 60)
    except Exception:
        return 0

def criar_ticket_db(user_id, user_name, categoria, contrato=None):
    created = now()
    with db() as conn:
        cur = conn.execute('''
            INSERT INTO tickets
            (user_id, user_name, categoria, contrato, status, created_at, last_message_at)
            VALUES (?, ?, ?, ?, 'aguardando', ?, ?)
        ''', (user_id, user_name, categoria, contrato, created, created))
        ticket_id = cur.lastrowid
        protocolo = f'COP-{ticket_id:04d}'
        conn.execute('UPDATE tickets SET protocolo=? WHERE id=?', (protocolo, ticket_id))
    return protocolo

def atualizar_ticket(protocolo, **kwargs):
    if not kwargs:
        return
    cols = ', '.join([f'{k}=?' for k in kwargs])
    values = list(kwargs.values()) + [protocolo]
    with db() as conn:
        conn.execute(f'UPDATE tickets SET {cols} WHERE protocolo=?', values)

def registrar_msg(protocolo, sender_id, sender_name, sender_role, message_type, text=''):
    with db() as conn:
        conn.execute('''
            INSERT INTO messages
            (protocolo, sender_id, sender_name, sender_role, message_type, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (protocolo, sender_id, sender_name, sender_role, message_type, text or '', now()))
        conn.execute('UPDATE tickets SET last_message_at=? WHERE protocolo=?', (now(), protocolo))

def buscar_ticket(protocolo):
    with db() as conn:
        row = conn.execute('SELECT * FROM tickets WHERE protocolo=?', (protocolo,)).fetchone()
        return dict(row) if row else None

def buscar_ticket_por_thread(thread_id):
    if not thread_id:
        return None
    with db() as conn:
        row = conn.execute('''
            SELECT * FROM tickets
            WHERE message_thread_id=? AND status IN ('aguardando','em_atendimento')
            ORDER BY id DESC LIMIT 1
        ''', (thread_id,)).fetchone()
        return dict(row) if row else None

def obter_ticket_ativo_usuario(user_id):
    with db() as conn:
        row = conn.execute('''
            SELECT * FROM tickets
            WHERE user_id=? AND status IN ('aguardando','em_atendimento')
            ORDER BY id DESC LIMIT 1
        ''', (user_id,)).fetchone()
        return dict(row) if row else None

def painel_texto():
    with db() as conn:
        aguardando = conn.execute("SELECT * FROM tickets WHERE status='aguardando' ORDER BY id ASC").fetchall()
        atendimento = conn.execute("SELECT * FROM tickets WHERE status='em_atendimento' ORDER BY assumed_at ASC").fetchall()
        finalizados = conn.execute("""
            SELECT COUNT(*) c FROM tickets
            WHERE status='finalizado' AND date(closed_at)=date('now','localtime')
        """).fetchone()['c']
    maior_espera = max([minutos(r['created_at']) for r in aguardando] or [0])
    linhas = [
        '📋 *FILA COP - ATENDIMENTOS*', '',
        f'🟡 Aguardando: *{len(aguardando)}*',
        f'🟢 Em atendimento: *{len(atendimento)}*',
        f'✅ Finalizados hoje: *{finalizados}*',
        f'⏱️ Maior espera: *{maior_espera} min*', '',
        '🟢 *EM ATENDIMENTO*',
    ]
    if atendimento:
        for r in atendimento[:10]:
            atendente = r['atendente_nome'] or 'Sem atendente'
            linhas.append(f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {atendente}")
    else:
        linhas.append('Nenhum atendimento em andamento.')
    linhas.extend(['', '🟡 *AGUARDANDO*'])
    if aguardando:
        for r in aguardando[:20]:
            espera = minutos(r['created_at'])
            linhas.append(f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {espera} min")
    else:
        linhas.append('Fila vazia no momento.')
    linhas.extend(['', f"🕘 Atualizado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"])
    return '\n'.join(linhas)

def teclado_painel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📌 Escolher atendimento', callback_data='adm_escolher')],
        [InlineKeyboardButton('🛠️ Gestão ADM', callback_data='adm_gestao')],
        [InlineKeyboardButton('🔄 Atualizar painel', callback_data='atualizar_painel')],
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
                parse_mode='Markdown',
                reply_markup=teclado_painel(),
            )
        else:
            msg = await context.bot.send_message(
                chat_id=ATENDENTES_CHAT_ID,
                text=painel_texto(),
                parse_mode='Markdown',
                reply_markup=teclado_painel(),
            )
            painel_message_id = msg.message_id
            try:
                await context.bot.pin_chat_message(chat_id=ATENDENTES_CHAT_ID, message_id=painel_message_id, disable_notification=True)
            except Exception:
                pass
    except Exception as e:
        logger.warning('Erro ao atualizar painel: %s', e)
        msg = await context.bot.send_message(chat_id=ATENDENTES_CHAT_ID, text=painel_texto(), parse_mode='Markdown', reply_markup=teclado_painel())
        painel_message_id = msg.message_id

def menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ PSW', callback_data='categoria:PSW')],
        [InlineKeyboardButton('📍 NAP', callback_data='categoria:NAP')],
        [InlineKeyboardButton('📞 Ativo', callback_data='categoria:Ativo')],
        [InlineKeyboardButton('📶 Atenuação', callback_data='categoria:Atenuacao')],
        [InlineKeyboardButton('📄 Outros', callback_data='categoria:Outros')],
    ])

async def criar_topico_do_chamado(context, protocolo, user_name, categoria):
    titulo = f'{protocolo} - {user_name[:18]} - {categoria}'
    topico = await context.bot.create_forum_topic(chat_id=ATENDENTES_CHAT_ID, name=titulo[:128])
    thread_id = topico.message_thread_id
    atualizar_ticket(protocolo, message_thread_id=thread_id)
    return thread_id

async def enviar_cabecalho_topico(context, protocolo):
    ticket = buscar_ticket(protocolo)
    if not ticket or not ticket.get('message_thread_id'):
        return
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(f'✅ Assumir {protocolo}', callback_data=f'topico_assumir:{protocolo}')],
        [InlineKeyboardButton(f'✅ Finalizar {protocolo}', callback_data=f'topico_finalizar:{protocolo}')],
        [InlineKeyboardButton('🔄 Devolver para fila', callback_data=f'adm_devolver:{protocolo}')],
    ])
    await context.bot.send_message(
        chat_id=ATENDENTES_CHAT_ID,
        message_thread_id=ticket['message_thread_id'],
        text=(
            f"🎫 *{protocolo}*\n"
            f"👤 Técnico: *{ticket['user_name']}*\n"
            f"📂 Fila: *{ticket['categoria']}*\n"
            f"📄 Contrato: *{ticket.get('contrato') or '-'}*\n\n"
            'Responda neste tópico para conversar com o técnico.'
        ),
        parse_mode='Markdown',
        reply_markup=teclado,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text('👋 Bem-vindo ao COP CIP Telecom.\n\nEscolha uma opção:', reply_markup=menu_principal())

async def adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        await update.message.reply_text('⛔ Apenas administradores podem acessar este menu.')
        return
    await enviar_menu_adm(context, update.effective_chat.id)

async def enviar_menu_adm(context, chat_id):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton('📌 Escolher atendimento aguardando', callback_data='adm_escolher')],
        [InlineKeyboardButton('🟢 Ver chamados em atendimento', callback_data='adm_gestao')],
        [InlineKeyboardButton('🔄 Atualizar painel', callback_data='atualizar_painel')],
    ])
    await context.bot.send_message(chat_id=chat_id, text='🛠️ *Painel ADM*\n\nEscolha uma opção:', parse_mode='Markdown', reply_markup=teclado)

async def listar_aguardando_adm(query, context):
    with db() as conn:
        rows = conn.execute("""
            SELECT protocolo, user_name, categoria, created_at
            FROM tickets WHERE status='aguardando'
            ORDER BY id ASC LIMIT 30
        """).fetchall()
    if not rows:
        await query.message.reply_text('Nenhum chamado aguardando no momento.')
        return
    botoes = []
    texto = '📌 *Escolha o atendimento para assumir:*\n\n'
    for r in rows:
        espera = minutos(r['created_at'])
        texto += f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {espera} min\n"
        botoes.append([InlineKeyboardButton(f"Assumir {r['protocolo']} - {r['user_name']}", callback_data=f"adm_assumir:{r['protocolo']}")])
    await query.message.reply_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(botoes))

async def listar_gestao_adm(query, context):
    with db() as conn:
        rows = conn.execute("""
            SELECT protocolo, user_name, categoria, atendente_nome, assumed_at
            FROM tickets WHERE status='em_atendimento'
            ORDER BY assumed_at ASC LIMIT 30
        """).fetchall()
    if not rows:
        await query.message.reply_text('Nenhum chamado em atendimento no momento.')
        return
    texto = '🛠️ *Chamados em atendimento:*\n\n'
    botoes = []
    for r in rows:
        tempo = minutos(r['assumed_at'])
        texto += f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']} - {r['atendente_nome']} - {tempo} min\n"
        botoes.append([
            InlineKeyboardButton(f"✅ Encerrar {r['protocolo']}", callback_data=f"adm_encerrar:{r['protocolo']}"),
            InlineKeyboardButton(f"🔄 Devolver {r['protocolo']}", callback_data=f"adm_devolver:{r['protocolo']}"),
        ])
    await query.message.reply_text(texto, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(botoes))

async def assumir_ticket(protocolo, user, context, origem_chat_id=None):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if origem_chat_id:
            await context.bot.send_message(chat_id=origem_chat_id, text='Chamado não encontrado.')
        return
    if ticket['status'] == 'finalizado':
        if origem_chat_id:
            await context.bot.send_message(chat_id=origem_chat_id, text='Esse chamado já foi finalizado.')
        return
    atualizar_ticket(protocolo, status='em_atendimento', atendente_id=user.id, atendente_nome=user.full_name, assumed_at=now(), last_message_at=now())
    ticket = buscar_ticket(protocolo)
    await context.bot.send_message(chat_id=ticket['user_id'], text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{user.full_name}*\n🎫 Protocolo: *{protocolo}*", parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
    if ticket.get('message_thread_id'):
        await context.bot.send_message(chat_id=ATENDENTES_CHAT_ID, message_thread_id=ticket['message_thread_id'], text=f'✅ Atendimento assumido por *{user.full_name}*.', parse_mode='Markdown')
    await atualizar_painel(context)

async def finalizar_ticket(protocolo, user, context, chat_id=None, admin=False):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text='Chamado não encontrado.')
        return
    if not admin and ticket.get('atendente_id') and ticket['atendente_id'] != user.id:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text='Você não é o responsável por esse chamado.')
        return
    atualizar_ticket(protocolo, status='finalizado', closed_at=now(), last_message_at=now())
    try:
        await context.bot.send_message(chat_id=ticket['user_id'], text=f'✅ Atendimento {protocolo} finalizado pelo COP.')
    except Exception:
        pass
    if ticket.get('message_thread_id'):
        await context.bot.send_message(chat_id=ATENDENTES_CHAT_ID, message_thread_id=ticket['message_thread_id'], text=f'✅ Atendimento finalizado por {user.full_name}.')
        try:
            await context.bot.close_forum_topic(chat_id=ATENDENTES_CHAT_ID, message_thread_id=ticket['message_thread_id'])
        except Exception:
            pass
    await atualizar_painel(context)

async def devolver_ticket(protocolo, user, context, chat_id=None):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text='Chamado não encontrado.')
        return
    atualizar_ticket(protocolo, status='aguardando', atendente_id=None, atendente_nome=None, assumed_at=None, last_message_at=now())
    try:
        await context.bot.send_message(chat_id=ticket['user_id'], text=f'🔄 Atendimento {protocolo} voltou para a fila do COP.')
    except Exception:
        pass
    if ticket.get('message_thread_id'):
        await context.bot.send_message(chat_id=ATENDENTES_CHAT_ID, message_thread_id=ticket['message_thread_id'], text=f'🔄 Atendimento devolvido para a fila por {user.full_name}.')
    await atualizar_painel(context)

async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await query.answer()
    if data.startswith('categoria:'):
        categoria = data.split(':', 1)[1]
        context.user_data['categoria'] = categoria
        context.user_data['etapa'] = 'contrato'
        await query.message.reply_text('📄 Informe o número do contrato:')
        return
    if data == 'adm_escolher':
        if not eh_admin(user.id):
            await query.answer('⛔ Apenas administradores podem escolher atendimentos.', show_alert=True)
            return
        await listar_aguardando_adm(query, context)
        return
    if data == 'adm_gestao':
        if not eh_admin(user.id):
            await query.answer('⛔ Apenas administradores podem acessar a gestão.', show_alert=True)
            return
        await listar_gestao_adm(query, context)
        return
    if data == 'atualizar_painel':
        await atualizar_painel(context)
        return
    if data.startswith('adm_assumir:'):
        if not eh_admin(user.id):
            await query.answer('⛔ Apenas administradores podem assumir atendimento específico.', show_alert=True)
            return
        await assumir_ticket(data.split(':', 1)[1], user, context, query.message.chat_id)
        return
    if data.startswith('topico_assumir:'):
        await assumir_ticket(data.split(':', 1)[1], user, context, query.message.chat_id)
        return
    if data.startswith('topico_finalizar:'):
        await finalizar_ticket(data.split(':', 1)[1], user, context, query.message.chat_id)
        return
    if data.startswith('adm_encerrar:'):
        if not eh_admin(user.id):
            await query.answer('⛔ Apenas administradores podem encerrar chamados.', show_alert=True)
            return
        await finalizar_ticket(data.split(':', 1)[1], user, context, query.message.chat_id, admin=True)
        return
    if data.startswith('adm_devolver:'):
        if not eh_admin(user.id):
            await query.answer('⛔ Apenas administradores podem devolver chamados.', show_alert=True)
            return
        await devolver_ticket(data.split(':', 1)[1], user, context, query.message.chat_id)
        return

async def tratar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg:
        return

    # Mensagem enviada dentro de um tópico do grupo: vai para o técnico correto
    if update.effective_chat and update.effective_chat.id == ATENDENTES_CHAT_ID and msg.message_thread_id:
        ticket = buscar_ticket_por_thread(msg.message_thread_id)
        if ticket and ticket['status'] in ['aguardando', 'em_atendimento']:
            if msg.text and msg.text.startswith('/'):
                return
            if ticket['status'] == 'aguardando':
                atualizar_ticket(ticket['protocolo'], status='em_atendimento', atendente_id=user.id, atendente_nome=user.full_name, assumed_at=now(), last_message_at=now())
                try:
                    await context.bot.send_message(chat_id=ticket['user_id'], text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{user.full_name}*\n🎫 Protocolo: *{ticket['protocolo']}*", parse_mode='Markdown')
                except Exception:
                    pass
            registrar_msg(ticket['protocolo'], user.id, user.full_name, 'atendente', 'mensagem', msg.text or msg.caption or '')
            await encaminhar_mensagem(msg, context, ticket['user_id'], f"📩 {ticket['protocolo']} - COP {user.full_name}")
            await atualizar_painel(context)
        return

    if context.user_data.get('etapa') == 'contrato':
        categoria = context.user_data.get('categoria', 'Outros')
        contrato = msg.text.strip() if msg.text else ''
        protocolo = criar_ticket_db(user.id, user.full_name, categoria, contrato)
        context.user_data['protocolo'] = protocolo
        context.user_data['etapa'] = 'fotos'
        usuarios_em_chamado[user.id] = protocolo
        teclado = ReplyKeyboardMarkup([['✅ Finalizar fotos']], resize_keyboard=True, one_time_keyboard=False)
        await msg.reply_text(f"🎫 Protocolo: *{protocolo}*\n\n📸 Envie as evidências/fotos necessárias.\n\nQuando terminar, toque no botão abaixo:", parse_mode='Markdown', reply_markup=teclado)
        await atualizar_painel(context)
        return

    if msg.text and msg.text.strip().lower() in ['✅ finalizar fotos', 'finalizar fotos']:
        protocolo = context.user_data.get('protocolo') or usuarios_em_chamado.get(user.id)
        if not protocolo:
            await msg.reply_text('Use /start para iniciar um novo atendimento.', reply_markup=ReplyKeyboardRemove())
            return
        ticket = buscar_ticket(protocolo)
        if not ticket.get('message_thread_id'):
            try:
                await criar_topico_do_chamado(context, protocolo, ticket['user_name'], ticket['categoria'])
                await enviar_cabecalho_topico(context, protocolo)
            except Exception as e:
                logger.exception('Erro ao criar tópico: %s', e)
                await msg.reply_text('⚠️ Não consegui criar o tópico no grupo. Verifique se o bot é admin e pode gerenciar tópicos.')
                return
        registrar_msg(protocolo, user.id, user.full_name, 'tecnico', 'finalizou_fotos', 'Finalizou fotos')
        await msg.reply_text(f'✅ Fotos recebidas.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.', reply_markup=ReplyKeyboardRemove())
        await atualizar_painel(context)
        return

    ticket_tecnico = obter_ticket_ativo_usuario(user.id)
    if ticket_tecnico and ticket_tecnico.get('message_thread_id'):
        registrar_msg(ticket_tecnico['protocolo'], user.id, user.full_name, 'tecnico', 'mensagem', msg.text or msg.caption or '')
        await encaminhar_mensagem(msg, context, ATENDENTES_CHAT_ID, f"📩 {ticket_tecnico['protocolo']} - {user.full_name}", thread_id=ticket_tecnico['message_thread_id'])
        return

    if context.user_data.get('etapa') == 'fotos':
        protocolo = context.user_data.get('protocolo')
        if msg.photo or msg.document or msg.video or msg.location:
            ticket = buscar_ticket(protocolo)
            fotos = (ticket.get('fotos') or 0) + 1
            atualizar_ticket(protocolo, fotos=fotos, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, 'tecnico', 'foto', msg.caption or '')
            teclado = ReplyKeyboardMarkup([['✅ Finalizar fotos']], resize_keyboard=True, one_time_keyboard=False)
            await msg.reply_text(f'✅ Evidência recebida. Total: {fotos}\n\nToque em *✅ Finalizar fotos* quando terminar.', parse_mode='Markdown', reply_markup=teclado)
            return
    await msg.reply_text('Use /start para iniciar um novo atendimento.')

async def encaminhar_mensagem(msg, context, destino, cabecalho, thread_id=None):
    kwargs = {'chat_id': destino}
    if thread_id:
        kwargs['message_thread_id'] = thread_id
    try:
        if msg.text:
            await context.bot.send_message(**kwargs, text=f'{cabecalho}\n\n{msg.text}')
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
        logger.exception('Erro ao encaminhar mensagem: %s', e)
        try:
            await msg.reply_text('Não consegui encaminhar a mensagem.')
        except Exception:
            pass

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('adm', adm))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, tratar_mensagem))
    app.run_polling()

if __name__ == '__main__':
    main()
