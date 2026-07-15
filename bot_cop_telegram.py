import os
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

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

# Grupos individuais por atendente.
#
# IMPORTANTE: a partir de agora, isso NÃO é mais lido em tempo de execução —
# serve só de semente (uma vez só) pra tabela `atendentes` no banco, pra não
# perder ninguém na transição. Atendente novo é cadastrado pelo próprio bot
# (menu "👥 Atendentes"), não editando essas linhas. Pode deixar como está.
COP_GROUPS = {
    8176848972: -1004293448057,  # Eduardo
    8342651270: -1004381937733,  # Rafael
    8649288570: -1003905148770,  # Julia
    8968758334: -1004458526402,  # Barbara
    8687881505: -1004315669101,  # Brian
    7610106736: -1004468702049,  # Luana
}

COP_NAMES = {
    8176848972: "Eduardo",
    8342651270: "Rafael",
    8649288570: "Julia",
    8968758334: "Barbara",
    8687881505: "Brian",
    7610106736: "Luana",
}


ATENDIMENTO_PARADO_MIN = int(os.getenv("ATENDIMENTO_PARADO_MIN", "15"))


RESPOSTAS_RAPIDAS = {
    "aguarde": "⏳ Aguarde um instante, estamos verificando sua solicitação.",
    "ligando": "📞 Estamos entrando em contato com o cliente. Aguarde um momento.",
    "psw_liberado": "✅ PSW liberado. Pode prosseguir com o atendimento.",
    "psw_recusado": "❌ PSW recusado. Verifique as evidências enviadas e encaminhe novamente conforme o padrão.",
    "rede": "🌐 Chamado encaminhado para a equipe de Rede. Aguarde novas orientações.",
    "nap": "📡 Estamos verificando as informações da NAP. Aguarde um momento.",
    "evidencias": "📸 Favor reenviar as evidências conforme o padrão solicitado.",
}


if not BOT_TOKEN:
    raise RuntimeError("Configure a variável TELEGRAM_BOT_TOKEN no Railway.")

if not DATABASE_URL:
    raise RuntimeError("Configure a variável DATABASE_URL no Railway.")

# Pool de conexões com o Postgres: reaproveita conexões já abertas em vez de
# abrir/fechar uma conexão TCP nova a cada consulta. Isso evita travar o loop
# de eventos do bot (assíncrono) a cada leitura/escrita no banco e reduz o
# risco de esgotar o limite de conexões do Postgres sob uso simultâneo.
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "10"))

pool = ConnectionPool(
    DATABASE_URL,
    min_size=DB_POOL_MIN_SIZE,
    max_size=DB_POOL_MAX_SIZE,
    kwargs={"row_factory": dict_row},
    open=False,
)
try:
    pool.open(wait=True, timeout=30)
except Exception as e:
    raise RuntimeError(f"Não foi possível conectar ao Postgres (DATABASE_URL) ao iniciar o pool: {e}")

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
        self.conn = pool.getconn()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            # Devolve a conexão ao pool para reuso, em vez de fechá-la.
            # Conexões quebradas são detectadas e descartadas pelo próprio
            # pool ao serem devolvidas.
            pool.putconn(self.conn)

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
                alertado_espera INTEGER DEFAULT 0,
                grupo_atendente BIGINT,
                alertado_parado INTEGER DEFAULT 0,
                control_message_id BIGINT
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
            # Tabela simples de chave/valor para guardar estadinhos do bot
            # que precisam sobreviver a um restart do processo (ex.: qual é
            # o message_id do painel fixado) — sem isso, a cada redeploy no
            # Railway o bot "esquece" e cria um painel novo do zero.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )
            """)
            # Atendentes (COPs): nome + grupo do Telegram + Telegram user_id.
            # Substitui os dicionários fixos COP_GROUPS/COP_NAMES do código —
            # atendente novo é cadastrado pelo próprio bot, sem editar/redeployar.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS atendentes (
                user_id BIGINT PRIMARY KEY,
                nome TEXT NOT NULL,
                grupo_id BIGINT NOT NULL,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                criado_em TEXT
            )
            """)
        conn.commit()

        add_column_if_missing(conn, "tickets", "message_thread_id", "BIGINT")
        add_column_if_missing(conn, "tickets", "subcategoria", "TEXT")
        add_column_if_missing(conn, "tickets", "latitude", "DOUBLE PRECISION")
        add_column_if_missing(conn, "tickets", "longitude", "DOUBLE PRECISION")
        add_column_if_missing(conn, "tickets", "historico_enviado", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "tickets", "alertado_espera", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "tickets", "grupo_atendente", "BIGINT")
        add_column_if_missing(conn, "tickets", "alertado_parado", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "tickets", "control_message_id", "BIGINT")

        add_column_if_missing(conn, "messages", "file_id", "TEXT")
        add_column_if_missing(conn, "messages", "latitude", "DOUBLE PRECISION")
        add_column_if_missing(conn, "messages", "longitude", "DOUBLE PRECISION")

# O servidor (Railway) roda em UTC, não no horário de Brasília — um
# datetime.now() puro fica 3 horas adiantado. Usamos isso pra pegar o
# horário de Brasília de verdade e então tiramos o timezone (.replace
# (tzinfo=None)) pra manter o mesmo formato de string "ingênuo" (sem
# indicação de fuso) que já é usado em todo o banco — só que agora com o
# valor certo. TODO horário absoluto do bot (não diferenças de tempo) deve
# vir daqui, nunca de datetime.now() puro.
TZ_BRASIL = ZoneInfo("America/Sao_Paulo")


def agora():
    return datetime.now(TZ_BRASIL).replace(tzinfo=None)


def now():
    return agora().isoformat(timespec="seconds")


def obter_estado(chave, default=None):
    with db() as conn:
        row = conn.execute("SELECT valor FROM bot_state WHERE chave=%s", (chave,)).fetchone()
        return row["valor"] if row else default


def salvar_estado(chave, valor):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (chave, valor) VALUES (%s, %s)
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
            """,
            (chave, str(valor)),
        )


def seed_atendentes_iniciais():
    """
    Roda só UMA VEZ na vida do bot (controlado por uma flag no bot_state):
    copia os atendentes que hoje estão fixos em COP_GROUPS/COP_NAMES para a
    tabela `atendentes`, para não perder ninguém na transição. Depois disso,
    adicionar/remover atendente é feito pelo próprio bot — essa função nunca
    mais sobrescreve nada (mesmo que o código com os dicionários antigos
    continue no repositório).
    """
    if obter_estado("seed_atendentes_feito"):
        return
    with db() as conn:
        for user_id, grupo_id in COP_GROUPS.items():
            nome = COP_NAMES.get(user_id, str(user_id))
            conn.execute(
                """
                INSERT INTO atendentes (user_id, nome, grupo_id, ativo, criado_em)
                VALUES (%s, %s, %s, TRUE, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, nome, grupo_id, now()),
            )
    salvar_estado("seed_atendentes_feito", "1")
    logger.info("Seed inicial de atendentes concluído (%d atendente(s) do código migrado(s) para o banco).", len(COP_GROUPS))


def listar_atendentes(somente_ativos=True):
    with db() as conn:
        if somente_ativos:
            rows = conn.execute(
                "SELECT user_id, nome, grupo_id, ativo FROM atendentes WHERE ativo=TRUE ORDER BY nome"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id, nome, grupo_id, ativo FROM atendentes ORDER BY nome"
            ).fetchall()
        return rows


def grupo_do_atendente(atendente_id):
    with db() as conn:
        row = conn.execute(
            "SELECT grupo_id FROM atendentes WHERE user_id=%s AND ativo=TRUE", (atendente_id,)
        ).fetchone()
        return row["grupo_id"] if row else None


def grupos_atendimento_set():
    with db() as conn:
        rows = conn.execute("SELECT grupo_id FROM atendentes WHERE ativo=TRUE").fetchall()
        return {r["grupo_id"] for r in rows}


def adicionar_atendente(user_id, nome, grupo_id):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO atendentes (user_id, nome, grupo_id, ativo, criado_em)
            VALUES (%s, %s, %s, TRUE, %s)
            ON CONFLICT (user_id) DO UPDATE SET nome=EXCLUDED.nome, grupo_id=EXCLUDED.grupo_id, ativo=TRUE
            """,
            (user_id, nome, grupo_id, now()),
        )


def remover_atendente(user_id):
    """'Remove' de forma reversível (soft delete) — mantém o histórico de
    chamados antigos desse atendente intacto e permite reativar depois."""
    with db() as conn:
        conn.execute("UPDATE atendentes SET ativo=FALSE WHERE user_id=%s", (user_id,))


def minutos(dt_iso):
    try:
        dt = datetime.fromisoformat(dt_iso)
        return int((agora() - dt).total_seconds() // 60)
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


def reivindicar_ticket(protocolo, novo_status, status_esperado):
    """
    Muda o status do ticket de forma atômica, só aplicando a mudança se o
    status atual no banco ainda for o esperado (via UPDATE ... WHERE status=...).

    Evita condições de corrida como:
    - dois atendentes clicando "Atender próximo" ao mesmo tempo e assumindo o
      mesmo chamado;
    - dois cliques de "Finalizar" (ex.: atendente e supervisor) fechando o
      mesmo tópico em duplicidade.

    Retorna True se esta chamada conseguiu aplicar a mudança, False se outro
    processo já tinha alterado o status antes (a "corrida" foi perdida).
    """
    with db() as conn:
        row = conn.execute(
            "UPDATE tickets SET status=%s WHERE protocolo=%s AND status=%s RETURNING id",
            (novo_status, protocolo, status_esperado),
        ).fetchone()
        return row is not None


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
        conn.execute("UPDATE tickets SET last_message_at=%s, alertado_parado=0 WHERE protocolo=%s", (now(), protocolo))


def buscar_ticket(protocolo):
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE protocolo=%s", (protocolo,)).fetchone()
        return dict(row) if row else None


def buscar_ticket_por_thread(thread_id, chat_id=None):
    if not thread_id:
        return None

    if chat_id is not None:
        with db() as conn:
            row = conn.execute("""
                SELECT * FROM tickets
                WHERE message_thread_id=%s
                  AND grupo_atendente=%s
                  AND status IN ('aguardando','em_atendimento')
                ORDER BY id DESC LIMIT 1
            """, (thread_id, chat_id)).fetchone()
            if row:
                return dict(row)
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
    hoje = agora().date()
    with db() as conn:
        aguardando = conn.execute("SELECT * FROM tickets WHERE status='aguardando' ORDER BY id ASC").fetchall()
        atendimento = conn.execute("SELECT * FROM tickets WHERE status='em_atendimento' ORDER BY assumed_at ASC").fetchall()
        finalizados = conn.execute("""
            SELECT COUNT(*) c FROM tickets
            WHERE status='finalizado' AND DATE(closed_at::timestamp)=%s
        """, (hoje,)).fetchone()["c"]
        carga_rows = conn.execute("""
            SELECT atendente_id, atendente_nome, COUNT(*) total
            FROM tickets
            WHERE status='em_atendimento'
            GROUP BY atendente_id, atendente_nome
            ORDER BY total DESC, atendente_nome
        """).fetchall()

    maior_espera = max([minutos(r["created_at"]) for r in aguardando] or [0])
    carga_map = {int(r["atendente_id"]): r["total"] for r in carga_rows if r["atendente_id"]}

    linhas = [
        "📋 *FILA COP - ATENDIMENTOS*",
        "",
        f"🟡 Aguardando: *{len(aguardando)}*",
        f"🟢 Em atendimento: *{len(atendimento)}*",
        f"✅ Finalizados hoje: *{finalizados}*",
        f"⏱️ Maior espera: *{maior_espera} min*",
        "",
        "👥 *CARGA POR COP*",
    ]

    for r_atendente in listar_atendentes():
        cop_id, nome = r_atendente["user_id"], r_atendente["nome"]
        total = carga_map.get(cop_id, 0)
        emoji = "🟢" if total else "⚪"
        linhas.append(f"{emoji} {nome}: *{total}*")

    linhas.extend(["", "🟢 *EM ATENDIMENTO*"])

    if atendimento:
        for r in atendimento[:12]:
            atendente = r["atendente_nome"] or "Sem atendente"
            sub = f" / {r['subcategoria']}" if r["subcategoria"] else ""
            tempo = minutos(r["assumed_at"]) if r["assumed_at"] else 0
            linhas.append(f"🎫 {r['protocolo']} - {r['user_name']} - {r['categoria']}{sub} - {atendente} - {tempo} min")
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

    linhas.extend(["", f"🕘 Atualizado em {agora().strftime('%d/%m/%Y %H:%M:%S')}"])
    return "\n".join(linhas)


def teclado_painel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Atender próximo", callback_data="assumir_proximo")],
        [InlineKeyboardButton("📌 Escolher atendimento", callback_data="adm_escolher")],
        [InlineKeyboardButton("🛠️ Gestão ADM", callback_data="adm_gestao")],
        [InlineKeyboardButton("👥 Atendentes", callback_data="adm_atendentes")],
        [InlineKeyboardButton("🔄 Atualizar painel", callback_data="atualizar_painel")],
    ])


def teclado_atendentes_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Listar atendentes", callback_data="adm_listar_atendentes")],
        [InlineKeyboardButton("➕ Adicionar atendente", callback_data="adm_add_atendente")],
        [InlineKeyboardButton("➖ Remover atendente", callback_data="adm_rm_atendente_menu")],
        [InlineKeyboardButton("⬅️ Voltar ao painel", callback_data="atualizar_painel")],
    ])


async def atualizar_painel(context: ContextTypes.DEFAULT_TYPE, reposicionar=False):
    global painel_message_id
    if not ATENDENTES_CHAT_ID:
        return

    # Se este processo acabou de subir (ex.: depois de um redeploy no
    # Railway), painel_message_id em memória está vazio. Recupera o ID
    # salvo no banco da última vez, para continuar editando a MESMA
    # mensagem (e ela seguir fixada) em vez de criar uma nova a cada
    # reinício do bot.
    if painel_message_id is None:
        salvo = obter_estado("painel_message_id")
        if salvo:
            try:
                painel_message_id = int(salvo)
            except ValueError:
                painel_message_id = None

    if reposicionar and painel_message_id:
        # Traz o painel de volta para o fim da conversa: apaga a mensagem
        # antiga e manda uma nova (já fixada), em vez de só editar no
        # lugar. Usado especificamente quando um alerta de espera ou de
        # atendimento parado é disparado — é justamente quando os COPs
        # mais precisam enxergar o painel sem precisar procurar, e o
        # painel reaparece logo abaixo do próprio alerta.
        try:
            await context.bot.delete_message(chat_id=ATENDENTES_CHAT_ID, message_id=painel_message_id)
        except Exception as e:
            logger.debug("Não consegui apagar o painel antigo ao reposicionar: %s", e)
        painel_message_id = None
        await _criar_e_fixar_painel(context)
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
            await _criar_e_fixar_painel(context)
    except Exception as e:
        logger.warning("Erro ao atualizar painel: %s", e)
        await _criar_e_fixar_painel(context)


async def _criar_e_fixar_painel(context: ContextTypes.DEFAULT_TYPE):
    """Manda uma mensagem nova de painel, salva o ID (sobrevive a restart)
    e fixa (pin) no chat — assim os COPs sempre encontram o painel numa
    barrinha fixa no topo, mesmo com alertas e outras mensagens chegando
    depois dele."""
    global painel_message_id

    msg = await context.bot.send_message(
        chat_id=ATENDENTES_CHAT_ID,
        text=painel_texto(),
        parse_mode="Markdown",
        reply_markup=teclado_painel(),
    )
    painel_message_id = msg.message_id
    salvar_estado("painel_message_id", msg.message_id)

    try:
        await context.bot.pin_chat_message(
            chat_id=ATENDENTES_CHAT_ID,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning(
            "Não consegui fixar o painel (verifique se o bot é admin com permissão de fixar mensagens): %s", e
        )


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


def menu_nap():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Localização da NAP", callback_data="nap:Localização da NAP")],
        [InlineKeyboardButton("📡 NAP mais próxima", callback_data="nap:NAP mais próxima")],
        [InlineKeyboardButton("🆔 ID da NAP", callback_data="nap:ID da NAP")],
    ])


def menu_atenuacao():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Numeração da NAP", callback_data="atenuacao:Numeração da NAP")],
        [InlineKeyboardButton("📍 Localização da NAP", callback_data="atenuacao:Localização da NAP")],
    ])


def teclado_localizacao():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Enviar localização", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def destino_ticket(ticket):
    return ticket.get("grupo_atendente") or ATENDENTES_CHAT_ID



async def criar_topico_do_chamado(context, protocolo, user_name, categoria, grupo_destino=None):
    ticket = buscar_ticket(protocolo)
    if ticket and ticket.get("message_thread_id") and ticket.get("grupo_atendente"):
        return ticket["message_thread_id"]

    grupo = grupo_destino or (ticket.get("grupo_atendente") if ticket else None) or ATENDENTES_CHAT_ID
    # O emoji no início do nome é o que mais salta aos olhos ao rolar a
    # lista de tópicos no Telegram (mais do que um sufixo de texto ou o
    # ícone de cadeado, que são pequenos). 🔵 = em andamento; ao finalizar,
    # troca para ✅ (ver finalizar_ticket) — o contraste fica bem visível.
    titulo = f"🔵 {protocolo} - {user_name[:18]} - {categoria}"
    topico = await context.bot.create_forum_topic(
        chat_id=grupo,
        name=titulo[:128],
    )
    thread_id = topico.message_thread_id
    atualizar_ticket(protocolo, message_thread_id=thread_id, grupo_atendente=grupo)
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
        chat_id=destino_ticket(ticket),
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
        chat_id=destino_ticket(ticket),
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
                await context.bot.send_message(chat_id=destino_ticket(ticket), message_thread_id=thread_id, text=legenda)
            elif m["message_type"] == "photo":
                await context.bot.send_photo(chat_id=destino_ticket(ticket), message_thread_id=thread_id, photo=m["file_id"], caption=legenda)
            elif m["message_type"] == "document":
                await context.bot.send_document(chat_id=destino_ticket(ticket), message_thread_id=thread_id, document=m["file_id"], caption=legenda)
            elif m["message_type"] == "video":
                await context.bot.send_video(chat_id=destino_ticket(ticket), message_thread_id=thread_id, video=m["file_id"], caption=legenda)
            elif m["message_type"] == "voice":
                await context.bot.send_voice(chat_id=destino_ticket(ticket), message_thread_id=thread_id, voice=m["file_id"], caption=legenda)
            elif m["message_type"] == "location":
                await context.bot.send_location(
                    chat_id=destino_ticket(ticket),
                    message_thread_id=thread_id,
                    latitude=m["latitude"],
                    longitude=m["longitude"],
                )
                await context.bot.send_message(chat_id=destino_ticket(ticket), message_thread_id=thread_id, text=legenda)
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
    user = update.effective_user

    # Antes de limpar o estado e abrir um atendimento novo, checa se esse
    # técnico já tem um chamado em aberto no banco. Sem essa checagem, um
    # /start no meio de um atendimento (por engano, ou clicando num botão
    # antigo) criava um SEGUNDO chamado duplicado, já que context.user_data
    # é só memória local — limpar ela não fecha o chamado que já existe.
    ticket_ativo = obter_ticket_ativo_usuario(user.id)
    if ticket_ativo:
        status_texto = "aguardando na fila" if ticket_ativo["status"] == "aguardando" else "em atendimento"
        await update.message.reply_text(
            f"⚠️ Você já tem um atendimento em aberto:\n\n"
            f"🎫 Protocolo: *{ticket_ativo['protocolo']}*\n"
            f"📌 Status: {status_texto}\n\n"
            "Continue por aqui mesmo — não é preciso abrir um atendimento novo.",
            parse_mode="Markdown",
        )
        # Restaura o protocolo no user_data, caso tenha se perdido (ex.: o
        # bot reiniciou entre uma mensagem e outra).
        context.user_data["protocolo"] = ticket_ativo["protocolo"]
        return

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


async def vincular_grupo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Último passo do cadastro de atendente: rodar /vincular DENTRO do grupo
    dedicado do novo atendente. Como context.user_data é por usuário (não
    por chat), o admin pode começar o cadastro no privado com o bot e vir
    até aqui, no grupo novo, sem perder o que já foi preenchido.
    """
    user = update.effective_user
    chat = update.effective_chat

    if not eh_admin(user.id):
        return  # comando administrativo: ignora silenciosamente pra quem não é admin

    if context.user_data.get("admin_etapa") != "aguardando_vinculo_grupo":
        await update.message.reply_text(
            "Não tem nenhum cadastro de atendente em andamento. Comece pelo botão "
            "\"👥 Atendentes\" → \"➕ Adicionar atendente\" no painel."
        )
        return

    nome = context.user_data.get("novo_atendente_nome")
    novo_user_id = context.user_data.get("novo_atendente_user_id")
    if not nome or not novo_user_id:
        await update.message.reply_text(
            "Faltou uma etapa anterior. Comece de novo pelo botão \"➕ Adicionar atendente\"."
        )
        context.user_data.pop("admin_etapa", None)
        return

    adicionar_atendente(novo_user_id, nome, chat.id)

    context.user_data.pop("admin_etapa", None)
    context.user_data.pop("novo_atendente_nome", None)
    context.user_data.pop("novo_atendente_user_id", None)

    await update.message.reply_text(
        f"✅ Atendente *{nome}* cadastrado e vinculado a este grupo!\nJá pode começar a receber atendimentos.",
        parse_mode="Markdown",
    )


async def encerrar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Deixa o PRÓPRIO TÉCNICO encerrar o chamado dele (ex.: resolveu sozinho,
    abriu por engano, não precisa mais de ajuda), sem depender de um
    atendente pra fechar. Pede confirmação antes, pra evitar fechar sem querer.
    """
    user = update.effective_user
    ticket = obter_ticket_ativo_usuario(user.id)
    if not ticket:
        await update.message.reply_text("Você não tem nenhum atendimento em aberto no momento.")
        return

    await update.message.reply_text(
        f"Tem certeza que quer encerrar o chamado *{ticket['protocolo']}*?\n\n"
        "Se precisar de ajuda de novo depois, é só usar /start e abrir um atendimento novo.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sim, encerrar", callback_data=f"tecnico_encerrar:{ticket['protocolo']}")],
            [InlineKeyboardButton("❌ Não, continuar aberto", callback_data="tecnico_encerrar_cancelar")],
        ]),
    )


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


async def mostrar_menu_atendentes(query, context):
    await query.message.reply_text(
        "👥 *Gerenciar atendentes*\n\nEscolha uma opção:",
        parse_mode="Markdown",
        reply_markup=teclado_atendentes_menu(),
    )


async def listar_atendentes_texto(query, context):
    ativos = listar_atendentes(somente_ativos=True)
    if not ativos:
        await query.message.reply_text("Nenhum atendente cadastrado ainda.")
        return
    linhas = ["👥 *Atendentes cadastrados:*\n"]
    for a in ativos:
        linhas.append(f"🟢 {a['nome']} — grupo `{a['grupo_id']}` — id `{a['user_id']}`")
    await query.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def iniciar_adicionar_atendente(query, context):
    context.user_data["admin_etapa"] = "aguardando_nome_atendente"
    context.user_data.pop("novo_atendente_nome", None)
    context.user_data.pop("novo_atendente_user_id", None)
    await query.message.reply_text(
        "➕ *Adicionar atendente*\n\n"
        "Passo 1 de 3 — qual é o nome desse atendente? (envie por texto aqui mesmo)",
        parse_mode="Markdown",
    )


async def iniciar_remover_atendente(query, context):
    ativos = listar_atendentes(somente_ativos=True)
    if not ativos:
        await query.message.reply_text("Nenhum atendente cadastrado pra remover.")
        return
    botoes = [
        [InlineKeyboardButton(f"➖ {a['nome']}", callback_data=f"adm_rm_atendente:{a['user_id']}")]
        for a in ativos
    ]
    botoes.append([InlineKeyboardButton("⬅️ Cancelar", callback_data="adm_atendentes")])
    await query.message.reply_text(
        "➖ *Remover atendente*\n\nQual atendente você quer remover?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(botoes),
    )


async def confirmar_remover_atendente(query, context, user_id):
    with db() as conn:
        row = conn.execute("SELECT nome FROM atendentes WHERE user_id=%s", (user_id,)).fetchone()
    nome = row["nome"] if row else str(user_id)
    await query.message.reply_text(
        f"Tem certeza que quer remover *{nome}*? Os chamados antigos dele continuam no histórico normalmente.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sim, remover", callback_data=f"adm_rm_confirma:{user_id}")],
            [InlineKeyboardButton("⬅️ Cancelar", callback_data="adm_atendentes")],
        ]),
    )


async def executar_remover_atendente(query, context, user_id):
    with db() as conn:
        row = conn.execute("SELECT nome FROM atendentes WHERE user_id=%s", (user_id,)).fetchone()
    nome = row["nome"] if row else str(user_id)
    remover_atendente(user_id)
    await query.message.reply_text(f"✅ {nome} removido(a). Pode reativar depois adicionando de novo, se precisar.")



def teclado_controles_principal(protocolo):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Finalizar {protocolo}",
            callback_data=f"topico_finalizar:{protocolo}"
        )],
        [InlineKeyboardButton(
            "📋 Respostas rápidas",
            callback_data=f"respostas_menu:{protocolo}"
        )],
        [InlineKeyboardButton(
            "🔄 Devolver para fila",
            callback_data=f"adm_devolver:{protocolo}"
        )],
    ])


def teclado_respostas_rapidas(protocolo):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📍 Aguarde um instante", callback_data=f"resp:{protocolo}:aguarde"),
            InlineKeyboardButton("📞 Ligando para o cliente", callback_data=f"resp:{protocolo}:ligando"),
        ],
        [
            InlineKeyboardButton("🟢 PSW liberado", callback_data=f"resp:{protocolo}:psw_liberado"),
            InlineKeyboardButton("🔴 PSW recusado", callback_data=f"resp:{protocolo}:psw_recusado"),
        ],
        [
            InlineKeyboardButton("🌐 Encaminhado para Rede", callback_data=f"resp:{protocolo}:rede"),
            InlineKeyboardButton("📡 Verificando NAP", callback_data=f"resp:{protocolo}:nap"),
        ],
        [InlineKeyboardButton("📸 Reenviar evidências", callback_data=f"resp:{protocolo}:evidencias")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data=f"respostas_voltar:{protocolo}")],
    ])


async def reposicionar_menu_respostas(context, protocolo):
    ticket = buscar_ticket(protocolo)
    if not ticket or ticket.get("status") != "em_atendimento":
        return

    grupo = destino_ticket(ticket)
    thread_id = ticket.get("message_thread_id")
    if not grupo or not thread_id:
        return

    if ticket.get("control_message_id"):
        try:
            await context.bot.delete_message(
                chat_id=grupo,
                message_id=ticket["control_message_id"],
            )
        except Exception:
            pass

    try:
        menu = await context.bot.send_message(
            chat_id=grupo,
            message_thread_id=thread_id,
            text=f"📋 *RESPOSTAS RÁPIDAS — {protocolo}*",
            parse_mode="Markdown",
            reply_markup=teclado_respostas_rapidas(protocolo),
            disable_notification=True,
        )
        atualizar_ticket(protocolo, control_message_id=menu.message_id)
    except Exception as e:
        logger.warning("Erro ao reposicionar menu de respostas rápidas: %s", e)


async def enviar_resposta_rapida(context, protocolo, chave, user):
    ticket = buscar_ticket(protocolo)
    if not ticket or ticket.get("status") != "em_atendimento":
        return False

    if ticket.get("atendente_id") and ticket["atendente_id"] != user.id and not eh_admin(user.id):
        return False

    texto = RESPOSTAS_RAPIDAS.get(chave)
    if not texto:
        return False

    try:
        await context.bot.send_message(chat_id=ticket["user_id"], text=texto)
    except Exception as e:
        logger.warning("Erro ao enviar resposta rápida ao técnico: %s", e)
        return False

    registrar_msg(
        protocolo,
        user.id,
        user.full_name,
        "atendente",
        "resposta_rapida",
        texto,
    )

    try:
        await context.bot.send_message(
            chat_id=destino_ticket(ticket),
            message_thread_id=ticket["message_thread_id"],
            text=f"📤 *Resposta rápida enviada por {user.full_name}*\n\n{texto}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await reposicionar_menu_respostas(context, protocolo)
    return True


async def atualizar_controles_topico(context, protocolo):
    """
    Mantém uma única mensagem de controles no final do tópico.
    A mensagem anterior é apagada e uma nova é enviada, ficando sempre por último.
    """
    ticket = buscar_ticket(protocolo)
    if not ticket:
        return

    if ticket.get("status") != "em_atendimento":
        return

    grupo = destino_ticket(ticket)
    thread_id = ticket.get("message_thread_id")
    if not grupo or not thread_id:
        return

    mensagem_anterior = ticket.get("control_message_id")
    if mensagem_anterior:
        try:
            await context.bot.delete_message(
                chat_id=grupo,
                message_id=mensagem_anterior,
            )
        except Exception:
            pass

    teclado = teclado_controles_principal(protocolo)

    try:
        msg_controle = await context.bot.send_message(
            chat_id=grupo,
            message_thread_id=thread_id,
            text=f"⚙️ *Controles do atendimento {protocolo}*",
            parse_mode="Markdown",
            reply_markup=teclado,
            disable_notification=True,
        )
        atualizar_ticket(
            protocolo,
            control_message_id=msg_controle.message_id,
        )
    except Exception as e:
        logger.warning(
            "Não consegui atualizar os controles do atendimento %s: %s",
            protocolo,
            e,
        )


async def remover_controles_topico(context, ticket):
    mensagem_controle = ticket.get("control_message_id")
    grupo = destino_ticket(ticket)

    if mensagem_controle and grupo:
        try:
            await context.bot.delete_message(
                chat_id=grupo,
                message_id=mensagem_controle,
            )
        except Exception:
            pass

    try:
        atualizar_ticket(ticket["protocolo"], control_message_id=None)
    except Exception:
        pass


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

    grupo_destino = grupo_do_atendente(user.id)
    if not grupo_destino:
        if origem_chat_id:
            await context.bot.send_message(
                chat_id=origem_chat_id,
                text="⚠️ Você ainda não possui um grupo individual configurado para receber atendimentos."
            )
        return

    # Reivindica o chamado de forma atômica ANTES de criar o tópico.
    # Isso evita que dois atendentes cliquem "Atender próximo"/"Assumir" ao
    # mesmo tempo e ambos passem pelas checagens acima antes que o status
    # seja de fato gravado no banco (condição de corrida clássica de
    # "check-then-act").
    conseguiu = reivindicar_ticket(protocolo, novo_status="em_atendimento", status_esperado="aguardando")
    if not conseguiu:
        ticket_atual = buscar_ticket(protocolo) or {}
        atendente = ticket_atual.get("atendente_nome") or "outro atendente"
        logger.info("Corrida ao assumir %s: %s perdeu para %s.", protocolo, user.full_name, atendente)
        if origem_chat_id:
            await context.bot.send_message(
                chat_id=origem_chat_id,
                text=f"⚠️ {protocolo} acabou de ser assumido por {atendente} (clique simultâneo)."
            )
        return

    if not ticket.get("message_thread_id") or ticket.get("grupo_atendente") != grupo_destino:
        try:
            await criar_topico_do_chamado(context, protocolo, ticket["user_name"], ticket["categoria"], grupo_destino=grupo_destino)
            ticket = buscar_ticket(protocolo)
            await enviar_cabecalho_topico(context, protocolo)
        except Exception as e:
            logger.exception("Erro ao criar tópico ao assumir: %s", e)
            # Devolve o chamado para a fila: sem tópico criado, não faz
            # sentido deixá-lo travado em "em_atendimento" sem atendente
            # de fato atribuído.
            reivindicar_ticket(protocolo, novo_status="aguardando", status_esperado="em_atendimento")
            if origem_chat_id:
                await context.bot.send_message(
                    chat_id=origem_chat_id,
                    text="⚠️ Não consegui criar o tópico no grupo individual. O chamado voltou para a fila. Verifique se o bot é admin e pode gerenciar tópicos."
                )
            return

    # O status já foi reivindicado atomicamente acima; aqui só completamos
    # os demais campos do atendimento.
    atualizar_ticket(
        protocolo,
        atendente_id=user.id,
        atendente_nome=user.full_name,
        assumed_at=now(),
        last_message_at=now(),
        grupo_atendente=grupo_destino,
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

    # Sem try/except aqui antes: uma falha nesse envio (ex.: tópico
    # temporariamente indisponível) pulava a atualização dos controles e do
    # painel logo abaixo, mesmo com o chamado já assumido no banco.
    try:
        await context.bot.send_message(
            chat_id=grupo_destino,
            message_thread_id=ticket["message_thread_id"],
            text=f"✅ Atendimento assumido por *{user.full_name}*.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Não consegui enviar confirmação de assumir no tópico de %s: %s", protocolo, e)

    # Deixa os controles sempre como a última mensagem do tópico.
    await atualizar_controles_topico(context, protocolo)

    await atualizar_painel(context)

async def finalizar_ticket(protocolo, user, context, chat_id=None, admin=False):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Chamado não encontrado.")
        return

    if ticket["status"] == "finalizado":
        # Evita poluir o tópico fechado quando alguém clica novamente no botão antigo.
        # O callback já foi respondido pelo Telegram; aqui apenas ignora silenciosamente.
        return

    if not admin and ticket.get("atendente_id") and ticket["atendente_id"] != user.id:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Você não é o responsável por esse chamado.")
        return

    # Reivindica o fechamento de forma atômica: evita que dois cliques quase
    # simultâneos (ex.: atendente e supervisor apertando "Finalizar" ao mesmo
    # tempo) disparem duas vezes as mensagens de encerramento e o fechamento
    # do tópico.
    conseguiu = reivindicar_ticket(protocolo, novo_status="finalizado", status_esperado=ticket["status"])
    if not conseguiu:
        logger.info("Corrida ao finalizar %s: outra ação já havia mudado o status.", protocolo)
        return

    atualizar_ticket(protocolo, closed_at=now(), last_message_at=now())

    # Remove a mensagem de controles antes de fechar o tópico.
    await remover_controles_topico(context, ticket)

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
        # Benigno na maioria das vezes (ex.: técnico bloqueou o bot).
        logger.debug("Não consegui avisar o técnico %s sobre finalização de %s.", ticket["user_id"], protocolo)

    if ticket.get("message_thread_id"):
        try:
            await context.bot.edit_forum_topic(
                chat_id=destino_ticket(ticket),
                message_thread_id=ticket["message_thread_id"],
                name=f"✅ {protocolo} - FINALIZADO",
            )
        except Exception as e:
            logger.warning("Não consegui renomear o tópico de %s: %s", protocolo, e)

        # Fecha primeiro para a mensagem automática do Telegram não ficar por último.
        try:
            await context.bot.close_forum_topic(
                chat_id=destino_ticket(ticket),
                message_thread_id=ticket["message_thread_id"],
            )
        except Exception as e:
            logger.warning("Não consegui fechar o tópico de %s: %s", protocolo, e)

        final_text = (
            "✅ *Atendimento Finalizado*\n"
            "━━━━━━━━━━━━━━\n"
            f"🎫 Protocolo: *{protocolo}*\n"
            f"👨‍💻 Atendente: *{user.full_name}*\n"
            f"🕒 Horário: *{agora().strftime('%d/%m/%Y %H:%M:%S')}*\n"
            "━━━━━━━━━━━━━━"
        )

        try:
            await context.bot.send_message(
                chat_id=destino_ticket(ticket),
                message_thread_id=ticket["message_thread_id"],
                text=final_text,
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            try:
                await context.bot.reopen_forum_topic(
                    chat_id=destino_ticket(ticket),
                    message_thread_id=ticket["message_thread_id"],
                )
                await context.bot.send_message(
                    chat_id=destino_ticket(ticket),
                    message_thread_id=ticket["message_thread_id"],
                    text=final_text,
                    parse_mode="Markdown",
                    reply_markup=None,
                )
                await context.bot.close_forum_topic(
                    chat_id=destino_ticket(ticket),
                    message_thread_id=ticket["message_thread_id"],
                )
            except Exception as e:
                logger.warning("Falha ao enviar mensagem final de encerramento no tópico de %s: %s", protocolo, e)

    await atualizar_painel(context)


async def devolver_ticket(protocolo, user, context, chat_id=None):
    ticket = buscar_ticket(protocolo)
    if not ticket:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Chamado não encontrado.")
        return

    # Remove os controles do tópico enquanto o chamado volta para a fila.
    await remover_controles_topico(context, ticket)

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
        logger.debug("Não consegui avisar o técnico %s sobre devolução de %s.", ticket["user_id"], protocolo)

    if ticket.get("message_thread_id"):
        # Antes sem try/except: se o tópico já estivesse fechado/apagado,
        # essa chamada lançava uma exceção não tratada que interrompia a
        # função aqui, deixando o painel (atualizar_painel abaixo) sem
        # atualizar.
        try:
            await context.bot.send_message(
                chat_id=destino_ticket(ticket),
                message_thread_id=ticket["message_thread_id"],
                text=f"🔄 Atendimento devolvido para a fila por {user.full_name}.",
            )
        except Exception as e:
            logger.warning("Não consegui avisar o tópico de %s sobre devolução: %s", protocolo, e)

    await atualizar_painel(context)


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    # Ações restritas a administradores, checadas ANTES do query.answer()
    # genérico logo abaixo. O Telegram só aceita responder a um clique de
    # botão UMA vez: antes, o código respondia aqui (vazio) e de novo mais
    # abaixo com o aviso "⛔ sem permissão" — a segunda chamada sempre
    # falhava, então quem não era admin nunca via o aviso aparecer.
    admin_gates = [
        (data == "adm_escolher", "⛔ Apenas administradores podem escolher atendimentos."),
        (data == "adm_gestao", "⛔ Apenas administradores podem acessar a gestão."),
        (data.startswith("adm_assumir:"), "⛔ Apenas administradores podem assumir atendimento específico."),
        (data.startswith("adm_encerrar:"), "⛔ Apenas administradores podem encerrar chamados."),
        (data.startswith("adm_devolver:"), "⛔ Apenas administradores podem devolver chamados."),
        (data == "adm_atendentes", "⛔ Apenas administradores podem gerenciar atendentes."),
        (data == "adm_listar_atendentes", "⛔ Apenas administradores podem gerenciar atendentes."),
        (data == "adm_add_atendente", "⛔ Apenas administradores podem gerenciar atendentes."),
        (data == "adm_rm_atendente_menu", "⛔ Apenas administradores podem gerenciar atendentes."),
        (data.startswith("adm_rm_atendente:"), "⛔ Apenas administradores podem gerenciar atendentes."),
        (data.startswith("adm_rm_confirma:"), "⛔ Apenas administradores podem gerenciar atendentes."),
    ]
    for e_esta_acao, aviso in admin_gates:
        if e_esta_acao and not eh_admin(user.id):
            await query.answer(aviso, show_alert=True)
            return

    await query.answer()

    if data.startswith("respostas_menu:"):
        protocolo = data.split(":", 1)[1]
        ticket = buscar_ticket(protocolo)
        if ticket and ticket.get("status") == "em_atendimento":
            try:
                await query.edit_message_text(
                    text=f"📋 *RESPOSTAS RÁPIDAS — {protocolo}*",
                    parse_mode="Markdown",
                    reply_markup=teclado_respostas_rapidas(protocolo),
                )
            except Exception:
                pass
        return

    if data.startswith("respostas_voltar:"):
        protocolo = data.split(":", 1)[1]
        ticket = buscar_ticket(protocolo)
        if ticket and ticket.get("status") == "em_atendimento":
            try:
                await query.edit_message_text(
                    text=f"⚙️ *Controles do atendimento {protocolo}*",
                    parse_mode="Markdown",
                    reply_markup=teclado_controles_principal(protocolo),
                )
            except Exception:
                pass
        return

    if data.startswith("resp:"):
        partes = data.split(":", 2)
        if len(partes) == 3:
            _, protocolo, chave = partes
            await enviar_resposta_rapida(context, protocolo, chave, user)
        return

    if data.startswith("categoria:"):
        # Mesma checagem do /start: se o botão que a pessoa tocou é de uma
        # mensagem antiga (de um atendimento que já foi aberto), não deixa
        # começar um chamado novo duplicado. Usa reply_text (não um segundo
        # query.answer — o Telegram só aceita responder um clique de botão
        # uma vez, e a linha 1550 já responde isso pra todo callback).
        ticket_ativo = obter_ticket_ativo_usuario(user.id)
        if ticket_ativo:
            await query.message.reply_text(
                f"⚠️ Você já tem o chamado *{ticket_ativo['protocolo']}* em aberto. Continue por aqui mesmo.",
                parse_mode="Markdown",
            )
            return

        categoria = data.split(":", 1)[1]

        if categoria == "Ativo":
            context.user_data["categoria"] = "Ativo"
            await query.message.reply_text("📞 Escolha o motivo do Ativo:", reply_markup=menu_ativo())
            return

        if categoria == "NAP":
            context.user_data["categoria"] = "NAP"
            await query.message.reply_text("📍 Escolha uma opção de NAP:", reply_markup=menu_nap())
            return

        if categoria == "Atenuacao":
            context.user_data["categoria"] = "Atenuacao"
            await query.message.reply_text(
                "📶 Para atenuação, escolha como deseja informar a NAP:",
                reply_markup=menu_atenuacao()
            )
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

    if data.startswith("nap:"):
        subcategoria = data.split(":", 1)[1]
        context.user_data["categoria"] = "NAP"
        context.user_data["subcategoria"] = subcategoria
        context.user_data["etapa"] = "contrato"
        await query.message.reply_text(
            f"📍 Opção selecionada: *{subcategoria}*\n\n📄 Informe o número do contrato:",
            parse_mode="Markdown",
        )
        return

    if data.startswith("atenuacao:"):
        subcategoria = data.split(":", 1)[1]
        context.user_data["categoria"] = "Atenuacao"
        context.user_data["subcategoria"] = subcategoria
        context.user_data["etapa"] = "contrato"
        await query.message.reply_text(
            f"📶 Opção selecionada: *{subcategoria}*\n\n📄 Informe o número do contrato:",
            parse_mode="Markdown",
        )
        return

    if data == "assumir_proximo":
        await assumir_proximo(query, context)
        return

    if data == "adm_escolher":
        await listar_aguardando_adm(query, context)
        return

    if data == "adm_gestao":
        await listar_gestao_adm(query, context)
        return

    if data == "adm_atendentes":
        await mostrar_menu_atendentes(query, context)
        return

    if data == "adm_listar_atendentes":
        await listar_atendentes_texto(query, context)
        return

    if data == "adm_add_atendente":
        await iniciar_adicionar_atendente(query, context)
        return

    if data == "adm_rm_atendente_menu":
        await iniciar_remover_atendente(query, context)
        return

    if data.startswith("adm_rm_atendente:"):
        user_id_remover = int(data.split(":", 1)[1])
        await confirmar_remover_atendente(query, context, user_id_remover)
        return

    if data.startswith("adm_rm_confirma:"):
        user_id_remover = int(data.split(":", 1)[1])
        await executar_remover_atendente(query, context, user_id_remover)
        return

    if data == "atualizar_painel":
        await atualizar_painel(context)
        return

    if data.startswith("adm_assumir:"):
        protocolo = data.split(":", 1)[1]
        await assumir_ticket(protocolo, user, context, query.message.chat_id)
        return

    if data.startswith("topico_assumir:"):
        protocolo = data.split(":", 1)[1]
        await assumir_ticket(protocolo, user, context, query.message.chat_id)
        return

    if data.startswith("topico_finalizar:"):
        protocolo = data.split(":", 1)[1]
        # Admin pode finalizar qualquer chamado, mesmo um que outro
        # atendente assumiu — sem isso, o botão "Finalizar" dentro do
        # tópico tratava admin igual atendente comum e recusava.
        await finalizar_ticket(protocolo, user, context, query.message.chat_id, admin=eh_admin(user.id))
        return

    if data.startswith("adm_encerrar:"):
        protocolo = data.split(":", 1)[1]
        await finalizar_ticket(protocolo, user, context, query.message.chat_id, admin=True)
        return

    if data.startswith("adm_devolver:"):
        protocolo = data.split(":", 1)[1]
        await devolver_ticket(protocolo, user, context, query.message.chat_id)
        return

    if data.startswith("tecnico_encerrar:"):
        protocolo = data.split(":", 1)[1]
        ticket = buscar_ticket(protocolo)
        if not ticket or ticket["user_id"] != user.id:
            await query.message.reply_text("Não encontrei esse chamado, ou ele não é seu.")
            return
        if ticket["status"] == "finalizado":
            await query.message.reply_text("Esse chamado já estava finalizado.")
            return

        # Mesmo padrão atômico usado no resto do bot: evita corrida se um
        # atendente estiver finalizando esse mesmo chamado ao mesmo tempo.
        conseguiu = reivindicar_ticket(protocolo, novo_status="finalizado", status_esperado=ticket["status"])
        if not conseguiu:
            await query.message.reply_text("Esse chamado acabou de ser alterado (talvez o atendente já tenha mexido nele).")
            return

        atualizar_ticket(protocolo, closed_at=now(), last_message_at=now())
        registrar_msg(protocolo, user.id, user.full_name, "sistema", "mensagem", "Chamado encerrado pelo próprio técnico.")
        usuarios_em_chamado.pop(user.id, None)

        await query.message.reply_text(
            f"✅ Chamado {protocolo} encerrado. Se precisar, é só abrir um novo atendimento com /start."
        )

        # Se já tinha atendente/tópico, avisa e fecha — mesmo se ainda
        # estava "aguardando" (sem tópico), não quebra nada aqui.
        if ticket.get("message_thread_id") and ticket.get("grupo_atendente"):
            try:
                await context.bot.send_message(
                    chat_id=ticket["grupo_atendente"],
                    message_thread_id=ticket["message_thread_id"],
                    text=f"ℹ️ O técnico encerrou o chamado {protocolo} por conta própria (não precisava mais de ajuda).",
                )
                await context.bot.edit_forum_topic(
                    chat_id=ticket["grupo_atendente"],
                    message_thread_id=ticket["message_thread_id"],
                    name=f"✅ {protocolo} - FINALIZADO",
                )
                await context.bot.close_forum_topic(
                    chat_id=ticket["grupo_atendente"],
                    message_thread_id=ticket["message_thread_id"],
                )
            except Exception as e:
                logger.warning("Erro ao avisar/fechar tópico após técnico encerrar %s: %s", protocolo, e)

        await atualizar_painel(context)
        return

    if data == "tecnico_encerrar_cancelar":
        await query.message.reply_text("Combinado, o chamado continua aberto.")
        return


async def tratar_etapa_admin_atendente(update, context):
    """
    Cuida dos passos do fluxo "Adicionar atendente" (nome -> encaminhar
    mensagem do atendente -> /vincular dentro do grupo novo). Retorna True
    se tratou a mensagem aqui — nesse caso, tratar_mensagem NÃO deve
    continuar processando a mesma mensagem como se fosse parte de um
    chamado normal.
    """
    etapa = context.user_data.get("admin_etapa")
    if not etapa:
        return False

    msg = update.message
    user = update.effective_user

    if not eh_admin(user.id):
        # Não deveria conseguir chegar aqui sem ser admin, mas por
        # segurança limpa o estado e deixa o fluxo normal seguir.
        context.user_data.pop("admin_etapa", None)
        return False

    if etapa == "aguardando_nome_atendente":
        nome = (msg.text or "").strip()
        if not nome:
            await msg.reply_text("Manda o nome em texto, por favor.")
            return True
        context.user_data["novo_atendente_nome"] = nome
        context.user_data["admin_etapa"] = "aguardando_forward_atendente"
        await msg.reply_text(
            f"Combinado, *{nome}*.\n\n"
            "Passo 2 de 3 — peça pra essa pessoa te mandar qualquer mensagem no privado, "
            "e ENCAMINHE (forward) essa mensagem pra mim aqui.",
            parse_mode="Markdown",
        )
        return True

    if etapa == "aguardando_forward_atendente":
        origem = msg.forward_origin
        if not origem:
            await msg.reply_text(
                "Isso não parece uma mensagem encaminhada. Peça pro atendente mandar "
                "qualquer coisa pra você e encaminhe (forward) ela pra mim aqui."
            )
            return True
        if origem.type != "user":
            await msg.reply_text(
                "Não consegui identificar quem mandou essa mensagem originalmente "
                "(a pessoa deve estar com a privacidade de encaminhamento ativada no Telegram). "
                "Peça pra ela mandar /start diretamente pra mim numa conversa privada e tente "
                "encaminhar de novo, ou peça pra ela desativar essa configuração de privacidade."
            )
            return True

        novo_id = origem.sender_user.id
        nome = context.user_data.get("novo_atendente_nome", "atendente")
        context.user_data["novo_atendente_user_id"] = novo_id
        context.user_data["admin_etapa"] = "aguardando_vinculo_grupo"
        await msg.reply_text(
            f"Peguei o ID de *{nome}*: `{novo_id}`.\n\n"
            "Passo 3 de 3 — agora vá até o grupo dedicado (já criado, com o bot como admin) "
            "e envie lá dentro o comando /vincular",
            parse_mode="Markdown",
        )
        return True

    return False


async def tratar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    # Ignora mensagens automáticas do Telegram, como "fixou mensagem",
    # alteração de tópico, entrada/saída de membros etc.
    # Isso evita o bot responder "Use /start..." no grupo.
    if not msg or getattr(msg, "pinned_message", None):
        return

    if user and getattr(user, "is_bot", False):
        return

    # Fluxo de cadastro de atendente em andamento tem prioridade sobre
    # qualquer outra interpretação da mensagem (nome do atendente, ou a
    # mensagem encaminhada que identifica o novo atendente).
    if await tratar_etapa_admin_atendente(update, context):
        return

    # Limpa sessão antiga se o usuário não tem ticket ativo.
    ticket_ativo = obter_ticket_ativo_usuario(user.id)
    if not ticket_ativo and context.user_data.get("protocolo"):
        context.user_data.clear()
        usuarios_em_chamado.pop(user.id, None)

    # Mensagem enviada dentro de um tópico do grupo individual do atendente: vai para o técnico correto.
    chat_id_atual = update.effective_chat.id if update.effective_chat else None
    grupos_atendimento = grupos_atendimento_set()
    if chat_id_atual in grupos_atendimento and msg.message_thread_id:
        ticket = buscar_ticket_por_thread(msg.message_thread_id, chat_id_atual)
        if ticket and ticket["status"] in ["aguardando", "em_atendimento"]:
            if msg.text and msg.text.startswith("/"):
                return

            if ticket["status"] == "aguardando":
                # Reivindica de forma atômica: se outro atendente já tiver
                # assumido este mesmo chamado entre a leitura acima e agora,
                # não sobrescrevemos o atendente_id/nome dele.
                conseguiu = reivindicar_ticket(
                    ticket["protocolo"], novo_status="em_atendimento", status_esperado="aguardando"
                )
                if conseguiu:
                    atualizar_ticket(
                        ticket["protocolo"],
                        atendente_id=user.id,
                        atendente_nome=user.full_name,
                        assumed_at=now(),
                        last_message_at=now(),
                    )
                    ticket["status"] = "em_atendimento"
                    try:
                        await context.bot.send_message(
                            chat_id=ticket["user_id"],
                            text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: *{user.full_name}*\n🎫 Protocolo: *{ticket['protocolo']}*",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.debug("Não consegui notificar o técnico %s sobre início do atendimento.", ticket["user_id"])
                else:
                    # Alguém já assumiu; recarrega para refletir o estado real.
                    ticket = buscar_ticket(ticket["protocolo"]) or ticket

            registrar_msg(ticket["protocolo"], user.id, user.full_name, "atendente", "mensagem", msg.text or msg.caption or "")
            await encaminhar_mensagem(msg, context, ticket["user_id"], f"📩 {ticket['protocolo']} - COP {user.full_name}")

            # Reposiciona os controles no final do tópico após cada resposta do COP.
            await atualizar_controles_topico(context, ticket["protocolo"])
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

        if categoria == "NAP" and subcategoria in ["NAP mais próxima", "ID da NAP"]:
            context.user_data["etapa"] = "localizacao_nap_sem_foto"
            await msg.reply_text(
                f"🎫 Protocolo: *{protocolo}*\n\n"
                f"📍 Para *{subcategoria}*, envie a localização pelo botão abaixo.\n\n"
                "Não precisa enviar fotos nessa opção.",
                parse_mode="Markdown",
                reply_markup=teclado_localizacao(),
            )
            await atualizar_painel(context)
            return

        if categoria == "Ativo" and subcategoria == "Cliente ausente":
            context.user_data["etapa"] = "ativo_cliente_ausente_localizacao"
            await msg.reply_text(
                f"🎫 Protocolo: *{protocolo}*\n\n"
                "📍 Envie a localização do endereço pelo botão abaixo.\n\n"
                "Depois será solicitada a *foto da fachada*.",
                parse_mode="Markdown",
                reply_markup=teclado_localizacao(),
            )
            await atualizar_painel(context)
            return

        if categoria == "Ativo" and subcategoria == "Endereço não localizado":
            context.user_data["etapa"] = "ativo_endereco_nao_localizado_localizacao"
            await msg.reply_text(
                f"🎫 Protocolo: *{protocolo}*\n\n"
                "📍 Envie a localização pelo botão abaixo.\n\n"
                "Depois será solicitada a *foto da placa da rua* ou da *numeração mais próxima*.",
                parse_mode="Markdown",
                reply_markup=teclado_localizacao(),
            )
            await atualizar_painel(context)
            return

        if categoria == "Atenuacao" and subcategoria == "Numeração da NAP":
            context.user_data["etapa"] = "atenuacao_numero_nap"
            await msg.reply_text(
                f"🎫 Protocolo: *{protocolo}*\n\n"
                "🔢 Informe a numeração/identificação da NAP para atenuar:",
                parse_mode="Markdown",
            )
            await atualizar_painel(context)
            return

        if categoria == "Atenuacao" and subcategoria == "Localização da NAP":
            context.user_data["etapa"] = "atenuacao_localizacao_nap"
            await msg.reply_text(
                f"🎫 Protocolo: *{protocolo}*\n\n"
                "📍 Envie a localização da NAP para atenuar pelo botão abaixo:",
                parse_mode="Markdown",
                reply_markup=teclado_localizacao(),
            )
            await atualizar_painel(context)
            return

        if categoria == "Outros" or (categoria == "Ativo" and subcategoria == "Outros"):
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Direcionado direto ao COP")
            await msg.reply_text(
                f"✅ Solicitação registrada.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["etapa"] = "aguardando_cop"
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

    # NAP mais próxima / ID da NAP: localização obrigatória e não precisa de fotos.
    if context.user_data.get("etapa") == "localizacao_nap_sem_foto":
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

            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Finalizado sem fotos - opção NAP")
            await msg.reply_text(
                f"✅ Localização recebida.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["etapa"] = "aguardando_cop"
            await atualizar_painel(context)
            return

        await msg.reply_text(
            "⚠️ Para essa opção de NAP é obrigatório enviar a localização pelo botão abaixo.",
            reply_markup=teclado_localizacao(),
        )
        return

    # Ativo - Cliente ausente: localização + foto da fachada.
    if context.user_data.get("etapa") == "ativo_cliente_ausente_localizacao":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text("Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        if msg.location:
            atualizar_ticket(protocolo, latitude=msg.location.latitude, longitude=msg.location.longitude, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "location", "Localização enviada", latitude=msg.location.latitude, longitude=msg.location.longitude)
            context.user_data["etapa"] = "ativo_cliente_ausente_fachada"
            await msg.reply_text(
                "✅ Localização recebida.\n\n📸 Agora envie a *foto da fachada*.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        await msg.reply_text("⚠️ Para cliente ausente, envie a localização pelo botão abaixo.", reply_markup=teclado_localizacao())
        return

    if context.user_data.get("etapa") == "ativo_cliente_ausente_fachada":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text("Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        if msg.photo or msg.document:
            msg_type, file_id, text = tipo_mensagem(msg)
            fotos = (ticket.get("fotos") or 0) + 1
            atualizar_ticket(protocolo, fotos=fotos, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", msg_type, text or "Foto da fachada", file_id=file_id)
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Cliente ausente - evidências enviadas")
            await msg.reply_text(
                f"✅ Foto da fachada recebida.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["etapa"] = "aguardando_cop"
            await atualizar_painel(context)
            return

        await msg.reply_text("⚠️ Envie a foto da fachada para continuar.")
        return

    # Ativo - Endereço não localizado: localização + foto da placa da rua ou numeração próxima.
    if context.user_data.get("etapa") == "ativo_endereco_nao_localizado_localizacao":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text("Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        if msg.location:
            atualizar_ticket(protocolo, latitude=msg.location.latitude, longitude=msg.location.longitude, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "location", "Localização enviada", latitude=msg.location.latitude, longitude=msg.location.longitude)
            context.user_data["etapa"] = "ativo_endereco_nao_localizado_foto"
            await msg.reply_text(
                "✅ Localização recebida.\n\n📸 Agora envie a *foto da placa da rua* ou da *numeração mais próxima*.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        await msg.reply_text("⚠️ Para endereço não localizado, envie a localização pelo botão abaixo.", reply_markup=teclado_localizacao())
        return

    if context.user_data.get("etapa") == "ativo_endereco_nao_localizado_foto":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text("Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        if msg.photo or msg.document:
            msg_type, file_id, text = tipo_mensagem(msg)
            fotos = (ticket.get("fotos") or 0) + 1
            atualizar_ticket(protocolo, fotos=fotos, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", msg_type, text or "Foto da placa da rua ou numeração próxima", file_id=file_id)
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Endereço não localizado - evidências enviadas")
            await msg.reply_text(
                f"✅ Foto recebida.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["etapa"] = "aguardando_cop"
            await atualizar_painel(context)
            return

        await msg.reply_text("⚠️ Envie a foto da placa da rua ou da numeração mais próxima para continuar.")
        return

    # Atenuação - numeração da NAP.
    if context.user_data.get("etapa") == "atenuacao_numero_nap":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text("Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        if msg.text:
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "text", f"Numeração da NAP para atenuar: {msg.text.strip()}")
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Atenuação - numeração da NAP informada")
            await msg.reply_text(
                f"✅ Numeração da NAP recebida.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["etapa"] = "aguardando_cop"
            await atualizar_painel(context)
            return

        await msg.reply_text("⚠️ Informe a numeração/identificação da NAP em texto.")
        return

    # Atenuação - localização da NAP.
    if context.user_data.get("etapa") == "atenuacao_localizacao_nap":
        protocolo = context.user_data.get("protocolo") or usuarios_em_chamado.get(user.id)
        ticket = buscar_ticket(protocolo) if protocolo else None
        if not ticket or ticket["status"] == "finalizado":
            context.user_data.clear()
            usuarios_em_chamado.pop(user.id, None)
            await msg.reply_text("Esse atendimento já foi finalizado. Use /start para iniciar um novo atendimento.", reply_markup=ReplyKeyboardRemove())
            return

        if msg.location:
            atualizar_ticket(protocolo, latitude=msg.location.latitude, longitude=msg.location.longitude, last_message_at=now())
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "location", "Localização da NAP para atenuar", latitude=msg.location.latitude, longitude=msg.location.longitude)
            registrar_msg(protocolo, user.id, user.full_name, "tecnico", "finalizou_fotos", "Atenuação - localização da NAP informada")
            await msg.reply_text(
                f"✅ Localização da NAP recebida.\n🎫 {protocolo}\n\nSeu atendimento entrou na fila do COP.",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["etapa"] = "aguardando_cop"
            await atualizar_painel(context)
            return

        await msg.reply_text("⚠️ Envie a localização da NAP pelo botão abaixo.", reply_markup=teclado_localizacao())
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
                destino_ticket(ticket),
                f"📩 {ticket['protocolo']} - {user.full_name}",
                thread_id=ticket["message_thread_id"],
            )

            if ticket.get("status") == "em_atendimento":
                await atualizar_controles_topico(context, ticket["protocolo"])
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
            destino_ticket(ticket_tecnico),
            f"📩 {ticket_tecnico['protocolo']} - {user.full_name}",
            thread_id=ticket_tecnico["message_thread_id"],
        )

        # Reposiciona os controles no final após mensagem nova do técnico.
        await atualizar_controles_topico(context, ticket_tecnico["protocolo"])
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


async def encaminhar_mensagem(msg, context, destino, cabecalho, thread_id=None, responder_erro=True):
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
        if responder_erro:
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

    algum_alerta_enviado = False
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
                algum_alerta_enviado = True
            except Exception as e:
                logger.warning("Erro ao enviar alerta de espera: %s", e)

    if algum_alerta_enviado:
        # Traz o painel de volta pra baixo, logo após o(s) alerta(s) —
        # exatamente o momento em que os COPs mais precisam achar o botão
        # "Atender próximo" sem precisar procurar.
        await atualizar_painel(context, reposicionar=True)



def produtividade_texto():
    hoje = agora().date()
    with db() as conn:
        rows = conn.execute("""
            SELECT
                atendente_nome,
                COUNT(*) total,
                ROUND(AVG(EXTRACT(EPOCH FROM (closed_at::timestamp - assumed_at::timestamp)) / 60.0)::numeric, 1) media_min
            FROM tickets
            WHERE status='finalizado'
              AND atendente_nome IS NOT NULL
              AND DATE(closed_at::timestamp)=%s
            GROUP BY atendente_nome
            ORDER BY total DESC
        """, (hoje,)).fetchall()

    linhas = ["🏆 *PRODUTIVIDADE COP - HOJE*", ""]
    if not rows:
        linhas.append("Nenhum atendimento finalizado hoje.")
    else:
        for i, r in enumerate(rows, start=1):
            linhas.append(f"{i}º {r['atendente_nome']} — ✅ *{r['total']}* | ⏱️ média *{r['media_min'] or '-'} min*")
    linhas.append(f"\n🕘 Atualizado em {agora().strftime('%d/%m/%Y %H:%M:%S')}")
    return "\n".join(linhas)



async def produtividade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Apenas administradores podem usar este comando.")
        return
    await update.message.reply_text(produtividade_texto(), parse_mode="Markdown")



async def alerta_atendimento_parado_job(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute("""
            SELECT protocolo, user_name, categoria, subcategoria, atendente_nome, last_message_at, assumed_at
            FROM tickets
            WHERE status='em_atendimento'
              AND COALESCE(alertado_parado, 0)=0
            ORDER BY last_message_at ASC
            LIMIT 50
        """).fetchall()

    algum_alerta_enviado = False
    for r in rows:
        base = r["last_message_at"] or r["assumed_at"]
        tempo = minutos(base)
        if tempo >= ATENDIMENTO_PARADO_MIN:
            sub = f" / {r['subcategoria']}" if r["subcategoria"] else ""
            try:
                await context.bot.send_message(
                    chat_id=ATENDENTES_CHAT_ID,
                    text=(
                        "⚠️ *Atendimento parado*\n\n"
                        f"🎫 {r['protocolo']}\n"
                        f"👤 Técnico: {r['user_name']}\n"
                        f"📂 Fila: {r['categoria']}{sub}\n"
                        f"👨‍💻 Atendente: {r['atendente_nome']}\n"
                        f"⏱️ Sem movimentação há *{tempo} min*"
                    ),
                    parse_mode="Markdown",
                )
                atualizar_ticket(r["protocolo"], alertado_parado=1)
                algum_alerta_enviado = True
            except Exception as e:
                logger.warning("Erro ao enviar alerta de atendimento parado: %s", e)

    if algum_alerta_enviado:
        await atualizar_painel(context, reposicionar=True)


async def debug_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"🧪 Debug do grupo\n\n"
        f"Chat ID: `{chat.id}`\n"
        f"Nome: *{chat.title or chat.full_name or '-'}*\n"
        f"Tipo: `{chat.type}`\n"
        f"É fórum/tópicos: `{getattr(chat, 'is_forum', None)}`\n"
        f"Seu ID: `{user.id}`",
        parse_mode="Markdown",
    )


def main():
    init_db()
    seed_atendentes_iniciais()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("adm", adm))
    app.add_handler(CommandHandler("vincular", vincular_grupo_cmd))
    app.add_handler(CommandHandler("encerrar", encerrar_cmd))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("debug", debug_grupo))
    app.add_handler(CommandHandler("produtividade", produtividade_cmd))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.VOICE | filters.LOCATION) & ~filters.COMMAND,
        tratar_mensagem
    ))

    try:
        app.job_queue.run_repeating(alerta_espera_job, interval=60, first=60)
        app.job_queue.run_repeating(alerta_atendimento_parado_job, interval=60, first=90)
    except Exception as e:
        logger.warning("Job queue não inicializada: %s", e)

    try:
        app.run_polling()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
